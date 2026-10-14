# -*- coding: utf-8 -*-
"""
MASTER OFFICIAL v0.3 - Web Dashboard Backend
+ Task Management + Settings + Queue System
+ BR (Lv<3) → Auto LW (Lv3+) + Region Auto + Target Auto-Delete
+ Source tracking (manual vs task)
"""
import asyncio
import json
import os
import time
import secrets as py_secrets
from typing import Dict, List, Any, Optional
from aiohttp import web


# ==================== AUTH CONFIG ====================
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "MASTERLV0.2")
SESSION_TTL = 86400  # 24 hours
SESSION_TOKENS: Dict[str, float] = {}

PUBLIC_PATHS = {"/", "/api/login", "/api/check", "/favicon.ico"}

# ==================== FILES ====================
ACCOUNTS_FILE = "accounts.json"
PENDING_FILE = "pending_accounts.json"
SETTINGS_FILE = "settings.json"

DEFAULT_SETTINGS = {
    "default_target_level": 10,
    "default_account_limit": 10,
}


def load_settings() -> Dict[str, Any]:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {**DEFAULT_SETTINGS, **data}
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(s: Dict[str, Any]):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


def load_pending() -> List[Dict]:
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, list):
                    return d
        except Exception:
            pass
    return []


def save_pending(lst: List[Dict]):
    try:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(lst, f, indent=2)
    except Exception:
        pass


def load_accounts_file() -> List[Dict]:
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, list):
                    return d
        except Exception:
            pass
    return []


def save_accounts_file(lst: List[Dict]):
    try:
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(lst, f, indent=2)
    except Exception:
        pass


def create_session() -> str:
    token = py_secrets.token_urlsafe(32)
    SESSION_TOKENS[token] = time.time() + SESSION_TTL
    now = time.time()
    for t in list(SESSION_TOKENS.keys()):
        if SESSION_TOKENS[t] < now:
            del SESSION_TOKENS[t]
    return token


def verify_session(token: Optional[str]) -> bool:
    if not token:
        return False
    exp = SESSION_TOKENS.get(token)
    if not exp:
        return False
    if exp < time.time():
        SESSION_TOKENS.pop(token, None)
        return False
    return True


@web.middleware
async def auth_middleware(request: web.Request, handler):
    path = request.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await handler(request)
    token = request.headers.get("X-Session-Token") or request.cookies.get("mx_session")
    if not verify_session(token):
        return web.json_response({"status": "unauthorized", "error": "Session expired or invalid"}, status=401)
    return await handler(request)


