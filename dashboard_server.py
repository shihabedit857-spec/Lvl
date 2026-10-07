# -*- coding: utf-8 -*-
import asyncio, json, os, time
from typing import Dict, List, Any, Optional
from aiohttp import web

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")

EXP_TABLE: Dict[int, int] = {
    1: 0, 2: 48, 3: 202, 4: 544, 5: 1012, 6: 1844, 7: 2792, 8: 3800,
    9: 4870, 10: 6004, 11: 7192, 12: 8448, 13: 9760, 14: 11140, 15: 12566,
    16: 14060, 17: 15610, 18: 17224, 19: 18902, 20: 20632, 21: 22424, 22: 24278,
    23: 26192, 24: 28166, 25: 30200, 26: 32294, 27: 34448, 28: 37804, 29: 41274,
    30: 44870, 31: 48582, 32: 53394, 33: 58566, 34: 64096, 35: 69994, 36: 76460,
    37: 83506, 38: 91128, 39: 99322, 40: 108092, 41: 120144, 42: 133266, 43: 147472,
    44: 162760, 45: 179126, 46: 196572, 47: 215368, 48: 235516, 49: 257010, 50: 279860,
    51: 304056, 52: 348318, 53: 394982, 54: 444044, 55: 495508, 56: 549364, 57: 633756,
    58: 721744, 59: 813336, 60: 908522, 61: 1041438, 62: 1180352, 63: 1325266,
    64: 1476184, 65: 1634300, 66: 1840946, 67: 2056594, 68: 2281242, 69: 2514880,
    70: 2757530, 71: 3059506, 72: 3372284, 73: 3699456, 74: 4041030, 75: 4397002,
    76: 4829104, 77: 5282204, 78: 5756304, 79: 6251408, 80: 6776502, 81: 7381324,
    82: 8043154, 83: 8752982, 84: 9510808, 85: 10316338, 86: 11277190, 87: 12291748,
    88: 13360304, 89: 14482858, 90: 15659418, 91: 17026708, 92: 18453950, 93: 19941280,
    94: 21488570, 95: 23095858, 96: 24763138, 97: 26490428, 98: 28378704, 99: 30124996,
    100: 32032884
}


def calculate_level_progress(level: int, current_exp: int) -> Dict[str, Any]:
    level = max(1, level)
    next_level = min(100, level + 1)
    base_exp = EXP_TABLE.get(level, 0)
    target_exp = EXP_TABLE.get(next_level, base_exp + 50000)
    needed = max(1, target_exp - base_exp)
    earned = max(0, current_exp - base_exp)
    remaining = max(0, target_exp - current_exp)
    pct = min(100.0, max(0.0, (earned / needed) * 100.0))
    return {"next_level": next_level, "base_exp": base_exp, "target_exp": target_exp,
            "needed_for_level": needed, "earned_in_level": earned,
            "remaining_exp": remaining, "progress_pct": round(pct, 1)}


