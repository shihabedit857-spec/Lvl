# -*- coding: utf-8 -*-
import sys, os
os.environ['PYTHONUNBUFFERED'] = '1'
os.environ.setdefault('PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION', 'python')
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import asyncio, httpx, random, json, socket, struct, time, uuid, itertools
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

from google_play_scraper import app as play_scraper
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from protobuf_decoder.protobuf_decoder import Parser
from message_ids import MESSAGE_ID_TO_NAME
import thunderFF_pb2

from dashboard_server import bot_state

# ═══════════════════ CONFIG ═══════════════════

START_MATCH_INTERVAL = 1.5
MAX_MATCH_DURATION = 900
MATCH_IDLE_TIMEOUT = 4.0
NON_MATCH_RECONNECT_DELAY = 0.5
MODE_ID = 1                        
MAP_ID = 1                         # 
START_MATCH_TIMEOUT = 30.0
HANDSHAKE_BOOTSTRAP_DEADLINE = 2.5
BR_TO_LW_LEVEL = 3

# ═══ ANTI-AFK ═══
ANTI_AFK_ENABLED = True
ANTI_AFK_MIN_INTERVAL = 1.8
ANTI_AFK_MAX_INTERVAL = 3.5
ANTI_AFK_FIRE_CHANCE = 0.65
ANTI_AFK_MOVE_ONLY_CHANCE = 0.35

xK, xV = b'Yg&tc%DEuh6%Zc^8', b'6oyZDr22E3ychjM%'
AES_KEY = xK
AES_IV = xV

CRC7_TABLE = bytes([
    0, 9, 18, 27, 36, 45, 54, 63, 72, 65, 90, 83, 108, 101, 126, 119,
    25, 16, 11, 2, 61, 52, 47, 38, 81, 88, 67, 74, 117, 124, 103, 110,
    50, 59, 32, 41, 22, 31, 4, 13, 122, 115, 104, 97, 94, 87, 76, 69,
    43, 34, 57, 48, 15, 6, 29, 20, 99, 106, 113, 120, 71, 78, 85, 92,
    100, 109, 118, 127, 64, 73, 82, 91, 44, 37, 62, 55, 8, 1, 26, 19,
    125, 116, 111, 102, 89, 80, 75, 66, 53, 60, 39, 46, 17, 24, 3, 10,
    86, 95, 68, 77, 114, 123, 96, 105, 30, 23, 12, 5, 58, 51, 40, 33,
    79, 70, 93, 84, 107, 98, 121, 112, 7, 14, 21, 28, 35, 42, 49, 56,
    65, 72, 83, 90, 101, 108, 119, 126, 9, 0, 27, 18, 45, 36, 63, 54,
    88, 81, 74, 67, 124, 117, 110, 103, 16, 25, 2, 11, 52, 61, 38, 47,
    115, 122, 97, 104, 87, 94, 69, 76, 59, 50, 41, 32, 31, 22, 13, 4,
    106, 99, 120, 113, 78, 71, 92, 85, 34, 43, 48, 57, 6, 15, 20, 29,
    37, 44, 55, 62, 1, 8, 19, 26, 109, 100, 127, 118, 73, 64, 91, 82,
    60, 53, 46, 39, 24, 17, 10, 3, 116, 125, 102, 111, 80, 89, 66, 75,
    23, 30, 5, 12, 51, 58, 33, 40, 95, 86, 77, 68, 123, 114, 105, 96,
    14, 7, 28, 21, 42, 35, 56, 49, 70, 79, 84, 93, 98, 107, 112, 121,
])

_DELTA = 0x9E3779B9
_ROUNDS = 16
_FIELD_SIZES = {0: 1, 1: 2, 2: 2, 3: 1, 4: 2}
_FIELD_NAMES = {0: "sendOption", 1: "cmd", 2: "orderId", 3: "flags", 4: "length"}

CLOUDFLARE_PRIMARY_DNS = "1.1.1.1"
CLOUDFLARE_SECONDARY_DNS = "1.0.0.1"
_DNS_CACHE: Dict[str, Tuple[str, float]] = {}
_DNS_CACHE_TTL = 300.0

client = httpx.AsyncClient(verify=False, timeout=15.0,
    limits=httpx.Limits(max_connections=300, max_keepalive_connections=150))
_LOGIN_SEMAPHORE = asyncio.Semaphore(4)

headers = {
    'User-Agent': 'UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)',
    'Connection': 'Keep-Alive', 'Accept-Encoding': 'gzip',
    'Content-Type': 'application/x-www-form-urlencoded',
    'Expect': '100-continue',
    'X-Unity-Version': '2018.4.12f1',
    'X-GA-SV': '1789535859', 'X-GA': 'v1 1', 'ReleaseVersion': 'OB55'
}

# ═══════════════════ LOG FILTER ═══════════════════
VERBOSE_LOGS = False

_QUIET_PATTERNS = (
    "[RAW-IN]", "[UDP-IN]", "[INFO-IN]", "[BR-SENT]", "[LW-SENT]",
    "[TCP-SENT]", "[QUEUE]", "[SKIP]", "[SEND-OK] PREPARE_ACK",
    "match length mismatch", "[INFO] Bootstrap deadline", "Match search sent",
)


def log(msg):
    print(msg, flush=True)
    if not VERBOSE_LOGS:
        for pat in _QUIET_PATTERNS:
            if pat in msg and not msg.startswith("[★]") and "[ANTI-AFK]" not in msg:
                return
    try:
        lvl = ("error" if msg.startswith("[-]") else
               "success" if msg.startswith("[+]") else
               "warning" if msg.startswith("[!]") else "info")
        bot_state.log(msg, lvl)
    except Exception:
        pass


def print_error(m): log(f"[-] {m}")
def print_success(m): log(f"[+] {m}")
def print_info(m): log(f"[i] {m}")
def print_warning(m): log(f"[!] {m}")


def get_proto_field(d, key, default=None):
    if not d or not isinstance(d, dict): return default
    if key in d:
        v = d[key].get('data'); return v if v is not None else default
    if str(key) in d:
        v = d[str(key)].get('data'); return v if v is not None else default
    return default


def _pb_varint(n):
    if n < 0: n = (1 << 64) + n
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        if n: b |= 0x80
        out.append(b)
        if not n: break
    return bytes(out)


def _pb_tag(f, w): return _pb_varint((f << 3) | w)


def _pb_field(f, v):
    if isinstance(v, bool): v = int(v)
    if isinstance(v, int): return _pb_tag(f, 0) + _pb_varint(v)
    if isinstance(v, str):
        d = v.encode('utf-8'); return _pb_tag(f, 2) + _pb_varint(len(d)) + d
    if isinstance(v, (bytes, bytearray)):
        d = bytes(v); return _pb_tag(f, 2) + _pb_varint(len(d)) + d
    return b""


def crc7(data):
    c = 0
    for b in data:
        c = CRC7_TABLE[((2 * (c & 0xFF)) ^ (b & 0xFF)) & 0xFF] & 0x7F
    return c & 0x7F


def uleb_encode(n):
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        if n: b |= 0x80
        out.append(b)
        if not n: break
    return bytes(out)


def has_ssan_zig(n):
    z = (n << 1) & 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while z >= 0x80:
        out.append((z & 0x7F) | 0x80); z >>= 7
    out.append(z)
    return bytes(out)


def _generate_new_device():
    dl = [
        ("Samsung", "SM-G998B", "Adreno (TM) 660", "Android OS 12 / API-31"),
        ("Xiaomi", "2201122G", "Adreno (TM) 730", "Android OS 13 / API-33"),
        ("Realme", "RMX3700", "Mali-G710", "Android OS 14 / API-34"),
        ("OnePlus", "CPH2451", "Adreno (TM) 740", "Android OS 13 / API-33"),
        ("OPPO", "CPH2611", "Adreno (TM) 720", "Android OS 14 / API-34"),
        ("Vivo", "V2203", "Mali-G710", "Android OS 12 / API-31"),
        ("Poco", "M2102J20SG", "Adreno (TM) 660", "Android OS 13 / API-33"),
    ]
    b, m, g, o = random.choice(dl)
    return {"unique_device_id": f"Google|{uuid.uuid4()}", "brand": b, "model": m,
            "gpu_renderer": g, "system_software": o,
            "screen_width": random.choice([1080, 1440, 720, 1280]),
            "screen_height": random.choice([2400, 3200, 1600, 2400]),
            "screen_dpi": str(random.randint(300, 420)),
            "memory": random.randint(2800, 6500),
            "processor_details": f"ARM64 FP ASIMD AES VMH | {random.randint(2200, 3200)} | {random.randint(6, 12)}",
            "client_ip": f"{random.randint(103, 223)}.{random.randint(10, 250)}.{random.randint(10, 250)}.{random.randint(10, 250)}"}


def get_device_for_account(_): return _generate_new_device()