# ==================== BOT STATE ====================
class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 300
        self.total_matches = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        self._uptime_task: Optional[asyncio.Task] = None

        # v0.3 additions
        self.settings = load_settings()
        self.pending_accounts = load_pending()
        self.completed_accounts = 0
        self.failed_accounts = 0
        self.total_uploaded = 0
        self.completed_uids = set()
        self.active_workers_meta: Dict[str, Dict] = {}

    # ---------- Logging ----------
    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        msg_lower = str(message).lower()
        category = "system"
        if level == "info":
            if any(k in msg_lower for k in ["match", "udp", "thunder", "sharma", "lone wolf", "battle", "searching", "loading", "br search", "lw search"]):
                category = "match"
            elif any(k in msg_lower for k in ["exp", "gained"]):
                category = "exp"
            elif any(k in msg_lower for k in ["login", "token", "auth", "cache", "session"]):
                category = "login"
            elif any(k in msg_lower for k in ["gateway", "tcp", "dns", "network", "connect"]):
                category = "network"
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "category": category,
            "message": str(message),
            "uid": uid,
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    # ---------- Account Registration ----------
    def register_account(self, uid, nickname, region, level, exp, likes=0, source="manual"):
        uid_str = str(uid)
        lvl_val = max(1, int(level or 1))
        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str,
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "IND",
                "source": source or "manual",
                "level": lvl_val,
                "initial_exp": exp,
                "current_exp": exp,
                "gained_exp": 0,
                "likes": likes or 0,
                "mode": "BR" if lvl_val < 3 else "LONE_WOLF",
                "mode_manual": False,
                "status": "ONLINE",
                "matches_played": 0,
                "matches_completed": 0,
                "matches_failed": 0,
                "active_matches": 0,
                "uptime_seconds": 0,
                "is_paused": False,
                "last_match_time": None,
                "last_updated": time.strftime("%H:%M:%S"),
                "target_level": self.get_target_level(uid_str),
                "completed": False,
            }
        else:
            acc = self.accounts[uid_str]
            if nickname:
                acc["nickname"] = nickname
            if region:
                acc["region"] = region
            if source:
                acc["source"] = source
            if level:
                acc["level"] = lvl_val
                acc["mode"] = "BR" if lvl_val < 3 else "LONE_WOLF"
            acc["current_exp"] = exp
            acc["gained_exp"] = max(0, exp - acc["initial_exp"])
            acc["likes"] = likes
            acc["status"] = "ONLINE" if not acc.get("is_paused") else "PAUSED"
            acc["last_updated"] = time.strftime("%H:%M:%S")
        self.recalc_totals()

    def update_exp(self, uid, current_exp, level=None):
        uid_str = str(uid)
        if uid_str not in self.accounts:
            return
        acc = self.accounts[uid_str]
        old_exp = acc.get("current_exp", 0)
        old_level = acc.get("level", 1)
        acc["current_exp"] = current_exp
        if level is not None and level > 0:
            acc["level"] = int(level)
            if old_level < 3 <= int(level):
                acc["mode"] = "LONE_WOLF"
                self.log(f"⚡ {uid_str} reached Lv3 → switching to LONE WOLF", "success", uid_str)
        acc["gained_exp"] = max(0, current_exp - acc.get("initial_exp", 0))
        acc["last_updated"] = time.strftime("%H:%M:%S")
        diff = current_exp - old_exp
        if diff > 0:
            self.log(f"+{diff} EXP | {acc.get('nickname', uid_str)} now Lv{acc.get('level', 1)}", "success", uid_str)
        self.recalc_totals()

    def get_account_level(self, uid):
        acc = self.accounts.get(str(uid))
        return int(acc.get("level", 1) or 1) if acc else 1

    def get_target_level(self, uid):
        uid_str = str(uid)
        cred = self.account_credentials.get(uid_str)
        if cred and cred.get("target_level"):
            try:
                return int(cred["target_level"])
            except Exception:
                pass
        return int(self.settings.get("default_target_level", 10))

    def get_account_mode(self, uid):
        acc = self.accounts.get(str(uid))
        if not acc:
            return "LONE_WOLF"
        return str(acc.get("mode", "LONE_WOLF") or "LONE_WOLF").upper()

    def set_account_mode(self, uid, mode):
        uid_str = str(uid)
        mode = str(mode or "").upper()
        if mode not in ("LONE_WOLF", "LW", "BR"):
            return False, "Invalid mode"
        acc = self.accounts.get(uid_str)
        if not acc:
            return False, "Account not found"
        acc["mode"] = "LONE_WOLF" if mode in ("LW", "LONE_WOLF") else "BR"
        acc["mode_manual"] = True
        return True, acc["mode"]

    def update_status(self, uid, status, active_matches=None):
        uid_str = str(uid)
        if uid_str not in self.accounts:
            return
        if self.accounts[uid_str].get("is_paused") and status not in ("PAUSED", "OFFLINE"):
            status = "PAUSED"
        self.accounts[uid_str]["status"] = status
        if active_matches is not None:
            self.accounts[uid_str]["active_matches"] = active_matches
        self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match(self, uid):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            acc["matches_played"] = acc.get("matches_played", 0) + 1
            acc["matches_completed"] = acc.get("matches_completed", 0) + 1
            acc["last_match_time"] = time.strftime("%H:%M:%S")

    def match_failed(self, uid):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["matches_failed"] = self.accounts[uid_str].get("matches_failed", 0) + 1

    def recalc_totals(self):
        self.total_gained_exp = sum(acc.get("gained_exp", 0) for acc in self.accounts.values())

    def is_paused(self, uid):
        acc = self.accounts.get(str(uid))
        if not acc:
            return False
        return bool(acc.get("is_paused", False) or acc.get("status") == "PAUSED")

    def set_paused(self, uid, paused):
        uid_str = str(uid)
        if uid_str not in self.accounts:
            return False
        self.accounts[uid_str]["is_paused"] = bool(paused)
        self.accounts[uid_str]["status"] = "PAUSED" if paused else "ONLINE"
        self.log(f"Node {'paused' if paused else 'resumed'}", "warning" if paused else "success", uid_str)
        return True

    def all_paused(self):
        if not self.accounts:
            return False
        return all(a.get("is_paused", False) or a.get("status") == "PAUSED" for a in self.accounts.values())

    def pause_all(self, paused=True):
        count = 0
        for uid in list(self.accounts.keys()):
            if self.set_paused(uid, paused):
                count += 1
        return count

    # ---------- v0.3: Task/Completion ----------
    def mark_account_completed(self, uid):
        uid_str = str(uid)
        if uid_str in self.completed_uids:
            return
        self.completed_uids.add(uid_str)
        self.completed_accounts += 1
        if uid_str in self.accounts:
            self.accounts[uid_str]["completed"] = True
            self.accounts[uid_str]["status"] = "COMPLETED"
        self.log(f"🎯 {uid_str} reached target level — account completed & removed", "success", uid_str)
        try:
            accs = load_accounts_file()
            cred = self.account_credentials.get(uid_str, {})
            auth_uid = str(cred.get("auth_uid", ""))
            auth_tok = cred.get("auth_token", "")
            new_accs = []
            for a in accs:
                if auth_uid and str(a.get("uid", "")) == auth_uid:
                    continue
                if auth_tok and a.get("token", "") == auth_tok:
                    continue
                new_accs.append(a)
            save_accounts_file(new_accs)
        except Exception:
            pass

    def mark_account_failed(self, uid):
        uid_str = str(uid)
        if uid_str in self.completed_uids:
            return
        self.completed_uids.add(uid_str)
        self.failed_accounts += 1
        self.log(f"❌ {uid_str} failed — removed from queue", "error", uid_str)

    async def uptime_ticker(self):
        while True:
            try:
                await asyncio.sleep(1)
                for uid, acc in list(self.accounts.items()):
                    if acc.get("status") in ("ONLINE", "IN_MATCH", "SEARCHING", "CONNECTING", "WAITING_LV3") and not acc.get("is_paused"):
                        acc["uptime_seconds"] = acc.get("uptime_seconds", 0) + 1
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def start_uptime_tracker(self):
        try:
            loop = asyncio.get_event_loop()
            if self._uptime_task is None or self._uptime_task.done():
                self._uptime_task = loop.create_task(self.uptime_ticker())
        except Exception:
            pass


bot_state = BotState()


# ==================== HTTP HANDLERS ====================
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")


async def handle_index(request):
    if os.path.exists(TEMPLATE_PATH):
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = "<h1>templates/index.html not found!</h1>"
    return web.Response(text=content, content_type="text/html", charset="utf-8")


async def handle_login(request):
    try:
        data = await request.json()
        pwd = str(data.get("password", ""))
        if pwd == DASHBOARD_PASSWORD:
            token = create_session()
            resp = web.json_response({"status": "ok", "token": token, "ttl": SESSION_TTL})
            resp.set_cookie("mx_session", token, max_age=SESSION_TTL, samesite="Lax")
            return resp
        await asyncio.sleep(0.6)
        return web.json_response({"status": "error", "error": "Invalid access key"}, status=401)
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_logout(request):
    token = request.headers.get("X-Session-Token") or request.cookies.get("mx_session")
    if token:
        SESSION_TOKENS.pop(token, None)
    resp = web.json_response({"status": "ok"})
    resp.del_cookie("mx_session")
    return resp


async def handle_check(request):
    token = request.headers.get("X-Session-Token") or request.cookies.get("mx_session")
    return web.json_response({"status": "ok" if verify_session(token) else "unauthorized"})


async def handle_get_stats(request):
    accounts_data = list(bot_state.accounts.values())
    accounts_data.sort(key=lambda x: x.get("gained_exp", 0), reverse=True)
    uptime = max(1, int(time.time() - bot_state.start_time))
    uptime_hours = max(0.016, uptime / 3600.0)
    exp_per_hour = int(bot_state.total_gained_exp / uptime_hours) if uptime_hours > 0 else 0
    total_active = sum(int(a.get("active_matches", 0) or 0) for a in accounts_data)
    running_workers = sum(1 for t in bot_state.account_workers.values() if not t.done())
    return web.json_response({
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_matches_started": bot_state.total_matches,
        "total_gained_exp": bot_state.total_gained_exp,
        "total_active_matches": total_active,
        "exp_per_hour": exp_per_hour,
        "accounts": accounts_data,
        "logs": bot_state.logs[-100:],
        "uptime": uptime,
        "queue": {
            "active": running_workers,
            "pending": len(bot_state.pending_accounts),
            "completed": bot_state.completed_accounts,
            "failed": bot_state.failed_accounts,
            "total_uploaded": bot_state.total_uploaded or len(load_accounts_file()),
            "limit": bot_state.settings.get("default_account_limit", 10),
            "default_target": bot_state.settings.get("default_target_level", 10),
        },
    })