class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 200
        self.total_matches = 0
        self.total_matches_started = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.account_token_map: Dict[str, str] = {}
        self.auth_to_game_id: Dict[str, str] = {}
        self.game_to_auth_id: Dict[str, str] = {}
        self.paused_accounts: set = set()
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        self.active_writers: Dict[str, set] = {}
        self.account_states: Dict[str, str] = {}

    def register_writer(self, uid, writer):
        uid_str = str(uid)
        if uid_str not in self.active_writers:
            self.active_writers[uid_str] = set()
        self.active_writers[uid_str].add(writer)

    def unregister_writer(self, uid, writer):
        uid_str = str(uid)
        if uid_str in self.active_writers:
            self.active_writers[uid_str].discard(writer)
            if not self.active_writers[uid_str]:
                self.active_writers.pop(uid_str, None)

    def close_writers_for_account(self, uid):
        uid_str = str(uid)
        cand = {uid_str}
        if uid_str in self.auth_to_game_id:
            cand.add(str(self.auth_to_game_id[uid_str]))
        if uid_str in self.game_to_auth_id:
            cand.add(str(self.game_to_auth_id[uid_str]))
        if uid_str in self.account_token_map:
            m = self.account_token_map[uid_str]
            cand.add(str(m))
            cand.add(str(m)[:16])
        for c in list(cand):
            for w in list(self.active_writers.get(c, [])):
                try:
                    if hasattr(w, "close"):
                        if hasattr(w, "is_closing"):
                            if not w.is_closing():
                                w.close()
                        else:
                            w.close()
                except Exception:
                    pass
            self.active_writers.pop(c, None)

    def log(self, message, level="info", uid=None):
        self.logs.append({"time": time.strftime("%H:%M:%S"), "level": level,
                          "message": message, "uid": str(uid) if uid else None})
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def is_target_reached(self, key):
        key = str(key)
        acc = self.accounts.get(key)
        if not acc:
            m = self.auth_to_game_id.get(key) or self.game_to_auth_id.get(key)
            if m and m in self.accounts:
                acc = self.accounts[m]
        if not acc:
            return False
        tgt = int(acc.get("target_level", 0) or 0)
        if tgt <= 0:
            return False
        return int(acc.get("level", 1) or 1) >= tgt

    def register_account(self, uid, nickname, region, level, exp, likes=0,
                         token=None, auth_uid=None, target_level=0,
                         game_uid=None, mode_override=""):
        uid_str = str(uid)
        auth_str = str(auth_uid) if auth_uid else self.game_to_auth_id.get(uid_str, "")

        # ═══ Auto-delete "Logging in..." placeholder ═══
        if auth_str and auth_str != uid_str:
            ph = self.accounts.get(auth_str)
            if ph and (ph.get("nickname") == "Logging in..." or not ph.get("game_uid")):
                self.accounts.pop(auth_str, None)
                # migrate worker task handle
                if auth_str in self.account_workers:
                    self.account_workers[uid_str] = self.account_workers.pop(auth_str, None)
                self.log(f"Placeholder {auth_str} merged → {uid_str}", "info", uid_str)

        if auth_str:
            self.auth_to_game_id[auth_str] = uid_str
            self.game_to_auth_id[uid_str] = auth_str
            self.account_token_map[auth_str] = uid_str
            self.account_token_map[uid_str] = auth_str
        if token:
            self.account_token_map[uid_str] = token
            self.account_token_map[token[:16]] = uid_str
            if auth_str:
                self.account_token_map[auth_str] = token

        prog = calculate_level_progress(level or 1, exp)
        lvl = level or 1
        acc_mode = "BR" if lvl < 3 else "LONE_WOLF"
        acc_label = "Battle Royale (Lvl < 3)" if lvl < 3 else "Lone Wolf (Lvl 3+)"

        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str, "auth_uid": auth_str or "",
                "game_uid": str(game_uid) if game_uid else uid_str,
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD", "level": lvl,
                "next_level": prog["next_level"], "mode": acc_mode, "mode_label": acc_label,
                "mode_override": mode_override,
                "initial_exp": exp, "current_exp": exp, "gained_exp": 0,
                "remaining_exp": prog["remaining_exp"], "target_exp": prog["target_exp"],
                "needed_for_level": prog["needed_for_level"],
                "earned_in_level": prog["earned_in_level"], "progress_pct": prog["progress_pct"],
                "likes": likes or 0,
                "status": "PAUSED" if self.is_paused(uid_str) else "ONLINE",
                "matches_played": 0, "active_matches": 0, "last_match_time": None,
                "token": token or "", "start_time": time.time(),
                "is_paused": self.is_paused(uid_str),
                "paused_at": time.time() if self.is_paused(uid_str) else None,
                "total_pause_duration": 0.0, "last_updated": time.strftime("%H:%M:%S"),
                "target_level": int(target_level or 0),
                "completed_at": None, "is_target_reached": False,
            }
        else:
            a = self.accounts[uid_str]
            if auth_str: a["auth_uid"] = auth_str
            if game_uid: a["game_uid"] = str(game_uid)
            if nickname: a["nickname"] = nickname
            if region: a["region"] = region
            if level: a["level"] = level
            if token: a["token"] = token
            if target_level: a["target_level"] = int(target_level)
            a["mode_override"] = mode_override
            a["current_exp"] = exp
            a["gained_exp"] = max(0, exp - a["initial_exp"])
            a["next_level"] = prog["next_level"]
            a["remaining_exp"] = prog["remaining_exp"]
            a["target_exp"] = prog["target_exp"]
            a["needed_for_level"] = prog["needed_for_level"]
            a["earned_in_level"] = prog["earned_in_level"]
            a["progress_pct"] = prog["progress_pct"]
            a["likes"] = likes
            if not a.get("is_paused"):
                a["status"] = "ONLINE"
            a["last_updated"] = time.strftime("%H:%M:%S")

        obj = self.accounts[uid_str]
        tgt = int(obj.get("target_level", 0) or 0)
        if tgt > 0 and obj.get("level", 1) >= tgt:
            obj["is_target_reached"] = True
            if not obj.get("completed_at"):
                obj["completed_at"] = time.strftime("%H:%M:%S")
        else:
            obj["is_target_reached"] = False
        self.recalc_totals()

    def get_account_uptime(self, uid_str):
        acc = self.accounts.get(uid_str)
        if not acc:
            m = self.game_to_auth_id.get(uid_str) or self.auth_to_game_id.get(uid_str)
            if m and m in self.accounts:
                acc = self.accounts[m]
        if not acc:
            return 0
        st = acc.get("start_time", time.time())
        tp = acc.get("total_pause_duration", 0.0)
        if acc.get("is_paused") and acc.get("paused_at"):
            return max(0, int(acc["paused_at"] - st - tp))
        return max(0, int(time.time() - st - tp))

    def is_paused(self, uid):
        u = str(uid)
        if u in self.paused_accounts: return True
        g = self.auth_to_game_id.get(u)
        if g and g in self.paused_accounts: return True
        a = self.game_to_auth_id.get(u)
        if a and a in self.paused_accounts: return True
        acc = self.accounts.get(u) or (self.accounts.get(g) if g else None)
        if acc and acc.get("is_paused"): return True
        return False

    def toggle_pause(self, uid):
        u = str(uid)
        cand = {u}
        if u in self.auth_to_game_id: cand.add(self.auth_to_game_id[u])
        if u in self.game_to_auth_id: cand.add(self.game_to_auth_id[u])
        tgt_acc = None; tgt_key = u
        for c in cand:
            if c in self.accounts:
                tgt_acc = self.accounts[c]; tgt_key = c; break
        now_paused = not self.is_paused(u)
        if now_paused:
            for c in cand:
                self.paused_accounts.add(c)
                self.close_writers_for_account(c)
            if tgt_acc:
                tgt_acc["is_paused"] = True
                tgt_acc["paused_at"] = time.time()
                tgt_acc["status"] = "PAUSED"
            self.log(f"⏸ {tgt_key} paused.", "warning", tgt_key)
        else:
            for c in cand:
                self.paused_accounts.discard(c)
            if tgt_acc:
                tgt_acc["is_paused"] = False
                if tgt_acc.get("paused_at"):
                    dur = time.time() - tgt_acc["paused_at"]
                    tgt_acc["total_pause_duration"] = tgt_acc.get("total_pause_duration", 0.0) + dur
                    tgt_acc["paused_at"] = None
                tgt_acc["status"] = "ONLINE"
            self.log(f"▶ {tgt_key} resumed.", "success", tgt_key)
        return now_paused

    def toggle_pause_all(self):
        any_active = any(not self.is_paused(k) for k in self.accounts.keys())
        for k in list(self.accounts.keys()):
            cp = self.is_paused(k)
            if any_active and not cp:
                self.toggle_pause(k)
            elif not any_active and cp:
                self.toggle_pause(k)
        return any_active

    def update_exp(self, uid, current_exp, level=None):
        u = str(uid)
        if u in self.accounts:
            a = self.accounts[u]
            old = a["current_exp"]; old_lvl = a.get("level", 1)
            a["current_exp"] = current_exp
            if level is not None and level > 0:
                a["level"] = level
            a["gained_exp"] = max(0, current_exp - a["initial_exp"])
            cl = a["level"]
            a["mode"] = "BR" if cl < 3 else "LONE_WOLF"
            a["mode_label"] = "Battle Royale (Lvl < 3)" if cl < 3 else "Lone Wolf (Lvl 3+)"
            pr = calculate_level_progress(cl, current_exp)
            a["next_level"] = pr["next_level"]
            a["remaining_exp"] = pr["remaining_exp"]
            a["target_exp"] = pr["target_exp"]
            a["needed_for_level"] = pr["needed_for_level"]
            a["earned_in_level"] = pr["earned_in_level"]
            a["progress_pct"] = pr["progress_pct"]
            a["last_updated"] = time.strftime("%H:%M:%S")
            tgt = int(a.get("target_level", 0) or 0)
            if tgt > 0 and cl >= tgt:
                a["is_target_reached"] = True
                if not a.get("completed_at"):
                    a["completed_at"] = time.strftime("%H:%M:%S")
            else:
                a["is_target_reached"] = False
            if old_lvl < 3 and cl >= 3:
                self.log(f"🎉 LEVEL UP! {u} → Lvl {cl}", "success", u)
            diff = current_exp - old
            if diff > 0:
                self.log(f"★ {u} gained +{diff:,} EXP | Lvl {cl} [{a['mode']}] ({pr['progress_pct']}%)", "success", u)
            self.recalc_totals()

    def get_account_level(self, uid):
        u = str(uid)
        acc = self.accounts.get(u)
        if not acc:
            m = self.game_to_auth_id.get(u) or self.auth_to_game_id.get(u)
            if m and m in self.accounts:
                acc = self.accounts[m]
        if acc:
            return int(acc.get("level", 1) or 1)
        return 1

    def update_status(self, uid, status, active_matches=None):
        u = str(uid)
        if u in self.accounts:
            self.accounts[u]["status"] = status
            if active_matches is not None:
                self.accounts[u]["active_matches"] = active_matches
            self.accounts[u]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match_started(self):
        self.total_matches_started += 1

    def increment_match(self, uid):
        u = str(uid)
        self.total_matches += 1
        if u in self.accounts:
            self.accounts[u]["matches_played"] += 1
            self.accounts[u]["last_match_time"] = time.strftime("%H:%M:%S")
            self.accounts[u]["last_updated"] = time.strftime("%H:%M:%S")

    def recalc_totals(self):
        self.total_gained_exp = sum(a.get("gained_exp", 0) for a in self.accounts.values())