async def resolve_host_cloudflare(hostname):
    if not hostname: return hostname
    p = hostname.split('.')
    if len(p) == 4 and all(x.isdigit() and 0 <= int(x) <= 255 for x in p):
        return hostname
    now = time.time()
    if hostname in _DNS_CACHE:
        ip, e = _DNS_CACHE[hostname]
        if now < e: return ip

    def _q(sip):
        s = None
        try:
            tid = random.randint(1000, 65535)
            h = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
            qn = b"".join(bytes([len(x)]) + x.encode('ascii') for x in hostname.split('.')) + b"\x00"
            pkt = h + qn + struct.pack(">HH", 1, 1)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(1.2)
            s.sendto(pkt, (sip, 53))
            r, _ = s.recvfrom(1024)
            if len(r) >= 12:
                an = struct.unpack(">H", r[6:8])[0]
                if an > 0:
                    off = 12 + len(qn) + 4
                    for _ in range(an):
                        if off >= len(r): break
                        if (r[off] & 0xC0) == 0xC0:
                            off += 2
                        else:
                            while off < len(r) and r[off] != 0:
                                off += 1 + r[off]
                            off += 1
                        if off + 10 > len(r): break
                        rt, rc, tt, rl = struct.unpack(">HHIH", r[off:off+10]); off += 10
                        if rt == 1 and rl == 4 and off + 4 <= len(r):
                            return socket.inet_ntoa(r[off:off+4])
                        off += rl
        except Exception:
            pass
        finally:
            if s:
                try: s.close()
                except Exception: pass
        return None

    loop = asyncio.get_running_loop()
    ip = await loop.run_in_executor(None, _q, CLOUDFLARE_PRIMARY_DNS)
    if not ip:
        ip = await loop.run_in_executor(None, _q, CLOUDFLARE_SECONDARY_DNS)
    if not ip:
        try:
            ii = await loop.getaddrinfo(hostname, None, family=socket.AF_INET)
            if ii: ip = ii[0][4][0]
        except Exception:
            ip = hostname
    if ip:
        _DNS_CACHE[hostname] = (ip, now + _DNS_CACHE_TTL)
    return ip or hostname


def optimize_tcp_socket(s):
    try:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "SIO_KEEPALIVE_VALS") and os.name == 'nt':
            try: s.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 2000))
            except Exception: pass
        elif hasattr(socket, "TCP_KEEPIDLE"):
            try:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
                if hasattr(socket, "TCP_KEEPINTVL"):
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 2)
                if hasattr(socket, "TCP_KEEPCNT"):
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
            except Exception:
                pass
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)
    except Exception:
        pass


def optimize_udp_socket(s):
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)
        if hasattr(socket, 'SIO_UDP_CONNRESET') and os.name == 'nt':
            try: s.ioctl(socket.SIO_UDP_CONNRESET, False)
            except Exception: pass
    except Exception:
        pass


async def safe_close_writer(w):
    if not w: return
    try:
        if not w.is_closing(): w.close()
        await asyncio.wait_for(w.wait_closed(), timeout=1.5)
    except Exception:
        pass


async def aes_encrypt(payload, key, iv):
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(payload, AES.block_size))


async def get_playstore_version():
    loop = asyncio.get_event_loop()
    try:
        r = await loop.run_in_executor(None, lambda: play_scraper('com.dts.freefireth', lang='hi', country='id'))
        return r.get("version")
    except Exception:
        return "1.132.8"


async def version_config():
    av = await get_playstore_version()
    u = ("https://version.ggwhitehawk.com/live/ver.php"
         f"?version={av}&lang=hi&device=android&channel=android"
         "&appstore=googleplay&region=ME&whitelist_version=1.3.0&whitelist_sp_version=1.0.0")
    try:
        r = await client.get(u); r.raise_for_status(); d = r.json()
        su = d.get("server_url"); rv = d.get("remote_version"); lr = d.get("latest_release_version")
        if not su or not rv or not lr: return None
        return lr, rv, su
    except Exception:
        return None


async def get_access_token(uid, password):
    url = "https://100067.connect.garena.com/oauth/guest/token/grant"
    hdrs = {"Host": "100067.connect.garena.com",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; SM-G998B Build/SP1A.210812.016)",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Encoding": "gzip, deflate, br", "Connection": "close"}
    data = {"uid": uid, "password": password, "response_type": "token", "client_type": "2",
            "client_secret": "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3",
            "client_id": "100067"}
    for _ in range(5):
        try:
            r = await client.post(url, headers=hdrs, data=data)
            if r.status_code == 200:
                rd = r.json()
                oi = rd.get("open_id"); at = rd.get("access_token"); pl = rd.get("platform", 4)
                if oi and at: return oi, at, pl
            if r.status_code == 429:
                await asyncio.sleep(1); continue
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return None


async def parse_results(pr):
    rd = {}
    for r in pr:
        fd = {"wire_type": r.wire_type}
        if r.wire_type in ("varint", "string", "bytes"):
            fd["data"] = r.data
        elif r.wire_type == "length_delimited":
            if hasattr(r.data, "results"):
                fd["data"] = await parse_results(r.data.results)
            elif isinstance(r.data, list):
                fd["data"] = await parse_results(r.data)
            else:
                fd["data"] = str(r.data)
        rd[str(r.field)] = fd
    return rd


async def decode_protobuf(data):
    return json.dumps(await parse_results(Parser().parse(data)))


async def build_majorlogin_payload(open_id, access_token, platform, client_version, device_info, verr=None):
    try:
        if verr is None: verr = client_version
        p = thunderFF_pb2.MajorLoginReq()
        p.event_time = str(datetime.now())[:-7]; p.game_name = "free fire"
        p.platform_id = 1; p.client_version = str(verr); p.client_version_code = "2019121229"
        p.system_software = "Android OS 15 / API-35 (AP3A.240905.015.A2/185014)"
        p.system_hardware = "Handheld"; p.device_type = "Handheld"
        p.screen_width = 1600; p.screen_height = 719; p.screen_dpi = "234"
        p.processor_details = "ARM64 FP ASIMD AES | 1820 | 8"; p.memory = 2798
        p.gpu_renderer = "Mali-G57"
        p.gpu_version = "OpenGL ES 3.2 v1.r49p1-04eac0.2848c17a2fd4e9340e06555168eaa3c9"
        p.unique_device_id = "Google|f744e396-5694-4e65-995d-97a958f2bd1f"
        p.client_ip = "197.0.137.129"; p.language = "pt-br"
        p.open_id = str(open_id); p.open_id_type = "4"; p.login_open_id_type = 4
        p.access_token = str(access_token); p.login_by = 2; p.platform_sdk_id = 1
        p.origin_platform_type = "4"; p.primary_platform_type = "4"; p.reg_avatar = 1
        p.channel_type = 3; p.telecom_operator = "TUNTEL"; p.network_operator_a = "TUNTEL"
        p.network_type = "WIFI"; p.network_type_a = "WIFI"; p.cpu_type = 2; p.cpu_architecture = "64"
        p.graphics_api = "OpenGLES2"; p.supported_astc_bitset = 8191
        p.client_using_version = "7428b253defc164018c604a1ebbfebdf"; p.loading_time = 15078
        p.release_channel = "android"
        p.extra_info = "KqsHTx3+QOmBRR1WKvaWewlcpqJBfjki+PPHQoQG8+0yV+Uos7gUFFjHMQ/e7u6han6Fl77r7c3vMN3p8UbKgN+nfycQCgwBmWgBzomx2gj84c+p"
        p.android_engine_init_flag = 111207; p.if_push = 1; p.is_vpn = 0
        ma = p.memory_available; ma.version = 55; ma.hidden_value = 81
        p.external_storage_total = 49973; p.external_storage_available = 11338
        p.internal_storage_total = 854; p.internal_storage_available = 11466
        p.game_disk_storage_total = 49973; p.game_disk_storage_available = 11466
        p.external_sdcard_total_storage = 49973; p.external_sdcard_avail_storage = 11466
        p.library_path = "/data/app/~~lHFxTCCbupG2QVJmsURtZw==/com.dts.freefireth-N3aCHpHNXpdxjD80uIIbww==/lib/arm64"
        p.library_token = "b8e0cd5e295eee42f5860d3c86e483dd|/data/app/~~lHFxTCCbupG2QVJmsURtZw==/com.dts.freefireth-N3aCHpHNXpdxjD80uIIbww==/base.apk"
        bp = p.SerializeToString(); ex = b""
        ex += _pb_field(96, '{"cur_rate":[90,60,120],"support_etc2":false}')
        ex += _pb_field(97, 1); ex += _pb_field(99, "4"); ex += _pb_field(100, "4")
        ex += _pb_field(102, b"\x17]ENWU\x0eR5"); ex += _pb_field(104, 52882)
        ex += _pb_field(105, 1)
        ex += _pb_field(106, "https://dl.ak.freefiremobile.com/live/ABHotUpdates/|https://core-ak.freefiremobile.com/live/ABHotUpdates/|6b2078db9d22dd98f8e9386a39af8462")
        ex += _pb_field(107, "c8e41b7a93f02d56e1a94c7b8203f5d1")
        return await aes_encrypt(bp + ex, AES_KEY, AES_IV)
    except Exception:
        return None