async def handle_add_account(request):
    try:
        data = await request.json()
        existing = load_accounts_file()
        target_level = data.get("target_level")
        try:
            target_level = int(target_level) if target_level else bot_state.settings.get("default_target_level", 10)
        except Exception:
            target_level = bot_state.settings.get("default_target_level", 10)

        new_entry = None
        if "uid" in data and "password" in data:
            uid = str(data["uid"]).strip()
            pwd = str(data["password"]).strip()
            if not uid or not pwd:
                return web.json_response({"status": "error", "error": "UID and Password required"}, status=400)
            existing = [acc for acc in existing if str(acc.get("uid")) != uid]
            new_entry = {"uid": uid, "password": pwd, "target_level": target_level, "source": "manual"}
        elif "token" in data:
            token = str(data["token"]).strip()
            if not token:
                return web.json_response({"status": "error", "error": "Token required"}, status=400)
            existing = [acc for acc in existing if acc.get("token") != token]
            new_entry = {"token": token, "target_level": target_level, "source": "manual"}
        else:
            return web.json_response({"status": "error", "error": "Invalid payload"}, status=400)

        existing.append(new_entry)
        save_accounts_file(existing)
        bot_state.total_uploaded = len(existing)
        bot_state.log(f"New account added (target Lv{target_level})", "success")

        if "on_account_added" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_account_added"](new_entry))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_delete_account(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        token = str(data.get("token", "")).strip()
        existing = load_accounts_file()
        if token:
            existing = [a for a in existing if str(a.get("token", "")).strip() != token]
        elif uid:
            existing = [a for a in existing if str(a.get("uid", "")).strip() != uid]
        save_accounts_file(existing)

        worker_keys = []
        if uid:
            worker_keys.append(uid)
        if token:
            worker_keys.append(token[:10])
        for key in worker_keys:
            worker = bot_state.account_workers.get(key)
            if worker:
                if not worker.done():
                    worker.cancel()
                bot_state.account_workers.pop(key, None)
        if uid and uid in bot_state.accounts:
            del bot_state.accounts[uid]
        bot_state.log(f"Account {uid or token[:10] or 'unknown'} removed.", "warning", uid or None)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_refresh_account(request):
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        if "on_refresh_account" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_refresh_account"](uid))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_mode_switch(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        mode = str(data.get("mode", "")).strip()
        ok, result = bot_state.set_account_mode(uid, mode)
        if not ok:
            return web.json_response({"status": "error", "error": result}, status=400)
        return web.json_response({"status": "ok", "uid": uid, "mode": result})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_pause(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "uid required"}, status=400)
        if uid not in bot_state.accounts:
            return web.json_response({"status": "error", "error": "account not found"}, status=404)
        new_state = not bot_state.is_paused(uid)
        bot_state.set_paused(uid, new_state)
        return web.json_response({"status": "ok", "is_paused": new_state, "uid": uid})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_pause_all(request):
    try:
        new_state = not bot_state.all_paused()
        count = bot_state.pause_all(new_state)
        return web.json_response({"status": "ok", "all_paused": new_state, "count": count})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_restart_account(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "uid required"}, status=400)
        worker = bot_state.account_workers.get(uid)
        if worker and not worker.done():
            worker.cancel()
        bot_state.log(f"Restarting worker for {uid}", "warning", uid)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_clear_logs(request):
    bot_state.logs.clear()
    return web.json_response({"status": "ok"})


# ==================== v0.3: SETTINGS ====================
async def handle_get_settings(request):
    return web.json_response({
        "status": "ok",
        "settings": bot_state.settings,
        "pending_count": len(bot_state.pending_accounts),
        "completed_count": bot_state.completed_accounts,
        "failed_count": bot_state.failed_accounts,
        "total_uploaded": bot_state.total_uploaded or len(load_accounts_file()),
        "active_count": sum(1 for t in bot_state.account_workers.values() if not t.done()),
    })


async def handle_update_settings(request):
    try:
        data = await request.json()
        s = dict(bot_state.settings)
        if "default_target_level" in data:
            s["default_target_level"] = max(1, min(100, int(data["default_target_level"])))
        if "default_account_limit" in data:
            s["default_account_limit"] = max(1, min(500, int(data["default_account_limit"])))
        bot_state.settings = s
        save_settings(s)
        bot_state.log(f"⚙ Settings updated: target=Lv{s['default_target_level']}, limit={s['default_account_limit']}", "success")
        return web.json_response({"status": "ok", "settings": s})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