bot_state = BotState()

FALLBACK_INDEX_HTML = """<!DOCTYPE html><html><head><title>Dashboard</title></head>
<body style="background:#0a0a12;color:#fff;font-family:sans-serif;text-align:center;padding:50px;">
<h1>BOT RUNNING</h1><p>templates/index.html missing.</p></body></html>"""


async def handle_index(request):
    content = FALLBACK_INDEX_HTML
    if os.path.exists(TEMPLATE_PATH):
        try:
            with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            pass
    return web.Response(text=content, content_type="text/html", charset="utf-8")


async def handle_get_stats(request):
    data = list(bot_state.accounts.values())
    data.sort(key=lambda x: x.get("gained_exp", 0), reverse=True)
    up = max(1, int(time.time() - bot_state.start_time))
    tot = bot_state.total_gained_exp
    eph = int((tot / up) * 3600)
    tam = sum(a.get("active_matches", 0) for a in data)
    for a in data:
        uk = str(a.get("uid", ""))
        a["uptime_seconds"] = bot_state.get_account_uptime(uk)
        a["is_paused"] = bot_state.is_paused(uk)
        a.setdefault("target_level", 0)
        a.setdefault("game_uid", "")
        a.setdefault("is_target_reached", False)
        a.setdefault("mode_override", "")
    return web.json_response({
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_matches_started": bot_state.total_matches_started,
        "total_active_matches": tam,
        "total_gained_exp": tot, "exp_per_hour": eph,
        "accounts": data, "logs": bot_state.logs[-80:], "uptime": up
    })