async def send_majorlogin(data, rv, su):
    try:
        url = f"{su}MajorLogin" if su.endswith('/') else f"{su}/MajorLogin"
        rh = headers.copy(); rh["ReleaseVersion"] = str(rv)
        r = await client.post(url, headers=rh, data=data)
        if r.status_code != 200: return None
        rc = r.content
        if len(rc) < 40: return None
        rp = thunderFF_pb2.MajorLoginRes()
        try:
            rp.ParseFromString(rc)
        except Exception:
            pass
        dr = {}
        try:
            dr = await parse_results(Parser().parse(rc.hex()))
        except Exception:
            pass
        kv = get_proto_field(dr, 22); ivv = get_proto_field(dr, 23)
        if not kv: kv = rp.aes_ak
        if not ivv: ivv = rp.iv_i
        if isinstance(kv, str):
            try: kv = bytes.fromhex(kv)
            except Exception: pass
        if isinstance(ivv, str):
            try: ivv = bytes.fromhex(ivv)
            except Exception: pass
        if not kv or not ivv:
            for off in range(min(128, len(rc))):
                try:
                    c = thunderFF_pb2.MajorLoginRes(); c.ParseFromString(rc[off:])
                    if c.region and c.token:
                        rp = c; break
                except Exception:
                    pass
            try:
                dr = await parse_results(Parser().parse(rc.hex()))
                kv2 = get_proto_field(dr, 22); iv2 = get_proto_field(dr, 23)
                if isinstance(kv2, str):
                    try: kv2 = bytes.fromhex(kv2)
                    except Exception: pass
                if isinstance(iv2, str):
                    try: iv2 = bytes.fromhex(iv2)
                    except Exception: pass
                if kv2: kv = kv2
                if iv2: ivv = iv2
            except Exception:
                pass
        if kv:
            try: rp.aes_ak = kv
            except Exception: pass
        if ivv:
            try: rp.iv_i = ivv
            except Exception: pass
        return rp
    except Exception as e:
        log(f"[-] send_majorlogin error: {e}")
        return None


async def send_getlogin(data, base_url, token, rv):
    try:
        url = f"{base_url.rstrip('/')}/GetLoginData"
        rh = headers.copy(); rh["ReleaseVersion"] = rv
        rh['Authorization'] = f"Bearer {token}"; rh['Host'] = "clientbp.ppmainecoonghj.com"
        r = await client.post(url, headers=rh, data=data)
        if r.status_code != 200: return None
        rc = r.content
        rp = thunderFF_pb2.GetLoginDataRes()
        try:
            rp.ParseFromString(rc)
        except Exception:
            pass

        def _ea(rb, fn):
            try:
                pos = 0; n = len(rb)

                def rv2(b, p):
                    res = 0; sh = 0
                    while p < len(b):
                        x = b[p]; p += 1; res |= (x & 0x7F) << sh
                        if not (x & 0x80): break
                        sh += 7
                    return res, p

                fs = {}
                while pos < n:
                    try:
                        k, pos = rv2(rb, pos)
                    except Exception:
                        break
                    fnn = k >> 3; wt = k & 0x07
                    try:
                        if wt == 0:
                            v, pos = rv2(rb, pos)
                        elif wt == 2:
                            ln, pos = rv2(rb, pos); v = rb[pos:pos+ln]; pos += ln
                        elif wt == 5:
                            v = rb[pos:pos+4]; pos += 4
                        elif wt == 1:
                            v = rb[pos:pos+8]; pos += 8
                        else:
                            break
                    except Exception:
                        break
                    fs.setdefault(fnn, []).append(v)
                if fn in fs:
                    x = fs[fn][0]
                    if isinstance(x, bytes):
                        try:
                            return x.decode('utf-8', 'ignore')
                        except Exception:
                            return None
                    return str(x)
            except Exception:
                return None

        try:
            for off in range(0, min(80, len(rc))):
                fa = _ea(rc[off:], 14); ia = _ea(rc[off:], 32)
                if fa and ":" in fa and ia and ":" in ia:
                    rp.functional_addrs = fa; rp.informational_addrs = ia; break
        except Exception:
            pass
        dr = {}
        try:
            dr = await parse_results(Parser().parse(rc.hex()))
        except Exception:
            pass
        return rp, dr
    except Exception as e:
        log(f"[-] send_getlogin error: {e}")
        return None


async def build_tcp_startup_packet(aid, token, st, key, iv, region="BD", typ='OnLine'):
    uh = f"{int(aid):016x}"; th = f"{int(st):08x}"
    ep = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(token.encode(), 16)).hex()
    epl = f"{len(ep) // 2:08x}"
    reg = str(region).upper() if region else "BD"
    if typ == 'OnLine':
        if reg in ("BD", "ME"):
            prefix = "7219"
        elif reg == "IND":
            prefix = "7214"
        else:
            prefix = "7215"
        return f"{prefix}{uh}{th}00000000{epl}{ep}"
    else:
        if reg in ("BD", "ME"):
            prefix = "8119"
        elif reg == "IND":
            prefix = "8114"
        else:
            prefix = "8115"
        return f"{prefix}{uh}{th}{epl}{ep}"


async def send_keep_alive(region="BD"):
    try:
        r = (str(region).upper() if region else "BD").strip()
        if r in ("ME", "BD"): return bytes.fromhex("0219")
        if r == "IND": return bytes.fromhex("0214")
        return bytes.fromhex("0215")
    except Exception:
        return bytes.fromhex("0219")


_REGION_PREFIX = {
    "ME": "031900", "IND": "031400", "BD": "031900",
    "SG": "031500", "TH": "031500", "PH": "031500",
    "VN": "031500", "MY": "031500", "ID": "031500", "HK": "031500",
    "TW": "031500", "PK": "031500", "BR": "031500", "RU": "031500",
}


def resolve_match_mode(uid: str, mode_override: str = "") -> str:
    mo = (mode_override or "AUTO").upper().strip()
    if mo == "BR": return "BR"
    if mo in ("LW", "LONE_WOLF", "LONE"): return "LW"
    try:
        acc = bot_state.accounts.get(str(uid))
        if acc is None:
            for v in bot_state.accounts.values():
                if (str(v.get("auth_uid")) == str(uid) or
                        str(v.get("uid")) == str(uid) or
                        str(v.get("game_uid")) == str(uid)):
                    acc = v; break
        lvl = int(acc.get("level", 1) or 1) if acc else 1
    except Exception:
        lvl = 1
    return "BR" if lvl < BR_TO_LW_LEVEL else "LW"


async def start_game_battle_royale(region, client_version, writer, key, iv):
    packet = bytes.fromhex("080112800a0a010110013a110a044944433110aa011a064555524f50453a100a044944433210311a064555524f504540014a0801090a0b1219202758016291090a8001303838463832424630324139363736373032303130313030303030303030303030303136303030313030313530303032323246393745454530463030303030303436373632353134303030303030303030303030303030303030303030303030303030303030303030303030303066663030303030303030636163666131366410241afb02735d5e571400024a775d45414d1a041b1c001f11010449715f4243481a001e1d071c1703004b1a4066785c524570735c51486775421b5c5a4c07504042685a63610816054e19025e75196001477c015165406370195f5547404e4550640103020f1304064863754268676c755f65576e40467e5f0a417a4701026d675d6e73670b1108495a4c6a0b78470b740065645e525a057258425f584a447d4e6759440c11044e7c596d7f4b625f7d04055a47505c4e1d6b5b4107447d7201057d7f0f14084e430457674f7e517d72015172415d027473577c4d615f79535256780911030f4d5e027a797f614165067806505d53777750475e75064257076500460817014e741e7e5078487e7a7c465e7669767153497064605a7376677773550d160148037e18675966787f4c42607a645f577e7b441b460776026b18685d0b110205490060020f70676175654674706671797f41067346677c4e06585e780f15074c57047b40517075415f6364027259674b5b0166407f7340600407770a22047a5d5c52300b3a0a167305067162727516134208312e3133302e3232480350015ae90403626253513635686e556f4e36416456324b796f566c636f477776484f624e56526c4d727073504b4f43654177616848494176795556497273743752737149734a7a786b3247525268377a2f637664626d504f6a73552f79626d38547a4c69586d2f474351696d494b53486833447955726f39515152756c34545350626d6d624b7949565937545671577059455372323646572f59624578507338514f706d317372785455736c30796a434144444d4f34616a654b615753366361496c554b4963797a494e396d52516f715277687939797257476d337a644345337a6a61436f492f5a585233656f65365a42647a64677654636b6b665733356e4d4c6a6a565072564b6433523172756174394e50514150724a5546627859696c4c5a3859707336654d5447666b6649793574666a526c314d4648706b51774c6373374439656378566c41636f374e664f6d2b30654756466c4434744478706771385533595973587645384842502f70666c767a737138316a32524f4d7857437556445442492f684735625462773166456e4249725162762b636144775147696f74554e316d4c4b77734379456f4766706746614251457645672b736a764c4c78704743334c304a5344532f74526169504354553344374e6249306547516651622f5a466f4c36455630775a324d6f583932414c572f5049752f56634663584e70596b356f7966326151416a536971486a2f363276354843644f525551303578754e6171795251625653704654303137655237675255636b4966366c6f447476342b514e4a4670766d74757077707774396a5a5974437a4b56743657726d6e36785837706658456251555434684f3758a201050803108703a201050804108103a20105080510c001a20105081d10cc01a2010408161078a20105080e10af01a201020815")
    proto = thunderFF_pb2.StartMatch()
    proto.ParseFromString(packet)
    reg = str(region).upper() if region else "BD"
    if hasattr(proto.main, 'region_list') and len(proto.main.region_list) > 0:
        proto.main.region_list[0].region = reg
        if len(proto.main.region_list) > 1:
            proto.main.region_list[1].region = reg
    if hasattr(proto.main, 'client_version'):
        proto.main.client_version.remote_version = client_version
    packet = proto.SerializeToString()
    encrypted_packet = (await aes_encrypt(packet, key, iv)).hex()
    packet_length = len(encrypted_packet) // 2
    hex_length = hex(packet_length)[2:]
    hex_length = hex_length if len(hex_length) > 1 else "0" + hex_length
    if reg in ("BD", "ME"):
        reg_prefix = "031900"
    elif reg == "IND":
        reg_prefix = "031400"
    else:
        reg_prefix = "031500"
    final_packet = reg_prefix + "0" * (6 - len(hex_length)) + hex_length + encrypted_packet
    writer.write(bytes.fromhex(final_packet))
    await writer.drain()
    log(f"[BR] Match search sent ({packet_length} bytes) | Region: {reg} | Prefix: {reg_prefix}")


async def start_game_lone_wolf(region, client_version, writer, key, iv):
    packet = bytes.fromhex("080112800a0a010b102b3a110a044944433110aa011a064555524f50453a100a044944433210311a064555524f504540014a0801090a0b1219202758016291090a8001303838463832424630324139363736373032303130313030303030303030303030303136303030313030313530303032323246393745454530463030303030303436373632353134303030303030303030303030303030303030303030303030303030303030303030303030303066663030303030303030636163666131366410241afb02735d5e571400024a775d45414d1a041b1c001f11010449715f4243481a001e1d071c1703004b1a4066785c524570735c51486775421b5c5a4c07504042685a63610816054e19025e75196001477c015165406370195f5547404e4550640103020f1304064863754268676c755f65576e40467e5f0a417a4701026d675d6e73670b1108495a4c6a0b78470b740065645e525a057258425f584a447d4e6759440c11044e7c596d7f4b625f7d04055a47505c4e1d6b5b4107447d7201057d7f0f14084e430457674f7e517d72015172415d027473577c4d615f79535256780911030f4d5e027a797f614165067806505d53777750475e75064257076500460817014e741e7e5078487e7a7c465e7669767153497064605a7376677773550d160148037e18675966787f4c42607a645f577e7b441b460776026b18685d0b110205490060020f70676175654674706671797f41067346677c4e06585e780f15074c57047b40517075415f6364027259674b5b0166407f7340600407770a22047a5d5c52300b3a0a167305067162727516134208312e3133302e3232480350015ae90403626253513635686e556f4e36416456324b796f566c636f477776484f624e56526c4d727073504b4f43654177616848494176795556497273743752737149734a7a786b3247525268377a2f637664626d504f6a73552f79626d38547a4c69586d2f474351696d494b53486833447955726f39515152756c34545350626d6d624b7949565937545671577059455372323646572f59624578507338514f706d317372785455736c30796a434144444d4f34616a654b615753366361496c554b4963797a494e396d52516f715277687939797257476d337a644345337a6a61436f492f5a585233656f65365a42647a64677654636b6b665733356e4d4c6a6a565072564b6433523172756174394e50514150724a5546627859696c4c5a3859707336654d5447666b6649793574666a526c314d4648706b51774c6373374439656378566c41636f374e664f6d2b30654756466c4434744478706771385533595973587645384842502f70666c767a737138316a32524f4d7857437556445442492f684735625462773166456e4249725162762b636144775147696f74554e316d4c4b77734379456f4766706746614251457645672b736a764c4c78704743334c304a5344532f74526169504354553344374e6249306547516651622f5a466f4c36455630775a324d6f583932414c572f5049752f56634663584e70596b356f7966326151416a536971486a2f363276354843644f525551303578754e6171795251625653704654303137655237675255636b4966366c6f447476342b514e4a4670766d74757077707774396a5a5974437a4b56743657726d6e36785837706658456251555434684f3758a201050803108703a201050804108103a20105080510c001a20105081d10cc01a2010408161078a20105080e10af01a201020815")
    proto = thunderFF_pb2.StartMatch()
    proto.ParseFromString(packet)
    reg = str(region).upper() if region else "BD"
    if hasattr(proto.main, 'region_list') and len(proto.main.region_list) > 0:
        proto.main.region_list[0].region = reg
        if len(proto.main.region_list) > 1:
            proto.main.region_list[1].region = reg
    if hasattr(proto.main, 'client_version'):
        proto.main.client_version.remote_version = client_version
    packet = proto.SerializeToString()
    encrypted_packet = (await aes_encrypt(packet, key, iv)).hex()
    packet_length = len(encrypted_packet) // 2
    hex_length = hex(packet_length)[2:]
    hex_length = hex_length if len(hex_length) > 1 else "0" + hex_length
    if reg in ("BD", "ME"):
        reg_prefix = "031900"
    elif reg == "IND":
        reg_prefix = "031400"
    else:
        reg_prefix = "031500"
    final_packet = reg_prefix + "0" * (6 - len(hex_length)) + hex_length + encrypted_packet
    writer.write(bytes.fromhex(final_packet))
    await writer.drain()
    log(f"[LW] Match search sent ({packet_length} bytes) | Region: {reg} | Prefix: {reg_prefix}")


async def tea_enc(v0, v1, k0, k1, k2, k3):
    s = 0
    for _ in range(_ROUNDS):
        s = (s + _DELTA) & 0xFFFFFFFF
        v0 = (v0 + (((((v1 << 4) & 0xFFFFFFFF) + k0) & 0xFFFFFFFF ^ ((v1 + s) & 0xFFFFFFFF) ^ (((v1 >> 5) + k1) & 0xFFFFFFFF)))) & 0xFFFFFFFF
        v1 = (v1 + (((((v0 << 4) & 0xFFFFFFFF) + k2) & 0xFFFFFFFF ^ ((v0 + s) & 0xFFFFFFFF) ^ (((v0 >> 5) + k3) & 0xFFFFFFFF)))) & 0xFFFFFFFF
    return v0, v1


async def tea_dec(v0, v1, k0, k1, k2, k3):
    s = (_DELTA * _ROUNDS) & 0xFFFFFFFF
    for _ in range(_ROUNDS):
        v1 = (v1 - (((((v0 << 4) & 0xFFFFFFFF) + k2) & 0xFFFFFFFF ^ ((v0 + s) & 0xFFFFFFFF) ^ (((v0 >> 5) + k3) & 0xFFFFFFFF)))) & 0xFFFFFFFF
        v0 = (v0 - (((((v1 << 4) & 0xFFFFFFFF) + k0) & 0xFFFFFFFF ^ ((v1 + s) & 0xFFFFFFFF) ^ (((v1 >> 5) + k1) & 0xFFFFFFFF)))) & 0xFFFFFFFF
        s = (s - _DELTA) & 0xFFFFFFFF
    return v0, v1


async def tea_cbc_encrypt(padded, kb):
    k0, k1, k2, k3 = (struct.unpack_from("<I", kb, o)[0] for o in (0, 4, 8, 12))
    out = bytearray(len(padded)); pc = bytearray(8); pi = bytearray(8)
    for i in range(0, len(padded), 8):
        x = bytearray(8)
        for j in range(8):
            x[j] = padded[i + j] ^ pc[j]
        e0, e1 = await tea_enc(struct.unpack_from("<I", x, 0)[0], struct.unpack_from("<I", x, 4)[0], k0, k1, k2, k3)
        en = bytearray(8); struct.pack_into("<I", en, 0, e0); struct.pack_into("<I", en, 4, e1)
        for j in range(8):
            out[i + j] = en[j] ^ pi[j]
        pc[:] = out[i:i + 8]; pi[:] = x
    return bytes(out)


async def tea_cbc_decrypt(body, kb):
    k0, k1, k2, k3 = (struct.unpack_from("<I", kb, o)[0] for o in (0, 4, 8, 12))
    out = bytearray(len(body)); pi = bytearray(8); pc = bytearray(8); x = bytearray(8); dc = bytearray(8)
    for i in range(0, len(body), 8):
        for j in range(8):
            x[j] = body[i + j] ^ pi[j]
        d0, d1 = await tea_dec(struct.unpack_from("<I", x, 0)[0], struct.unpack_from("<I", x, 4)[0], k0, k1, k2, k3)
        struct.pack_into("<I", dc, 0, d0); struct.pack_into("<I", dc, 4, d1)
        for j in range(8):
            out[i + j] = dc[j] ^ pc[j]
        pc[:] = body[i:i + 8]; pi[:] = dc
    return bytes(out)


async def build_padded(c):
    pl = (8 - (len(c) + 10) % 8) % 8
    return bytes([pl, 0, 0]) + b"\x00" * pl + c + b"\x00" * 7


async def encode_header(layout, so, cmd, oid, fl, ln, k, v80):
    out = bytearray()
    for code in layout:
        v = {0: so, 1: cmd, 2: oid, 3: fl, 4: ln}[code]
        if _FIELD_SIZES[code] == 1:
            out.append((v & 0xFF) ^ k)
        else:
            x = ((v & 0xFFFF) ^ v80) & 0xFFFF
            out.append(x & 0xFF); out.append((x >> 8) & 0xFF)
    return bytes(out)


async def crc7_buff(crc, buf):
    c = crc & 0x7F
    for b in buf:
        c = CRC7_TABLE[((2 * (c & 0xFF)) ^ (b & 0xFF)) & 0xFF] & 0x7F
    return c & 0x7F


async def sv_frame(mk, layout, so, cmd, oid, fl, c, key, enc=True):
    k = key[0]; v80 = ((k << 8) | k) & 0xFFFF
    body = await tea_cbc_encrypt(await build_padded(c), key) if enc else c
    hdr = bytearray([mk, 0]) + await encode_header(layout, so, cmd, oid, fl, len(body), k, v80)
    pkt = bytearray(hdr + body); pkt[1] = await crc7_buff(0, bytes(pkt[2:])) & 0x7F
    return bytes(pkt)


async def build_packet(mk, layout, so, cmd, oid, fl, c, key, enc=True):
    k = key[0]; v80 = ((k << 8) | k) & 0xFFFF
    body = await tea_cbc_encrypt(await build_padded(c), key) if enc else c
    hdr = bytearray([mk, 0])
    for code in layout:
        v = {0: so, 1: cmd, 2: oid, 3: fl, 4: len(body)}[code]
        if _FIELD_SIZES[code] == 1:
            hdr.append((v & 0xFF) ^ k)
        else:
            x = ((v & 0xFFFF) ^ v80) & 0xFFFF
            hdr.append(x & 0xFF); hdr.append((x >> 8) & 0xFF)
    pkt = bytearray(hdr + body); pkt[1] = await crc7_buff(0, bytes(pkt[2:])) & 0x7F
    return bytes(pkt)


async def build_match_startup_packets(token, udp_key, match_code, aid, block_val,
                                       server_ip="", region="BD", client_version="1.132.8",
                                       client_version_code="2019121229", access_token="",
                                       mode_id=MODE_ID, map_id=MAP_ID, mode="BR"):
    token = token.strip()
    udp_key = bytes.fromhex(udp_key)
    match_code = [int(c) for c in str(match_code).strip()]
    tj = token[:660] if len(token) > 660 else token
    sj = token[660:] if len(token) > 660 else ""
    et = tj.encode() if isinstance(tj, str) else tj
    es = sj.encode() if isinstance(sj, str) else sj
    g = has_ssan_zig(len(et)) + et
    reg = str(region).upper() if region else "BD"
    cs = bytes.fromhex("ca0163736f7665727365612e7374726f6e67686f6c642e66726565666972656d6f62696c652e636f6d3b302e302e302e303b33342e3132362e37362e34353b33342e38372e3137372e31343b33342e38372e3137302e3233303b33352e3138352e3138332e3537000000000000010000000000000000000000000100000800000100000000000100a8a2d7bebd8d8bdf110200")
    mid = bytes.fromhex('0000000001000102030101') + has_ssan_zig(len(reg)) + reg.encode()
    mid += bytes.fromhex('0001030003000004')
    mid += has_ssan_zig(len(client_version)) + client_version.encode()
    mid += has_ssan_zig(len(client_version_code)) + client_version_code.encode()
    mid += cs
    cip = server_ip.split(':')[0] if server_ip else "0.0.0.0"
    mid += has_ssan_zig(len(cip)) + cip.encode()
    cat = access_token.strip() if access_token else ""
    if cat:
        mid += has_ssan_zig(len(cat)) + cat.encode()
    mid += has_ssan_zig(len(es)) + es

    if mode == "LW":
        _mid = 43; _map = 11
    else:
        _mid = int(mode_id); _map = int(map_id)

    tg = (uleb_encode(int(aid)) + uleb_encode(int(block_val)) + uleb_encode(1) +
          uleb_encode(_mid) + uleb_encode(int(block_val)) + uleb_encode(_map) + mid)
    pr = await sv_frame(0x5E, match_code, 2, 447, 0, 1, g, udp_key)
    ld = await sv_frame(0x5A, match_code, 2, 448, 1, 1, tg, udp_key)
    return pr.hex(), ld.hex()


async def produce_xor_key(sk):
    k = sk[0] if sk and len(sk) > 0 else 10
    return k, ((k << 8) | k) & 0xFFFF


async def parse_layout(l):
    if isinstance(l, str): return [int(c) for c in l.strip()]
    return list(l)


async def build_hello_packet(text, key, layout):
    d = text.encode("utf-8")
    if len(d) > 25: raise ValueError(f"Text too long ({len(d)})")
    c = b"\x10\x00\x00\x00" + d + b"\x00" * (29 - 4 - len(d))
    k, v80 = await produce_xor_key(key)
    layout = await parse_layout(layout)
    pd = await build_padded(c)
    eb = await tea_cbc_encrypt(pd, key)
    hb = await encode_header(layout, 1, 1, 0, 1, len(eb), k, v80)
    pkt = bytearray([0x63, 0x00]) + hb + eb
    pkt[1] = await crc7_buff(0, pkt[2:]) & 0x7F
    return bytes(pkt).hex()


async def classify(f):
    cmd = f["cmd"]
    mn = MESSAGE_ID_TO_NAME.get(cmd, f"UNKNOWN_{cmd}")
    if mn == "UDP_HELLO": return "HELLO"
    if mn == "UDP_ACK": return "ACK"
    if mn == "UDP_PING": return "PING"
    if mn == "RUDP_JOIN_MATCH": return "JOIN_MATCH"
    if mn.startswith("RUDP_"): return mn
    if mn.startswith("UDP_"): return mn
    return "DATA"


async def layouts_from_mask(mask):
    ru = [int(c) for c in str(mask).strip()]
    return ru, [c for c in ru if c != 2]


async def reply_for(f, key, mask, ack_key=0x68, ping_key=0x6D, hello_key=0x5B, ack_style="short"):
    _, nr = await layouts_from_mask(mask)
    t = await classify(f)
    if t == "HELLO":
        if ack_style == "echo":
            c = f["content"] if f["content"] else b"\x10\x00\x00\x00"
            return t, await build_packet(hello_key, nr, 1, 1, None, 1, c, key)
        return t, await build_packet(ack_key, nr, 0, 2, None, 1, b"\x01\x00", key)
    if t == "ACK":
        c = f["content"] if f["content"] else b"\x01\x00"
        return t, await build_packet(ack_key, nr, 0, 2, None, 1, c, key)
    if t == "PING":
        c = f["content"]; cn = c[:4] if len(c) >= 4 else c
        return t, await build_packet(ping_key, nr, 0, 3, None, 0, cn + b"\x00\x00\x00", key, False)
    if t == "JOIN_MATCH":
        return t, await build_packet(ack_key, nr, 0, 2, None, 1, b"\x02\x00", key)
    return t, None


async def keepalive_ping(sock, ip, port, kb, mask, stop_ev):
    _, nr = await layouts_from_mask(mask)
    keys = [0x66, 0x6D, 0x69, 0x6C, 0x6B, 0x6E, 0x6F, 0x70]
    loop = asyncio.get_event_loop(); i = 0
    while not stop_ev.is_set():
        pk = keys[i % len(keys)]
        cnt = int(time.time() * 1000) & 0xFFFFFFFF
        pkt = await build_packet(pk, nr, 0, 3, None, 0, struct.pack("<I", cnt) + b"\x00\x00\x00", kb, False)
        try:
            await loop.sock_sendto(sock, pkt, (ip, port))
        except Exception:
            pass
        i += 1
        try:
            await asyncio.wait_for(stop_ev.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pass


async def try_header(buf, layout, k, v80):
    off = 2; out = {}
    for code in layout:
        sz = _FIELD_SIZES[code]
        if off + sz > len(buf): return None
        out[_FIELD_NAMES[code]] = (buf[off] ^ k) if sz == 1 else ((buf[off] | (buf[off + 1] << 8)) ^ v80) & 0xFFFF
        off += sz
    out["headerLen"] = off
    return out


async def oicq_unpad(pd):
    if not pd or len(pd) < 8: return None
    if not all(pd[-1 - i] == 0 for i in range(7)): return None
    pl = pd[0] & 0x07; s = 3 + pl; e = len(pd) - 7
    return pd[s:e] if s < e else b""


async def decode_packet(pkt, key, mask=None):
    data = bytes(pkt) if isinstance(pkt, bytes) else bytes.fromhex(pkt)
    if len(data) < 8: return None
    k = key[0]; v80 = ((k << 8) | k) & 0xFFFF
    cok = (data[1] & 0x7F) == await crc7_buff(0, data[2:])
    cands = []
    if mask:
        ru, nr = await layouts_from_mask(mask)
        layouts = [("RUDP", ru), ("nonRUDP", nr)]
    else:
        layouts = [("RUDP", list(p)) for p in itertools.permutations([0, 1, 2, 3, 4])]
        layouts += [("nonRUDP", list(p)) for p in itertools.permutations([0, 1, 3, 4])]
    for kind, layout in layouts:
        f = await try_header(data, layout, k, v80)
        if not f: continue
        if f["flags"] > 7 or f["sendOption"] > 7: continue
        if f["length"] != len(data) - f["headerLen"]: continue
        body = data[f["headerLen"]:f["headerLen"] + f["length"]]
        content = None; padded = None
        if f["flags"] & 1:
            if len(body) < 8 or len(body) % 8 != 0: continue
            padded = await tea_cbc_decrypt(body, key)
            content = await oicq_unpad(padded)
            if content is None: continue
        else:
            content = body
        score = (1 if cok else 0) + (1 if content is not None else 0)
        cands.append({"kind": kind, "layout": layout, "headerLen": f["headerLen"],
                      "msgKey": data[0], "cmd": f["cmd"], "flags": f["flags"],
                      "sendOption": f["sendOption"], "orderId": f.get("orderId"),
                      "length": f["length"], "content": content, "crcOk": cok,
                      "padded": padded, "score": score, "total": len(data)})
    if not cands: return None
    cands.sort(key=lambda c: (c["kind"] == "RUDP" or c["kind"] == "nonRUDP", c["score"]), reverse=True)
    return cands[0]

async def anti_afk_worker(sock, ip, port, udp_key, match_code,
                          account_id, stop_ev, match_index, match_mode="BR"):
    uid_str = str(account_id)
    try:
        _, non_rudp = await layouts_from_mask(match_code)
        keys = [0x66, 0x6D, 0x69, 0x6C, 0x6B, 0x6E, 0x6F, 0x70]
        loop = asyncio.get_running_loop()

        i = 0
        move_count = 0
        fire_count = 0

        while not stop_ev.is_set():
            try:
                interval = random.uniform(ANTI_AFK_MIN_INTERVAL, ANTI_AFK_MAX_INTERVAL)
                try:
                    await asyncio.wait_for(stop_ev.wait(), timeout=interval)
                    break
                except asyncio.TimeoutError:
                    pass

                i += 1
                key_byte = keys[i % len(keys)]
                ts = int(time.time() * 1000) & 0xFFFFFFFF
                dx = random.randint(-100, 100)
                dy = random.randint(-100, 100)

                move_payload = struct.pack("<Ihh", ts, dx, dy) + b"\x00\x00"
                move_pkt = await build_packet(
                    key_byte, non_rudp, 0, 3, None, 0,
                    move_payload, udp_key, False
                )
                await loop.sock_sendto(sock, move_pkt, (ip, port))
                move_count += 1

                do_fire = (random.random() < ANTI_AFK_FIRE_CHANCE
                           and random.random() >= ANTI_AFK_MOVE_ONLY_CHANCE)

                if do_fire:
                    fire_payload = struct.pack("<I", ts) + b"\x01\x00"
                    fire_pkt = await build_packet(
                        key_byte, non_rudp, 0, 3, None, 0,
                        fire_payload, udp_key, False
                    )
                    await loop.sock_sendto(sock, fire_pkt, (ip, port))
                    fire_count += 1
                    log(f"[ANTI-AFK] [{match_mode}] Match #{match_index} | 🔥 MOVE+FIRE #{move_count} "
                        f"| dx={dx} dy={dy} | UID {uid_str}")
                else:
                    log(f"[ANTI-AFK] [{match_mode}] Match #{match_index} | 🚶 MOVE #{move_count} "
                        f"| dx={dx} dy={dy} | UID {uid_str}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"[ANTI-AFK] Match #{match_index} | [!] Send error: {e}")
                await asyncio.sleep(2)

        log(f"[ANTI-AFK] [{match_mode}] Match #{match_index} | Stopped "
            f"(moves={move_count}, fires={fire_count}) | UID {uid_str}")

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log(f"[ANTI-AFK] Match #{match_index} | [!] Worker failed: {e}")


# ═══════════════════ PLAY GAME ═══════════════════
async def play_game(server_ip_port, thunder, sharma, udp_key, match_code,
                    account_id, player_region, client_version, key, iv,
                    match_index: int, match_mode: str = "BR"):
    mst = time.time(); pt = None; sock = None; ps = asyncio.Event()
    anti_afk_task = None
    completed_cleanly = False
    uid_str = str(account_id)

    try:
        bot_state.update_status(account_id, "IN_MATCH", active_matches=1)
    except Exception:
        pass

    try:
        ip, port = server_ip_port.split(":"); port = int(port)
        rip = await resolve_host_cloudflare(ip)
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(('0.0.0.0', 0))
        except Exception:
            pass
        optimize_udp_socket(sock); sock.setblocking(False)
        ukb = bytes.fromhex(udp_key)
        hp = await build_hello_packet(f"{account_id}_2585", ukb, match_code)
        try:
            await loop.sock_sendto(sock, bytes.fromhex(hp), (rip, port))
            log(f"[MATCH #{match_index}] [{match_mode}] Started -> {rip}:{port}")
        except Exception as e:
            log(f"[MATCH #{match_index}] [SEND-FAIL] HELLO: {e}")

        ack_state = "waiting_for_hello_reply"
        thunder_sent = sharma_sent = join_match_received = False
        local_closed = False; send_lock = asyncio.Lock()
        pt = asyncio.create_task(keepalive_ping(sock, rip, port, ukb, match_code, ps))

        if ANTI_AFK_ENABLED:
            anti_afk_task = asyncio.create_task(
                anti_afk_worker(sock, rip, port, ukb, match_code,
                                account_id, ps, match_index, match_mode)
            )
            log(f"[ANTI-AFK] Started | Match #{match_index} | Mode: {match_mode} | UID {uid_str}")

        last_activity = time.time()
        MAX_IDLE = 7.0

        async def send_start():
            nonlocal ack_state, thunder_sent, sharma_sent
            if thunder_sent: return
            async with send_lock:
                if thunder_sent: return
                try:
                    tb = bytes.fromhex(thunder)
                    await loop.sock_sendto(sock, tb, (rip, port))
                    thunder_sent = True
                    await asyncio.sleep(0.3)
                    pa = await build_packet(0x68, (await layouts_from_mask(match_code))[1], 0, 2, None, 1, b"\x01\x00", ukb)
                    await loop.sock_sendto(sock, pa, (rip, port))
                    await asyncio.sleep(0.4)
                    sb = bytes.fromhex(sharma)
                    await loop.sock_sendto(sock, sb, (rip, port))
                    sharma_sent = True; ack_state = "thunder_sharma_sent"
                    log(f"[MATCH #{match_index}] ✓ Playing {match_mode}")
                except Exception as e:
                    log(f"[MATCH #{match_index}] [SEND-FAIL] Startup: {e}")

        while not local_closed:
            el = time.time() - mst
            if el > MAX_MATCH_DURATION: break
            if ack_state == "waiting_for_hello_reply" and not thunder_sent and el > HANDSHAKE_BOOTSTRAP_DEADLINE:
                ack_state = "ready_to_send_thunder"
                try:
                    await send_start()
                except Exception:
                    pass
                continue
            try:
                r, sa = await asyncio.wait_for(loop.sock_recvfrom(sock, 65535), timeout=2.0)
                if r:
                    last_activity = time.time()
                    fr = await decode_packet(r, ukb, match_code)
                    if fr:
                        ptp = await classify(fr); cmd = fr['cmd']
                        if cmd in [103, 107]:
                            log(f"[★] Match #{match_index} finished (cmd {cmd})")
                            completed_cleanly = True
                            local_closed = True; continue
                        if cmd == 101:
                            try:
                                ap = await build_packet(0x68, (await layouts_from_mask(match_code))[1], 0, 2, None, 1, b"\x01\x00", ukb)
                                await loop.sock_sendto(sock, ap, sa)
                            except Exception:
                                pass
                        if ptp in ["ACK", "PING", "HELLO", "JOIN_MATCH"]:
                            if ptp == "HELLO" and ack_state == "waiting_for_hello_reply":
                                _, rp = await reply_for(fr, ukb, match_code, ack_style="short")
                                if rp:
                                    try:
                                        await loop.sock_sendto(sock, rp, sa)
                                    except Exception:
                                        pass
                                ack_state = "ack_sent_waiting"
                            elif ptp == "ACK":
                                _, rp = await reply_for(fr, ukb, match_code)
                                if rp:
                                    try:
                                        await loop.sock_sendto(sock, rp, sa)
                                    except Exception:
                                        pass
                                if ack_state in ("waiting_for_hello_reply", "ack_sent_waiting"):
                                    ack_state = "ready_to_send_thunder"
                            elif ptp == "PING":
                                _, rp = await reply_for(fr, ukb, match_code)
                                if rp:
                                    try:
                                        await loop.sock_sendto(sock, rp, sa)
                                    except Exception:
                                        pass
                                if ack_state in ("waiting_for_hello_reply", "ack_sent_waiting"):
                                    ack_state = "ready_to_send_thunder"
                            elif ptp == "JOIN_MATCH" and not join_match_received:
                                _, rp = await reply_for(fr, ukb, match_code)
                                if rp:
                                    try:
                                        await loop.sock_sendto(sock, rp, sa)
                                    except Exception:
                                        pass
                                join_match_received = True
            except asyncio.TimeoutError:
                if ack_state == "ready_to_send_thunder" and not thunder_sent:
                    await send_start()
                elif ack_state == "waiting_for_hello_reply":
                    if time.time() - last_activity > MAX_IDLE:
                        try:
                            p = await build_hello_packet(f"{account_id}_2585", ukb, match_code)
                            await loop.sock_sendto(sock, bytes.fromhex(p), (rip, port))
                        except Exception:
                            pass
                        last_activity = time.time()
                    if time.time() - mst > 25.0:
                        break
                elif ack_state == "thunder_sharma_sent":
                    if time.time() - last_activity > MATCH_IDLE_TIMEOUT:
                        log(f"[MATCH #{match_index}] Finished (idle)")
                        completed_cleanly = True
                        local_closed = True; continue
            except (BlockingIOError, OSError):
                await asyncio.sleep(0.3); continue
            except Exception:
                await asyncio.sleep(0.3); continue
            if ack_state == "ready_to_send_thunder" and not thunder_sent:
                await send_start()
        return f"match #{match_index} finished"
    except Exception as e:
        log(f"[MATCH #{match_index}] Session error: {e}")
        return f"match #{match_index} error"
    finally:
        if completed_cleanly:
            try:
                bot_state.increment_match(uid_str)
                log(f"[★] Match #{match_index} Complete | UID {uid_str}")
            except Exception:
                pass

        ps.set()

        if anti_afk_task:
            anti_afk_task.cancel()
            try:
                await anti_afk_task
            except (asyncio.CancelledError, Exception):
                pass

        if pt:
            pt.cancel()
            try:
                await pt
            except asyncio.CancelledError:
                pass
        if sock:
            try:
                sock.close()
            except Exception:
                pass

        try:
            remaining = 0
            acc = bot_state.accounts.get(uid_str)
            if acc:
                remaining = max(0, int(acc.get("active_matches", 0)) - 1)
                acc["active_matches"] = remaining
            bot_state.update_status(uid_str, "IN_MATCH" if remaining > 0 else "ONLINE", remaining)
        except Exception:
            try:
                bot_state.update_status(uid_str, "ONLINE", 0)
            except Exception:
                pass


# ═══════════════════ FUNCTIONAL WORKER ═══════════════════
async def functional_br(addrs, starter_packet, account_region, client_version,
                        key, iv, account_id="", account_data=None, max_reconnects=10):
    reconnects = 0
    ip, port = addrs.split(":")
    play_matches: List[asyncio.Task] = []
    search_attempts = 0
    last_start_time = 0.0
    current_start_sent = False
    uid_str = str(account_id)
    current_token = starter_packet
    current_key = key
    current_iv = iv
    current_account_data = account_data

    lw_silent_count = 0
    lw_last_sent_at = 0.0
    lw_disabled_for_session = False
    LW_SILENCE_TIMEOUT = 15.0

    try:
        while True:
            writer = None
            gateway_ping_task = None
            try:
                rip = await resolve_host_cloudflare(ip)
                reader, writer = await asyncio.open_connection(rip, int(port))
                try:
                    bot_state.register_writer(uid_str, writer)
                except Exception:
                    pass
                rs = writer.get_extra_info('socket')
                if rs:
                    optimize_tcp_socket(rs)
                writer.write(bytes.fromhex(current_token))
                await writer.drain()
                try:
                    ik = await send_keep_alive(account_region)
                    if ik and writer and not writer.is_closing():
                        writer.write(ik)
                        await asyncio.wait_for(writer.drain(), timeout=3)
                except Exception:
                    pass

                async def _gp():
                    kb = await send_keep_alive(account_region)
                    while True:
                        await asyncio.sleep(5)
                        try:
                            if writer and not writer.is_closing():
                                writer.write(kb)
                                await writer.drain()
                        except Exception:
                            break

                gateway_ping_task = asyncio.create_task(_gp())
                log(f"[+] TCP gateway connected | UID: {uid_str} | Region: {(account_region or 'BD').upper()}")
                reconnects = 0
                last_start_time = 0.0
                current_start_sent = False
                lw_last_sent_at = 0.0

                async def _ssm():
                    nonlocal search_attempts, last_start_time, current_start_sent
                    nonlocal lw_last_sent_at, lw_silent_count, lw_disabled_for_session

                    search_attempts += 1
                    cr = (account_region or "BD").upper()

                    mo = ""
                    if current_account_data:
                        mo = str(current_account_data.get("mode_override") or "")
                    if not mo and current_account_data:
                        mo = str(current_account_data.get("mode") or "")

                    mode = resolve_match_mode(uid_str, mo)

                    if mode == "LW":
                        if lw_disabled_for_session:
                            log(f"[!] LW disabled for this session — using BR instead")
                            mode = "BR"
                        elif mo.upper() == "LW":
                            if lw_silent_count >= 3:
                                log(f"[!] LW silent {lw_silent_count}x — server may not support LW in {cr}")
                        elif lw_silent_count >= 3:
                            log(f"[!] LW silent {lw_silent_count}x in {cr} — AUTO fallback to BR")
                            lw_disabled_for_session = True
                            mode = "BR"
                            lw_silent_count = 0

                    last_start_time = asyncio.get_running_loop().time()
                    try:
                        log(f"[{mode}] UID {uid_str} -> Match #{search_attempts} | Region: {cr} | Mode: {mo or 'AUTO'}")
                        try:
                            bot_state.increment_match_started()
                        except Exception:
                            pass

                        if mode == "LW":
                            await start_game_lone_wolf(cr, client_version, writer, current_key, current_iv)
                            lw_last_sent_at = asyncio.get_running_loop().time()
                        else:
                            await start_game_battle_royale(cr, client_version, writer, current_key, current_iv)
                            lw_last_sent_at = 0.0

                        current_start_sent = True
                    except Exception as e:
                        log(f"[!] StartMatch notice: {e}")

                await _ssm()

                while True:
                    try:
                        if bot_state.is_paused(uid_str):
                            bot_state.update_status(uid_str, "PAUSED")
                            raise asyncio.CancelledError("paused")
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass

                    play_matches[:] = [m for m in play_matches if not m.done()]
                    now = asyncio.get_running_loop().time()

                    if lw_last_sent_at > 0 and not lw_disabled_for_session:
                        silent_for = now - lw_last_sent_at
                        if silent_for > LW_SILENCE_TIMEOUT and len(play_matches) == 0:
                            lw_silent_count += 1
                            log(f"[!] LW silent for {int(silent_for)}s (count={lw_silent_count})")
                            lw_last_sent_at = 0.0

                    if (len(play_matches) == 0 and
                            (not current_start_sent or (now - last_start_time) > START_MATCH_TIMEOUT)):
                        await _ssm()

                    try:
                        chunk = await asyncio.wait_for(reader.read(65535), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    if not chunk:
                        raise ConnectionError("Connection closed")

                    if lw_last_sent_at > 0:
                        lw_last_sent_at = 0.0

                    if len(chunk) >= 5 and chunk[0] == 0x03 and chunk[1] == 0x00:
                        payload_len = (chunk[2] << 16) | (chunk[3] << 8) | chunk[4]
                        actual_len = len(chunk) - 5
                        if payload_len != actual_len:
                            continue
                        payload = chunk[5:]

                        if payload_len < 30 and payload[:1] == b"\x08":
                            try:
                                bot_state.update_status(uid_str, "SEARCHING", 0)
                            except Exception:
                                pass
                            continue
                        if payload_len < 30:
                            continue

                        log(f"[+] Match found | UID: {uid_str} | size={payload_len}")
                        payload_hex = payload.hex()

                        try:
                            res = json.loads(await decode_protobuf(payload_hex))
                            token = udp_key = match_code = server_ip_port = None
                            match_account_id = block_val = None

                            if '42' in res and 'data' in res['42']:
                                match_code = res['42']['data']
                            if '5' in res and 'data' in res['5']:
                                f5 = res['5']['data']
                                server_ip_port = f5.get('2', {}).get('data')
                                udp_key = f5.get('3', {}).get('data')
                                token = f5.get('4', {}).get('data')
                                if '42' in f5:
                                    match_code = f5['42']['data']
                            if '1' in res and 'data' in res['1']:
                                match_account_id = res['1']['data']
                            if '5' in res and 'data' in res['5']:
                                block_val = res['5']['data'].get('1', {}).get('data')

                            eaid = match_account_id or account_id or "BD_BOT"
                            if not (token and udp_key and match_code and server_ip_port):
                                continue

                            mo = ""
                            if current_account_data:
                                mo = str(current_account_data.get("mode_override") or "")
                            cur_mode = resolve_match_mode(uid_str, mo)
                            if lw_disabled_for_session:
                                cur_mode = "BR"

                            at = current_account_data.get('access_token', '') if current_account_data else ""
                            thunder, sharma = await build_match_startup_packets(
                                token, udp_key, match_code, eaid, block_val or 0,
                                server_ip=server_ip_port, region=account_region,
                                client_version=client_version, access_token=at,
                                mode_id=MODE_ID, map_id=MAP_ID, mode=cur_mode)
                            mi = len(play_matches) + 1
                            log(f"[+] Match #{mi} [{cur_mode}] injected -> {server_ip_port}")
                            nm = asyncio.create_task(play_game(
                                server_ip_port, thunder, sharma, udp_key, match_code,
                                eaid, (account_region or "BD").upper(),
                                client_version, current_key, current_iv,
                                match_index=mi, match_mode=cur_mode))
                            play_matches.append(nm)
                            try:
                                await nm
                            except Exception as e:
                                log(f"[MATCH #{mi}] error: {e}")
                            try:
                                bot_state.update_status(eaid, "ONLINE", active_matches=0)
                            except Exception:
                                pass
                            play_matches[:] = [m for m in play_matches if not m.done()]
                            try:
                                await refresh_account_profile(current_account_data)
                            except Exception:
                                pass
                            log(f"[*] UID {uid_str} | match done")

                            if gateway_ping_task:
                                gateway_ping_task.cancel()
                            try:
                                bot_state.unregister_writer(uid_str, writer)
                            except Exception:
                                pass
                            await safe_close_writer(writer)
                            writer = None
                            current_start_sent = False
                            break
                        except Exception as e:
                            log(f"[!] Match packet notice: {e}")
                            continue
                    else:
                        continue

                    if writer is None:
                        break

            except asyncio.CancelledError:
                if gateway_ping_task:
                    gateway_ping_task.cancel()
                raise
            except Exception as e:
                if gateway_ping_task:
                    gateway_ping_task.cancel()
                play_matches[:] = [m for m in play_matches if not m.done()]
                try:
                    if writer:
                        bot_state.unregister_writer(uid_str, writer)
                except Exception:
                    pass
                await safe_close_writer(writer)
                if "Cache expired" in str(e):
                    break
                reconnects += 1
                if reconnects > max_reconnects:
                    reconnects = 0
                    break
                await asyncio.sleep(min(reconnects * 0.5, 2.0))
            finally:
                if gateway_ping_task:
                    gateway_ping_task.cancel()
                if writer:
                    try:
                        bot_state.unregister_writer(uid_str, writer)
                    except Exception:
                        pass
                    await safe_close_writer(writer)
    except asyncio.CancelledError:
        raise
    finally:
        for m in play_matches:
            if not m.done():
                m.cancel()


async def informational(addrs, starter_packet, key, iv, region="BD", account_id="", max_reconnects=3):
    uid_str = str(account_id); reconnects = 0
    ip, port = addrs.split(":")
    while True:
        writer = None; pt = None
        try:
            rip = await resolve_host_cloudflare(ip)
            reader, writer = await asyncio.open_connection(rip, int(port))
            try:
                bot_state.register_writer(uid_str, writer)
            except Exception:
                pass
            rs = writer.get_extra_info('socket')
            if rs:
                optimize_tcp_socket(rs)
            writer.write(bytes.fromhex(starter_packet)); await writer.drain()
            reconnects = 0
            try:
                ik = await send_keep_alive(region)
                if ik and writer and not writer.is_closing():
                    writer.write(ik); await asyncio.wait_for(writer.drain(), timeout=3)
            except Exception:
                pass

            async def _ik():
                kb = await send_keep_alive(region)
                while True:
                    await asyncio.sleep(5)
                    try:
                        if writer and not writer.is_closing():
                            writer.write(kb); await writer.drain()
                    except Exception:
                        break

            pt = asyncio.create_task(_ik())
            while True:
                try:
                    d = await asyncio.wait_for(reader.read(8192), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if not d:
                    raise ConnectionError("Connection closed")
        except asyncio.CancelledError:
            if pt: pt.cancel()
            try:
                if writer: bot_state.unregister_writer(uid_str, writer)
            except Exception:
                pass
            await safe_close_writer(writer); raise
        except Exception:
            if pt: pt.cancel()
            try:
                if writer: bot_state.unregister_writer(uid_str, writer)
            except Exception:
                pass
            await safe_close_writer(writer)
            reconnects += 1
            if reconnects > max_reconnects:
                await asyncio.sleep(3); reconnects = 0
            else:
                await asyncio.sleep(1)


async def refresh_account_profile(account_data):
    try:
        if not account_data: return
        url = account_data.get('server_url'); token = account_data.get('token')
        rv = account_data.get('release_version'); payload = account_data.get('login_payload_data')
        if not (url and token and rv and payload): return
        res = await send_getlogin(payload, url, token, rv)
        if not res: return
        rp, dr = res
        lv = int(get_proto_field(dr, 6, 1)); ex = int(get_proto_field(dr, 7, 0))
        nn = rp.nickname or get_proto_field(dr, 4, "")
        if lv <= 0: lv = 1
        if ex < 0: ex = 0
        account_data['level'] = lv; account_data['exp'] = ex
        if nn: account_data['nickname'] = nn
        try:
            bot_state.update_exp(account_data['account_id'], ex, lv)
        except Exception:
            pass
    except Exception:
        pass


async def process_account_uid_pass(uid, password, forced_region=None,
                                    forced_mode=None, forced_target_level=0):
    log(f"[*] Logging in UID: {uid}")
    try:
        async with _LOGIN_SEMAPHORE:
            vc = await version_config()
            if vc is None: return None
            rv, cv, su = vc
            tg = await get_access_token(uid, password)
            if tg is None:
                log(f"[-] OAuth failed for UID {uid}"); return None
            oi, at, pl = tg
            di = get_device_for_account(uid)
            lp = await build_majorlogin_payload(oi, at, pl, cv, di)
            if lp is None: return None
            ml = await send_majorlogin(lp, rv, su)
            if ml is None:
                log(f"[-] MajorLogin failed for {uid}"); return None
            gl = await send_getlogin(lp, ml.url, ml.token, rv)
            if gl is None:
                log(f"[-] GetLoginData failed for {uid}"); return None
            rp, dr = gl
        aid = str(ml.account_id)
        if aid == "0" or not aid or aid == "None":
            log(f"[-] Invalid account_id for UID {uid}"); return None
        lv = int(get_proto_field(dr, 6, 1)); ex = int(get_proto_field(dr, 7, 0))
        nn = rp.nickname or get_proto_field(dr, 4, f"Player_{aid}")
        rg = ml.region or get_proto_field(dr, 3, "BD")
        if forced_region:
            rg = str(forced_region).upper().strip()
        if lv <= 0: lv = 1
        if ex < 0: ex = 0
        fa = rp.functional_addrs; ia = rp.informational_addrs
        if not isinstance(fa, str) or not fa or ":" not in fa: fa = None
        if not isinstance(ia, str) or not ia or ":" not in ia: ia = None
        kv = ml.aes_ak; ivv = ml.iv_i
        if not isinstance(kv, (bytes, bytearray)) or len(kv) < 16: kv = AES_KEY
        if not isinstance(ivv, (bytes, bytearray)) or len(ivv) < 16: ivv = AES_IV

        mo = (forced_mode or "AUTO").upper()
        initial_mode = resolve_match_mode(aid, mo)
        log(f"[+] Login OK | UID {aid} | {nn} | Lvl {lv} | Region {rg} | Mode: {initial_mode} ({mo})")

        try:
            bot_state.register_account(uid=aid, nickname=nn, region=rg, level=lv, exp=ex,
                                       token=at, auth_uid=uid, target_level=forced_target_level or 0,
                                       game_uid=aid, mode_override=mo)
            bot_state.update_status(aid, "ONLINE")
        except Exception as e:
            log(f"[!] Dashboard register failed: {e}")

        return {'account_id': ml.account_id, 'nickname': nn, 'region': rg, 'level': lv, 'exp': ex,
                'open_id': oi, 'access_token': at, 'platform': str(pl), 'token': ml.token,
                'server_time': ml.server_time, 'aes_ak': kv, 'iv_i': ivv,
                'functional_addrs': fa, 'informational_addrs': ia,
                'release_version': rv, 'client_version': cv, 'server_url': ml.url,
                'login_payload_data': lp, 'auth_type': 'guest',
                'auth_uid': uid, 'auth_password': password,
                'mode_override': mo, 'target_level': int(forced_target_level or 0)}
    except Exception as e:
        log(f"[-] process_account_uid_pass error: {e}"); return None


async def run_account_worker(account_data, label):
    aid = str(account_data['account_id'])
    it = et = ft = None
    try:
        rg = account_data.get('region', 'BD')
        fa = account_data.get('functional_addrs')
        ia = account_data.get('informational_addrs')
        if not fa or not isinstance(fa, str) or ":" not in fa:
            print_warning(f"[!] No functional_addrs for {aid}")
            while True:
                await asyncio.sleep(60)
            return
        tpo = await build_tcp_startup_packet(account_data['account_id'], account_data['token'],
                                              account_data['server_time'], account_data['aes_ak'],
                                              account_data['iv_i'], region=rg, typ='OnLine')
        tpc = await build_tcp_startup_packet(account_data['account_id'], account_data['token'],
                                              account_data['server_time'], account_data['aes_ak'],
                                              account_data['iv_i'], region=rg, typ='ChaT')

        if ia and isinstance(ia, str) and ":" in ia:
            it = asyncio.create_task(informational(ia, tpc, account_data['aes_ak'],
                                                     account_data['iv_i'], region=rg, account_id=aid))

        async def _er():
            while True:
                await asyncio.sleep(60 + random.uniform(-10.0, 10.0))
                try:
                    await refresh_account_profile(account_data)
                except Exception:
                    pass

        et = asyncio.create_task(_er())
        ft = asyncio.create_task(functional_br(fa, tpo, account_data['region'],
                                                account_data['client_version'], account_data['aes_ak'],
                                                account_data['iv_i'], account_id=aid,
                                                account_data=account_data))
        await ft
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print_error(f"run_account_worker error for {label}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        for t in (it, et, ft):
            if t and not t.done():
                t.cancel()
        await asyncio.sleep(0.2)
        for t in (it, et, ft):
            if t and not t.done():
                pass
        try:
            cur = bot_state.accounts.get(aid, {}).get("status", "")
            if cur not in ("PAUSED", "STOPPED"):
                bot_state.update_status(aid, "STOPPED")
        except Exception:
            pass


async def account_loop_guest(uid, password, forced_region=None, mode_override="AUTO", target_level=0):
    uid_str = str(uid)
    while True:
        try:
            log(f"[*] Starting login for UID: {uid_str} (mode={mode_override or 'AUTO'})")
            ad = await process_account_uid_pass(uid_str, password, forced_region=forced_region,
                                                 forced_mode=mode_override, forced_target_level=target_level)
            if not ad:
                log(f"[-] Login failed for {uid_str}. Retry in 15s...")
                try:
                    bot_state.update_status(uid_str, "ERROR")
                except Exception:
                    pass
                await asyncio.sleep(15); continue
            await run_account_worker(ad, uid_str)
            try:
                if target_level > 0:
                    acc = bot_state.accounts.get(str(ad['account_id']))
                    if acc and int(acc.get("level", 1)) >= target_level:
                        bot_state.update_status(str(ad['account_id']), "COMPLETE")
                        bot_state.log(f"🎯 Target Lvl {target_level} reached for {uid_str}",
                                      "success", str(ad['account_id']))
                        return
            except Exception:
                pass
            log(f"[!] Session ended for {uid_str}. Reconnecting in 3s...")
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            log(f"[!] Worker stopped for {uid_str}")
            try:
                for key in [uid_str,
                            bot_state.auth_to_game_id.get(uid_str),
                            bot_state.game_to_auth_id.get(uid_str)]:
                    if key and key in bot_state.accounts:
                        cur = bot_state.accounts[key].get("status", "")
                        if cur not in ("PAUSED", "COMPLETE"):
                            bot_state.accounts[key]["status"] = "STOPPED"
                            bot_state.accounts[key]["active_matches"] = 0
            except Exception:
                pass
            break
        except Exception as e:
            log(f"[-] Error for UID {uid_str}: {e}. Retry in 10s...")
            await asyncio.sleep(10)