# ==================== v0.3: TASK MANAGEMENT ====================
async def handle_task_upload(request):
    try:
        data = await request.json()
        accounts = data.get("accounts", [])
        try:
            target_level = int(data.get("target_level") or bot_state.settings.get("default_target_level", 10))
        except Exception:
            target_level = bot_state.settings.get("default_target_level", 10)
        try:
            max_used = int(data.get("max_used_id") or bot_state.settings.get("default_account_limit", 10))
        except Exception:
            max_used = bot_state.settings.get("default_account_limit", 10)

        if not isinstance(accounts, list) or not accounts:
            return web.json_response({"status": "error", "error": "No accounts in payload"}, status=400)

        if data.get("max_used_id"):
            bot_state.settings["default_account_limit"] = max_used
            bot_state.settings["default_target_level"] = target_level
            save_settings(bot_state.settings)

        existing = load_accounts_file()
        existing_keys = set()
        for a in existing:
            if a.get("uid"):
                existing_keys.add("u:" + str(a["uid"]))
            if a.get("token"):
                existing_keys.add("t:" + str(a["token"]))

        added = 0
        for a in accounts:
            if not isinstance(a, dict):
                continue
            entry = dict(a)
            entry["target_level"] = target_level
            entry["source"] = "task"
            key = None
            if entry.get("uid"):
                key = "u:" + str(entry["uid"])
            elif entry.get("token"):
                key = "t:" + str(entry["token"])
            if not key:
                continue
            if key in existing_keys:
                continue
            existing.append(entry)
            existing_keys.add(key)
            added += 1

        save_accounts_file(existing)
        bot_state.total_uploaded = len(existing)
        bot_state.log(f"📁 Task uploaded: +{added} accounts (target Lv{target_level}, limit {max_used})", "success")

        if "on_task_uploaded" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_task_uploaded"]())

        return web.json_response({
            "status": "ok",
            "added": added,
            "total": len(existing),
            "target_level": target_level,
            "max_used": max_used,
        })
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_task_stats(request):
    running = sum(1 for t in bot_state.account_workers.values() if not t.done())
    total_up = bot_state.total_uploaded or len(load_accounts_file())
    remain = max(0, total_up - bot_state.completed_accounts - bot_state.failed_accounts - running)
    return web.json_response({
        "status": "ok",
        "total_uploaded": total_up,
        "active": running,
        "pending": len(bot_state.pending_accounts),
        "completed": bot_state.completed_accounts,
        "failed": bot_state.failed_accounts,
        "remaining": remain,
        "total_exp": bot_state.total_gained_exp,
        "total_matches": bot_state.total_matches,
        "settings": bot_state.settings,
    })


async def handle_task_clear_completed(request):
    bot_state.completed_accounts = 0
    bot_state.failed_accounts = 0
    bot_state.completed_uids.clear()
    return web.json_response({"status": "ok"})


# ==================== DASHBOARD START ====================
async def start_web_dashboard(host: str = "0.0.0.0", port: int = 20333):
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/", handle_index)
    app.router.add_post("/api/login", handle_login)
    app.router.add_post("/api/logout", handle_logout)
    app.router.add_get("/api/check", handle_check)
    app.router.add_get("/api/stats", handle_get_stats)

    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/refresh", handle_refresh_account)
    app.router.add_post("/api/account/restart", handle_restart_account)
    app.router.add_post("/api/account/mode", handle_mode_switch)
    app.router.add_post("/api/account/pause", handle_pause)
    app.router.add_post("/api/account/pause_all", handle_pause_all)
    app.router.add_post("/api/logs/clear", handle_clear_logs)

    # v0.3
    app.router.add_get("/api/settings", handle_get_settings)
    app.router.add_post("/api/settings/update", handle_update_settings)
    app.router.add_post("/api/task/upload", handle_task_upload)
    app.router.add_get("/api/task/stats", handle_task_stats)
    app.router.add_post("/api/task/clear_completed", handle_task_clear_completed)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    bot_state.start_uptime_tracker()

    print(f"\033[92m[+] Web Dashboard running on http://localhost:{port}\033[0m")
    print(f"\033[93m[+] Dashboard Password: {DASHBOARD_PASSWORD}\033[0m")