async def handle_add_account(request):
    try:
        data = await request.json()
        accounts_file = "accounts.json"
        existing = []
        if os.path.exists(accounts_file):
            try:
                with open(accounts_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = []
        tl = int(data.get("target_level", 0) or 0)
        mode = str(data.get("mode", "AUTO")).strip().upper()
        if mode not in ("AUTO", "BR", "LW"):
            mode = "AUTO"

        if "uid" in data and "password" in data:
            uid = str(data["uid"]).strip()
            pwd = str(data["password"]).strip()
            if not uid or not pwd:
                return web.json_response({"status": "error", "error": "UID & Password required"})
            if uid in bot_state.account_workers:
                try: bot_state.account_workers[uid].cancel()
                except Exception: pass
                bot_state.account_workers.pop(uid, None)
            existing = [a for a in existing if str(a.get("uid", "")) != uid]
            e = {"uid": uid, "password": pwd, "mode": mode}
            if tl > 0: e["target_level"] = tl
            existing.append(e)
        elif "token" in data:
            tok = str(data["token"]).strip()
            if not tok:
                return web.json_response({"status": "error", "error": "Token required"})
            tk = tok[:16]
            for k in list(bot_state.account_workers.keys()):
                if k == tk or k.startswith(tk[:10]) or tk.startswith(k[:10]):
                    try: bot_state.account_workers[k].cancel()
                    except Exception: pass
                    bot_state.account_workers.pop(k, None)
            existing = [a for a in existing if a.get("token", "") != tok]
            e = {"token": tok, "mode": mode}
            if tl > 0: e["target_level"] = tl
            existing.append(e)
        else:
            return web.json_response({"status": "error", "error": "Invalid payload"})

        with open(accounts_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        bot_state.log(f"Account added (mode={mode})", "success")
        if "on_account_added" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_account_added"](data))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_delete_account(request):
    try:
        data = await request.json()
        req_uid = str(data.get("uid", "")).strip()
        req_auth = str(data.get("auth_uid", "")).strip()
        if not req_uid and not req_auth:
            return web.json_response({"status": "error", "error": "UID required"})
        cand = set()
        if req_uid: cand.add(req_uid)
        if req_auth: cand.add(req_auth)
        for c in list(cand):
            if c in bot_state.game_to_auth_id:
                cand.add(str(bot_state.game_to_auth_id[c]))
            if c in bot_state.auth_to_game_id:
                cand.add(str(bot_state.auth_to_game_id[c]))
        toks = set()
        for c in list(cand):
            a = bot_state.accounts.get(c, {})
            if a:
                if a.get("auth_uid"): cand.add(str(a["auth_uid"]))
                if a.get("uid"): cand.add(str(a["uid"]))
                if a.get("game_uid"): cand.add(str(a["game_uid"]))
                t = a.get("token")
                if t: toks.add(str(t))

        tcf = "token_cache.json"
        if os.path.exists(tcf):
            try:
                with open(tcf, "r", encoding="utf-8") as f:
                    tc = json.load(f)
                dirty = False
                for k, v in list(tc.items()):
                    ks = str(k); va = str(v.get("account_id", "")); vu = str(v.get("auth_uid", ""))
                    if ks in cand or va in cand or vu in cand:
                        cand.add(ks)
                        if va: cand.add(va)
                        if vu: cand.add(vu)
                        del tc[k]; dirty = True
                if dirty:
                    with open(tcf, "w", encoding="utf-8") as f:
                        json.dump(tc, f, indent=2)
            except Exception:
                pass

        af = "accounts.json"
        if os.path.exists(af):
            try:
                with open(af, "r", encoding="utf-8") as f:
                    ex = json.load(f)
                ne = []
                for a in ex:
                    au = str(a.get("uid", "")).strip()
                    at = str(a.get("token", "")).strip()
                    hit = False
                    if au and au in cand: hit = True
                    if at and (at in cand or at in toks): hit = True
                    for t in toks:
                        if at and (at.startswith(t[:16]) or t.startswith(at[:16])): hit = True
                    if not hit: ne.append(a)
                with open(af, "w", encoding="utf-8") as f:
                    json.dump(ne, f, indent=2)
            except Exception:
                pass

        for c in cand:
            bot_state.accounts.pop(c, None)
            bot_state.account_credentials.pop(c, None)
            bot_state.account_states.pop(c, None)
            bot_state.auth_to_game_id.pop(c, None)
            bot_state.game_to_auth_id.pop(c, None)
            bot_state.account_token_map.pop(c, None)

        ck = []
        for k, w in list(bot_state.account_workers.items()):
            ks = str(k); sc = False
            if ks in cand: sc = True
            for t in toks:
                if ks == t[:16] or ks.startswith(t[:10]): sc = True
            if sc:
                try: w.cancel()
                except Exception: pass
                ck.append(k)
        for k in ck:
            bot_state.account_workers.pop(k, None)
        for c in cand:
            bot_state.close_writers_for_account(c)

        if "on_account_deleted" in bot_state.refresh_callbacks:
            try:
                asyncio.create_task(bot_state.refresh_callbacks["on_account_deleted"](list(cand)))
            except Exception:
                pass
        bot_state.log(f"Deleted: {req_uid or req_auth}", "warning")
        bot_state.recalc_totals()
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_refresh_account(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if "on_refresh_account" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_refresh_account"](uid))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_restart_account(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        auth_uid = str(data.get("auth_uid", "")).strip()
        target = uid or auth_uid
        if not target:
            return web.json_response({"status": "error", "error": "UID required"})
        for c in [uid, auth_uid]:
            if c and c in bot_state.accounts:
                bot_state.accounts[c]["status"] = "CONNECTING"
        if "on_restart_account" in bot_state.refresh_callbacks:
            asyncio.create_task(
                bot_state.refresh_callbacks["on_restart_account"](target, auth_uid)
            )
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_start_account(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"})
        af = "accounts.json"
        cred = None
        if os.path.exists(af):
            try:
                with open(af, "r", encoding="utf-8") as f:
                    for a in json.load(f):
                        if str(a.get("uid")) == uid:
                            cred = a; break
            except Exception:
                pass
        if not cred:
            return web.json_response({"status": "error", "error": "account not found"})
        if "on_account_added" in bot_state.refresh_callbacks:
            await bot_state.refresh_callbacks["on_account_added"](cred)
        if uid in bot_state.accounts:
            bot_state.accounts[uid]["status"] = "CONNECTING"
            bot_state.accounts[uid]["is_paused"] = False
            bot_state.accounts[uid]["start_time"] = time.time()
        bot_state.paused_accounts.discard(uid)
        bot_state.log(f"Starting {uid}", "success", uid)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_stop_account(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        auth_uid = str(data.get("auth_uid", "")).strip()
        target = uid or auth_uid
        if not target:
            return web.json_response({"status": "error", "error": "UID required"})

        if "on_account_stopped" in bot_state.refresh_callbacks:
            asyncio.create_task(
                bot_state.refresh_callbacks["on_account_stopped"](target)
            )
        else:
            cand = {target}
            if auth_uid: cand.add(auth_uid)
            if target in bot_state.game_to_auth_id:
                cand.add(str(bot_state.game_to_auth_id[target]))
            if target in bot_state.auth_to_game_id:
                cand.add(str(bot_state.auth_to_game_id[target]))
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

        for c in [uid, auth_uid]:
            if c and c in bot_state.accounts:
                bot_state.accounts[c]["status"] = "STOPPED"
                bot_state.accounts[c]["active_matches"] = 0
                bot_state.accounts[c]["is_paused"] = False

        bot_state.log(f"⏹ Stopped {target}", "warning", target)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_clear_logs(request):
    bot_state.logs.clear()
    return web.json_response({"status": "ok"})


async def handle_toggle_pause(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"})
        p = bot_state.toggle_pause(uid)
        return web.json_response({"status": "ok", "is_paused": p})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_toggle_pause_all(request):
    try:
        p = bot_state.toggle_pause_all()
        return web.json_response({"status": "ok", "all_paused": p})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def start_web_dashboard(host="0.0.0.0", port=3000):
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/stats", handle_get_stats)
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/refresh", handle_refresh_account)
    app.router.add_post("/api/account/restart", handle_restart_account)
    app.router.add_post("/api/account/start", handle_start_account)
    app.router.add_post("/api/account/stop", handle_stop_account)
    app.router.add_post("/api/account/pause", handle_toggle_pause)
    app.router.add_post("/api/account/pause_all", handle_toggle_pause_all)
    app.router.add_post("/api/logs/clear", handle_clear_logs)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner