# -*- coding: utf-8 -*-
import sys, os, asyncio, json, time, socket
os.environ.setdefault('PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION', 'python')
try:
    sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')
except Exception:
    pass

from dashboard_server import bot_state, start_web_dashboard
import bot_engine

ACCOUNTS_FILE = "accounts.json"


async def on_account_added(data: dict):
    try:
        uid = str(data.get("uid") or "").strip()
        pwd = str(data.get("password") or "").strip()
        token = str(data.get("token") or "").strip()
        region = str(data.get("region") or "").strip().upper() or None
        mode = str(data.get("mode") or "AUTO").strip().upper()
        if mode not in ("AUTO", "BR", "LW"):
            mode = "AUTO"
        target_level = int(data.get("target_level") or 0)

        if token and not uid:
            bot_state.log(f"Token-only account not supported yet", "warning")
            return
        if not uid or not pwd:
            bot_state.log("Cannot spawn: missing uid/password", "error")
            return

        old = bot_state.account_workers.get(uid)
        if old and not old.done():
            old.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(old), timeout=3.0)
            except Exception:
                pass
            bot_state.account_workers.pop(uid, None)

        task = asyncio.create_task(
            bot_engine.account_loop_guest(
                uid, pwd,
                forced_region=region,
                mode_override=mode,
                target_level=target_level,
            ),
            name=f"worker-{uid}",
        )
        bot_state.account_workers[uid] = task

        bot_state.accounts.setdefault(uid, {
            "uid": uid, "auth_uid": uid, "game_uid": "",
            "nickname": "Logging in...",
            "region": region or "?",
            "level": 1, "next_level": 2, "mode": "BR",
            "mode_label": "Battle Royale", "mode_override": mode,
            "initial_exp": 0, "current_exp": 0, "gained_exp": 0,
            "remaining_exp": 0, "target_exp": 0, "needed_for_level": 0,
            "earned_in_level": 0, "progress_pct": 0, "likes": 0,
            "status": "CONNECTING", "matches_played": 0, "active_matches": 0,
            "last_match_time": None, "token": "",
            "start_time": time.time(), "is_paused": False, "paused_at": None,
            "total_pause_duration": 0.0,
            "last_updated": time.strftime("%H:%M:%S"),
            "target_level": target_level,
            "completed_at": None, "is_target_reached": False,
        })
        bot_state.log(f"Worker spawned for {uid} (mode={mode}, target={target_level or '∞'})", "success", uid)
    except Exception as e:
        bot_state.log(f"on_account_added failed: {e}", "error")


async def on_refresh_account(uid: str):
    uid_str = str(uid)
    bot_state.log(f"Manual refresh: {uid_str}", "info", uid_str)
    acc = bot_state.accounts.get(uid_str)
    if acc and hasattr(bot_engine, "refresh_account_profile"):
        try:
            await bot_engine.refresh_account_profile(acc)
        except Exception as e:
            bot_state.log(f"Refresh error: {e}", "error", uid_str)


async def on_restart_account(uid: str, auth_uid: str = ""):
    uid_str = str(uid)
    auth_str = str(auth_uid) if auth_uid else ""
    bot_state.log(f"🔄 Restart: {uid_str}", "warning", uid_str)

    cand = {uid_str}
    if auth_str:
        cand.add(auth_str)
    if uid_str in bot_state.game_to_auth_id:
        cand.add(str(bot_state.game_to_auth_id[uid_str]))
    if uid_str in bot_state.auth_to_game_id:
        cand.add(str(bot_state.auth_to_game_id[uid_str]))

    for c in list(cand):
        t = bot_state.account_workers.get(c)
        if t and not t.done():
            t.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        bot_state.account_workers.pop(c, None)
        bot_state.close_writers_for_account(c)

    await asyncio.sleep(0.5)

    cred = None
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                for a in json.load(f):
                    a_uid = str(a.get("uid", ""))
                    if a_uid and a_uid in cand:
                        cred = a
                        break
        except Exception:
            pass

    if not cred:
        bot_state.log(f"No credential found for {uid_str}", "error", uid_str)
        for c in cand:
            if c in bot_state.accounts:
                bot_state.accounts[c]["status"] = "STOPPED"
        return

    for c in cand:
        if c in bot_state.accounts:
            bot_state.accounts[c]["status"] = "CONNECTING"
            bot_state.accounts[c]["is_paused"] = False

    await on_account_added(cred)


async def on_account_stopped(uid: str):
    uid_str = str(uid)

    cand = {uid_str}
    if uid_str in bot_state.game_to_auth_id:
        cand.add(str(bot_state.game_to_auth_id[uid_str]))
    if uid_str in bot_state.auth_to_game_id:
        cand.add(str(bot_state.auth_to_game_id[uid_str]))

    for c in list(cand):
        t = bot_state.account_workers.get(c)
        if t and not t.done():
            t.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        bot_state.account_workers.pop(c, None)
        bot_state.close_writers_for_account(c)

    for c in cand:
        if c in bot_state.accounts:
            bot_state.accounts[c]["status"] = "STOPPED"
            bot_state.accounts[c]["active_matches"] = 0
            bot_state.accounts[c]["is_paused"] = False

    bot_state.log(f"⏹ Stopped worker {uid_str}", "warning", uid_str)


async def on_account_deleted(uids: list):
    for uid in uids:
        uid_str = str(uid)
        task = bot_state.account_workers.get(uid_str)
        if task and not task.done():
            task.cancel()
        bot_state.account_workers.pop(uid_str, None)
        bot_state.close_writers_for_account(uid_str)


async def main():
    print("=" * 62)
    print("   TEAM 84FF — Web-Managed FreeFire Bot")
    print("=" * 62)

    bot_state.refresh_callbacks["on_account_added"] = on_account_added
    bot_state.refresh_callbacks["on_refresh_account"] = on_refresh_account
    bot_state.refresh_callbacks["on_restart_account"] = on_restart_account
    bot_state.refresh_callbacks["on_account_deleted"] = on_account_deleted
    bot_state.refresh_callbacks["on_account_stopped"] = on_account_stopped

    # ═══ Railway / Local PORT auto-detect ═══
    def _get_local_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    PORT = int(os.environ.get("PORT", 3000))
    local_ip = _get_local_ip()

    await start_web_dashboard("0.0.0.0", PORT)

    railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_STATIC_URL")
    if railway_domain:
        url = railway_domain if railway_domain.startswith("http") else f"https://{railway_domain}"
        print("\033[92m[+] Dashboard → {}\033[0m".format(url))
    else:
        print("\033[92m[+] Dashboard → http://{}:{}\033[0m".format(local_ip, PORT))
    print("\033[92m[+] Local     → http://127.0.0.1:{}\033[0m".format(PORT))
    print("\033[92m[+] Access key: 15985683337\033[0m")

    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            bot_state.log(f"Auto-loading {len(saved)} account(s)", "info")
            for acc in saved:
                asyncio.create_task(on_account_added(acc))
                await asyncio.sleep(0.3)
        except Exception as e:
            bot_state.log(f"accounts.json load error: {e}", "error")

    print("\n[i] Press Ctrl+C to stop.\n")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
