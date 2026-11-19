# ==============================================================================
# JXE LEVEL PRO — Main Application Server & Multi-Account Matchmaking Runner
# ==============================================================================

import asyncio, sys, time, secrets, socket, os, threading, uuid, json, random, logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template, Response
import flask.cli

if sys.platform == "win32":
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetConsoleMode(k32.GetStdHandle(-11), 7)
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        import asyncio.windows_events
        _orig_cancel_ov = asyncio.windows_events._OverlappedFuture._cancel_overlapped
        def _safe_cancel_ov(self):
            try:
                _orig_cancel_ov(self)
            except (OSError, RuntimeError, AttributeError):
                pass
        asyncio.windows_events._OverlappedFuture._cancel_overlapped = _safe_cancel_ov
    except Exception:
        pass

# ==================== SILENCE HTTP / API REQUEST LOGS ====================
flask.cli.show_server_banner = lambda *args, **kwargs: None
logging.getLogger("werkzeug").setLevel(logging.CRITICAL)
logging.getLogger("werkzeug").disabled = True
logging.getLogger("urllib3").setLevel(logging.CRITICAL)

from JXE_m import (
    Colors, hdr, ok, info, warn, err, read_varint, decode_header, cmd_name,
    pkt_hello, pkt_ack, pkt_auth447, pkt_auth448, pkt_ping,
    udp_decrypt_body, _udp_unpad, resolve_host_cloudflare,
    tcp_open_and_auth, tcp_heartbeat_loop, tcp_wait_for_0300,
    send_start_match, log_match, log_search, log_match_banner, parse_0300,
    log_info, log_warn, log_err, log_ok, aes_encrypt,
    build_tcp_startup_packet, send_keep_alive, start_game_lone_wolf,
    informational,
    oauth, inspect_token, major_login, get_login_data, patch_account_id,
    refresh_profile,
)
from JXE_s import (
    MATCH_START_DECRYPTED_HEX, HANDSHAKE_STEP_DELAY,
    TCP_HEARTBEAT_INTERVAL, PING_ENABLED, PING_INTERVAL, PING_TICK_BASE,
    UDP_CLIENT_VERSION, UDP_WAIT_JOIN, REGION, REGION_BYTE,
)

try:
    from JXE_s import PRESET_73
except ImportError:
    PRESET_73 = {}

# ── Core Match & System Constants ──
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
ACCOUNTS_JSON = os.path.join(DATA_DIR, "accounts.json")

MATCH_CYCLE_INTERVAL = 2.0
MATCH_IDLE_TIMEOUT = 15.0
EXP_POLL_S = 90
MAX_PARALLEL_PER_ACCOUNT = int(os.environ.get("MAX_PARALLEL", "999999"))
NEW_MATCH_DELAY = float(os.environ.get("NEW_MATCH_DELAY", "0.0"))
DEFAULT_DAILY_EXP_LIMIT = 50000
MAX_DAILY_EXP_LIMIT = 1_000_000_000
GLOBAL_SLOTS = 999999
GLOBAL_EXPIRES = 4102444800

LEVEL_EXP_THRESHOLDS = [
    0,48,202,544,1012,1844,2792,3800,4870,6004,7192,8448,9760,11140,12566,14060,
    15610,17224,18902,20632,22424,24278,26192,28166,30200,32294,34448,37804,41274,
    44870,48582,53394,58566,64096,69994,76260,83506,91128,99322,108092,120144,
    133266,147472,162760,179126,196572,215368,235316,257010,279860,304056,348318,
    394982,444044,495508,549364,633756,721744,813336,908522,1041438,1180352,1325266,
    1476184,1634300,1840946,2056594,2281242,2514880,2757530,3059506,3372284,3699456,
    4041030,4397002,4829104,5282204,5756304,6251404,6767502,7381324,8043154,8752982,
    9510808,10316638,11277190,12291748,13360304,14482858,15659418,17026708,18453990,
    19941280,21488570,23095858,24763138,26490428,28277708,30124996,32032284
]

app = Flask(__name__, template_folder="templates", static_folder="static")
app.logger.disabled = True

_state_lock = threading.RLock()
_dirty_flag = threading.Event()
START_TIME = time.time()

ACCOUNTS = {}
SESSIONS = {}
RUNNERS = {}
EXP_STOPS = {}
LOGS = {}

_HTTP_POOL = ThreadPoolExecutor(max_workers=512, thread_name_prefix="jxe_io")
_LOGIN_LOCK = threading.Semaphore(4)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _today_key():
    return time.strftime("%Y-%m-%d")


def _log(uid, msg, kind="info"):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _state_lock:
        buf = LOGS.setdefault(str(uid), [])
        buf.append(line)
        del buf[:-100]

    if kind == "match":
        tag = f"{Colors.BGREEN}[ MATCH ]{Colors.ENDC}"
        body = f"{Colors.BGREEN}✔ {msg}{Colors.ENDC}"
    elif kind == "login":
        tag = f"{Colors.MAGENTA}[ LOGIN ]{Colors.ENDC}"
        body = f"{Colors.WHITE}{msg}{Colors.ENDC}"
    elif kind == "ok":
        tag = f"{Colors.GREEN}[   OK  ]{Colors.ENDC}"
        body = f"{Colors.GREEN}{msg}{Colors.ENDC}"
    elif kind == "exp":
        tag = f"{Colors.BGREEN}[  EXP  ]{Colors.ENDC}"
        body = f"{Colors.GREEN}{msg}{Colors.ENDC}"
    elif kind == "warn":
        tag = f"{Colors.WARNING}[  WARN ]{Colors.ENDC}"
        body = f"{Colors.WARNING}{msg}{Colors.ENDC}"
    elif kind == "err":
        tag = f"{Colors.FAIL}[  FAIL ]{Colors.ENDC}"
        body = f"{Colors.FAIL}{msg}{Colors.ENDC}"
    else:
        tag = f"{Colors.CYAN}[ INFO  ]{Colors.ENDC}"
        body = f"{Colors.WHITE}{msg}{Colors.ENDC}"

    print(f"{Colors.DIM}[{ts}]{Colors.ENDC} {tag} {Colors.CYAN}[{uid}]{Colors.ENDC} {body}", flush=True)


def log_banner(port):
    print("")
    print(f"{Colors.CYAN}+{'=' * 62}+{Colors.ENDC}")
    print(f"{Colors.CYAN}|{Colors.ENDC}{' ' * 62}{Colors.CYAN}|{Colors.ENDC}")
    print(f"{Colors.CYAN}|{Colors.ENDC}{Colors.BOLD}{Colors.MAGENTA}{'JXE LEVEL PRO — AUTO LEVEL UP BOT'.center(62)}{Colors.ENDC}{Colors.CYAN}|{Colors.ENDC}")
    print(f"{Colors.CYAN}|{Colors.ENDC}{Colors.DIM}{'High-Speed Multi-Account Engine | BD Server'.center(62)}{Colors.ENDC}{Colors.CYAN}|{Colors.ENDC}")
    print(f"{Colors.CYAN}|{Colors.ENDC}{' ' * 62}{Colors.CYAN}|{Colors.ENDC}")
    print(f"{Colors.CYAN}|{Colors.ENDC}{Colors.BGREEN}{f'Dashboard Live : http://localhost:{port}'.center(62)}{Colors.ENDC}{Colors.CYAN}|{Colors.ENDC}")
    print(f"{Colors.CYAN}|{Colors.ENDC}{' ' * 62}{Colors.CYAN}|{Colors.ENDC}")
    print(f"{Colors.CYAN}+{'=' * 62}+{Colors.ENDC}")
    print("", flush=True)


def _normalize_row(raw):
    if not isinstance(raw, dict):
        return None
    acc_type = raw.get("type")
    uid = str(raw.get("uid") or "").strip()
    pw = str(raw.get("password") or "").strip()
    token = str(raw.get("token") or "").strip()
    if not acc_type:
        acc_type = "token" if (token and not pw) else "guest"
    if acc_type == "guest":
        if not uid or not pw:
            return None
        key = uid
    else:
        if not token:
            return None
        key = uid or token[:20]

    now = time.time()
    today = _today_key()
    exp = int(raw.get("exp", raw.get("current_exp", 0)) or 0)
    init_exp = int(raw.get("initial_exp", exp - int(raw.get("gain", raw.get("gained_exp", 0)) or 0)) or 0)
    if init_exp <= 0 and exp > 0:
        init_exp = exp
    gained = max(0, exp - init_exp) if exp >= init_exp else int(raw.get("gain", raw.get("gained_exp", 0)) or 0)

    exp_day = raw.get("exp_day") or today
    daily_init = int(raw.get("daily_initial_exp", exp if exp_day != today else (exp - int(raw.get("daily_exp_gained", gained) or 0))) or 0)
    if exp_day != today:
        exp_day = today
        daily_init = exp
        daily_gained = 0
        exp_limit_reached = False
    else:
        daily_gained = max(0, exp - daily_init) if exp > 0 else int(raw.get("daily_exp_gained", 0) or 0)
        exp_limit_reached = bool(raw.get("exp_limit_reached", False))

    exp_limit = int(raw.get("exp_limit") or DEFAULT_DAILY_EXP_LIMIT)
    exp_limit_custom = bool(raw.get("exp_limit_custom", False))
    level_limit = raw.get("level_limit")
    if level_limit is not None:
        try:
            level_limit = int(level_limit)
            if not (1 <= level_limit <= 100):
                level_limit = None
        except (ValueError, TypeError):
            level_limit = None

    level = int(raw.get("level", 1) or 1)
    level_limit_reached = bool(level_limit and level >= level_limit)
    if exp_limit > 0 and daily_gained >= exp_limit:
        exp_limit_reached = True

    paused = bool(raw.get("is_paused", False))
    status = str(raw.get("status") or "CONNECTING").upper()
    if paused or status == "PAUSED":
        paused = True
        status = "PAUSED"
    elif level_limit_reached:
        status = "STOPPED"
    elif exp_limit_reached:
        status = "EXPIRED"
    elif status in ("IDLE", "STOPPING", "ERROR"):
        status = "CONNECTING"

    return {
        "id": raw.get("id") or str(uuid.uuid4()),
        "key": key,
        "type": acc_type,
        "uid": key,
        "password": pw,
        "token": token,
        "nickname": raw.get("nickname") or f"Player_{key[:6]}",
        "game_uid": str(raw.get("game_uid") or raw.get("acc_id") or key),
        "acc_id": str(raw.get("acc_id") or raw.get("game_uid") or key),
        "region": str(raw.get("region") or REGION).upper(),
        "level": level,
        "initial_exp": init_exp,
        "exp": exp,
        "prev_exp": int(raw.get("prev_exp", exp) or 0),
        "gain": gained,
        "likes": int(raw.get("likes", 0) or 0),
        "matches_played": int(raw.get("matches_played", 0) or 0),
        "active_matches": 0,
        "status": status,
        "is_paused": paused,
        "last_error": None,
        "created_at": float(raw.get("created_at") or now),
        "task_started_at": 0,
        "last_match_at": float(raw.get("last_match_at") or 0) or None,
        "last_update": float(raw.get("last_update") or now),
        "updated_at": raw.get("updated_at") or _now_iso(),
        "exp_day": exp_day,
        "daily_initial_exp": daily_init,
        "daily_exp_gained": daily_gained,
        "exp_limit": exp_limit,
        "exp_limit_custom": exp_limit_custom,
        "exp_limit_reached": exp_limit_reached,
        "level_limit": level_limit,
        "level_limit_reached": level_limit_reached,
    }


def _load_initial_accounts():
    with _state_lock:
        if not os.path.exists(ACCOUNTS_JSON):
            return
        try:
            with open(ACCOUNTS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            rows = data.get("accounts", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for r in rows:
                norm = _normalize_row(r)
                if norm:
                    ACCOUNTS[norm["key"]] = norm
        except Exception as e:
            warn(f"Failed to load {ACCOUNTS_JSON}: {e}")


def _flush_to_disk():
    with _state_lock:
        rows = list(ACCOUNTS.values())
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = ACCOUNTS_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"accounts": rows}, f, indent=2, ensure_ascii=False)
        os.replace(tmp, ACCOUNTS_JSON)
    except Exception:
        pass


def _mark_dirty():
    _dirty_flag.set()


def _disk_writer_loop():
    while True:
        _dirty_flag.wait()
        time.sleep(1.0)
        _dirty_flag.clear()
        _flush_to_disk()


def _resolve_key(identifier):
    target = str(identifier or "").strip()
    if not target:
        return None
    with _state_lock:
        if target in ACCOUNTS:
            return target
        prefix = target[:20]
        if prefix in ACCOUNTS:
            return prefix
        for k, acc in ACCOUNTS.items():
            if (str(acc.get("uid")) == target or
                str(acc.get("game_uid")) == target or
                str(acc.get("acc_id")) == target or
                (acc.get("token") and (acc["token"] == target or acc["token"][:20] == prefix))):
                return k
    return None


def _rollover_daily_if_needed(acc):
    today = _today_key()
    if acc.get("exp_day") != today:
        cur_exp = int(acc.get("exp", 0) or 0)
        acc["exp_day"] = today
        acc["daily_initial_exp"] = cur_exp
        acc["daily_exp_gained"] = 0
        acc["exp_limit_reached"] = False
        if acc.get("status") == "EXPIRED" and not acc.get("is_paused"):
            acc["status"] = "ONLINE"
        return True
    return False


def _check_limits_locked(acc):
    _rollover_daily_if_needed(acc)
    cur_exp = int(acc.get("exp", 0) or 0)
    cur_lvl = int(acc.get("level", 1) or 1)
    daily_init = int(acc.get("daily_initial_exp", cur_exp) or 0)
    if daily_init <= 0 and cur_exp > 0:
        acc["daily_initial_exp"] = cur_exp
        daily_init = cur_exp
    daily_gained = max(0, cur_exp - daily_init)
    acc["daily_exp_gained"] = daily_gained

    lvl_lim = acc.get("level_limit")
    exp_lim = int(acc.get("exp_limit") or DEFAULT_DAILY_EXP_LIMIT)

    lvl_hit = False
    if lvl_lim and 1 <= int(lvl_lim) <= 100:
        target_exp = LEVEL_EXP_THRESHOLDS[int(lvl_lim) - 1]
        if cur_lvl >= int(lvl_lim) or (cur_exp > 0 and cur_exp >= target_exp):
            lvl_hit = True

    exp_hit = exp_lim > 0 and daily_gained >= exp_lim

    acc["level_limit_reached"] = lvl_hit
    acc["exp_limit_reached"] = exp_hit

    if lvl_hit:
        acc["status"] = "STOPPED"
        return "level"
    if exp_hit:
        acc["status"] = "EXPIRED"
        return "exp"
    return None


# ── Exact Working UDP/Match Core from MEHEDI ──
def _zz(v):
    return (v >> 1) ^ -(v & 1)


def parse_addr(s):
    if not s:
        return None, None
    s = str(s).strip()
    if ":" in s:
        h, p = s.rsplit(":", 1)
        try:
            return h, int(p)
        except ValueError:
            return h, None
    return s, None


def is_placeholder_host(h):
    if not h or h in ("localhost", "0.0.0.0", "::1", ""):
        return True
    for pref in ("127.", "10.", "192.168.", "169.254."):
        if h.startswith(pref):
            return True
    return False


async def udp_session(host, port, key_hex, token, player_id, match_id, mask, *,
                      extra448=None, mode_override=None,
                      on_join=None, duration=0.0, match_label=""):
    try:
        key = bytes.fromhex(key_hex)
    except Exception:
        return False
    if len(key) != 16 or token.count(".") != 2:
        return False

    if is_placeholder_host(host):
        return False
    try:
        resolved = await resolve_host_cloudflare(host)
    except Exception:
        return False
    if not resolved:
        return False

    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        sock.connect((resolved, port))
    except OSError:
        sock.close()
        return False

    st = {
        "recv_q": asyncio.Queue(maxsize=4000),
        "stop_recv": asyncio.Event(), "stop_bg": asyncio.Event(),
        "ack_next": 0, "ack_sent": 0, "recv_orders": set(),
        "joined": asyncio.Event(), "last_rx": time.time(),
        "spawn_known": asyncio.Event(),
    }

    suffix = str(10000 + secrets.randbelow(90000))
    hello = pkt_hello(str(player_id), suffix, key, mask)
    a447 = pkt_auth447(token, key, mask, order_id=0)
    kw = dict(PRESET_73)
    kw.update({k: v for k, v in (extra448 or {}).items() if k != "hot_update_url"})
    if mode_override is not None:
        kw["mode_id"] = mode_override
    a448 = pkt_auth448(token, key, mask, player_id, match_id,
                       UDP_CLIENT_VERSION, order_id=1, **kw)

    async def send(pkt):
        try:
            await loop.sock_sendall(sock, pkt)
            return True
        except OSError:
            return False

    def _mark_joined():
        if st["joined"].is_set():
            return
        st["joined"].set()
        if on_join:
            try:
                on_join()
            except Exception:
                pass

    async def handle(pkt):
        h = decode_header(pkt, key, mask)
        if not h:
            return None
        cmd, oid = h["cmd"], h.get("orderId")
        st["last_rx"] = time.time()
        dup = oid is not None and cmd != 2 and oid in st["recv_orders"]
        if oid is not None and cmd != 2:
            st["recv_orders"].add(oid)
            while st["ack_next"] in st["recv_orders"]:
                st["ack_next"] += 1
        if oid is not None and cmd != 2 and (h.get("sendOption") == 2 or (h["flags"] & 1)):
            if st["ack_next"] > st["ack_sent"] or dup:
                st["ack_sent"] = st["ack_next"]
                await send(pkt_ack(st["ack_next"], key, mask, "regular"))
        if cmd in (99, 176, 849, 2001):
            _mark_joined()
        elif cmd == 1:
            await send(pkt_ack(1, key, mask, "short"))
        return cmd

    async def recv_loop():
        while not st["stop_recv"].is_set():
            try:
                pkt, _ = await loop.sock_recvfrom(sock, 65536)
                st["recv_q"].put_nowait(pkt)
            except (asyncio.CancelledError, OSError, Exception):
                break

    async def consume():
        while not st["stop_bg"].is_set():
            try:
                pkt = await st["recv_q"].get()
                if pkt is None:
                    break
                await handle(pkt)
            except (asyncio.CancelledError, Exception):
                break

    async def handshake(pkt, expect, name, timeout=HANDSHAKE_STEP_DELAY):
        start = time.time()
        await send(pkt)
        while time.time() - start < timeout:
            try:
                got = await asyncio.wait_for(st["recv_q"].get(), timeout=0.3)
            except asyncio.TimeoutError:
                continue
            cmd = await handle(got)
            if cmd in expect:
                return True
        return False

    recv_task = asyncio.create_task(recv_loop())
    consume_task = ping_task = None

    try:
        if not await handshake(hello, {1, 2, 11}, "HELLO"):
            return False
        if not await handshake(a447, {2}, "447"):
            return False
        await handshake(a448, {2, 99, 100, 176, 849, 2001}, "448")
        consume_task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(st["joined"].wait(), timeout=15.0)
        except asyncio.TimeoutError:
            if on_join:
                try:
                    on_join()
                except Exception:
                    pass

        async def ping_loop():
            t0 = time.time()
            while not st["stop_bg"].is_set():
                el = int((time.time() - t0) * 1000)
                await send(pkt_ping((PING_TICK_BASE - 2 * el) & 0xFFFFFFFF, key, mask))
                await asyncio.sleep(PING_INTERVAL)

        if PING_ENABLED:
            ping_task = asyncio.create_task(ping_loop())

        while not st["stop_bg"].is_set():
            await asyncio.sleep(0.5)
            if not st["joined"].is_set():
                continue
            if time.time() - st["last_rx"] > MATCH_IDLE_TIMEOUT:
                break
        return st["joined"].is_set()
    finally:
        st["stop_bg"].set()
        st["stop_recv"].set()
        _sub_tasks = [t for t in (recv_task, consume_task, ping_task) if t and not t.done()]
        for t in _sub_tasks:
            t.cancel()
        if _sub_tasks:
            try:
                await asyncio.wait(_sub_tasks, timeout=0.1)
            except Exception:
                pass
        try:
            sock.close()
        except Exception:
            pass


async def run_udp_match(match_no, key, acc_id, server, udp_key, token, match_id, mask, extra448, jwt, oid, plat, stop_evt):
    host, port = parse_addr(server)
    if not port:
        port = 10018

    log_match(f"[MATCH #{match_no}] UDP connecting -> {server} │ UID: {acc_id}")
    _log(key, f"[MATCH #{match_no}] UDP connecting -> {server}", kind="info")

    def _on_join():
        log_match(f"[MATCH #{match_no}] Joined Game World -> {server} │ UID: {acc_id}")
        _log(key, f"[MATCH #{match_no}] Joined Game World -> {server}", kind="match")

    try:
        await udp_session(
            host=host, port=port, key_hex=udp_key, token=token,
            player_id=int(acc_id), match_id=int(match_id), mask=mask,
            extra448=extra448, on_join=_on_join,
            match_label=f"M#{match_no}"
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log_err(f"[MATCH #{match_no}] UDP session error: {e}")
    finally:
        with _state_lock:
            cur = ACCOUNTS.get(key)
            if cur:
                active = max(0, int(cur.get("active_matches", 1) or 1) - 1)
                cur["active_matches"] = active
                cur["matches_played"] = int(cur.get("matches_played", 0) or 0) + 1
                if active == 0 and cur.get("status") == "IN_MATCH":
                    cur["status"] = "ONLINE"
                cur["last_update"] = time.time()
                cur["updated_at"] = _now_iso()
                _mark_dirty()

        tot = cur.get("matches_played", 0) if cur else 0
        log_match(f"[MATCH #{match_no}] Completed cleanly │ UID: {acc_id} │ Total Completed: {tot}")
        _log(key, f"✔ Match #{match_no} Completed cleanly │ Total Completed: {tot}", kind="ok")

        async def _post_exp():
            for wait_s in (3.0, 7.0, 12.0):
                if stop_evt.is_set():
                    return
                await asyncio.sleep(wait_s)
                try:
                    res = await asyncio.to_thread(_refresh_account_sync, key, True)
                    if res and isinstance(res, dict) and res.get("delta", 0) > 0:
                        break
                except Exception:
                    pass

        asyncio.create_task(_post_exp())


def _refresh_account_sync(key, is_post_match=False):
    with _state_lock:
        sess = SESSIONS.get(key)
        if not sess:
            return None
        jwt = sess["jwt"]
        oid = sess["open_id"]
        plat = sess["platform"]

    gld = refresh_profile(jwt, oid, plat)
    new_exp = int(gld.get("exp", 0))
    new_level = int(gld.get("level", 1))
    new_likes = int(gld.get("likes", 0))
    now = time.time()

    with _state_lock:
        cur = ACCOUNTS.get(key)
        if not cur:
            return None
        _rollover_daily_if_needed(cur)
        if cur.get("initial_exp", 0) <= 0 and new_exp > 0:
            cur["initial_exp"] = new_exp
        if cur.get("daily_initial_exp", 0) <= 0 and new_exp > 0:
            cur["daily_initial_exp"] = new_exp
        prev = int(cur.get("exp") or 0)
        delta = max(0, new_exp - prev) if prev > 0 else 0
        cur["prev_exp"] = prev
        cur["exp"] = new_exp
        cur["gain"] = max(0, new_exp - int(cur.get("initial_exp") or new_exp))
        cur["daily_exp_gained"] = max(0, new_exp - int(cur.get("daily_initial_exp") or new_exp))
        cur["level"] = new_level
        cur["likes"] = new_likes
        if gld.get("nickname"):
            cur["nickname"] = gld["nickname"]
        cur["last_update"] = now
        cur["updated_at"] = _now_iso()
        hit = _check_limits_locked(cur)
        _mark_dirty()

    if is_post_match:
        _log(key, f"EXP Updated │ Level {new_level} │ EXP: {new_exp:,} (+{delta:,})", kind="exp")
        log_ok(f"EXP Updated │ Level {new_level} │ EXP: {new_exp:,} (+{delta:,}) │ UID: {cur.get('uid') or key}")

    if hit:
        _log(key, f"{hit.upper()} limit reached — stopping account.", kind="warn")
        _stop_bot(key)
    return {**gld, "delta": delta}


def _exp_refresher(key, stop_evt, session_id):
    while not stop_evt.is_set():
        for _ in range(EXP_POLL_S):
            if stop_evt.is_set():
                return
            time.sleep(1)
        with _state_lock:
            sess = SESSIONS.get(key)
            if not sess or sess.get("id") != session_id:
                return
        try:
            _refresh_account_sync(key, is_post_match=False)
        except Exception:
            pass


def _run_bot(key):
    with _state_lock:
        acc = ACCOUNTS.get(key)
        if not acc or acc.get("is_paused"):
            return
        if _check_limits_locked(acc):
            return
        if key in RUNNERS:
            return
        stop_evt = threading.Event()
        RUNNERS[key] = {"stop": stop_evt, "log": LOGS.setdefault(key, [])}
        acc_type = acc.get("type", "guest")
        uid = acc.get("uid", key)
        password = acc.get("password", "")
        token_val = acc.get("token", "")

    async def _bot_loop():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(_HTTP_POOL)

        while not stop_evt.is_set():
            with _state_lock:
                acc_now = ACCOUNTS.get(key)
                if not acc_now or acc_now.get("is_paused") or _check_limits_locked(acc_now):
                    return
                if not acc_now.get("task_started_at"):
                    acc_now["task_started_at"] = time.time()
                acc_now["status"] = "CONNECTING"
                acc_now["last_error"] = None
                acc_now["last_update"] = time.time()
                _mark_dirty()

            _log(key, "Authenticating account on BD Server...", kind="login")
            try:
                def _do_auth():
                    with _LOGIN_LOCK:
                        if acc_type == "token":
                            o_id, pl = inspect_token(token_val)
                            a_t = token_val
                        else:
                            o_id, a_t = oauth(uid, password)
                            pl = "4"
                        j_wt, s_k, s_iv, a_id, k_ts = major_login(o_id, a_t, pl)
                        g_ld = get_login_data(j_wt, o_id, pl)
                        return o_id, a_t, pl, j_wt, s_k, s_iv, a_id, k_ts, g_ld

                oid, at, plat, jwt, sk, siv, acc_id, kts, gld = await asyncio.to_thread(_do_auth)
            except Exception as e:
                _log(key, f"Login failed: {e} (retrying shortly)", kind="err")
                with _state_lock:
                    acc_now = ACCOUNTS.get(key)
                    if acc_now and not acc_now.get("is_paused"):
                        acc_now["status"] = "CONNECTING"
                        acc_now["last_error"] = str(e)
                retry_wait = random.uniform(6.0, 12.0)
                await asyncio.sleep(retry_wait)
                continue

            session_id = str(uuid.uuid4())
            now = time.time()
            cur_exp = int(gld.get("exp", 0))
            cur_lvl = int(gld.get("level", 1))
            cur_likes = int(gld.get("likes", 0))

            with _state_lock:
                SESSIONS[key] = {"jwt": jwt, "open_id": oid, "platform": plat, "id": session_id}
                acc_now = ACCOUNTS.get(key)
                if not acc_now:
                    return
                _rollover_daily_if_needed(acc_now)
                if acc_now.get("initial_exp", 0) <= 0:
                    acc_now["initial_exp"] = cur_exp
                if acc_now.get("daily_initial_exp", 0) <= 0:
                    acc_now["daily_initial_exp"] = cur_exp
                acc_now["nickname"] = gld["nickname"]
                acc_now["region"] = str(gld.get("region") or REGION).upper()
                acc_now["game_uid"] = str(acc_id)
                acc_now["acc_id"] = str(acc_id)
                acc_now["level"] = cur_lvl
                acc_now["exp"] = cur_exp
                acc_now["prev_exp"] = cur_exp
                acc_now["gain"] = max(0, cur_exp - int(acc_now.get("initial_exp") or cur_exp))
                acc_now["daily_exp_gained"] = max(0, cur_exp - int(acc_now.get("daily_initial_exp") or cur_exp))
                acc_now["likes"] = cur_likes
                acc_now["status"] = "ONLINE"
                acc_now["last_update"] = now
                acc_now["updated_at"] = _now_iso()
                if _check_limits_locked(acc_now):
                    _mark_dirty()
                    return
                _mark_dirty()

            _log(key, f"Login Success │ {gld['nickname']} (UID: {acc_id}) │ Lv.{cur_lvl} │ EXP: {cur_exp:,}", kind="ok")

            exp_stop = threading.Event()
            with _state_lock:
                old_exp_stop = EXP_STOPS.pop(key, None)
                if old_exp_stop:
                    old_exp_stop.set()
                EXP_STOPS[key] = exp_stop
            threading.Thread(target=_exp_refresher, args=(key, exp_stop, session_id), daemon=True).start()

            info_task = None
            info_addr = gld.get("informational")
            if info_addr:
                reg_name = str(REGION).upper()
                tcp_packet_chat = build_tcp_startup_packet(
                    acc_id, jwt, kts, sk, siv, region=reg_name, typ='ChaT'
                )
                info_task = asyncio.create_task(
                    informational(info_addr, tcp_packet_chat, region=reg_name, stop_evt=stop_evt)
                )
                log_info(f"Informational Presence Active │ UID: {acc_id} │ Addr: {info_addr}")

            ms_plain = patch_account_id(MATCH_START_DECRYPTED_HEX, str(acc_id))
            max_parallel = int(acc.get("max_parallel") or MAX_PARALLEL_PER_ACCOUNT or 999999)
            NEW_MATCH_DELAY = 0.0

            play_matches = []
            match_counter = 0

            with _state_lock:
                acc_now = ACCOUNTS.get(key)
                if acc_now:
                    acc_now["status"] = "ONLINE"
                    acc_now["active_matches"] = 0
                    _mark_dirty()

            _log(key, "Matchmaking Loop Active │ Mode: UNLIMITED Parallel Matches", kind="info")

            try:
                while not stop_evt.is_set():
                    with _state_lock:
                        acc_now = ACCOUNTS.get(key)
                        if not acc_now or acc_now.get("is_paused") or _check_limits_locked(acc_now):
                            stop_evt.set()
                            break

                    play_matches[:] = [m for m in play_matches if not m.done()]
                    active_count = len(play_matches)

                    with _state_lock:
                        cur = ACCOUNTS.get(key)
                        if cur:
                            cur["active_matches"] = active_count
                            if active_count > 0:
                                cur["status"] = "IN_MATCH"
                            elif cur.get("status") not in ("SEARCHING", "CONNECTING"):
                                cur["status"] = "ONLINE"
                            _mark_dirty()

                    if max_parallel > 0 and active_count >= max_parallel:
                        await asyncio.sleep(0.5)
                        continue

                    # Set searching status
                    with _state_lock:
                        cur = ACCOUNTS.get(key)
                        if cur:
                            cur["status"] = "SEARCHING" if active_count == 0 else "IN_MATCH"
                            _mark_dirty()

                    _log(key, f"Searching for Lone Wolf Match... (Active: {active_count})", kind="search")
                    log_search(f"Connecting to Gateway │ UID: {acc_id} │ Active matches: {active_count}")

                    writer = None
                    fr = None
                    stop_hb = asyncio.Event()
                    hb_task = None
                    found = None
                    try:
                        writer, fr = await tcp_open_and_auth(gld["online"], sk, siv, jwt, acc_id, kts, REGION, ms_plain)
                        if writer is None or fr is None:
                            await asyncio.sleep(0.5)
                            continue

                        hb_task = asyncio.create_task(tcp_heartbeat_loop(writer, sk, siv, stop_hb, TCP_HEARTBEAT_INTERVAL))
                        found = await tcp_wait_for_0300(writer, fr, sk, siv, ms_plain, REGION, 60.0, acc_id)

                    except Exception as e:
                        log_warn(f"TCP Gateway error ({acc_id}): {e}")
                    finally:
                        stop_hb.set()
                        if hb_task:
                            try:
                                await asyncio.wait_for(hb_task, timeout=1.0)
                            except Exception:
                                hb_task.cancel()
                        if writer:
                            try:
                                writer.close()
                                await writer.wait_closed()
                            except Exception:
                                pass
                        writer = None

                    if not found or not found.get("server"):
                        await asyncio.sleep(0.5)
                        continue

                    match_counter += 1
                    my_match_idx = match_counter
                    server = found["server"]
                    udp_key = found["udp_key"]
                    tok = found["token"]
                    match_id = found["match_id"]
                    mask = found["mask"]
                    extra448 = found.get("extra448")

                    # 1. Instant Bold Green Banner Console Output
                    print(f"\n{Colors.BGREEN}{'=' * 58}\n[ MATCH ] ✔ MATCH FOUND! │ UID: {acc_id}\n{'=' * 58}{Colors.ENDC}\n", flush=True)
                    log_match_banner(my_match_idx, acc_id, server)

                    # 2. Web Log
                    _log(key, f"✔ MATCH FOUND! Match #{my_match_idx} │ Server: {server} │ Match ID: {match_id}", kind="match")

                    # 3. Update Account State Immediately (Mark In-Match & increment active matches)
                    now_m = time.time()
                    with _state_lock:
                        cur = ACCOUNTS.get(key)
                        if cur:
                            cur["status"] = "IN_MATCH"
                            cur["active_matches"] = len([m for m in play_matches if not m.done()]) + 1
                            cur["last_match_time"] = time.strftime("%H:%M:%S")
                            cur["last_match_at"] = now_m
                            cur["last_update"] = now_m
                            cur["updated_at"] = _now_iso()
                            _mark_dirty()

                    # 4. Spawn Background UDP Match Task
                    new_match_task = asyncio.create_task(
                        run_udp_match(
                            match_no=my_match_idx,
                            key=key,
                            acc_id=acc_id,
                            server=server,
                            udp_key=udp_key,
                            token=tok,
                            match_id=match_id,
                            mask=mask,
                            extra448=extra448,
                            jwt=jwt,
                            oid=oid,
                            plat=plat,
                            stop_evt=stop_evt,
                        )
                    )
                    play_matches.append(new_match_task)

                    log_info(f"Instant Next Search -> StartMatch │ UID: {acc_id}")
                    _log(key, "Match running in background! Searching next match immediately...", kind="info")
                    await asyncio.sleep(0.05)
            finally:
                exp_stop.set()
                if info_task and not info_task.done():
                    info_task.cancel()
                for m in play_matches:
                    if not m.done():
                        m.cancel()
                if play_matches:
                    await asyncio.gather(*play_matches, return_exceptions=True)
                with _state_lock:
                    acc_now = ACCOUNTS.get(key)
                    if acc_now:
                        acc_now["active_matches"] = 0
                _log(key, "Session stopped")
            break

    def _runner():
        try:
            asyncio.run(_bot_loop())
        except Exception as e:
            with _state_lock:
                acc_now = ACCOUNTS.get(key)
                if acc_now and not acc_now.get("is_paused"):
                    acc_now["status"] = "OFFLINE"
                    acc_now["last_error"] = str(e)
            _log(key, f"Worker error: {e}")
        finally:
            with _state_lock:
                e = EXP_STOPS.pop(key, None)
                if e:
                    e.set()
                SESSIONS.pop(key, None)
                RUNNERS.pop(key, None)
                acc_now = ACCOUNTS.get(key)
                if acc_now:
                    acc_now["active_matches"] = 0
                    acc_now["task_started_at"] = 0
                    if acc_now.get("is_paused"):
                        acc_now["status"] = "PAUSED"
                    elif acc_now.get("level_limit_reached"):
                        acc_now["status"] = "STOPPED"
                    elif acc_now.get("exp_limit_reached"):
                        acc_now["status"] = "EXPIRED"
                    _mark_dirty()

    threading.Thread(target=_runner, daemon=True, name=f"jxe_bot_{key}").start()


def _stop_bot(key):
    with _state_lock:
        r = RUNNERS.get(key)
        e = EXP_STOPS.get(key)
    if r:
        r["stop"].set()
    if e:
        e.set()


def _boot_saved_accounts():
    _load_initial_accounts()
    threading.Thread(target=_disk_writer_loop, daemon=True, name="jxe_disk_writer").start()

    def _staggered_boot():
        with _state_lock:
            keys = [
                k for k, acc in ACCOUNTS.items()
                if not acc.get("is_paused")
                and not acc.get("level_limit_reached")
                and not acc.get("exp_limit_reached")
            ]
        for k in keys:
            _run_bot(k)
            time.sleep(0.15)

    threading.Thread(target=_staggered_boot, daemon=True, name="jxe_boot").start()


_boot_saved_accounts()


# ── Dashboard & System API Routes ──
@app.get("/healthz")
def health():
    return "ok", 200


@app.get("/")
def index():
    return render_template("index.html")


def _format_account_for_stats(acc, now):
    key = acc["key"]
    display_uid = str(acc.get("game_uid") or acc.get("uid") or key)
    created_at = float(acc.get("created_at") or now)
    gained = int(acc.get("gain", 0) or 0)
    exp_limit = int(acc.get("exp_limit") or DEFAULT_DAILY_EXP_LIMIT)
    daily_gained = int(acc.get("daily_exp_gained", 0) or 0)

    is_running = key in RUNNERS
    status = str(acc.get("status") or "ONLINE").upper()
    if acc.get("is_paused"):
        status = "PAUSED"
    elif acc.get("level_limit_reached"):
        status = "STOPPED"
    elif acc.get("exp_limit_reached"):
        status = "EXPIRED"
    elif not is_running and status not in ("CONNECTING", "PAUSED", "STOPPED", "EXPIRED"):
        status = "CONNECTING"

    task_started = float(acc.get("task_started_at") or 0)
    if not is_running or status in ("OFFLINE", "PAUSED", "STOPPED", "EXPIRED"):
        task_started = 0
        rate_per_min = 0.0
    else:
        elapsed_min = max(0.1, (now - task_started) / 60.0) if task_started > 0 else 1.0
        rate_per_min = round(gained / elapsed_min, 2) if (gained > 0 and task_started > 0) else 0.0

    return {
        "uid": display_uid,
        "display_uid": display_uid,
        "auth_uid": str(acc.get("uid") or key),
        "actual_uid": display_uid,
        "account_id": display_uid,
        "auth_token_prefix": key if acc.get("type") == "token" else "",
        "nickname": acc.get("nickname") or f"Player_{display_uid[:6]}",
        "region": acc.get("region") or "BD",
        "level": int(acc.get("level", 1) or 1),
        "initial_exp": int(acc.get("initial_exp", 0) or 0),
        "current_exp": int(acc.get("exp", 0) or 0),
        "gained_exp": gained,
        "matches_played": int(acc.get("matches_played", 0) or 0),
        "status": status,
        "active_matches": int(acc.get("active_matches", 0) or 0),
        "created_at": created_at,
        "last_update": acc.get("last_update") or now,
        "level_limit": acc.get("level_limit"),
        "level_limit_reached": bool(acc.get("level_limit_reached", False)),
        "exp_limit": exp_limit,
        "exp_limit_custom": bool(acc.get("exp_limit_custom", False)),
        "daily_exp_gained": daily_gained,
        "daily_exp_remaining": max(0, exp_limit - daily_gained),
        "exp_limit_reached": bool(acc.get("exp_limit_reached", False)),
        "exp_rate_per_min": rate_per_min,
        "task_started_at": task_started,
        "last_match_at": acc.get("last_match_at") or acc.get("last_update") or created_at,
    }


@app.get("/api/stats")
def api_stats():
    now = time.time()
    out = []
    total_gained = 0
    total_matches = 0
    running_count = 0
    paused_count = 0
    to_start = []

    with _state_lock:
        for key, acc in ACCOUNTS.items():
            if _rollover_daily_if_needed(acc):
                _mark_dirty()
            _check_limits_locked(acc)
            item = _format_account_for_stats(acc, now)
            out.append(item)
            total_gained += item["gained_exp"]
            total_matches += item["matches_played"]
            if item["status"] == "PAUSED":
                paused_count += 1
            elif item["status"] not in ("STOPPED", "EXPIRED"):
                running_count += 1
                if key not in RUNNERS:
                    to_start.append(key)

    for key in to_start:
        _run_bot(key)

    out.sort(key=lambda x: (x.get("gained_exp", 0), x.get("current_exp", 0)), reverse=True)
    total_acc = len(out)
    return jsonify({
        "username": "dashboard",
        "slots": GLOBAL_SLOTS,
        "seconds_left": GLOBAL_EXPIRES - int(now),
        "expired": False,
        "accounts": out,
        "total_accounts": total_acc,
        "running_accounts": running_count,
        "paused_accounts": paused_count,
        "global_paused": total_acc > 0 and paused_count == total_acc,
        "total_gained_exp": total_gained,
        "total_matches": total_matches,
        "uptime": int(now - START_TIME),
    })


def _upsert_account_entry(item):
    if not isinstance(item, dict):
        return None, False
    uid = str(item.get("uid") or "").strip()
    pw = str(item.get("password") or "").strip()
    tk = str(item.get("token") or "").strip()
    now = time.time()

    with _state_lock:
        if uid and pw:
            key = uid
            if key in ACCOUNTS:
                acc = ACCOUNTS[key]
                acc["password"] = pw
                acc["is_paused"] = False
                if acc.get("status") in ("PAUSED", "OFFLINE", "ERROR"):
                    acc["status"] = "CONNECTING"
                _mark_dirty()
                return key, True
            norm = _normalize_row({
                "type": "guest",
                "uid": uid,
                "password": pw,
                "nickname": f"Player_{uid[:6]}",
                "level": 1,
                "status": "CONNECTING",
                "created_at": now,
                "task_started_at": now,
            })
            ACCOUNTS[key] = norm
            _mark_dirty()
            return key, False
        elif tk:
            key = tk[:20]
            if key in ACCOUNTS:
                acc = ACCOUNTS[key]
                acc["token"] = tk
                acc["is_paused"] = False
                if acc.get("status") in ("PAUSED", "OFFLINE", "ERROR"):
                    acc["status"] = "CONNECTING"
                _mark_dirty()
                return key, True
            norm = _normalize_row({
                "type": "token",
                "uid": key,
                "token": tk,
                "nickname": f"Player_{key[:6]}",
                "level": 1,
                "status": "CONNECTING",
                "created_at": now,
                "task_started_at": now,
            })
            ACCOUNTS[key] = norm
            _mark_dirty()
            return key, False
    return None, False


@app.post("/api/account/add")
def api_acc_add():
    d = request.get_json(force=True, silent=True) or {}
    key, already_exists = _upsert_account_entry(d)
    if not key:
        return jsonify({"status": "error", "error": "UID & Password or Token required"}), 400
    _run_bot(key)
    return jsonify({"status": "ok", "uid": key, "already_exists": already_exists})


@app.post("/api/accounts/bulk-add")
def api_acc_bulk_add():
    d = request.get_json(force=True, silent=True) or {}
    items = d.get("accounts") or []
    if not isinstance(items, list):
        return jsonify({"status": "error", "error": "accounts must be a list"}), 400

    added = 0
    skipped = 0
    added_uids = []
    to_launch = []
    for item in items:
        key, existed = _upsert_account_entry(item)
        if not key:
            skipped += 1
            continue
        if existed:
            skipped += 1
            to_launch.append(key)
        else:
            added += 1
            added_uids.append(key)
            to_launch.append(key)

    def _launch_batch(keys):
        for k in keys:
            _run_bot(k)
            time.sleep(0.15)

    threading.Thread(target=_launch_batch, args=(to_launch,), daemon=True).start()
    return jsonify({"status": "ok", "added": added, "skipped": skipped, "uids": added_uids})


@app.post("/api/account/delete")
def api_acc_delete():
    d = request.get_json(force=True, silent=True) or {}
    req_uid = str(d.get("uid") or "").strip()
    if not req_uid:
        return jsonify({"status": "error", "error": "Missing UID"}), 400

    key = _resolve_key(req_uid)
    if not key:
        return jsonify({"status": "ok", "removed": False, "removed_account": {"uid": req_uid}})

    _stop_bot(key)
    with _state_lock:
        acc = ACCOUNTS.pop(key, None)
        LOGS.pop(key, None)
        _mark_dirty()

    removed_data = None
    if acc:
        removed_data = {
            "type": acc.get("type", "guest"),
            "uid": acc.get("uid", key),
            "password": acc.get("password", ""),
            "token": acc.get("token", ""),
        }
    return jsonify({"status": "ok", "removed": True, "removed_account": removed_data})


@app.post("/api/account/restore")
def api_acc_restore():
    d = request.get_json(force=True, silent=True) or {}
    backup = d.get("account") or {}
    key, _ = _upsert_account_entry(backup)
    if not key:
        return jsonify({"status": "error", "error": "Invalid account backup"}), 400
    _run_bot(key)
    return jsonify({"status": "ok", "uid": key, "restored": True})


@app.post("/api/account/pause")
def api_acc_pause():
    d = request.get_json(force=True, silent=True) or {}
    req_uid = str(d.get("uid") or "").strip()
    key = _resolve_key(req_uid)
    if not key:
        return jsonify({"status": "error", "error": "Account not found"}), 404
    with _state_lock:
        acc = ACCOUNTS.get(key)
        if acc:
            acc["is_paused"] = True
            acc["status"] = "PAUSED"
            acc["active_matches"] = 0
            _mark_dirty()
    _stop_bot(key)
    return jsonify({"status": "ok"})


@app.post("/api/account/start")
@app.post("/api/account/resume")
def api_acc_start():
    d = request.get_json(force=True, silent=True) or {}
    req_uid = str(d.get("uid") or "").strip()
    key = _resolve_key(req_uid)
    if not key:
        return jsonify({"status": "error", "error": "Account not found"}), 404
    with _state_lock:
        acc = ACCOUNTS.get(key)
        if not acc:
            return jsonify({"status": "error", "error": "Account not found"}), 404
        _rollover_daily_if_needed(acc)
        hit = _check_limits_locked(acc)
        if hit == "exp":
            return jsonify({"status": "error", "error": "Daily EXP limit reached. Increase the EXP limit to continue."}), 409
        if hit == "level":
            return jsonify({"status": "error", "error": "Level limit reached. Increase the Level limit to continue."}), 409
        acc["is_paused"] = False
        acc["status"] = "CONNECTING"
        acc["task_started_at"] = time.time()
        _mark_dirty()
    _run_bot(key)
    return jsonify({"status": "ok"})


@app.post("/api/account/refresh")
def api_acc_refresh():
    d = request.get_json(force=True, silent=True) or {}
    req_uid = str(d.get("uid") or "").strip()
    key = _resolve_key(req_uid)
    if not key:
        return jsonify({"status": "error", "error": "Account not found"}), 404
    try:
        _refresh_account_sync(key)
    except Exception:
        pass
    return jsonify({"status": "ok"})


@app.post("/api/account/limit")
def api_acc_limit():
    d = request.get_json(force=True, silent=True) or {}
    req_uid = str(d.get("uid") or "").strip()
    key = _resolve_key(req_uid)
    if not key:
        return jsonify({"status": "error", "error": "Account not found"}), 404

    val = d.get("limit")
    if val is None or str(val).strip() == "":
        lim = None
    else:
        try:
            lim = int(str(val).strip())
            if not (1 <= lim <= 100):
                return jsonify({"status": "error", "error": "Limit must be 1-100"}), 400
        except (ValueError, TypeError):
            return jsonify({"status": "error", "error": "Invalid limit"}), 400

    should_stop = False
    should_start = False
    with _state_lock:
        acc = ACCOUNTS.get(key)
        if acc:
            acc["level_limit"] = lim
            hit = _check_limits_locked(acc)
            if hit == "level":
                should_stop = True
            elif not acc.get("is_paused") and not acc.get("exp_limit_reached"):
                if acc.get("status") == "STOPPED":
                    acc["status"] = "CONNECTING"
                should_start = True
            _mark_dirty()

    if should_stop:
        _stop_bot(key)
    elif should_start:
        _run_bot(key)
    return jsonify({"status": "ok", "limit": lim})


@app.post("/api/account/exp-limit")
def api_acc_exp_limit():
    d = request.get_json(force=True, silent=True) or {}
    req_uid = str(d.get("uid") or "").strip()
    key = _resolve_key(req_uid)
    if not key:
        return jsonify({"status": "error", "error": "Account not found"}), 404

    val = d.get("limit")
    if val is None or str(val).strip() == "":
        lim = DEFAULT_DAILY_EXP_LIMIT
        custom = False
    else:
        try:
            lim = int(str(val).strip())
            if not (1 <= lim <= MAX_DAILY_EXP_LIMIT):
                return jsonify({"status": "error", "error": f"EXP limit must be between 1 and {MAX_DAILY_EXP_LIMIT:,}"}), 400
            custom = True
        except (ValueError, TypeError):
            return jsonify({"status": "error", "error": "EXP limit must be a whole number"}), 400

    should_stop = False
    should_start = False
    with _state_lock:
        acc = ACCOUNTS.get(key)
        if acc:
            acc["exp_limit"] = lim
            acc["exp_limit_custom"] = custom
            hit = _check_limits_locked(acc)
            if hit == "exp":
                should_stop = True
            elif not acc.get("is_paused") and not acc.get("level_limit_reached"):
                if acc.get("status") == "EXPIRED":
                    acc["status"] = "CONNECTING"
                should_start = True
            _mark_dirty()

    if should_stop:
        _stop_bot(key)
    elif should_start:
        _run_bot(key)
    return jsonify({"status": "ok", "limit": lim, "custom": custom})


@app.post("/api/system/limits")
def api_system_limits():
    d = request.get_json(force=True, silent=True) or {}
    lvl_val = d.get("level_limit")
    exp_val = d.get("exp_limit")

    if lvl_val is None or str(lvl_val).strip() == "":
        lvl_lim = None
    else:
        try:
            lvl_lim = int(str(lvl_val).strip())
            if not (1 <= lvl_lim <= 100):
                return jsonify({"status": "error", "error": "Level limit must be 1-100"}), 400
        except (ValueError, TypeError):
            return jsonify({"status": "error", "error": "Invalid level limit"}), 400

    if exp_val is None or str(exp_val).strip() == "":
        exp_lim = DEFAULT_DAILY_EXP_LIMIT
        exp_custom = False
    else:
        try:
            exp_lim = int(str(exp_val).strip())
            if not (1 <= exp_lim <= MAX_DAILY_EXP_LIMIT):
                return jsonify({"status": "error", "error": "Invalid EXP limit"}), 400
            exp_custom = True
        except (ValueError, TypeError):
            return jsonify({"status": "error", "error": "Invalid EXP limit"}), 400

    to_stop = []
    to_start = []
    with _state_lock:
        for key, acc in ACCOUNTS.items():
            acc["level_limit"] = lvl_lim
            acc["exp_limit"] = exp_lim
            acc["exp_limit_custom"] = exp_custom
            hit = _check_limits_locked(acc)
            if hit:
                to_stop.append(key)
            elif not acc.get("is_paused"):
                if acc.get("status") in ("STOPPED", "EXPIRED"):
                    acc["status"] = "CONNECTING"
                to_start.append(key)
        _mark_dirty()
        count = len(ACCOUNTS)

    for k in to_stop:
        _stop_bot(k)
    for k in to_start:
        _run_bot(k)
    return jsonify({"status": "ok", "count": count})


@app.post("/api/system/start-all")
def api_system_start_all():
    now = time.time()
    to_start = []
    with _state_lock:
        for key, acc in ACCOUNTS.items():
            acc["is_paused"] = False
            _rollover_daily_if_needed(acc)
            if not _check_limits_locked(acc):
                acc["status"] = "CONNECTING"
                acc["task_started_at"] = now
                to_start.append(key)
        _mark_dirty()
        count = len(ACCOUNTS)

    def _start_batch(keys):
        for k in keys:
            _run_bot(k)
            time.sleep(0.15)

    threading.Thread(target=_start_batch, args=(to_start,), daemon=True).start()
    return jsonify({"status": "ok", "count": count})


@app.post("/api/system/pause-all")
def api_system_pause_all():
    with _state_lock:
        keys = list(ACCOUNTS.keys())
        for acc in ACCOUNTS.values():
            acc["is_paused"] = True
            acc["status"] = "PAUSED"
            acc["active_matches"] = 0
        _mark_dirty()

    for k in keys:
        _stop_bot(k)
    return jsonify({"status": "ok", "count": len(keys)})


@app.post("/api/system/delete-all")
def api_system_delete_all():
    d = request.get_json(force=True, silent=True) or {}
    if str(d.get("confirm", "")).upper() != "DELETE":
        return jsonify({"status": "error", "error": "Confirmation required"}), 400

    with _state_lock:
        keys = list(ACCOUNTS.keys())
        count = len(keys)
        ACCOUNTS.clear()
        LOGS.clear()
        SESSIONS.clear()
        _mark_dirty()

    for k in keys:
        _stop_bot(k)
    _flush_to_disk()
    return jsonify({"status": "ok", "count": count})


@app.post("/api/system/refresh")
def api_system_refresh():
    now = time.time()
    with _state_lock:
        keys = list(ACCOUNTS.keys())
        for acc in ACCOUNTS.values():
            acc["is_paused"] = False
            _rollover_daily_if_needed(acc)
            if not _check_limits_locked(acc):
                acc["status"] = "CONNECTING"
                acc["task_started_at"] = now
        _mark_dirty()

    def _restart_batch(k_list):
        for k in k_list:
            _stop_bot(k)
        time.sleep(0.6)
        for k in k_list:
            _run_bot(k)
            time.sleep(0.15)

    threading.Thread(target=_restart_batch, args=(keys,), daemon=True).start()
    return jsonify({"status": "ok", "count": len(keys)})


@app.get("/api/accounts/download")
def download_accounts():
    with _state_lock:
        rows = list(ACCOUNTS.values())
    payload = {
        "exported_at": _now_iso(),
        "count": len(rows),
        "accounts": [
            {
                "type": r.get("type", "guest"),
                "uid": r["uid"],
                "password": r.get("password", ""),
                "token": r.get("token", ""),
                "nickname": r.get("nickname"),
                "game_uid": r.get("game_uid"),
                "level": r.get("level"),
                "exp": r.get("exp"),
                "likes": r.get("likes"),
                "matches_played": r.get("matches_played"),
            }
            for r in rows
        ],
    }
    buf = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    return Response(
        buf,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=accounts.json"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 27472))
    log_banner(port)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
