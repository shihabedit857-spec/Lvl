# -*- coding: utf-8 -*-
"""
TEAM 84FF — Level Up Bot + Marketplace (Merged Single File, Port 5000)
"""
import sys, os, asyncio, httpx, random, json, socket, struct, time, uuid, itertools
import threading, re, string
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from functools import wraps

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'): sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass

from flask import Flask, request, redirect, url_for, jsonify, render_template_string
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
import jwt
from google_play_scraper import app as play_scraper
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from protobuf_decoder.protobuf_decoder import Parser
from message_ids import MESSAGE_ID_TO_NAME
import thunderFF_pb2

# ==================== CONFIG ====================
WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.environ.get("PORT", 5000))
ACCOUNTS_FILE = "accounts.json"
TOKEN_CACHE_FILE = "token_cache.json"
DEVICES_FILE = "devices.json"
TOKEN_CACHE_TTL = 1200
START_MATCH_INTERVAL = 3.0
NEW_MATCH_DELAY = 3.0
MAX_MATCH_DURATION = 700
MATCH_IDLE_TIMEOUT = 8.0
MAX_CONSECUTIVE_PARSE_FAILURES = 5.0
NON_MATCH_RECONNECT_DELAY = 1.0
EXP_REFRESH_INTERVAL = 20.0
_bot_loop: Optional[asyncio.AbstractEventLoop] = None

# ==================== BOT STATE ====================
class BotState:
    def __init__(self):
        self.accounts = {}
        self.logs = []
        self.max_logs = 200
        self.total_matches = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers = {}
        self.refresh_callbacks = {}
        self.account_credentials = {}
        self.account_states = {}

    def log(self, message, level="info", uid=None):
        entry = {"time": time.strftime("%H:%M:%S"), "level": level, "message": message, "uid": uid}
        self.logs.append(entry)
        if len(self.logs) > self.max_logs: self.logs.pop(0)

    def register_account(self, uid, nickname, region, level, exp, likes=0, target_level=0, game_uid=None, user_id=None, group_id=None):
        uid_str = str(uid)
        game_uid_str = str(game_uid) if game_uid else None
        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str, "game_uid": game_uid_str or uid_str,
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD", "level": level or 1,
                "initial_exp": exp, "current_exp": exp, "gained_exp": 0,
                "likes": likes or 0, "status": "OFFLINE",
                "target_level": int(target_level or 0),
                "matches_played": 0, "active_matches": 0,
                "last_match_time": None, "last_updated": time.strftime("%H:%M:%S"),
                "completed_at": None, "user_id": user_id, "group_id": group_id
            }
        else:
            acc = self.accounts[uid_str]
            if game_uid_str: acc["game_uid"] = game_uid_str
            if nickname: acc["nickname"] = nickname
            if region and region != "—": acc["region"] = region
            if level and level > 0: acc["level"] = level
            if target_level and int(target_level) > 0: acc["target_level"] = int(target_level)
            if exp > 0 or acc.get("initial_exp", 0) == 0:
                acc["current_exp"] = exp
                if acc.get("initial_exp", 0) == 0: acc["initial_exp"] = exp
            acc["gained_exp"] = max(0, acc["current_exp"] - acc["initial_exp"])
            acc["likes"] = likes or acc.get("likes", 0)
            acc["last_updated"] = time.strftime("%H:%M:%S")
            if user_id is not None: acc["user_id"] = user_id
            if group_id is not None: acc["group_id"] = group_id
        self.recalc_totals()

    def update_exp(self, uid, current_exp, level=None):
        u = str(uid)
        if u in self.accounts:
            acc = self.accounts[u]
            old = acc["current_exp"]
            acc["current_exp"] = current_exp
            if level is not None and level > 0: acc["level"] = level
            acc["gained_exp"] = max(0, current_exp - acc["initial_exp"])
            acc["last_updated"] = time.strftime("%H:%M:%S")
            if current_exp - old > 0:
                self.log(f"{acc['nickname']} gained +{current_exp - old} EXP!", "success", u)
            self.recalc_totals()

    def update_status(self, uid, status, active_matches=None):
        u = str(uid)
        if u in self.accounts:
            self.accounts[u]["status"] = status
            if active_matches is not None: self.accounts[u]["active_matches"] = active_matches
            self.accounts[u]["last_updated"] = time.strftime("%H:%M:%S")
            if status == "COMPLETE":
                self.accounts[u]["completed_at"] = time.strftime("%H:%M:%S")

    def increment_match(self, uid):
        u = str(uid)
        self.total_matches += 1
        if u in self.accounts:
            self.accounts[u]["matches_played"] += 1
            self.accounts[u]["last_match_time"] = time.strftime("%H:%M:%S")

    def is_target_reached(self, uid):
        u = str(uid)
        if u not in self.accounts: return False
        acc = self.accounts[u]
        t = int(acc.get("target_level") or 0)
        if t <= 0: return False
        return int(acc.get("level") or 1) >= t

    def recalc_totals(self):
        self.total_gained_exp = sum(a.get("gained_exp", 0) for a in self.accounts.values())

bot_state = BotState()

# ==================== FLASK APP + DB ====================
flask_app = Flask(__name__)
flask_app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'clanboost-secret-2025')
flask_app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(flask_app)
bcrypt = Bcrypt(flask_app)

# ==================== DB MODELS ====================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    basic_credits = db.Column(db.Integer, default=0)
    premium_credits = db.Column(db.Integer, default=0)
    role = db.Column(db.String(20), default='user')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    def set_password(self, p): self.password_hash = bcrypt.generate_password_hash(p).decode('utf-8')
    def check_password(self, p): return bcrypt.check_password_hash(self.password_hash, p)
    def total_credits(self):
        return int(self.basic_credits or 0) + int(self.premium_credits or 0)

    def deduct_credit(self, amount=1):
        amount = int(amount)
        if self.total_credits() < amount:
            return False
        left = amount
        if int(self.basic_credits or 0) >= left:
            self.basic_credits = int(self.basic_credits or 0) - left
            return True
        left -= int(self.basic_credits or 0)
        self.basic_credits = 0
        self.premium_credits = max(0, int(self.premium_credits or 0) - left)
        return True

    def add_credit(self, amount=1):
        self.basic_credits = int(self.basic_credits or 0) + int(amount)

    def to_dict(self):
        total = self.total_credits()
        return {'id': self.id, 'username': self.username, 'email': self.email,
                'basic_credits': total, 'premium_credits': 0, 'credits': total,
                'role': self.role}

class LevelUpOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    guest_uid = db.Column(db.String(50), nullable=False)
    password = db.Column(db.String(200), nullable=False)
    target_level = db.Column(db.Integer, default=10)
    region = db.Column(db.String(20), default='BD')
    region_tier = db.Column(db.String(20), default='basic')
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User', backref=db.backref('levelup_orders', lazy=True))
    def to_dict(self):
        return {'id': self.id, 'guest_uid': self.guest_uid, 'target_level': self.target_level,
                'region': self.region, 'region_tier': self.region_tier, 'status': self.status,
                'created_at': self.created_at.isoformat() if self.created_at else None,
                'username': self.user.username if self.user else 'Unknown'}

class LevelUpGroup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    guest_uid = db.Column(db.String(50), nullable=False)
    password = db.Column(db.String(200), nullable=False)
    game_uid = db.Column(db.String(50), default='')
    nickname = db.Column(db.String(100), default='')
    region = db.Column(db.String(20), default='BD')
    region_tier = db.Column(db.String(20), default='basic')
    status = db.Column(db.String(20), default='running')
    current_level = db.Column(db.Integer, default=1)
    initial_exp = db.Column(db.Integer, default=0)
    current_exp = db.Column(db.Integer, default=0)
    gained_exp = db.Column(db.Integer, default=0)
    target_level = db.Column(db.Integer, default=10)
    matches_played = db.Column(db.Integer, default=0)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User', backref=db.backref('levelup_groups', lazy=True))
    def to_dict(self):
        return {'id': self.id, 'guest_uid': self.guest_uid, 'game_uid': self.game_uid,
                'nickname': self.nickname, 'region': self.region, 'region_tier': self.region_tier,
                'status': self.status, 'current_level': self.current_level, 'initial_exp': self.initial_exp,
                'current_exp': self.current_exp, 'gained_exp': self.gained_exp,
                'target_level': self.target_level, 'matches_played': self.matches_played,
                'started_at': self.started_at.isoformat() if self.started_at else None,
                'glory_earned': self.gained_exp, 'target_glory': 0,
                'guild_name': self.nickname, 'members': '0/20', 'level': self.current_level}

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    basic_credits = db.Column(db.Integer, default=0)
    premium_credits = db.Column(db.Integer, default=0)
    order_id = db.Column(db.String(100))
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User', backref=db.backref('transactions', lazy=True))
    def to_dict(self):
        return {'id': self.id, 'amount': self.amount, 'basic_credits': self.basic_credits,
                'premium_credits': self.premium_credits, 'order_id': self.order_id,
                'status': self.status, 'created_at': self.created_at.isoformat() if self.created_at else None}

class Coupon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    basic_credits = db.Column(db.Integer, default=0)
    premium_credits = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='active')
    used_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    def to_dict(self):
        return {'id': self.id, 'code': self.code, 'basic_credits': self.basic_credits,
                'premium_credits': self.premium_credits, 'status': self.status,
                'created_at': self.created_at.isoformat() if self.created_at else None}

# ==================== JWT + HELPERS ====================
def generate_token(uid):
    return jwt.encode({'user_id': uid, 'exp': datetime.utcnow() + timedelta(hours=24), 'iat': datetime.utcnow()},
                      flask_app.config['SECRET_KEY'], algorithm='HS256')

def verify_token(tok):
    try: return jwt.decode(tok, flask_app.config['SECRET_KEY'], algorithms=['HS256'])['user_id']
    except: return None

def api_login_required(f):
    @wraps(f)
    def deco(*a, **kw):
        tok = request.cookies.get('auth_token')
        if not tok: return jsonify({'error': 'Unauthorized'}), 401
        uid = verify_token(tok)
        if not uid: return jsonify({'error': 'Session expired'}), 401
        user = db.session.get(User, uid)
        if not user: return jsonify({'error': 'User not found'}), 401
        return f(user, *a, **kw)
    return deco

def admin_required(f):
    @wraps(f)
    def deco(*a, **kw):
        tok = request.cookies.get('auth_token')
        if not tok: return jsonify({'error': 'Unauthorized'}), 401
        uid = verify_token(tok)
        if not uid: return jsonify({'error': 'Session expired'}), 401
        user = db.session.get(User, uid)
        if not user or user.role != 'admin': return jsonify({'error': 'Admin required'}), 403
        return f(user, *a, **kw)
    return deco

def gen_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))

def schedule_async(coro):
    if _bot_loop is None: return
    asyncio.run_coroutine_threadsafe(coro, _bot_loop)

# ==================== HTML TEMPLATES ====================
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ClanBoost Pro - Free Fire Level Up Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Space+Grotesk:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Inter', sans-serif; background: #0b1120; color: #e2e8f0; line-height: 1.5; overflow-x: hidden; }
:root { --blue-primary: #3b82f6; --blue-dark: #2563eb; --blue-light: #60a5fa; --bg-dark: #0f172a; --bg-card: #1e293b; --border-dim: #334155; }
.particles-bg { position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: -2; overflow: hidden; }
.particle { position: absolute; width: 3px; height: 3px; background: #3b82f6; border-radius: 50%; opacity: 0.4; animation: floatParticle 15s infinite linear; }
@keyframes floatParticle { 0% { transform: translateY(100vh) translateX(0); opacity: 0; } 10% { opacity: 0.4; } 90% { opacity: 0.4; } 100% { transform: translateY(-100vh) translateX(100px); opacity: 0; } }
.bg-gradient { position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: -1; background: radial-gradient(ellipse at 30% 40%, rgba(59, 130, 246, 0.08) 0%, transparent 50%); animation: bgPulse 8s ease-in-out infinite; }
@keyframes bgPulse { 0%, 100% { opacity: 0.5; transform: scale(1); } 50% { opacity: 1; transform: scale(1.05); } }
nav { position: fixed; top: 0; left: 0; right: 0; background: rgba(15, 23, 42, 0.92); backdrop-filter: blur(16px); border-bottom: 1px solid #334155; padding: 1rem 5%; display: flex; justify-content: space-between; align-items: center; z-index: 100; transition: all 0.3s ease; }
nav.scrolled { padding: 0.7rem 5%; background: rgba(15, 23, 42, 0.98); }
.logo { font-size: 1.6rem; font-weight: 700; background: linear-gradient(135deg, #3b82f6, #93c5fd); -webkit-background-clip: text; background-clip: text; color: transparent; text-decoration: none; }
.nav-links { display: flex; gap: 2rem; list-style: none; }
.nav-links a { color: #cbd5e1; text-decoration: none; font-weight: 500; transition: 0.3s; }
.nav-links a:hover { color: #3b82f6; }
.nav-btn { background: #3b82f6; padding: 0.6rem 1.4rem; border-radius: 40px; color: white !important; }
.nav-btn:hover { background: #2563eb; }
.mobile-menu { display: none; font-size: 1.5rem; background: none; border: none; color: white; cursor: pointer; }
.hero { padding: 140px 5% 80px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 3rem; max-width: 1300px; margin: 0 auto; }
.hero-left { flex: 1; min-width: 280px; }
.hero-left h1 { font-size: 3.2rem; font-weight: 800; line-height: 1.2; margin-bottom: 1.2rem; }
.hero-left h1 span { color: #3b82f6; }
.hero-left p { color: #94a3b8; font-size: 1.1rem; margin-bottom: 2rem; }
.stats { display: flex; gap: 2rem; margin-bottom: 2rem; }
.stat h3 { font-size: 2rem; font-weight: 700; color: #60a5fa; }
.stat p { font-size: 0.8rem; color: #64748b; }
.btn-group { display: flex; gap: 1rem; flex-wrap: wrap; }
.btn-blue { background: #3b82f6; padding: 0.8rem 1.8rem; border-radius: 40px; color: white; text-decoration: none; font-weight: 600; transition: 0.3s; display: inline-flex; align-items: center; gap: 8px; }
.btn-blue:hover { background: #2563eb; transform: translateY(-2px); }
.btn-outline { border: 1.5px solid #3b82f6; padding: 0.8rem 1.8rem; border-radius: 40px; color: white; text-decoration: none; font-weight: 500; transition: 0.3s; }
.btn-outline:hover { background: rgba(59, 130, 246, 0.1); }
.hero-right { flex: 1; min-width: 280px; background: #1e293b; border: 1px solid #334155; border-radius: 28px; padding: 2rem; box-shadow: 0 20px 35px -12px rgba(0, 0, 0, 0.4); }
.info-row { display: flex; justify-content: space-between; margin-bottom: 1rem; }
.progress-bar { background: #334155; height: 8px; border-radius: 10px; margin: 10px 0; overflow: hidden; }
.progress-fill { width: 74%; height: 100%; background: #3b82f6; border-radius: 10px; animation: fillProgress 1.5s ease-out forwards; }
@keyframes fillProgress { from { width: 0; } to { width: 74%; } }
.big-number { font-size: 2.2rem; font-weight: 700; color: #93c5fd; text-align: center; margin: 1.2rem 0; }
.grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; text-align: center; margin-top: 1rem; }
.grid-item { background: #0f172a; padding: 12px; border-radius: 16px; }
.float-badge { position: absolute; background: #1e293b; border: 1px solid #3b82f6; padding: 0.5rem 1rem; border-radius: 50px; display: flex; align-items: center; gap: 6px; font-size: 0.75rem; animation: floatBadge 3s ease-in-out infinite; }
.float-badge.top { top: -15px; right: -15px; }
.float-badge.bottom { bottom: -15px; left: -15px; animation-delay: 1.5s; }
@keyframes floatBadge { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-8px); } }
.features { padding: 80px 5%; background: #0f172a; text-align: center; }
.section-badge { display: inline-block; background: rgba(59, 130, 246, 0.15); color: #60a5fa; padding: 0.3rem 1rem; border-radius: 40px; font-size: 0.8rem; margin-bottom: 1rem; }
.section-title { font-size: 2.2rem; font-weight: 700; margin-bottom: 1rem; }
.section-title span { color: #3b82f6; }
.features-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 2rem; max-width: 1200px; margin: 3rem auto 0; }
.feature { background: #1e293b; border: 1px solid #334155; border-radius: 24px; padding: 2rem 1.5rem; text-align: left; transition: 0.3s; }
.feature:hover { border-color: #3b82f6; transform: translateY(-5px); }
.feature i { font-size: 2.2rem; color: #3b82f6; margin-bottom: 1rem; }
.feature h3 { font-size: 1.3rem; margin-bottom: 0.6rem; }
.feature p { color: #94a3b8; font-size: 0.9rem; }
.steps { padding: 80px 5%; text-align: center; }
.step-container { display: flex; flex-wrap: wrap; justify-content: center; gap: 2rem; margin-top: 3rem; }
.step { background: #1e293b; border-radius: 28px; padding: 2rem; width: 240px; text-align: center; }
.step-icon { width: 70px; height: 70px; background: #0f172a; border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 1rem; font-size: 1.8rem; color: #3b82f6; border: 1px solid #334155; }
.pricing { background: #0f172a; padding: 80px 5%; text-align: center; }
.pricing-card { max-width: 380px; margin: 0 auto; background: #1e293b; border: 1px solid #3b82f6; border-radius: 32px; padding: 2.5rem; }
.price { font-size: 3rem; font-weight: 700; color: #93c5fd; margin: 1rem 0; }
.testimonials { padding: 80px 5%; }
.test-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 2rem; max-width: 1200px; margin: 3rem auto 0; }
.test-card { background: #1e293b; border-radius: 24px; padding: 1.8rem; }
.stars { color: #fbbf24; margin-bottom: 1rem; }
.cta { background: #0f172a; padding: 70px 5%; text-align: center; }
footer { background: #0b1120; padding: 60px 5% 30px; }
.footer-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 2rem; max-width: 1200px; margin: 0 auto; }
.footer-col a { display: block; color: #94a3b8; text-decoration: none; margin: 0.6rem 0; }
.footer-col a:hover { color: #3b82f6; }
.copyright { text-align: center; padding-top: 3rem; color: #475569; font-size: 0.8rem; }
@media (max-width: 768px) { .nav-links { display: none; } .mobile-menu { display: block; } .hero { flex-direction: column; text-align: center; } .stats { justify-content: center; } .btn-group { justify-content: center; } .float-badge { display: none; } }
</style>
</head>
<body>
<div class="particles-bg" id="particlesBg"></div>
<div class="bg-gradient"></div>
<nav id="navbar">
<a href="/" class="logo">⚡ ClanBoost Pro</a>
<ul class="nav-links" id="navLinks">
<li><a href="#features">Features</a></li>
<li><a href="#steps">Process</a></li>
<li><a href="#pricing">Plans</a></li>
<li><a href="#reviews">Reviews</a></li>
<li><a href="/login" class="nav-btn">Dashboard</a></li>
</ul>
<button class="mobile-menu" id="menuBtn"><i class="fas fa-bars"></i></button>
</nav>
<section class="hero">
<div class="hero-left">
<h1>Auto Level Up <span>System</span> for Free Fire</h1>
<p>Level up your Free Fire account automatically with our advanced bot. No manual work needed.</p>
<div class="stats">
<div class="stat"><h3>35M+</h3><p>Daily EXP</p></div>
<div class="stat"><h3>99.9%</h3><p>Uptime</p></div>
<div class="stat"><h3>200+</h3><p>Active Bots</p></div>
</div>
<div class="btn-group">
<a href="/login" class="btn-blue"><i class="fas fa-bolt"></i> Get Started</a>
<a href="#steps" class="btn-outline">How it works</a>
</div>
</div>
<div class="hero-right" style="position: relative;">
<div class="info-row"><span>Daily Target</span><span>74%</span></div>
<div class="progress-bar"><div class="progress-fill"></div></div>
<div class="big-number">+8.4M EXP</div>
<div class="grid-3">
<div class="grid-item"><strong>6</strong><br>Active Bots</div>
<div class="grid-item"><strong>24/7</strong><br>Auto Mode</div>
<div class="grid-item"><strong>Global</strong><br>Regions</div>
</div>
<div class="float-badge top"><i class="fas fa-bolt"></i> +250k EXP/Hour</div>
<div class="float-badge bottom"><i class="fas fa-shield-alt"></i> 100% Safe</div>
</div>
</section>
<section id="features" class="features">
<span class="section-badge">Core Features</span>
<h2 class="section-title">Why <span>ClanBoost Pro</span> ?</h2>
<div class="features-grid">
<div class="feature"><i class="fas fa-microchip"></i><h3>Smart Bot</h3><p>Fully automated level up with intelligent scheduling.</p></div>
<div class="feature"><i class="fas fa-chart-line"></i><h3>Live Stats</h3><p>Track progress in real time from your dashboard.</p></div>
<div class="feature"><i class="fas fa-globe"></i><h3>All Regions</h3><p>Works for ME, India, Bangladesh, SEA & more.</p></div>
<div class="feature"><i class="fas fa-shield"></i><h3>Secure</h3><p>Encrypted system & safe for your account.</p></div>
<div class="feature"><i class="fas fa-credit-card"></i><h3>Credit System</h3><p>Pay only for what you use. Auto refund on issues.</p></div>
<div class="feature"><i class="fas fa-headset"></i><h3>Fast Support</h3><p>Response within 12 hours on Telegram.</p></div>
</div>
</section>
<section id="steps" class="steps">
<span class="section-badge">Simple Process</span>
<h2 class="section-title">Get Started in <span>4 Steps</span></h2>
<div class="step-container">
<div class="step"><div class="step-icon"><i class="fas fa-user-plus"></i></div><h3>Sign up</h3><p>Free account in seconds</p></div>
<div class="step"><div class="step-icon"><i class="fas fa-coins"></i></div><h3>Add credits</h3><p>Choose any plan</p></div>
<div class="step"><div class="step-icon"><i class="fas fa-gamepad"></i></div><h3>Enter Guest UID</h3><p>Paste your UID + password</p></div>
<div class="step"><div class="step-icon"><i class="fas fa-chart-simple"></i></div><h3>Auto level up</h3><p>Sit back & enjoy ranking</p></div>
</div>
</section>
<section id="pricing" class="pricing">
<span class="section-badge">Pricing</span>
<h2 class="section-title">Simple <span>Plans</span></h2>
<div class="pricing-card">
<h3>Pro Plan</h3>
<p>Most popular choice</p>
<div class="price">$20 <small>/ 3 credits</small></div>
<ul class="pricing-feature" style="list-style: none; text-align: left; margin: 1.5rem 0;">
<li style="margin: 0.8rem 0;"><i class="fas fa-check-circle" style="color: #3b82f6;"></i> 3 full level up sessions</li>
<li style="margin: 0.8rem 0;"><i class="fas fa-check-circle" style="color: #3b82f6;"></i> Full dashboard access</li>
<li style="margin: 0.8rem 0;"><i class="fas fa-check-circle" style="color: #3b82f6;"></i> Priority support</li>
<li style="margin: 0.8rem 0;"><i class="fas fa-check-circle" style="color: #3b82f6;"></i> Auto refund if fail</li>
</ul>
<a href="/login" class="btn-blue" style="display: inline-block; width: 100%; text-align: center;">Get Started →</a>
</div>
</section>
<section id="reviews" class="testimonials">
<span class="section-badge">Testimonials</span>
<h2 class="section-title">Trusted by <span>Players</span></h2>
<div class="test-grid">
<div class="test-card"><div class="stars">★★★★★</div><p>"My account jumped from level 10 to level 25 in just 10 days!"</p><div class="author" style="display: flex; align-items: center; gap: 12px; margin-top: 1rem;"><div class="avatar" style="width: 44px; height: 44px; background: #3b82f6; border-radius: 50%; display: flex; align-items: center; justify-content: center;">AK</div><div><strong>Ahmed</strong><br>Desert Clan</div></div></div>
<div class="test-card"><div class="stars">★★★★★</div><p>"Support is super fast and the refund system is fair."</p><div class="author" style="display: flex; align-items: center; gap: 12px; margin-top: 1rem;"><div class="avatar" style="width: 44px; height: 44px; background: #3b82f6; border-radius: 50%; display: flex; align-items: center; justify-content: center;">RJ</div><div><strong>Rajan</strong><br>Phoenix Squad</div></div></div>
<div class="test-card"><div class="stars">★★★★½</div><p>"Dashboard is clean and easy. We use it every week."</p><div class="author" style="display: flex; align-items: center; gap: 12px; margin-top: 1rem;"><div class="avatar" style="width: 44px; height: 44px; background: #3b82f6; border-radius: 50%; display: flex; align-items: center; justify-content: center;">MH</div><div><strong>Mohamed</strong><br>Night Wolves</div></div></div>
</div>
</section>
<section class="cta">
<div class="cta-box" style="max-width: 700px; margin: 0 auto;">
<h2 class="section-title">Ready to <span>Level Up</span> ?</h2>
<p style="color: #94a3b8; margin-bottom: 1.5rem;">Join 200+ players already boosting with ClanBoost Pro.</p>
<a href="/login" class="btn-blue"><i class="fas fa-crown"></i> Get Started</a>
</div>
</section>
<footer>
<div class="footer-grid">
<div class="footer-col"><a href="/" class="logo" style="font-size: 1.4rem;">⚡ ClanBoost Pro</a><p style="color: #64748b; margin-top: 0.8rem;">Advanced automation for Free Fire players.</p></div>
<div class="footer-col"><h4 style="color: #cbd5e1; margin-bottom: 1rem;">Quick</h4><a href="#features">Features</a><a href="#steps">Process</a><a href="#pricing">Pricing</a></div>
<div class="footer-col"><h4 style="color: #cbd5e1; margin-bottom: 1rem;">Support</h4><a href="#">Help</a><a href="#">Telegram</a><a href="#">Contact</a></div>
<div class="footer-col"><h4 style="color: #cbd5e1; margin-bottom: 1rem;">Legal</h4><a href="#">Terms</a><a href="#">Privacy</a><a href="#">Refund</a></div>
</div>
<div class="copyright">© 2025 ClanBoost Pro — Made for Free Fire players</div>
</footer>
<script>
const menuBtn = document.getElementById('menuBtn');
const navLinks = document.getElementById('navLinks');
menuBtn.addEventListener('click', () => {
if (navLinks.style.display === 'flex') navLinks.style.display = 'none';
else navLinks.style.display = 'flex';
});
window.addEventListener('scroll', () => {
const nav = document.getElementById('navbar');
if (window.scrollY > 50) nav.classList.add('scrolled');
else nav.classList.remove('scrolled');
});
document.querySelectorAll('a[href^="#"]').forEach(anchor => {
anchor.addEventListener('click', function(e) {
e.preventDefault();
const target = document.querySelector(this.getAttribute('href'));
if(target) target.scrollIntoView({ behavior: 'smooth' });
});
});
const container = document.getElementById('particlesBg');
for (let i = 0; i < 50; i++) {
const p = document.createElement('div');
p.classList.add('particle');
p.style.left = Math.random() * 100 + '%';
p.style.animationDelay = Math.random() * 20 + 's';
p.style.animationDuration = (12 + Math.random() * 15) + 's';
container.appendChild(p);
}
</script>
</body>
</html>"""

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ClanBoost Pro - Login / Signup</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Inter', sans-serif; background: #0b1120; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
:root { --blue: #3b82f6; --blue-dark: #2563eb; --blue-light: #60a5fa; --bg-card: #1e293b; --border: #334155; --text: #e2e8f0; --text-dim: #94a3b8; }
.bg-effect { position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: -1; background: radial-gradient(ellipse at 30% 40%, rgba(59, 130, 246, 0.08) 0%, transparent 50%); }
.auth-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 28px; width: 100%; max-width: 460px; padding: 2.5rem; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5); animation: fadeIn 0.5s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
.logo { text-align: center; margin-bottom: 2rem; }
.logo h1 { font-size: 1.8rem; font-weight: 700; background: linear-gradient(135deg, var(--blue), var(--blue-light)); -webkit-background-clip: text; background-clip: text; color: transparent; }
.logo p { color: var(--text-dim); font-size: 0.85rem; margin-top: 0.3rem; }
.tabs { display: flex; gap: 1rem; margin-bottom: 2rem; border-bottom: 1px solid var(--border); }
.tab { flex: 1; text-align: center; padding: 0.8rem; background: none; border: none; font-size: 1rem; font-weight: 600; color: var(--text-dim); cursor: pointer; transition: 0.3s; position: relative; }
.tab.active { color: var(--blue); }
.tab.active::after { content: ''; position: absolute; bottom: -1px; left: 0; right: 0; height: 2px; background: var(--blue); }
.tab:hover { color: var(--blue); }
.form { display: none; animation: fadeIn 0.3s ease; }
.form.active { display: block; }
.input-group { margin-bottom: 1.2rem; }
.input-group input { width: 100%; padding: 0.9rem 1rem; background: #0f172a; border: 1px solid var(--border); border-radius: 14px; font-size: 0.95rem; color: white; font-family: inherit; transition: 0.3s; }
.input-group input:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2); }
.input-group input::placeholder { color: #64748b; }
.checkbox-group { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; font-size: 0.85rem; }
.checkbox { display: flex; align-items: center; gap: 8px; cursor: pointer; color: var(--text-dim); }
.checkbox input { width: 16px; height: 16px; cursor: pointer; accent-color: var(--blue); }
.forgot { color: var(--blue); text-decoration: none; }
.forgot:hover { text-decoration: underline; }
.btn { width: 100%; padding: 0.9rem; background: var(--blue); border: none; border-radius: 14px; font-size: 1rem; font-weight: 600; color: white; cursor: pointer; transition: 0.3s; margin-bottom: 1.5rem; }
.btn:hover { background: var(--blue-dark); transform: translateY(-2px); box-shadow: 0 10px 25px rgba(59, 130, 246, 0.3); }
.divider { text-align: center; position: relative; margin: 1.5rem 0; }
.divider::before { content: ''; position: absolute; top: 50%; left: 0; right: 0; height: 1px; background: var(--border); }
.divider span { background: var(--bg-card); padding: 0 1rem; position: relative; font-size: 0.8rem; color: var(--text-dim); }
.social { display: flex; justify-content: center; gap: 1rem; }
.social a { width: 44px; height: 44px; background: #0f172a; border: 1px solid var(--border); border-radius: 50%; display: flex; align-items: center; justify-content: center; color: var(--text-dim); text-decoration: none; transition: 0.3s; }
.social a:hover { background: var(--blue); color: white; transform: translateY(-3px); }
.alert { padding: 0.8rem; border-radius: 12px; margin-bottom: 1rem; font-size: 0.85rem; display: none; }
.alert.error { background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.3); color: #f87171; display: block; }
.alert.success { background: rgba(34, 197, 94, 0.15); border: 1px solid rgba(34, 197, 94, 0.3); color: #4ade80; display: block; }
.terms-text { font-size: 0.75rem; color: var(--text-dim); text-align: center; margin-top: 1.5rem; }
.terms-text a { color: var(--blue); text-decoration: none; }
@media (max-width: 500px) { .auth-card { padding: 1.8rem; } .logo h1 { font-size: 1.5rem; } }
</style>
</head>
<body>
<div class="bg-effect"></div>
<div class="auth-card">
<div class="logo">
<h1>⚡ ClanBoost Pro</h1>
<p>Free Fire Level Up Automation</p>
</div>
<div class="tabs">
<button class="tab active" data-form="login">Login</button>
<button class="tab" data-form="signup">Sign Up</button>
</div>
<div class="form active" id="loginForm">
<div class="alert" id="loginAlert"></div>
<form id="login">
<div class="input-group"><input type="email" id="loginEmail" placeholder="Email address" required></div>
<div class="input-group"><input type="password" id="loginPassword" placeholder="Password" required></div>
<div class="checkbox-group">
<label class="checkbox"><input type="checkbox"> Remember me</label>
<a href="#" class="forgot">Forgot password?</a>
</div>
<button type="submit" class="btn">Login</button>
</form>
</div>
<div class="form" id="signupForm">
<div class="alert" id="signupAlert"></div>
<form id="signup">
<div class="input-group"><input type="text" id="signupName" placeholder="Full name" required></div>
<div class="input-group"><input type="email" id="signupEmail" placeholder="Email address" required></div>
<div class="input-group"><input type="password" id="signupPassword" placeholder="Password (min 6 characters)" required></div>
<div class="input-group"><input type="password" id="signupConfirm" placeholder="Confirm password" required></div>
<div class="checkbox-group">
<label class="checkbox"><input type="checkbox" id="termsCheck"> I agree to the Terms</label>
</div>
<button type="submit" class="btn">Create Account</button>
</form>
</div>
<div class="terms-text">By continuing, you agree to our <a href="#">Terms of Service</a> and <a href="#">Privacy Policy</a></div>
</div>
<script>
const tabs = document.querySelectorAll('.tab');
const forms = document.querySelectorAll('.form');
tabs.forEach(tab => {
tab.addEventListener('click', () => {
const formId = tab.dataset.form;
tabs.forEach(t => t.classList.remove('active'));
tab.classList.add('active');
forms.forEach(form => form.classList.remove('active'));
document.getElementById(formId + 'Form').classList.add('active');
});
});
document.getElementById('login').addEventListener('submit', async (e) => {
e.preventDefault();
const email = document.getElementById('loginEmail').value;
const password = document.getElementById('loginPassword').value;
const alertBox = document.getElementById('loginAlert');
if (!email || !password) { alertBox.textContent = 'Please fill in all fields'; alertBox.className = 'alert error'; setTimeout(() => alertBox.className = 'alert', 3000); return; }
try {
const res = await fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password }) });
const data = await res.json();
if (res.ok && data.success) { alertBox.textContent = 'Login successful! Redirecting...'; alertBox.className = 'alert success'; setTimeout(() => { window.location.href = '/home'; }, 1000); }
else { alertBox.textContent = data.error || 'Login failed'; alertBox.className = 'alert error'; setTimeout(() => alertBox.className = 'alert', 3000); }
} catch (err) { alertBox.textContent = 'Network error.'; alertBox.className = 'alert error'; setTimeout(() => alertBox.className = 'alert', 3000); }
});
document.getElementById('signup').addEventListener('submit', async (e) => {
e.preventDefault();
const name = document.getElementById('signupName').value;
const email = document.getElementById('signupEmail').value;
const password = document.getElementById('signupPassword').value;
const confirm = document.getElementById('signupConfirm').value;
const terms = document.getElementById('termsCheck').checked;
const alertBox = document.getElementById('signupAlert');
if (!name || !email || !password || !confirm) { alertBox.textContent = 'Please fill in all fields'; alertBox.className = 'alert error'; setTimeout(() => alertBox.className = 'alert', 3000); return; }
if (password !== confirm) { alertBox.textContent = 'Passwords do not match'; alertBox.className = 'alert error'; setTimeout(() => alertBox.className = 'alert', 3000); return; }
if (password.length < 6) { alertBox.textContent = 'Password must be at least 6 characters'; alertBox.className = 'alert error'; setTimeout(() => alertBox.className = 'alert', 3000); return; }
if (!terms) { alertBox.textContent = 'Please agree to the Terms'; alertBox.className = 'alert error'; setTimeout(() => alertBox.className = 'alert', 3000); return; }
try {
const res = await fetch('/api/auth/register', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: name, email, password }) });
const data = await res.json();
if (res.ok && data.success) { alertBox.textContent = 'Account created!'; alertBox.className = 'alert success'; setTimeout(() => { document.getElementById('loginEmail').value = email; document.querySelector('.tab[data-form="login"]').click(); alertBox.className = 'alert'; }, 1500); }
else { alertBox.textContent = data.error || 'Registration failed'; alertBox.className = 'alert error'; setTimeout(() => alertBox.className = 'alert', 3000); }
} catch (err) { alertBox.textContent = 'Network error.'; alertBox.className = 'alert error'; setTimeout(() => alertBox.className = 'alert', 3000); }
});
</script>
</body>
</html>"""

HOME_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=yes">
<title>ClanBoost Pro - Dashboard</title>
<script src="https://cdn.tailwindcss.com/3.4.17"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Space+Grotesk:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Inter', sans-serif; background: #0a0c15; color: #e2e8f0; min-height: 100vh; }
:root { --primary: #3b82f6; --primary-dark: #2563eb; --primary-light: #60a5fa; --bg-dark: #0a0c15; --bg-card: #0f111a; --bg-elevated: #151821; --border-dim: #1e2130; }
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg-dark); border-radius: 10px; }
::-webkit-scrollbar-thumb { background: var(--primary); border-radius: 10px; }
.ng-card { background: var(--bg-card); border: 1px solid var(--border-dim); border-radius: 20px; transition: all 0.3s ease; }
.ng-card:hover { border-color: var(--primary); box-shadow: 0 8px 25px -10px rgba(59, 130, 246, 0.2); }
.ng-input { background: var(--bg-elevated); border: 1px solid var(--border-dim); border-radius: 12px; padding: 0.75rem 1rem; width: 100%; color: #e2e8f0; transition: all 0.3s ease; }
.ng-input:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1); }
.ng-input:disabled { opacity: 0.5; cursor: not-allowed; }
.ng-btn-primary { background: var(--primary); border: none; border-radius: 12px; padding: 0.75rem 1.5rem; color: white; font-weight: 600; cursor: pointer; transition: all 0.3s ease; }
.ng-btn-primary:hover:not(:disabled) { background: var(--primary-dark); transform: translateY(-2px); }
.ng-btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.ng-btn-outline { background: transparent; border: 1px solid var(--border-dim); border-radius: 12px; padding: 0.75rem 1.5rem; color: #94a3b8; font-weight: 500; cursor: pointer; transition: all 0.3s ease; }
.ng-btn-outline:hover:not(:disabled) { border-color: var(--primary); color: var(--primary); }
.ng-btn-success { background: linear-gradient(135deg, #10b981, #059669); border: none; border-radius: 12px; padding: 0.75rem 1.5rem; color: white; font-weight: 600; cursor: pointer; transition: all 0.3s ease; }
.ng-btn-success:hover:not(:disabled) { transform: translateY(-2px); }
.ng-btn-success:disabled { opacity: 0.5; cursor: not-allowed; }
.ng-badge { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 50px; font-size: 0.7rem; font-weight: 500; }
.ng-badge-blue { background: rgba(59, 130, 246, 0.15); color: var(--primary-light); border: 1px solid rgba(59, 130, 246, 0.3); }
.ng-badge-gold { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }
.ng-badge-yellow { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }
.ng-badge-green { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
.ng-section-title { display: flex; align-items: center; gap: 12px; font-size: 1.2rem; font-weight: 600; }
.ng-section-icon { width: 36px; height: 36px; background: rgba(59, 130, 246, 0.1); border-radius: 12px; display: flex; align-items: center; justify-content: center; border: 1px solid rgba(59, 130, 246, 0.2); }
.loader { width: 20px; height: 20px; border: 2px solid var(--border-dim); border-top-color: var(--primary); border-radius: 50%; animation: spin 0.8s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }
.nav-item-active { background: rgba(59, 130, 246, 0.1); color: var(--primary-light); border-left: 3px solid var(--primary); }
.toast-show { animation: toastIn 0.3s ease-out; }
@keyframes toastIn { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
.group-card { background: linear-gradient(135deg, #0f111a 0%, #0a0c15 100%); border: 1px solid #1e2130; border-radius: 24px; transition: all 0.3s ease; overflow: hidden; }
.group-card:hover { border-color: var(--primary); transform: translateY(-2px); }
.status-badge { display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px; border-radius: 50px; font-size: 0.7rem; font-weight: 600; }
.status-running { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
.status-stopped { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
.status-completed { background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); }
.status-pending { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }
.status-online { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
.status-offline { background: rgba(107, 114, 128, 0.2); color: #9ca3af; border: 1px solid rgba(107, 114, 128, 0.35); }
.status-searching { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }
.status-in_match { background: rgba(168, 85, 247, 0.15); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.35); }
.stat-item { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.05); }
.stat-label { color: #94a3b8; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.5px; }
.stat-value { color: #e2e8f0; font-size: 0.9rem; font-weight: 500; }
.bot-avatar { width: 48px; height: 48px; border-radius: 16px; background: linear-gradient(135deg, #1e293b, #0f172a); display: flex; align-items: center; justify-content: center; border: 1px solid #334155; }
.bot-avatar img { width: 32px; height: 32px; object-fit: contain; }
.btn-restart { background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.3); color: #34d399; border-radius: 8px; padding: 6px 14px; font-size: 0.75rem; font-weight: 500; transition: all 0.2s ease; cursor: pointer; }
.btn-restart:hover:not(:disabled) { background: rgba(16, 185, 129, 0.2); border-color: #10b981; }
.btn-stop { background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); color: #f87171; border-radius: 8px; padding: 6px 14px; font-size: 0.75rem; font-weight: 500; cursor: pointer; }
.btn-stop:hover:not(:disabled) { background: rgba(239, 68, 68, 0.2); border-color: #ef4444; }
.btn-delete { background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); color: #f87171; border-radius: 8px; padding: 6px 14px; font-size: 0.75rem; font-weight: 500; cursor: pointer; }
.btn-delete:hover:not(:disabled) { background: rgba(239, 68, 68, 0.2); border-color: #ef4444; }
.button-group { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 8px; }
.pending-card { background: linear-gradient(135deg, #1a1d28 0%, #0f111a 100%); border: 1px solid #fbbf2430; border-radius: 24px; transition: all 0.3s ease; overflow: hidden; }
.buy-credits-card { background: linear-gradient(135deg, #1a1d28 0%, #0f111a 100%); border: 1px solid #3b82f640; border-radius: 24px; transition: all 0.3s ease; overflow: hidden; text-align: center; padding: 3rem 2rem; max-width: 500px; margin: 2rem auto; }
.buy-credits-card:hover { border-color: #3b82f6; transform: translateY(-4px); box-shadow: 0 20px 40px -15px rgba(59, 130, 246, 0.2); }
.telegram-icon { width: 80px; height: 80px; background: linear-gradient(135deg, #0088cc, #00a3e0); border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 1.5rem; box-shadow: 0 0 20px rgba(0, 136, 204, 0.4); }
.glow-btn { background: linear-gradient(135deg, #0088cc, #00a3e0); border: none; border-radius: 40px; padding: 12px 28px; color: white; font-weight: 600; font-size: 0.9rem; cursor: pointer; transition: all 0.3s ease; box-shadow: 0 0 15px rgba(0, 136, 204, 0.5); animation: glow 2s ease-in-out infinite; }
.glow-btn:hover { transform: translateY(-2px); box-shadow: 0 0 25px rgba(0, 136, 204, 0.8); }
@keyframes glow { 0%, 100% { box-shadow: 0 0 15px rgba(0, 136, 204, 0.4); } 50% { box-shadow: 0 0 25px rgba(0, 136, 204, 0.8); } }
.full-page-card { display: flex; align-items: center; justify-content: center; min-height: 60vh; padding: 2rem; }
@keyframes modalSlideUp { from { opacity: 0; transform: translateY(30px); } to { opacity: 1; transform: translateY(0); } }
.animate-modal-slide-up { animation: modalSlideUp 0.3s ease-out; }
.btn-spinner { width: 14px; height: 14px; border: 2px solid currentColor; border-top-color: transparent; border-radius: 50%; animation: spin 0.6s linear infinite; display: inline-block; }
@media (max-width: 640px) { .bot-avatar { width: 40px; height: 40px; } .bot-avatar img { width: 24px; height: 24px; } }
</style>
</head>
<body>

<div class="flex-grow flex flex-col">
<nav class="sticky top-0 z-20 px-4 py-3 bg-[#0f111a]/95 backdrop-blur-lg border-b border-[#1e2130]">
<div class="flex items-center justify-between">
<div class="flex items-center gap-3">
<div class="w-9 h-9 rounded-xl bg-gradient-to-br from-blue-500 to-blue-600 flex items-center justify-center"><i class="fas fa-bolt text-white text-sm"></i></div>
<h1 class="text-lg font-bold bg-gradient-to-r from-blue-400 to-blue-300 bg-clip-text text-transparent">ClanBoost Pro</h1>
<div class="hidden sm:flex items-center gap-1.5 px-2 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/20">
<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
<span class="text-[10px] text-emerald-400 font-medium">LIVE</span>
</div>
</div>
<div class="flex items-center gap-2">
<button id="logout-btn-mobile" class="sm:hidden text-xs text-gray-400 border border-[#1e2130] rounded-lg px-3 py-1.5"><i class="fas fa-sign-out-alt mr-1"></i>Exit</button>
<button id="logout-btn-desktop" class="hidden sm:flex ng-btn-outline text-xs px-3 py-1.5 items-center gap-1"><i class="fas fa-sign-out-alt"></i> Logout</button>
</div>
</div>
<div class="flex items-center gap-3 mt-3 overflow-x-auto pb-1">
<span id="user-badge" class="ng-badge ng-badge-blue text-[11px] whitespace-nowrap"><i class="fas fa-user-circle mr-1"></i> Loading...</span>
<span id="basic-credits-badge" class="ng-badge ng-badge-blue text-[11px] hidden whitespace-nowrap"><i class="fas fa-gem mr-1"></i> Credits: <span id="basic-credits-val" class="font-bold">0</span></span>
</div>
</nav>

<div class="flex-grow p-4 md:p-6 overflow-auto pb-24 lg:pb-6">
<div class="flex flex-col lg:flex-row gap-6">

<!-- DESKTOP SIDEBAR -->
<aside class="hidden lg:block w-64 flex-shrink-0">
<div class="ng-card p-2 sticky top-4">
<nav class="space-y-1">
<button onclick="switchTab('dashboard')" id="sidebar-dashboard" class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all text-gray-400 hover:text-white hover:bg-white/5"><i class="fas fa-chart-line w-4"></i> Overview</button>
<button onclick="switchTab('groups')" id="sidebar-groups" class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all text-gray-400 hover:text-white hover:bg-white/5"><i class="fas fa-layer-group w-4"></i> Level Up Bots</button>
<button onclick="switchTab('transactions')" id="sidebar-transactions" class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all text-gray-400 hover:text-white hover:bg-white/5"><i class="fas fa-receipt w-4"></i> Transactions</button>
<button onclick="switchTab('coupons')" id="sidebar-coupons" class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all text-gray-400 hover:text-white hover:bg-white/5"><i class="fas fa-ticket-alt w-4"></i> Coupons</button>
<button onclick="switchTab('account')" id="sidebar-account" class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all text-gray-400 hover:text-white hover:bg-white/5"><i class="fas fa-user-shield w-4"></i> Security</button>
<button onclick="showBuyCreditsOnly()" id="sidebar-buycredits" class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all text-gray-400 hover:text-white hover:bg-white/5 mt-2 border-t border-[#1e2130] pt-3"><i class="fab fa-telegram-plane w-4"></i> Buy Credits</button>
</nav>
</div>
</aside>

<!-- MOBILE BOTTOM NAV -->
<div class="lg:hidden fixed bottom-0 left-0 right-0 z-50 bg-[#0f111a]/95 backdrop-blur-lg border-t border-[#1e2130]">
<div class="flex items-center justify-around px-2 py-2">
<button onclick="switchTab('dashboard')" id="mob-dashboard" class="mob-nav-item flex flex-col items-center gap-0.5 px-3 py-2 rounded-xl text-gray-400"><i class="fas fa-chart-line text-lg"></i><span class="text-[10px]">Home</span></button>
<button onclick="switchTab('groups')" id="mob-groups" class="mob-nav-item flex flex-col items-center gap-0.5 px-3 py-2 rounded-xl text-gray-400"><i class="fas fa-layer-group text-lg"></i><span class="text-[10px]">Bots</span></button>
<button onclick="switchTab('transactions')" id="mob-transactions" class="mob-nav-item flex flex-col items-center gap-0.5 px-3 py-2 rounded-xl text-gray-400"><i class="fas fa-receipt text-lg"></i><span class="text-[10px]">TX</span></button>
<button onclick="switchTab('coupons')" id="mob-coupons" class="mob-nav-item flex flex-col items-center gap-0.5 px-3 py-2 rounded-xl text-gray-400"><i class="fas fa-ticket-alt text-lg"></i><span class="text-[10px]">Coupons</span></button>
<button onclick="showBuyCreditsOnly()" id="mob-buycredits" class="mob-nav-item flex flex-col items-center gap-0.5 px-3 py-2 rounded-xl text-gray-400"><i class="fab fa-telegram-plane text-lg"></i><span class="text-[10px]">Buy</span></button>
</div>
</div>

<div class="flex-1 min-w-0">
<div id="normal-content" class="space-y-6">

<!-- TAB: DASHBOARD -->
<div id="tab-dashboard" class="tab-content space-y-6">
<div class="ng-card p-5 bg-gradient-to-r from-blue-600/10 to-transparent border-blue-500/20">
<div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
<div>
<h2 class="text-xl font-bold">Welcome back, <span id="welcome-name" class="text-blue-400">User</span>!</h2>
<p class="text-sm text-gray-400 mt-1">Ready to level up your Free Fire account?</p>
</div>
<div class="flex gap-2">
<div class="text-center px-4 py-2 rounded-xl bg-blue-500/10 border border-blue-500/20"><p class="text-xs text-gray-400">Credits</p><p class="text-xl font-bold text-blue-400" id="dashboard-basic">0</p></div>
</div>
</div>
</div>

<div class="ng-card p-5">
<div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
<div>
<div class="ng-section-title"><div class="ng-section-icon"><i class="fas fa-rocket text-blue-400"></i></div><h3>Create Level Up Bot</h3></div>
<p class="text-xs text-gray-400 mt-1">1 Credit per bot · starts instantly</p>
</div>
<button type="button" onclick="openCreateBotModal()" class="ng-btn-success px-6 py-3 flex items-center justify-center gap-2"><i class="fas fa-plus"></i> Create Bot</button>
</div>
</div>

<div class="ng-card p-5" id="pending-orders-section" style="display: none;">
<div class="ng-section-title mb-4"><div class="ng-section-icon"><i class="fas fa-clock text-yellow-400"></i></div><h3 class="text-yellow-400">Pending Approval</h3></div>
<div id="pending-orders-list" class="space-y-3"></div>
</div>

<div class="ng-card p-5">
<div class="flex items-center justify-between mb-4">
<div class="ng-section-title"><div class="ng-section-icon"><i class="fas fa-server text-blue-400"></i></div><h3>Level Up Bots</h3><span id="groups-count" class="ng-badge ng-badge-blue text-xs">0</span></div>
<button onclick="refreshGroups()" class="text-blue-400 hover:text-blue-300 text-sm"><i class="fas fa-sync-alt"></i> Refresh</button>
</div>
<div id="groups-list" class="space-y-4"></div>
</div>
</div>

<!-- TAB: GROUPS -->
<div id="tab-groups" class="tab-content hidden space-y-6">
<div class="ng-card p-5">
<div class="flex items-center justify-between mb-4"><div class="ng-section-title"><div class="ng-section-icon"><i class="fas fa-server text-blue-400"></i></div><h3>All Level Up Bots</h3></div><button onclick="refreshGroups()" class="ng-btn-outline text-xs px-3 py-1.5"><i class="fas fa-sync-alt mr-1"></i> Refresh</button></div>
<div id="groups-full-list" class="space-y-4"></div>
</div>
</div>

<!-- TAB: TRANSACTIONS -->
<div id="tab-transactions" class="tab-content hidden space-y-6">
<div class="ng-card p-5">
<div class="ng-section-title mb-4"><div class="ng-section-icon"><i class="fas fa-receipt text-blue-400"></i></div><h3>Transaction History</h3></div>
<div id="transactions-list" class="space-y-2"><div class="text-center py-8 text-gray-500"><i class="fas fa-history text-4xl mb-2 opacity-50"></i><p>No transactions yet</p></div></div>
</div>
</div>

<!-- TAB: COUPONS -->
<div id="tab-coupons" class="tab-content hidden space-y-6">
<div class="ng-card p-5 border border-emerald-500/20">
<div class="ng-section-title mb-3"><div class="ng-section-icon"><i class="fas fa-gift text-emerald-400"></i></div><h3>Redeem Coupon Code</h3></div>
<p class="text-xs text-gray-400 mb-3">Admin dewa coupon diye Credit nin</p>
<div class="flex flex-col sm:flex-row gap-3"><input type="text" id="coupon-code" placeholder="Enter coupon code" class="ng-input flex-1" style="text-transform:uppercase;"><button onclick="redeemCoupon()" class="ng-btn-primary px-6"><i class="fas fa-check mr-1"></i> Redeem</button></div>
</div>
<div class="ng-card p-5">
<div class="ng-section-title mb-4"><div class="ng-section-icon"><i class="fas fa-ticket-alt text-blue-400"></i></div><h3>Create Gift Code</h3></div>
<p class="text-xs text-gray-400 mb-3">Nijer Credit diye gift code banan</p>
<div class="mb-4">
<label class="text-xs text-gray-400 block mb-1">Credits</label>
<div class="flex items-center gap-2">
<button type="button" onclick="adjustCredits('basic', -1)" class="w-8 h-8 rounded-lg bg-[#1e2130] hover:bg-[#2a2e40]">-</button>
<input type="number" id="coupon-basic" value="1" min="0" max="100" class="ng-input text-center w-24">
<button type="button" onclick="adjustCredits('basic', 1)" class="w-8 h-8 rounded-lg bg-[#1e2130] hover:bg-[#2a2e40]">+</button>
<input type="hidden" id="coupon-premium" value="0">
</div>
</div>
<button onclick="createCoupon()" class="w-full ng-btn-success py-2.5"><i class="fas fa-plus-circle mr-1"></i> Generate Code</button>
</div>
<div class="ng-card p-5">
<div class="ng-section-title mb-4"><div class="ng-section-icon"><i class="fas fa-list text-blue-400"></i></div><h3>My Codes</h3></div>
<div id="my-coupons-list" class="space-y-2"><div class="text-center py-6 text-gray-500"><i class="fas fa-ticket-alt text-3xl mb-2 opacity-50"></i><p>No codes created yet</p></div></div>
</div>
</div>

<!-- TAB: ACCOUNT -->
<div id="tab-account" class="tab-content hidden space-y-6">
<div class="ng-card p-5">
<div class="ng-section-title mb-4"><div class="ng-section-icon"><i class="fas fa-lock text-blue-400"></i></div><h3>Change Password</h3></div>
<div class="space-y-4 max-w-md">
<div><label class="text-xs text-gray-400 block mb-1">Current Password</label><input type="password" id="current-pw" class="ng-input" placeholder="Enter current password"></div>
<div><label class="text-xs text-gray-400 block mb-1">New Password</label><input type="password" id="new-pw" class="ng-input" placeholder="Min. 6 characters"></div>
<div><label class="text-xs text-gray-400 block mb-1">Confirm Password</label><input type="password" id="confirm-pw" class="ng-input" placeholder="Re-enter new password"></div>
<button onclick="changePassword()" class="ng-btn-primary w-full sm:w-auto"><i class="fas fa-save mr-1"></i> Update Password</button>
<div id="pw-message" class="text-sm hidden"></div>
</div>
</div>
</div>

</div>

<!-- BUY CREDITS PAGE -->
<div id="buy-credits-container" class="hidden full-page-card">
<div class="buy-credits-card">
<div class="telegram-icon"><i class="fab fa-telegram-plane text-white text-3xl"></i></div>
<h3 class="text-xl font-bold text-white mb-2">Buy Credits</h3>
<p class="text-gray-400 text-sm mb-5">Contact us on Telegram to purchase credits</p>
<button onclick="openTelegramBuy()" class="glow-btn"><i class="fab fa-telegram-plane mr-2"></i> Contact on Telegram</button>
</div>
</div>

</div>
</div>
</div>
</div>

<!-- CREATE BOT MODAL -->
<div id="create-bot-modal" class="fixed inset-0 bg-black/80 hidden items-center justify-center z-50 backdrop-blur-md p-4">
<div class="bg-[#0f111a] border border-[#2a2e40] rounded-2xl max-w-md w-full shadow-2xl animate-modal-slide-up overflow-hidden">
<div class="px-4 pt-5 pb-2 flex items-center justify-between">
<h2 class="text-xl font-bold text-white">Create Bot</h2>
<button type="button" onclick="closeCreateBotModal()" class="text-gray-400 hover:text-white text-lg"><i class="fas fa-times"></i></button>
</div>
<div class="p-5 space-y-4">
<div>
<label class="block text-xs text-gray-400 mb-1">Select Region</label>
<select id="region-select" class="ng-input" required><option value="" disabled selected>Loading regions...</option></select>
<div id="region-price-info" class="mt-2 text-xs"><span class="text-gray-500">Cost:</span> <span id="region-cost" class="text-amber-400 font-medium">💎 1 Credit</span></div>
</div>
<div>
<label class="block text-xs text-gray-400 mb-1">Target Level</label>
<input type="number" id="target-level" placeholder="e.g. 20" min="2" max="100" value="10" class="ng-input" required>
</div>
<div>
<label class="block text-xs text-gray-400 mb-1">Guest UID</label>
<input type="text" id="guest-uid" placeholder="Enter Free Fire UID" class="ng-input" required>
</div>
<div>
<label class="block text-xs text-gray-400 mb-1">Guest Password</label>
<input type="text" id="guest-password" placeholder="Enter guest password" class="ng-input" required>
</div>
<div class="flex gap-3 pt-2">
<button type="button" onclick="closeCreateBotModal()" class="flex-1 py-2.5 rounded-xl bg-transparent border border-[#3a3e50] text-gray-300 text-sm font-medium hover:bg-[#2a2e40]">Cancel</button>
<button type="button" id="create-bot-submit" onclick="submitCreateBot()" class="flex-1 py-2.5 rounded-xl bg-gradient-to-r from-emerald-500 to-emerald-600 text-white text-sm font-medium">Create</button>
</div>
<div id="launch-message" class="text-sm text-center hidden"></div>
</div>
</div>
</div>

<!-- CONFIRM MODAL -->
<div id="confirm-modal" class="fixed inset-0 bg-black/80 hidden items-center justify-center z-50 backdrop-blur-md p-4">
<div class="bg-[#0f111a] border border-[#2a2e40] rounded-2xl max-w-sm w-full shadow-2xl animate-modal-slide-up overflow-hidden">
<div class="px-4 pt-5 pb-2"><h2 class="text-xl font-bold text-center text-white">Confirm Launch</h2></div>
<div class="p-5">
<div class="text-center mb-5"><p class="text-gray-300 text-sm">Are you sure you want to launch a new level up bot?</p></div>
<div class="bg-[#1a1d28] rounded-xl p-4 mb-5 space-y-3">
<div class="flex justify-between items-center"><span class="text-gray-400 text-sm">Region:</span><span id="confirm-region" class="text-white font-medium text-sm">--</span></div>
<div class="flex justify-between items-center"><span class="text-gray-400 text-sm">Guest UID:</span><span id="confirm-clan" class="text-white font-mono text-sm">--</span></div>
<div class="flex justify-between items-center"><span class="text-gray-400 text-sm">Target Level:</span><span id="confirm-target" class="text-white font-medium text-sm">--</span></div>
<div class="border-t border-[#2a2e40] pt-2 mt-1 flex justify-between items-center"><span class="text-gray-400 text-sm">Cost:</span><span id="confirm-cost" class="text-amber-400 font-bold text-sm">--</span></div>
</div>
<div class="flex gap-3">
<button onclick="closeConfirmModal()" class="flex-1 py-2.5 rounded-xl bg-transparent border border-[#3a3e50] text-gray-300 text-sm font-medium hover:bg-[#2a2e40]">Cancel</button>
<button id="confirm-launch-btn" class="flex-1 py-2.5 rounded-xl bg-gradient-to-r from-blue-500 to-blue-600 text-white text-sm font-medium hover:from-blue-600 hover:to-blue-700">Launch</button>
</div>
</div>
</div>
</div>

<div id="toast-container" class="fixed bottom-20 right-4 z-50 space-y-2"></div>

<script>
const TELEGRAM_BUY_LINK = "https://t.me/jubayer_codex?text=HI%20JUBAYER%20CODEX%20%0AI%20WANT%20TO%20BUY%20FF%20LEVEL%20UP";
function openTelegramBuy() { window.open(TELEGRAM_BUY_LINK, '_blank'); }

function showBuyCreditsOnly() {
document.getElementById('normal-content').classList.add('hidden');
document.getElementById('buy-credits-container').classList.remove('hidden');
document.querySelectorAll('.nav-item').forEach(item => { item.classList.remove('nav-item-active'); item.classList.remove('text-blue-400'); item.classList.add('text-gray-400'); });
document.querySelectorAll('.mob-nav-item').forEach(item => { item.classList.remove('text-blue-400'); item.classList.add('text-gray-400'); });
const sidebarBtn = document.getElementById('sidebar-buycredits');
if (sidebarBtn) { sidebarBtn.classList.add('nav-item-active'); sidebarBtn.classList.remove('text-gray-400'); }
const mobBtn = document.getElementById('mob-buycredits');
if (mobBtn) { mobBtn.classList.remove('text-gray-400'); mobBtn.classList.add('text-blue-400'); }
}
function showNormalContent() {
document.getElementById('normal-content').classList.remove('hidden');
document.getElementById('buy-credits-container').classList.add('hidden');
}

const BOT_IMAGE_URL = "https://image2url.com/r2/default/images/1775901380672-8b0bdd25-a43b-422d-b5a5-8b9a0100b254.png";

const API = {
me: '/api/auth/me', logout: '/api/auth/logout', regions: '/api/public/regions',
launch: '/api/client/levelup/launch', groups: '/api/client/levelup/groups',
groupAction: '/api/client/levelup/action', transactions: '/api/client/transactions',
createCoupon: '/api/client/coupons/create', redeemCoupon: '/api/client/coupons/redeem',
myCoupons: '/api/client/coupons/my', changePassword: '/api/auth/change-password',
pendingOrders: '/api/client/levelup/pending', stats: '/api/levelup/stats'
};

let currentUser = null, pendingLaunch = null, currentTab = 'dashboard';

function showToast(message, type = 'info') {
const container = document.getElementById('toast-container');
const toast = document.createElement('div');
const colors = { success: 'bg-green-500/20 border-green-500/30 text-green-400', error: 'bg-red-500/20 border-red-500/30 text-red-400', warning: 'bg-amber-500/20 border-amber-500/30 text-amber-400', info: 'bg-blue-500/20 border-blue-500/30 text-blue-400' };
const icons = { success: 'fa-check-circle', error: 'fa-exclamation-circle', warning: 'fa-exclamation-triangle', info: 'fa-info-circle' };
toast.className = `flex items-center gap-3 px-4 py-3 rounded-xl border backdrop-blur-sm ${colors[type]} toast-show`;
toast.innerHTML = `<i class="fas ${icons[type]}"></i><span class="text-sm">${message}</span>`;
container.appendChild(toast);
setTimeout(() => { toast.style.opacity = '0'; toast.style.transform = 'translateX(100px)'; setTimeout(() => toast.remove(), 300); }, 4000);
}

function escapeHtml(str) { if (!str) return ''; return String(str).replace(/[&<>"']/g, function(m) { if (m === '&') return '&amp;'; if (m === '<') return '&lt;'; if (m === '>') return '&gt;'; if (m === '"') return '&quot;'; if (m === "'") return '&#39;'; return m; }); }

const LEVELS_MAP = {1:0,2:48,3:202,4:544,5:1012,6:1844,7:2792,8:3800,9:4870,10:6004,11:7192,12:8448,13:9776,14:11140,15:12566,16:14060,17:15610,18:17224,19:18902,20:20632,21:22424,22:24728,23:26192,24:28166,25:30200,26:32294,27:34448,28:37804,29:41174,30:44870,31:48852,32:53334,33:58566,34:64096,35:69994,36:76460,37:83108,38:91128,39:99322,40:108092,41:120144,42:133266,43:147472,44:162760,45:179126,46:196572,47:215368,48:235516,49:257010,50:279860,51:304056,52:348318,53:394982,54:444044,55:495508,56:549364,57:633756,58:721744,59:813336,60:908522,61:1041438,62:1180352,63:1325256,64:1476184,65:1634300,66:1840946,67:2056594,68:2281242,69:2514880,70:2757530,71:3059506,72:3372284,73:3699456,74:4041030,75:4397020,76:4829104,77:5282204,78:5756304,79:6251404,80:6767504,81:7381324,82:8043154,83:8752952,84:9510808,85:10316638,86:11277190,87:12360748,88:13360304,89:14482858,90:15659418,91:17026708,92:18453688,93:19941280,94:21488570,95:23095858,96:24763138,97:26490138,98:28277708,99:30124996,100:32032284};

function calcProgressPct(acc, isComplete) {
if (isComplete) return 100;
const lvl = acc.level || 1;
const target = acc.target_level || 0;
const exp = acc.current_exp || 0;
const curBase = LEVELS_MAP[lvl] || 0;
const nextBase = LEVELS_MAP[lvl+1] || (curBase + 1000);
const expForLevel = Math.max(1, nextBase - curBase);
const expInLevel = Math.max(0, exp - curBase);
const frac = Math.min(1, expInLevel / expForLevel);
let pct;
if (target <= 0) pct = frac * 100;
else if (target <= lvl) pct = 100;
else pct = ((lvl - 1 + frac) / (target - 1)) * 100;
return Math.min(99.5, Math.max(0.5, pct));
}

async function init() {
try {
const res = await fetch(API.me);
if (!res.ok) { window.location.href = '/login'; return; }
currentUser = await res.json();
document.getElementById('user-badge').innerHTML = `<i class="fas fa-user-circle mr-1"></i> ${escapeHtml(currentUser.username)}`;
document.getElementById('welcome-name').textContent = currentUser.username;
const _cr = (currentUser.credits != null) ? currentUser.credits : ((currentUser.basic_credits || 0) + (currentUser.premium_credits || 0));
document.getElementById('basic-credits-val').textContent = _cr;
document.getElementById('dashboard-basic').textContent = _cr;
document.getElementById('basic-credits-badge').classList.remove('hidden');
await loadRegions();
await loadPendingOrders();
await loadGroups();
await loadTransactions();
await loadCoupons();
} catch (e) { window.location.href = '/login'; }
}

document.getElementById('logout-btn-desktop')?.addEventListener('click', async () => { await fetch(API.logout, { method: 'POST' }); window.location.href = '/login'; });
document.getElementById('logout-btn-mobile')?.addEventListener('click', async () => { await fetch(API.logout, { method: 'POST' }); window.location.href = '/login'; });

async function loadRegions() {
try {
const res = await fetch(API.regions);
const data = await res.json();
const select = document.getElementById('region-select');
select.innerHTML = '<option value="" disabled selected>Select region</option>';
(data.regions || []).forEach(region => {
if (region.enabled) {
const option = document.createElement('option');
option.value = region.id;
option.dataset.tier = region.tier || 'credit';
const flagMap = { 'br': '🇧🇷', 'me': '🌍', 'ghrab': '🦅', 'bd': '🇧🇩', 'in': '🇮🇳', 'indo': '🇮🇩' };
const flag = flagMap[region.id] || '🏳️';
option.textContent = `${flag} ${region.region_name}`;
select.appendChild(option);
}
});
} catch (e) { console.error(e); }
}

function openCreateBotModal() {
document.getElementById('create-bot-modal').style.display = 'flex';
const sel = document.getElementById('region-select');
if (sel && sel.options.length <= 1) loadRegions();
}
function closeCreateBotModal() {
document.getElementById('create-bot-modal').style.display = 'none';
const msg = document.getElementById('launch-message');
if (msg) { msg.classList.add('hidden'); msg.textContent = ''; }
}

function submitCreateBot() {
const region = document.getElementById('region-select').value;
const guestUid = document.getElementById('guest-uid').value.trim();
const password = document.getElementById('guest-password').value.trim();
const targetLevel = parseInt(document.getElementById('target-level').value) || 10;
if (!region || !guestUid || !password) { showToast('Please fill all fields', 'error'); return; }
if (targetLevel < 2 || targetLevel > 100) { showToast('Target level must be 2-100', 'error'); return; }
const selectedOption = document.getElementById('region-select').options[document.getElementById('region-select').selectedIndex];
const totalCredits = (currentUser.credits != null) ? currentUser.credits : ((currentUser.basic_credits || 0) + (currentUser.premium_credits || 0));
if (totalCredits < 1) { showToast('❌ Insufficient Credits', 'error'); return; }
pendingLaunch = { region, guest_uid: guestUid, password, target_level: targetLevel };
document.getElementById('confirm-region').innerHTML = selectedOption ? selectedOption.textContent : region;
document.getElementById('confirm-clan').textContent = guestUid;
document.getElementById('confirm-target').textContent = 'Level ' + targetLevel;
document.getElementById('confirm-cost').innerHTML = `💎 1 Credit`;
document.getElementById('create-bot-modal').style.display = 'none';
document.getElementById('confirm-modal').style.display = 'flex';
}

function closeConfirmModal() {
document.getElementById('confirm-modal').style.display = 'none';
pendingLaunch = null;
const btn = document.getElementById('confirm-launch-btn');
if (btn) { btn.innerHTML = 'Launch'; btn.disabled = false; }
}

document.getElementById('confirm-launch-btn')?.addEventListener('click', async () => {
if (!pendingLaunch) return;
const btn = document.getElementById('confirm-launch-btn');
btn.disabled = true; btn.innerHTML = '<span class="loader"></span> Launching...';
try {
const res = await fetch(API.launch, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(pendingLaunch) });
const data = await res.json();
if (res.ok && data.success) {
showToast('✅ Bot started!', 'success');
document.getElementById('guest-uid').value = '';
document.getElementById('guest-password').value = '';
await loadPendingOrders();
await loadGroups();
await refreshUserCredits();
closeConfirmModal();
closeCreateBotModal();
} else { showToast(data.error || 'Launch failed', 'error'); btn.disabled = false; btn.innerHTML = 'Launch'; }
} catch (e) { showToast('Network error', 'error'); btn.disabled = false; btn.innerHTML = 'Launch'; }
});

async function loadPendingOrders() {
try {
const res = await fetch(API.pendingOrders);
const orders = await res.json();
const section = document.getElementById('pending-orders-section');
const container = document.getElementById('pending-orders-list');
if (orders && orders.length > 0) {
section.style.display = 'block';
const regionNames = { 'bd': 'Bangladesh', 'in': 'India', 'indo': 'Indonesia', 'br': 'Brazil', 'me': 'Middle East', 'ghrab': 'MENA' };
container.innerHTML = orders.map(order => `<div class="pending-card p-4"><div class="flex items-center justify-between mb-2 flex-wrap gap-2"><div class="flex items-center gap-2"><i class="fas fa-clock text-yellow-400"></i><span class="font-medium">Guest: ${escapeHtml(order.guest_uid)} → Lv${order.target_level}</span></div><span class="status-badge status-pending"><span class="w-1.5 h-1.5 rounded-full bg-yellow-400 animate-pulse"></span> PENDING</span></div><div class="text-sm text-gray-400">${regionNames[order.region] || order.region.toUpperCase()}</div><div class="text-xs text-gray-500 mt-2">Placed: ${new Date(order.created_at).toLocaleString()}</div></div>`).join('');
} else { section.style.display = 'none'; }
} catch (e) { console.error(e); }
}

async function refreshUserCredits() {
try {
const res = await fetch(API.me);
if (res.ok) {
const user = await res.json();
currentUser = user;
const _cr2 = (user.credits != null) ? user.credits : ((user.basic_credits || 0) + (user.premium_credits || 0));
document.getElementById('basic-credits-val').textContent = _cr2;
document.getElementById('dashboard-basic').textContent = _cr2;
}
} catch (e) {}
}

function setButtonLoading(button, isLoading, originalHtml = null) {
if (!button) return;
if (isLoading) { button.dataset.originalHtml = button.innerHTML; button.disabled = true; button.innerHTML = '<span class="btn-spinner"></span>'; }
else { button.disabled = false; if (button.dataset.originalHtml) { button.innerHTML = button.dataset.originalHtml; } else if (originalHtml) { button.innerHTML = originalHtml; } }
}

let _groupsFetchCtrl = null;
async function loadGroups() {
try {
if (_groupsFetchCtrl) { try { _groupsFetchCtrl.abort(); } catch(e) {} }
_groupsFetchCtrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
const res = await fetch(API.groups, _groupsFetchCtrl ? { signal: _groupsFetchCtrl.signal } : undefined);
let groups = await res.json();
const groupsList = document.getElementById('groups-list');
const groupsFull = document.getElementById('groups-full-list');
const groupsCount = document.getElementById('groups-count');
if (groupsCount) groupsCount.textContent = groups.length;
if (!groups || groups.length === 0) {
const emptyHtml = `<div class="text-center py-12"><div class="w-20 h-20 mx-auto mb-4 rounded-full bg-[#1e2130] flex items-center justify-center"><i class="fas fa-robot text-3xl text-gray-600"></i></div><p class="text-gray-500">No level up bots running</p><p class="text-xs text-gray-600 mt-1">Launch a new bot to get started</p></div>`;
if (groupsList) groupsList.innerHTML = emptyHtml;
if (groupsFull) groupsFull.innerHTML = emptyHtml;
return;
}
const renderCard = (group) => {
const regionNames = { 'bd': 'Bangladesh', 'in': 'India', 'indo': 'Indonesia', 'br': 'Brazil', 'me': 'Middle East', 'ghrab': 'MENA' };
const regionName = regionNames[group.region] || (group.region || '').toUpperCase();
const flagMap = { 'bd': '🇧🇩', 'in': '🇮🇳', 'indo': '🇮🇩', 'br': '🇧🇷', 'me': '🌍', 'ghrab': '🦅' };
const flag = flagMap[group.region] || '🏳️';
const level = group.current_level || 1;
const targetLevel = group.target_level || 10;
const gainedExp = group.gained_exp || 0;
const matchesPlayed = group.matches_played || 0;
const currentExp = group.current_exp || 0;
const initialExp = group.initial_exp || 0;
const rawStatus = (group.status || 'offline').toString().toLowerCase().replace(/\s+/g, '_');
const isCompleted = (targetLevel > 0 && level >= targetLevel) || rawStatus === 'completed' || rawStatus === 'complete';
const isStopped = rawStatus === 'stopped';
const isOffline = rawStatus === 'offline';
const isActive = !isCompleted && !isStopped && !isOffline;
const pct = calcProgressPct({ level, target_level: targetLevel, current_exp: currentExp }, isCompleted);
let statusClass = 'status-offline', statusText = 'OFFLINE', statusDot = 'bg-gray-400';
if (isCompleted) { statusClass = 'status-completed'; statusText = 'COMPLETED'; statusDot = 'bg-blue-400'; }
else if (isStopped) { statusClass = 'status-stopped'; statusText = 'STOPPED'; statusDot = 'bg-red-400'; }
else if (rawStatus === 'searching') { statusClass = 'status-searching'; statusText = 'SEARCHING'; statusDot = 'bg-amber-400'; }
else if (['in_match', 'in-match', 'inmatch'].includes(rawStatus)) { statusClass = 'status-in_match'; statusText = 'IN MATCH'; statusDot = 'bg-purple-400'; }
else if (rawStatus === 'online' || rawStatus === 'running') { statusClass = 'status-online'; statusText = 'ONLINE'; statusDot = 'bg-green-400'; }
else if (rawStatus === 'offline') { statusClass = 'status-offline'; statusText = 'OFFLINE'; statusDot = 'bg-gray-400'; }
else { statusClass = 'status-running'; statusText = rawStatus.toUpperCase().replace(/_/g,' '); statusDot = 'bg-green-400'; }
const displayName = group.nickname || ('Player_' + String(group.guest_uid||'').slice(-6));
const displayUid = group.game_uid || group.guest_uid;
const guestPart = (group.game_uid && group.game_uid !== group.guest_uid) ? `<span class="text-gray-500 text-xs">(guest: ${escapeHtml(group.guest_uid)})</span>` : '';
return `<div class="group-card"><div class="p-4">
<div class="flex items-center justify-between mb-3 flex-wrap gap-2">
<div class="flex items-center gap-3">
<div class="bot-avatar"><img src="${BOT_IMAGE_URL}" onerror="this.src='https://via.placeholder.com/32?text=🤖'"></div>
<div>
<div class="flex items-center gap-2 flex-wrap"><h3 class="text-lg font-bold text-white">${escapeHtml(displayName)}</h3><span class="text-xs px-2 py-0.5 rounded-full bg-purple-500/20 text-purple-400">Lv.${level}${targetLevel>0?' / '+targetLevel:''}</span></div>
<div class="flex items-center gap-2 mt-0.5 text-xs text-gray-400 flex-wrap"><span>UID: ${escapeHtml(String(displayUid))}</span>${guestPart}<span>|</span><span>${flag} ${regionName}</span></div>
</div>
</div>
<div class="status-badge ${statusClass}"><span class="w-1.5 h-1.5 rounded-full ${statusDot} animate-pulse"></span> ${statusText}</div>
</div>
${isCompleted ? '<div class="bg-blue-500/10 border border-blue-500/30 rounded-xl p-3 mb-3"><p class="text-sm text-blue-400 font-semibold"><i class="fas fa-check-circle mr-1"></i> Target reached! Bot auto-stopped.</p></div>' : ''}
<div class="space-y-2 mb-4">
<div class="stat-item"><span class="stat-label">INITIAL EXP</span><span class="stat-value">${Number(initialExp).toLocaleString()}</span></div>
<div class="stat-item"><span class="stat-label">CURRENT EXP</span><span class="stat-value">${Number(currentExp).toLocaleString()}</span></div>
<div class="stat-item"><span class="stat-label">EXP GAINED</span><span class="stat-value text-emerald-400 font-semibold">+${Number(gainedExp).toLocaleString()}</span></div>
<div class="stat-item"><span class="stat-label">MATCHES</span><span class="stat-value">${matchesPlayed}</span></div>
<div class="stat-item"><span class="stat-label">PROGRESS</span>
<div class="flex items-center gap-2 flex-1 justify-end">
<div class="w-28 h-1.5 bg-gray-700 rounded-full overflow-hidden"><div class="h-full bg-gradient-to-r from-emerald-500 to-blue-500" style="width: ${pct.toFixed(1)}%"></div></div>
<span class="text-xs text-emerald-400 font-mono">${pct.toFixed(1)}%</span>
</div>
</div>
</div>
<div class="button-group">
${isActive ? `<button onclick="restartGroup('${group.id}', this)" class="btn-restart flex items-center gap-1"><i class="fas fa-sync-alt text-xs"></i> Restart</button><button onclick="stopGroup('${group.id}', this)" class="btn-stop flex items-center gap-1"><i class="fas fa-stop text-xs"></i> Stop</button>` : ''}
${(isStopped || isOffline) && !isCompleted ? `<button onclick="startGroup('${group.id}', this)" class="btn-restart flex items-center gap-1"><i class="fas fa-play text-xs"></i> Start</button>` : ''}
<button onclick="deleteGroup('${group.id}', this)" class="btn-delete flex items-center gap-1"><i class="fas fa-trash text-xs"></i> Delete</button>
</div>
</div></div>`;
};
const html = groups.map(renderCard).join('');
if (groupsList) groupsList.innerHTML = html;
if (groupsFull) groupsFull.innerHTML = html;
} catch (e) { console.error('Failed to load groups:', e); }
}

async function refreshGroups() { await loadGroups(); await loadPendingOrders(); showToast('Refreshed', 'success'); }

async function restartGroup(groupId, button) {
if (!confirm('Restart this bot?')) return;
const orig = button.innerHTML; setButtonLoading(button, true);
try {
const res = await fetch(API.groupAction, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ group_id: parseInt(groupId), action: 'restart' }) });
const data = await res.json();
if (res.ok && data.success) { showToast('✅ Bot restarted!', 'success'); await loadGroups(); }
else { showToast(data.error || 'Failed', 'error'); }
} catch (e) { showToast('Network error', 'error'); }
finally { setButtonLoading(button, false, orig); }
}

async function startGroup(groupId, button) {
if (!confirm('Start this bot?')) return;
const orig = button.innerHTML; setButtonLoading(button, true);
try {
const res = await fetch(API.groupAction, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ group_id: parseInt(groupId), action: 'start' }) });
const data = await res.json();
if (res.ok && data.success) { showToast('✅ Bot started!', 'success'); await loadGroups(); }
else { showToast(data.error || 'Failed', 'error'); }
} catch (e) { showToast('Network error', 'error'); }
finally { setButtonLoading(button, false, orig); }
}

async function stopGroup(groupId, button) {
if (!confirm('Stop this bot?')) return;
const orig = button.innerHTML; setButtonLoading(button, true);
try {
const res = await fetch(API.groupAction, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ group_id: parseInt(groupId), action: 'stop' }) });
const data = await res.json();
if (res.ok && data.success) { showToast('✅ Bot stopped!', 'success'); await loadGroups(); await refreshUserCredits(); }
else { showToast(data.error || 'Failed', 'error'); }
} catch (e) { showToast('Network error', 'error'); }
finally { setButtonLoading(button, false, orig); }
}

async function deleteGroup(groupId, button) {
if (!confirm('Delete this bot? Credits will NOT be refunded.')) return;
const orig = button.innerHTML; setButtonLoading(button, true);
try {
const res = await fetch(API.groupAction, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ group_id: parseInt(groupId), action: 'delete' }) });
if (res.ok) { showToast('Bot deleted', 'success'); await loadGroups(); await refreshUserCredits(); }
else { showToast('Failed to delete', 'error'); }
} catch (e) { showToast('Network error', 'error'); }
finally { setButtonLoading(button, false, orig); }
}

async function loadTransactions() {
try {
const res = await fetch(API.transactions);
const txs = await res.json();
const container = document.getElementById('transactions-list');
if (!txs || txs.length === 0) { container.innerHTML = `<div class="text-center py-8 text-gray-500"><i class="fas fa-receipt text-4xl mb-2 opacity-50"></i><p>No transactions</p></div>`; return; }
container.innerHTML = txs.map(tx => { const statusClass = tx.status === 'completed' ? 'bg-green-500/20 text-green-400' : tx.status === 'pending' ? 'bg-amber-500/20 text-amber-400' : 'bg-red-500/20 text-red-400'; const date = new Date(tx.created_at).toLocaleString(); return `<div class="bg-[#1e2130] rounded-xl p-3 flex flex-col sm:flex-row sm:items-center justify-between gap-2"><div><p class="font-mono text-xs text-gray-500">${tx.id}</p><p class="text-sm">$${tx.amount?.toFixed(2) || '0.00'} → ${tx.basic_credits || 0} Credits</p><p class="text-xs text-gray-500">${date}</p></div><span class="text-xs px-2 py-1 rounded-full ${statusClass}">${tx.status}</span></div>`; }).join('');
} catch (e) { console.error(e); }
}

function adjustCredits(type, delta) { const input = document.getElementById(`coupon-${type}`); let val = parseInt(input.value) || 0; const max = 100; val = Math.max(0, Math.min(max, val + delta)); input.value = val; }

async function createCoupon() {
const basic = parseInt(document.getElementById('coupon-basic').value) || 0;
if (basic === 0) { showToast('Select at least 1 credit', 'error'); return; }
const btn = event.target;
const orig = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<span class="loader"></span>';
try {
const res = await fetch(API.createCoupon, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ basic_credits: basic, premium_credits: 0 }) });
const data = await res.json();
if (res.ok && data.success) { showToast(`Code created: ${data.coupon.code}`, 'success'); document.getElementById('coupon-basic').value = 1; await loadCoupons(); await refreshUserCredits(); }
else { showToast(data.error || 'Creation failed', 'error'); }
} catch (e) { showToast('Network error', 'error'); }
finally { btn.disabled = false; btn.innerHTML = orig; }
}

async function redeemCoupon() {
const input = document.getElementById('coupon-code');
const code = (input?.value || '').trim();
const btn = event?.target?.closest('button');
if (!code) { showToast('Enter a code', 'error'); return; }
const orig = btn ? btn.innerHTML : '';
if (btn) { btn.disabled = true; btn.innerHTML = '<span class="loader"></span>'; }
try {
const res = await fetch(API.redeemCoupon, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: code }) });
const data = await res.json();
if (res.ok && data.success) {
const total = (data.credits != null) ? data.credits : ((data.basic_credits || 0) + (data.premium_credits || 0));
showToast(`✅ Redeemed! +${total} Credit`, 'success');
if (input) input.value = '';
await refreshUserCredits();
await loadCoupons();
} else { showToast(data.error || 'Invalid code', 'error'); }
} catch (e) { showToast('Network error', 'error'); }
finally { if (btn) { btn.disabled = false; btn.innerHTML = orig || '<i class="fas fa-check"></i> Redeem'; } }
}

async function loadCoupons() {
try {
const res = await fetch(API.myCoupons);
const coupons = await res.json();
const container = document.getElementById('my-coupons-list');
if (!container) return;
if (!coupons || coupons.length === 0) { container.innerHTML = `<div class="text-center py-6 text-gray-500"><i class="fas fa-ticket-alt text-3xl mb-2 opacity-50"></i><p>No codes created</p></div>`; return; }
container.innerHTML = coupons.map(c => {
const total = (c.basic_credits || 0) + (c.premium_credits || 0);
return `<div class="bg-[#1e2130] rounded-xl p-3 flex flex-col sm:flex-row sm:items-center justify-between gap-2"><div><code class="font-mono text-sm text-blue-400">${c.code}</code><p class="text-xs text-gray-500 mt-1">${total} Credit</p></div><span class="text-xs px-2 py-1 rounded-full ${c.status === 'active' ? 'bg-green-500/20 text-green-400' : 'bg-gray-500/20 text-gray-400'}">${c.status}</span></div>`;
}).join('');
} catch (e) { console.error(e); }
}

async function changePassword() {
const current = document.getElementById('current-pw').value;
const newPw = document.getElementById('new-pw').value;
const confirm = document.getElementById('confirm-pw').value;
const msgBox = document.getElementById('pw-message');
if (!current || !newPw || !confirm) { msgBox.className = 'text-sm text-red-400 mt-2'; msgBox.innerHTML = 'All fields required'; msgBox.classList.remove('hidden'); return; }
if (newPw.length < 6) { msgBox.className = 'text-sm text-red-400 mt-2'; msgBox.innerHTML = 'Password must be 6+ characters'; msgBox.classList.remove('hidden'); return; }
if (newPw !== confirm) { msgBox.className = 'text-sm text-red-400 mt-2'; msgBox.innerHTML = 'Passwords do not match'; msgBox.classList.remove('hidden'); return; }
try {
const res = await fetch(API.changePassword, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ current_password: current, new_password: newPw }) });
const data = await res.json();
if (res.ok) { msgBox.className = 'text-sm text-green-400 mt-2'; msgBox.innerHTML = 'Password changed!'; msgBox.classList.remove('hidden'); document.getElementById('current-pw').value = ''; document.getElementById('new-pw').value = ''; document.getElementById('confirm-pw').value = ''; setTimeout(() => msgBox.classList.add('hidden'), 3000); }
else { msgBox.className = 'text-sm text-red-400 mt-2'; msgBox.innerHTML = data.error || 'Failed'; msgBox.classList.remove('hidden'); }
} catch (e) { msgBox.className = 'text-sm text-red-400 mt-2'; msgBox.innerHTML = 'Network error'; msgBox.classList.remove('hidden'); }
}

function switchTab(tab) {
currentTab = tab;
showNormalContent();
document.querySelectorAll('.tab-content').forEach(t => t.classList.add('hidden'));
document.getElementById(`tab-${tab}`).classList.remove('hidden');
document.querySelectorAll('.nav-item').forEach(item => { item.classList.remove('nav-item-active'); item.classList.remove('text-blue-400'); item.classList.add('text-gray-400'); });
const sidebarBtn = document.getElementById(`sidebar-${tab}`);
if (sidebarBtn) { sidebarBtn.classList.add('nav-item-active'); sidebarBtn.classList.remove('text-gray-400'); }
document.querySelectorAll('.mob-nav-item').forEach(item => { item.classList.remove('text-blue-400'); item.classList.add('text-gray-400'); });
const mobBtn = document.getElementById(`mob-${tab}`);
if (mobBtn) { mobBtn.classList.remove('text-gray-400'); mobBtn.classList.add('text-blue-400'); }
if (tab === 'groups') { loadGroups(); loadPendingOrders(); }
if (tab === 'transactions') loadTransactions();
if (tab === 'coupons') loadCoupons();
}

window.switchTab = switchTab;
window.refreshGroups = refreshGroups;
window.restartGroup = restartGroup;
window.startGroup = startGroup;
window.stopGroup = stopGroup;
window.deleteGroup = deleteGroup;
window.closeConfirmModal = closeConfirmModal;
window.openCreateBotModal = openCreateBotModal;
window.closeCreateBotModal = closeCreateBotModal;
window.submitCreateBot = submitCreateBot;
window.adjustCredits = adjustCredits;
window.createCoupon = createCoupon;
window.redeemCoupon = redeemCoupon;
window.changePassword = changePassword;
window.openTelegramBuy = openTelegramBuy;
window.showBuyCreditsOnly = showBuyCreditsOnly;

(async () => {
await init();
if (!window.pollGroupsInterval) {
window.pollGroupsInterval = setInterval(() => {
if (document.hidden) return;
if (currentTab === 'dashboard' || currentTab === 'groups') loadGroups();
}, 12000);
}
})();
</script>
</body>
</html>"""

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=yes">
<title>Admin Panel - ClanBoost Pro</title>
<script src="https://cdn.tailwindcss.com/3.4.17"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Inter', sans-serif; background: #0a0c15; color: #e2e8f0; min-height: 100vh; }
:root { --primary: #3b82f6; --primary-dark: #2563eb; --primary-light: #60a5fa; --bg-dark: #0a0c15; --bg-card: #0f111a; --bg-elevated: #151821; --border-dim: #1e2130; }
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg-dark); border-radius: 10px; }
::-webkit-scrollbar-thumb { background: var(--primary); border-radius: 10px; }
.ng-card { background: var(--bg-card); border: 1px solid var(--border-dim); border-radius: 20px; transition: all 0.3s ease; }
.ng-card:hover { border-color: var(--primary); box-shadow: 0 8px 25px -10px rgba(59, 130, 246, 0.2); }
.ng-input { background: var(--bg-elevated); border: 1px solid var(--border-dim); border-radius: 12px; padding: 0.75rem 1rem; width: 100%; color: #e2e8f0; }
.ng-input:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1); }
.ng-btn-primary { background: var(--primary); border: none; border-radius: 12px; padding: 0.75rem 1.5rem; color: white; font-weight: 600; cursor: pointer; transition: all 0.3s ease; }
.ng-btn-primary:hover { background: var(--primary-dark); transform: translateY(-2px); }
.ng-btn-outline { background: transparent; border: 1px solid var(--border-dim); border-radius: 12px; padding: 0.75rem 1.5rem; color: #94a3b8; font-weight: 500; cursor: pointer; transition: all 0.3s ease; }
.ng-btn-outline:hover { border-color: var(--primary); color: var(--primary); }
.ng-btn-success { background: linear-gradient(135deg, #10b981, #059669); border: none; border-radius: 12px; padding: 0.75rem 1.5rem; color: white; font-weight: 600; cursor: pointer; }
.ng-btn-success:hover { transform: translateY(-2px); }
.ng-badge { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 50px; font-size: 0.7rem; font-weight: 500; }
.ng-badge-blue { background: rgba(59, 130, 246, 0.15); color: var(--primary-light); border: 1px solid rgba(59, 130, 246, 0.3); }
.ng-badge-green { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
.ng-badge-red { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
.ng-badge-yellow { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }
.ng-badge-purple { background: rgba(168, 85, 247, 0.15); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.3); }
.ng-badge-gray { background: rgba(107, 114, 128, 0.2); color: #9ca3af; border: 1px solid rgba(107, 114, 128, 0.35); }
.ng-section-title { display: flex; align-items: center; gap: 12px; font-size: 1.2rem; font-weight: 600; }
.ng-section-icon { width: 36px; height: 36px; background: rgba(59, 130, 246, 0.1); border-radius: 12px; display: flex; align-items: center; justify-content: center; border: 1px solid rgba(59, 130, 246, 0.2); }
.loader { width: 20px; height: 20px; border: 2px solid var(--border-dim); border-top-color: var(--primary); border-radius: 50%; animation: spin 0.8s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }
.nav-item-active { background: rgba(59, 130, 246, 0.1); color: var(--primary-light); border-left: 3px solid var(--primary); }
.toast-show { animation: toastIn 0.3s ease-out; }
@keyframes toastIn { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
.stat-card { background: linear-gradient(135deg, #0f111a, #0a0c15); border: 1px solid var(--border-dim); border-radius: 20px; padding: 1.25rem; transition: all 0.3s ease; }
.stat-card:hover { border-color: var(--primary); transform: translateY(-2px); }
.order-card { background: linear-gradient(135deg, #1a1d28 0%, #0f111a 100%); border: 1px solid #fbbf2430; border-radius: 20px; transition: all 0.3s ease; overflow: hidden; }
.bot-card { background: linear-gradient(135deg, #0f111a 0%, #0a0c15 100%); border: 1px solid #1e2130; border-radius: 20px; transition: all 0.3s ease; overflow: hidden; }
.bot-card:hover { border-color: var(--primary); }
.uid-box { font-family: 'Courier New', monospace; background: #050710; border: 1px solid #1e2130; border-radius: 8px; padding: 6px 10px; font-size: 0.75rem; color: #60a5fa; word-break: break-all; }
.pwd-box { font-family: 'Courier New', monospace; background: #050710; border: 1px solid #1e2130; border-radius: 8px; padding: 6px 10px; font-size: 0.75rem; color: #fbbf24; word-break: break-all; }
.copy-btn { color: #64748b; cursor: pointer; transition: 0.2s; }
.copy-btn:hover { color: #3b82f6; }
@media (max-width: 768px) { .stat-card { padding: 1rem; } }
</style>
</head>
<body>

<div class="flex-grow flex flex-col">
<nav class="sticky top-0 z-20 px-4 py-3 bg-[#0f111a]/95 backdrop-blur-lg border-b border-[#1e2130]">
<div class="flex items-center justify-between">
<div class="flex items-center gap-3">
<div class="w-9 h-9 rounded-xl bg-gradient-to-br from-purple-500 to-purple-600 flex items-center justify-center"><i class="fas fa-shield-alt text-white text-sm"></i></div>
<h1 class="text-lg font-bold bg-gradient-to-r from-purple-400 to-purple-300 bg-clip-text text-transparent">Admin Panel</h1>
<div class="hidden sm:flex items-center gap-1.5 px-2 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/20"><span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span><span class="text-[10px] text-emerald-400 font-medium">ADMIN</span></div>
</div>
<div class="flex items-center gap-2">
<span id="admin-badge" class="hidden sm:flex ng-badge ng-badge-blue text-xs"><i class="fas fa-user-cog mr-1"></i> <span id="admin-name">Admin</span></span>
<button id="logout-btn" class="ng-btn-outline text-xs px-3 py-1.5 flex items-center gap-1"><i class="fas fa-sign-out-alt"></i> Logout</button>
</div>
</div>
</nav>

<div class="flex-grow p-4 md:p-6 overflow-auto">
<div class="flex flex-col lg:flex-row gap-6">

<!-- DESKTOP SIDEBAR -->
<aside class="hidden lg:block w-64 flex-shrink-0">
<div class="ng-card p-2 sticky top-4">
<nav class="space-y-1">
<button onclick="switchTab('dashboard')" id="sidebar-dashboard" class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all text-gray-400 hover:text-white hover:bg-white/5"><i class="fas fa-chart-pie w-4"></i> Dashboard</button>
<button onclick="switchTab('glory')" id="sidebar-glory" class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all text-gray-400 hover:text-white hover:bg-white/5"><i class="fas fa-bolt w-4"></i> Level Up Management</button>
<button onclick="switchTab('orders')" id="sidebar-orders" class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all text-gray-400 hover:text-white hover:bg-white/5"><i class="fas fa-shopping-cart w-4"></i> Orders<span id="orders-badge" class="ml-auto ng-badge ng-badge-yellow text-[10px] hidden">0</span></button>
<button onclick="switchTab('users')" id="sidebar-users" class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all text-gray-400 hover:text-white hover:bg-white/5"><i class="fas fa-users w-4"></i> Users</button>
<button onclick="switchTab('groups')" id="sidebar-groups" class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all text-gray-400 hover:text-white hover:bg-white/5"><i class="fas fa-robot w-4"></i> Bot Groups</button>
<button onclick="switchTab('transactions')" id="sidebar-transactions" class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all text-gray-400 hover:text-white hover:bg-white/5"><i class="fas fa-receipt w-4"></i> Transactions</button>
<button onclick="switchTab('coupons')" id="sidebar-coupons" class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all text-gray-400 hover:text-white hover:bg-white/5"><i class="fas fa-ticket-alt w-4"></i> Generate Coupon</button>
</nav>
</div>
</aside>

<!-- MOBILE BOTTOM NAV -->
<div class="lg:hidden fixed bottom-0 left-0 right-0 z-50 bg-[#0f111a]/95 backdrop-blur-lg border-t border-[#1e2130]">
<div class="flex items-center justify-around px-2 py-2">
<button onclick="switchTab('dashboard')" id="mob-dashboard" class="mob-nav-item flex flex-col items-center gap-0.5 px-3 py-2 rounded-xl text-gray-400"><i class="fas fa-chart-pie text-lg"></i><span class="text-[10px]">Home</span></button>
<button onclick="switchTab('glory')" id="mob-glory" class="mob-nav-item flex flex-col items-center gap-0.5 px-3 py-2 rounded-xl text-gray-400"><i class="fas fa-bolt text-lg"></i><span class="text-[10px]">Bots</span></button>
<button onclick="switchTab('orders')" id="mob-orders" class="mob-nav-item flex flex-col items-center gap-0.5 px-3 py-2 rounded-xl text-gray-400"><i class="fas fa-shopping-cart text-lg"></i><span class="text-[10px]">Orders</span></button>
<button onclick="switchTab('users')" id="mob-users" class="mob-nav-item flex flex-col items-center gap-0.5 px-3 py-2 rounded-xl text-gray-400"><i class="fas fa-users text-lg"></i><span class="text-[10px]">Users</span></button>
<button onclick="switchTab('groups')" id="mob-groups" class="mob-nav-item flex flex-col items-center gap-0.5 px-3 py-2 rounded-xl text-gray-400"><i class="fas fa-robot text-lg"></i><span class="text-[10px]">Groups</span></button>
</div>
</div>

<div class="flex-1 min-w-0 space-y-6 pb-24 lg:pb-0">

<!-- TAB: DASHBOARD -->
<div id="tab-dashboard" class="tab-content space-y-6">
<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
<div class="stat-card"><div class="flex items-center justify-between"><div><p class="text-xs text-gray-500 uppercase">Total Users</p><p class="text-2xl font-bold text-white" id="stat-users">0</p></div><div class="w-10 h-10 rounded-xl bg-blue-500/20 flex items-center justify-center"><i class="fas fa-users text-blue-400"></i></div></div></div>
<div class="stat-card"><div class="flex items-center justify-between"><div><p class="text-xs text-gray-500 uppercase">Active Bots</p><p class="text-2xl font-bold text-white" id="stat-groups">0</p></div><div class="w-10 h-10 rounded-xl bg-emerald-500/20 flex items-center justify-center"><i class="fas fa-robot text-emerald-400"></i></div></div></div>
<div class="stat-card"><div class="flex items-center justify-between"><div><p class="text-xs text-gray-500 uppercase">Total EXP</p><p class="text-2xl font-bold text-amber-400" id="stat-glory">0</p></div><div class="w-10 h-10 rounded-xl bg-amber-500/20 flex items-center justify-center"><i class="fas fa-bolt text-amber-400"></i></div></div></div>
<div class="stat-card"><div class="flex items-center justify-between"><div><p class="text-xs text-gray-500 uppercase">Pending Orders</p><p class="text-2xl font-bold text-yellow-400" id="stat-orders">0</p></div><div class="w-10 h-10 rounded-xl bg-yellow-500/20 flex items-center justify-center"><i class="fas fa-clock text-yellow-400"></i></div></div></div>
</div>
</div>

<!-- TAB: GLORY (Level Up Management) -->
<div id="tab-glory" class="tab-content hidden space-y-6">
<div class="ng-card p-5">
<div class="ng-section-title mb-4"><div class="ng-section-icon"><i class="fas fa-bolt text-amber-400"></i></div><h3 class="text-amber-400">Active Level Up Bots</h3><button onclick="loadActiveGroups()" class="ml-auto text-blue-400 hover:text-blue-300 text-sm"><i class="fas fa-sync-alt"></i> Refresh</button></div>
<div id="active-groups-list" class="space-y-4"><div class="text-center py-8 text-gray-500"><i class="fas fa-inbox text-4xl mb-2 opacity-50"></i><p>No active bots</p></div></div>
</div>
</div>

<!-- TAB: ORDERS -->
<div id="tab-orders" class="tab-content hidden space-y-6">
<div class="ng-card p-5">
<div class="ng-section-title mb-4"><div class="ng-section-icon"><i class="fas fa-shopping-cart text-yellow-400"></i></div><h3 class="text-yellow-400">New Orders</h3><span id="orders-count-badge" class="ng-badge ng-badge-yellow text-xs hidden">0</span></div>
<div id="orders-list" class="space-y-4"><div class="text-center py-8 text-gray-500"><i class="fas fa-inbox text-4xl mb-2 opacity-50"></i><p>No pending orders</p></div></div>
</div>
</div>

<!-- TAB: USERS -->
<div id="tab-users" class="tab-content hidden space-y-6">
<div class="ng-card p-5">
<div class="ng-section-title mb-4"><div class="ng-section-icon"><i class="fas fa-users text-blue-400"></i></div><h3>All Users</h3><button onclick="loadUsers()" class="ml-auto text-blue-400 hover:text-blue-300 text-sm"><i class="fas fa-sync-alt"></i> Refresh</button></div>
<div class="overflow-x-auto"><table class="w-full text-sm"><thead class="border-b border-[#1e2130]"><tr class="text-left text-gray-500"><th class="pb-2">ID</th><th class="pb-2">Username</th><th class="pb-2">Email</th><th class="pb-2">Credits</th><th class="pb-2">Groups</th><th class="pb-2">Role</th><th class="pb-2">Actions</th></tr></thead><tbody id="users-list"></tbody></table></div>
</div>
</div>

<!-- TAB: GROUPS (Enhanced with UID + Password + Full Status) -->
<div id="tab-groups" class="tab-content hidden space-y-6">
<div class="ng-card p-5">
<div class="ng-section-title mb-4"><div class="ng-section-icon"><i class="fas fa-robot text-blue-400"></i></div><h3>All Bot Groups — Full Details</h3><button onclick="loadGroups()" class="ml-auto text-blue-400 hover:text-blue-300 text-sm"><i class="fas fa-sync-alt"></i> Refresh</button></div>
<div id="groups-list" class="space-y-4"><div class="text-center py-8 text-gray-500"><i class="fas fa-inbox text-4xl mb-2 opacity-50"></i><p>No bots found</p></div></div>
</div>
</div>

<!-- TAB: TRANSACTIONS -->
<div id="tab-transactions" class="tab-content hidden space-y-6">
<div class="ng-card p-5">
<div class="ng-section-title mb-4"><div class="ng-section-icon"><i class="fas fa-receipt text-blue-400"></i></div><h3>All Transactions</h3><button onclick="loadTransactions()" class="ml-auto text-blue-400 hover:text-blue-300 text-sm"><i class="fas fa-sync-alt"></i> Refresh</button></div>
<div class="overflow-x-auto"><table class="w-full text-sm"><thead class="border-b border-[#1e2130]"><tr class="text-left text-gray-500"><th class="pb-2">ID</th><th class="pb-2">User</th><th class="pb-2">Amount</th><th class="pb-2">Credits</th><th class="pb-2">Status</th><th class="pb-2">Actions</th></tr></thead><tbody id="transactions-list"></tbody></table></div>
</div>
</div>

<!-- TAB: COUPONS -->
<div id="tab-coupons" class="tab-content hidden space-y-6">
<div class="ng-card p-5">
<div class="ng-section-title mb-4"><div class="ng-section-icon"><i class="fas fa-ticket-alt text-blue-400"></i></div><h3>Generate Admin Coupon</h3></div>
<div class="mb-4"><label class="text-xs text-gray-400 block mb-1">Credits</label><input type="number" id="coupon-basic" value="1" min="1" max="1000" class="ng-input"></div>
<button onclick="generateAdminCoupon()" class="ng-btn-success w-full py-2.5"><i class="fas fa-plus-circle mr-1"></i> Generate Coupon Code</button>
<div id="generated-coupon" class="mt-4 hidden">
<div class="bg-[#1e2130] rounded-xl p-4 text-center">
<p class="text-xs text-gray-500 mb-1">Coupon Code Generated</p>
<code id="coupon-code-display" class="text-xl font-mono font-bold text-blue-400"></code>
<button onclick="copyCouponCode()" class="ml-2 text-blue-400 hover:text-blue-300"><i class="fas fa-copy"></i></button>
</div>
</div>
</div>
</div>

</div>
</div>
</div>
</div>

<!-- APPROVE MODAL -->
<div id="approve-modal" class="fixed inset-0 bg-black/80 hidden items-center justify-center z-50 backdrop-blur-md p-4">
<div class="ng-card p-6 max-w-md w-full">
<div class="flex justify-between items-center mb-4"><h3 class="text-xl font-bold text-yellow-400">Approve Order</h3><button onclick="closeApproveModal()" class="text-gray-400 hover:text-white text-2xl">&times;</button></div>
<div class="space-y-4">
<div><label class="text-xs text-gray-400 block mb-1">Nickname (In-Game)</label><input type="text" id="approve-guild-name" class="ng-input" placeholder="Enter nickname"></div>
<div><label class="text-xs text-gray-400 block mb-1">Real Game UID (optional)</label><input type="text" id="approve-leader-name" class="ng-input" placeholder="Real in-game UID"></div>
<button id="approve-submit-btn" class="ng-btn-success w-full py-2.5">Approve & Start Level Up</button>
</div>
</div>
</div>

<!-- UPDATE LEVEL MODAL -->
<div id="update-glory-modal" class="fixed inset-0 bg-black/80 hidden items-center justify-center z-50 backdrop-blur-md p-4">
<div class="ng-card p-6 max-w-md w-full">
<div class="flex justify-between items-center mb-4"><h3 class="text-xl font-bold text-amber-400">Update Level</h3><button onclick="closeUpdateGloryModal()" class="text-gray-400 hover:text-white text-2xl">&times;</button></div>
<div class="space-y-4">
<div><label class="text-xs text-gray-400 block mb-1">Current Level</label><p id="update-current-glory" class="text-white font-bold text-lg">0</p></div>
<div><label class="text-xs text-gray-400 block mb-1">New Level</label><input type="number" id="update-glory-amount" class="ng-input" placeholder="Enter new level"></div>
<button id="update-glory-btn" class="ng-btn-primary w-full py-2.5">Update Level</button>
</div>
</div>
</div>

<!-- EDIT CREDITS MODAL -->
<div id="edit-credits-modal" class="fixed inset-0 bg-black/70 hidden items-center justify-center z-50 backdrop-blur-sm p-4">
<div class="ng-card p-6 max-w-md w-full">
<div class="flex justify-between items-center mb-4"><h3 class="text-xl font-bold">Edit User Credits</h3><button onclick="closeEditModal()" class="text-gray-400 hover:text-white text-2xl">&times;</button></div>
<div class="space-y-4">
<div><label class="text-xs text-gray-400 block mb-1">User</label><p id="edit-username" class="text-white font-medium"></p></div>
<div><label class="text-xs text-gray-400 block mb-1">Credits</label><input type="number" id="edit-basic" class="ng-input"></div>
<button onclick="saveUserCredits()" class="ng-btn-primary w-full">Save Changes</button>
</div>
</div>
</div>

<div id="toast-container" class="fixed bottom-20 right-4 z-50 space-y-2"></div>

<script>
let currentOrderId = null, currentGroupId = null, currentEditUserId = null;

function showToast(message, type = 'info') {
const container = document.getElementById('toast-container');
const toast = document.createElement('div');
const colors = { success: 'bg-green-500/20 border-green-500/30 text-green-400', error: 'bg-red-500/20 border-red-500/30 text-red-400', warning: 'bg-amber-500/20 border-amber-500/30 text-amber-400', info: 'bg-blue-500/20 border-blue-500/30 text-blue-400' };
const icons = { success: 'fa-check-circle', error: 'fa-exclamation-circle', warning: 'fa-exclamation-triangle', info: 'fa-info-circle' };
toast.className = `flex items-center gap-3 px-4 py-3 rounded-xl border backdrop-blur-sm ${colors[type]} toast-show`;
toast.innerHTML = `<i class="fas ${icons[type]}"></i><span class="text-sm">${message}</span>`;
container.appendChild(toast);
setTimeout(() => { toast.style.opacity = '0'; toast.style.transform = 'translateX(100px)'; setTimeout(() => toast.remove(), 300); }, 4000);
}

function escapeHtml(str) { if (!str) return ''; return String(str).replace(/[&<>"']/g, function(m) { if (m === '&') return '&amp;'; if (m === '<') return '&lt;'; if (m === '>') return '&gt;'; if (m === '"') return '&quot;'; if (m === "'") return '&#39;'; return m; }); }

function copyText(text) {
navigator.clipboard.writeText(text).then(() => showToast('Copied!', 'success')).catch(() => showToast('Copy failed', 'error'));
}

async function checkAuth() {
try {
const res = await fetch('/api/auth/me');
if (!res.ok) { window.location.href = '/login'; return false; }
const user = await res.json();
if (user.role !== 'admin') { window.location.href = '/home'; return false; }
document.getElementById('admin-name').textContent = user.username;
return true;
} catch (e) { window.location.href = '/login'; return false; }
}

async function loadStats() {
try {
const res = await fetch('/api/admin/stats');
const data = await res.json();
document.getElementById('stat-users').textContent = data.total_users;
document.getElementById('stat-groups').textContent = data.active_groups;
document.getElementById('stat-glory').textContent = (data.total_glory || 0).toLocaleString();
document.getElementById('stat-orders').textContent = data.pending_orders || 0;
} catch (e) { console.error(e); }
}

// === ACTIVE GROUPS (Level Up Management tab) - shows UID + Password + Full status ===
async function loadActiveGroups() {
try {
const res = await fetch('/api/admin/levelup/groups');
const groups = await res.json();
const container = document.getElementById('active-groups-list');
if (!groups || groups.length === 0) { container.innerHTML = `<div class="text-center py-8 text-gray-500"><i class="fas fa-inbox text-4xl mb-2 opacity-50"></i><p>No active bots</p></div>`; return; }
const regionNames = { 'bd': 'Bangladesh', 'in': 'India', 'indo': 'Indonesia', 'br': 'Brazil', 'me': 'Middle East', 'ghrab': 'MENA' };
container.innerHTML = groups.map(g => {
const currentLevel = g.current_level || 1;
const targetLevel = g.target_level || 10;
const gainedExp = g.gained_exp || 0;
const matches = g.matches_played || 0;
const initialExp = g.initial_exp || 0;
const currentExp = g.current_exp || 0;
const progress = Math.min(Math.floor((currentLevel / Math.max(1, targetLevel)) * 100), 100);
const rawStatus = (g.status || 'offline').toString().toLowerCase();
let statusClass = 'ng-badge-gray';
if (rawStatus === 'running' || rawStatus === 'online') statusClass = 'ng-badge-green';
else if (rawStatus === 'completed' || rawStatus === 'complete') statusClass = 'ng-badge-blue';
else if (rawStatus === 'stopped') statusClass = 'ng-badge-red';
else if (rawStatus === 'in_match' || rawStatus === 'in-match') statusClass = 'ng-badge-purple';
else if (rawStatus === 'searching') statusClass = 'ng-badge-yellow';
return `<div class="bot-card p-4">
<div class="flex flex-col gap-3">
<div class="flex items-center justify-between flex-wrap gap-2">
<div>
<div class="flex items-center gap-2 flex-wrap"><i class="fas fa-bolt text-emerald-400"></i><span class="font-bold text-white">${escapeHtml(g.nickname || g.guest_uid)}</span><span class="ng-badge ng-badge-green text-xs">Lv.${currentLevel} / ${targetLevel}</span><span class="ng-badge ${statusClass} text-xs">${rawStatus.toUpperCase()}</span></div>
<div class="text-sm text-gray-400 mt-1">${escapeHtml(g.username || '')} · ${escapeHtml(g.email || '')} · ${regionNames[g.region] || (g.region||'').toUpperCase()}</div>
</div>
<div class="flex gap-2"><button onclick="openUpdateGloryModal(${g.id}, ${currentLevel})" class="px-3 py-1.5 rounded-lg bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 text-xs"><i class="fas fa-edit"></i> Update Level</button></div>
</div>

<!-- UID + Password + Guest UID + Game UID -->
<div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
<div>
<label class="text-[10px] text-gray-500 uppercase tracking-wide">Guest UID</label>
<div class="uid-box flex items-center justify-between gap-2 mt-1"><span>${escapeHtml(g.guest_uid || '-')}</span><i class="fas fa-copy copy-btn" onclick="copyText('${escapeHtml(g.guest_uid || '')}')"></i></div>
</div>
<div>
<label class="text-[10px] text-gray-500 uppercase tracking-wide">Guest Password</label>
<div class="pwd-box flex items-center justify-between gap-2 mt-1"><span>${escapeHtml(g.password || '-')}</span><i class="fas fa-copy copy-btn" onclick="copyText('${escapeHtml(g.password || '')}')"></i></div>
</div>
<div>
<label class="text-[10px] text-gray-500 uppercase tracking-wide">Game UID</label>
<div class="uid-box flex items-center justify-between gap-2 mt-1"><span>${escapeHtml(g.game_uid || '-')}</span><i class="fas fa-copy copy-btn" onclick="copyText('${escapeHtml(g.game_uid || '')}')"></i></div>
</div>
<div>
<label class="text-[10px] text-gray-500 uppercase tracking-wide">Nickname</label>
<div class="uid-box flex items-center justify-between gap-2 mt-1"><span>${escapeHtml(g.nickname || '-')}</span></div>
</div>
</div>

<!-- Full Status -->
<div class="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-1">
<div class="bg-[#050710] border border-[#1e2130] rounded-lg p-2 text-center"><p class="text-[10px] text-gray-500">INITIAL EXP</p><p class="text-sm text-white font-semibold">${Number(initialExp).toLocaleString()}</p></div>
<div class="bg-[#050710] border border-[#1e2130] rounded-lg p-2 text-center"><p class="text-[10px] text-gray-500">CURRENT EXP</p><p class="text-sm text-white font-semibold">${Number(currentExp).toLocaleString()}</p></div>
<div class="bg-[#050710] border border-[#1e2130] rounded-lg p-2 text-center"><p class="text-[10px] text-gray-500">GAINED</p><p class="text-sm text-emerald-400 font-semibold">+${Number(gainedExp).toLocaleString()}</p></div>
<div class="bg-[#050710] border border-[#1e2130] rounded-lg p-2 text-center"><p class="text-[10px] text-gray-500">MATCHES</p><p class="text-sm text-white font-semibold">${matches}</p></div>
</div>
<div class="flex items-center justify-between text-xs text-gray-400 mt-1"><span>Level ${currentLevel} → Target ${targetLevel}</span><span>Region: ${(g.region||'--').toUpperCase()}</span></div>
<div class="mt-1"><div class="h-2 bg-gray-700 rounded-full overflow-hidden"><div class="h-full bg-emerald-500 rounded-full" style="width: ${progress}%"></div></div></div>
<div class="text-[10px] text-gray-500 mt-1">Created: ${g.started_at ? new Date(g.started_at).toLocaleString() : '--'}</div>
</div>
</div>`;
}).join('');
} catch (e) { console.error(e); showToast('Failed to load groups', 'error'); }
}

function openUpdateGloryModal(groupId, currentLevel) {
currentGroupId = groupId;
document.getElementById('update-current-glory').textContent = currentLevel;
document.getElementById('update-glory-amount').value = currentLevel;
document.getElementById('update-glory-modal').style.display = 'flex';
}
function closeUpdateGloryModal() { document.getElementById('update-glory-modal').style.display = 'none'; currentGroupId = null; }

document.getElementById('update-glory-btn')?.addEventListener('click', async () => {
if (!currentGroupId) return;
const newLevel = parseInt(document.getElementById('update-glory-amount').value) || 1;
const btn = document.getElementById('update-glory-btn');
const orig = btn.innerHTML;
btn.disabled = true; btn.innerHTML = '<span class="loader"></span>';
try {
const res = await fetch('/api/admin/levelup/update-progress', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ group_id: currentGroupId, current_level: newLevel }) });
const data = await res.json();
if (data.success) { showToast('Level updated!', 'success'); closeUpdateGloryModal(); loadActiveGroups(); loadGroups(); loadStats(); }
else { showToast(data.error || 'Failed', 'error'); }
} catch (e) { showToast('Network error', 'error'); }
finally { btn.disabled = false; btn.innerHTML = orig; }
});

async function loadOrders() {
try {
const res = await fetch('/api/admin/levelup/orders');
const orders = await res.json();
const container = document.getElementById('orders-list');
const badge = document.getElementById('orders-count-badge');
const sidebarBadge = document.getElementById('orders-badge');
if (orders && orders.length > 0) {
badge.classList.remove('hidden');
sidebarBadge.classList.remove('hidden');
badge.textContent = orders.length;
sidebarBadge.textContent = orders.length;
const regionNames = { 'bd': 'Bangladesh', 'in': 'India', 'indo': 'Indonesia', 'br': 'Brazil', 'me': 'Middle East', 'ghrab': 'MENA' };
container.innerHTML = orders.map(order => `<div class="order-card p-4"><div class="flex flex-col md:flex-row md:items-start justify-between gap-4"><div><div class="flex items-center gap-2 mb-2"><i class="fas fa-bolt text-yellow-400"></i><span class="font-bold text-white">Guest UID: ${escapeHtml(order.guest_uid)}</span><span class="ng-badge ng-badge-blue text-xs">Target Lv${order.target_level}</span></div><div class="text-sm text-gray-400">${escapeHtml(order.username)} · ${regionNames[order.region] || order.region.toUpperCase()}</div><div class="text-xs text-gray-500 mt-1">Placed: ${new Date(order.created_at).toLocaleString()}</div></div><div class="flex gap-2"><button onclick="openApproveModal(${order.id})" class="px-4 py-2 rounded-lg bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 text-sm"><i class="fas fa-check mr-1"></i> Approve</button><button onclick="rejectOrder(${order.id})" class="px-4 py-2 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-400 text-sm"><i class="fas fa-times mr-1"></i> Reject</button></div></div></div>`).join('');
} else {
badge.classList.add('hidden');
sidebarBadge.classList.add('hidden');
container.innerHTML = `<div class="text-center py-8 text-gray-500"><i class="fas fa-inbox text-4xl mb-2 opacity-50"></i><p>No pending orders</p></div>`;
}
} catch (e) { console.error(e); }
}

function openApproveModal(orderId) {
currentOrderId = orderId;
document.getElementById('approve-guild-name').value = '';
document.getElementById('approve-leader-name').value = '';
document.getElementById('approve-modal').style.display = 'flex';
}
function closeApproveModal() { document.getElementById('approve-modal').style.display = 'none'; currentOrderId = null; }

document.getElementById('approve-submit-btn')?.addEventListener('click', async () => {
if (!currentOrderId) return;
const nickname = document.getElementById('approve-guild-name').value;
const gameUid = document.getElementById('approve-leader-name').value;
const btn = document.getElementById('approve-submit-btn');
const orig = btn.innerHTML;
btn.disabled = true; btn.innerHTML = '<span class="loader"></span> Processing...';
try {
const res = await fetch('/api/admin/levelup/approve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ order_id: currentOrderId, nickname: nickname, game_uid: gameUid }) });
const data = await res.json();
if (res.ok && data.success) { showToast('✅ Order approved!', 'success'); closeApproveModal(); loadOrders(); loadStats(); loadGroups(); loadActiveGroups(); }
else { showToast(data.error || 'Failed', 'error'); }
} catch (e) { showToast('Network error', 'error'); }
finally { btn.disabled = false; btn.innerHTML = orig; }
});

async function rejectOrder(orderId) {
if (!confirm('Reject this order? Credit will be refunded.')) return;
try {
const res = await fetch('/api/admin/levelup/reject', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ order_id: orderId }) });
const data = await res.json();
if (res.ok && data.success) { showToast('Order rejected', 'warning'); loadOrders(); loadStats(); }
else { showToast(data.error || 'Failed', 'error'); }
} catch (e) { showToast('Network error', 'error'); }
}

async function loadUsers() {
try {
const res = await fetch('/api/admin/users');
const users = await res.json();
document.getElementById('users-list').innerHTML = users.map(user => `<tr class="border-b border-[#1e2130]"><td class="py-2">${user.id}</td><td class="py-2 font-medium">${escapeHtml(user.username)}</td><td class="py-2 text-gray-400">${escapeHtml(user.email)}</td><td class="py-2 text-blue-400">${(user.basic_credits||0) + (user.premium_credits||0)}</td><td class="py-2">${user.group_count}</td><td class="py-2"><span class="ng-badge ${user.role === 'admin' ? 'ng-badge-yellow' : 'ng-badge-blue'}">${user.role}</span></td><td class="py-2"><button onclick="openEditModal(${user.id}, '${escapeHtml(user.username)}', ${(user.basic_credits||0) + (user.premium_credits||0)})" class="text-blue-400 hover:text-blue-300"><i class="fas fa-edit"></i></button></td></tr>`).join('');
} catch (e) { console.error(e); }
}

// === BOT GROUPS (full details: UID + Password + Status) ===
async function loadGroups() {
try {
const res = await fetch('/api/admin/levelup/groups');
const groups = await res.json();
const container = document.getElementById('groups-list');
if (!groups || groups.length === 0) { container.innerHTML = `<div class="text-center py-8 text-gray-500"><i class="fas fa-inbox text-4xl mb-2 opacity-50"></i><p>No bots found</p></div>`; return; }
const regionNames = { 'bd': 'Bangladesh', 'in': 'India', 'indo': 'Indonesia', 'br': 'Brazil', 'me': 'Middle East', 'ghrab': 'MENA' };
container.innerHTML = groups.map(g => {
const currentLevel = g.current_level || 1;
const targetLevel = g.target_level || 10;
const gainedExp = g.gained_exp || 0;
const matches = g.matches_played || 0;
const initialExp = g.initial_exp || 0;
const currentExp = g.current_exp || 0;
const rawStatus = (g.status || 'offline').toString().toLowerCase();
let statusClass = 'ng-badge-gray';
if (rawStatus === 'running' || rawStatus === 'online') statusClass = 'ng-badge-green';
else if (rawStatus === 'completed' || rawStatus === 'complete') statusClass = 'ng-badge-blue';
else if (rawStatus === 'stopped') statusClass = 'ng-badge-red';
else if (rawStatus === 'in_match' || rawStatus === 'in-match') statusClass = 'ng-badge-purple';
else if (rawStatus === 'searching') statusClass = 'ng-badge-yellow';
return `<div class="bot-card p-4">
<div class="flex items-center justify-between mb-3 flex-wrap gap-2">
<div class="flex items-center gap-2 flex-wrap">
<span class="font-bold text-white">#${g.id}</span>
<span class="text-sm text-gray-400">${escapeHtml(g.nickname || 'Player')}</span>
<span class="ng-badge ng-badge-green text-xs">Lv.${currentLevel}/${targetLevel}</span>
<span class="ng-badge ${statusClass} text-xs">${rawStatus.toUpperCase()}</span>
</div>
<div class="text-xs text-gray-500">${escapeHtml(g.username || '')} · ${escapeHtml(g.email || '')}</div>
</div>

<div class="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-3">
<div>
<label class="text-[10px] text-gray-500 uppercase tracking-wide">Guest UID</label>
<div class="uid-box flex items-center justify-between gap-2 mt-1"><span>${escapeHtml(g.guest_uid || '-')}</span><i class="fas fa-copy copy-btn" onclick="copyText('${escapeHtml(g.guest_uid || '')}')"></i></div>
</div>
<div>
<label class="text-[10px] text-gray-500 uppercase tracking-wide">Guest Password</label>
<div class="pwd-box flex items-center justify-between gap-2 mt-1"><span>${escapeHtml(g.password || '-')}</span><i class="fas fa-copy copy-btn" onclick="copyText('${escapeHtml(g.password || '')}')"></i></div>
</div>
<div>
<label class="text-[10px] text-gray-500 uppercase tracking-wide">Game UID</label>
<div class="uid-box flex items-center justify-between gap-2 mt-1"><span>${escapeHtml(g.game_uid || '-')}</span><i class="fas fa-copy copy-btn" onclick="copyText('${escapeHtml(g.game_uid || '')}')"></i></div>
</div>
<div>
<label class="text-[10px] text-gray-500 uppercase tracking-wide">Region</label>
<div class="uid-box flex items-center justify-between gap-2 mt-1"><span>${regionNames[g.region] || (g.region||'').toUpperCase()}</span></div>
</div>
</div>

<div class="grid grid-cols-2 sm:grid-cols-4 gap-2">
<div class="bg-[#050710] border border-[#1e2130] rounded-lg p-2 text-center"><p class="text-[10px] text-gray-500">INITIAL EXP</p><p class="text-sm text-white font-semibold">${Number(initialExp).toLocaleString()}</p></div>
<div class="bg-[#050710] border border-[#1e2130] rounded-lg p-2 text-center"><p class="text-[10px] text-gray-500">CURRENT EXP</p><p class="text-sm text-white font-semibold">${Number(currentExp).toLocaleString()}</p></div>
<div class="bg-[#050710] border border-[#1e2130] rounded-lg p-2 text-center"><p class="text-[10px] text-gray-500">GAINED</p><p class="text-sm text-emerald-400 font-semibold">+${Number(gainedExp).toLocaleString()}</p></div>
<div class="bg-[#050710] border border-[#1e2130] rounded-lg p-2 text-center"><p class="text-[10px] text-gray-500">MATCHES</p><p class="text-sm text-white font-semibold">${matches}</p></div>
</div>
<div class="text-[10px] text-gray-500 mt-2">Created: ${g.started_at ? new Date(g.started_at).toLocaleString() : '--'}</div>
</div>`;
}).join('');
} catch (e) { console.error(e); }
}

async function loadTransactions() {
try {
const res = await fetch('/api/admin/transactions');
const txs = await res.json();
document.getElementById('transactions-list').innerHTML = txs.map(tx => `<tr class="border-b border-[#1e2130]"><td class="py-2">${tx.id}</td><td class="py-2">${escapeHtml(tx.username)}</td><td class="py-2">$${tx.amount.toFixed(2)}</td><td class="py-2 text-blue-400">${tx.basic_credits}</td><td class="py-2"><span class="ng-badge ${tx.status === 'pending' ? 'ng-badge-yellow' : tx.status === 'completed' ? 'ng-badge-green' : 'ng-badge-red'}">${tx.status}</span></td><td class="py-2">${tx.status === 'pending' ? `<button onclick="approveTransaction(${tx.id})" class="text-green-400 hover:text-green-300 mr-2"><i class="fas fa-check"></i></button><button onclick="rejectTransaction(${tx.id})" class="text-red-400 hover:text-red-300"><i class="fas fa-times"></i></button>` : '-'}</td></tr>`).join('');
} catch (e) { console.error(e); }
}

async function approveTransaction(txId) {
try {
const res = await fetch('/api/admin/transaction/approve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ transaction_id: txId }) });
const data = await res.json();
if (data.success) { showToast('Transaction approved!', 'success'); loadStats(); loadTransactions(); loadUsers(); }
else { showToast(data.error || 'Failed', 'error'); }
} catch (e) { showToast('Network error', 'error'); }
}

async function rejectTransaction(txId) {
try {
const res = await fetch('/api/admin/transaction/reject', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ transaction_id: txId }) });
const data = await res.json();
if (data.success) { showToast('Transaction rejected', 'warning'); loadStats(); loadTransactions(); }
else { showToast(data.error || 'Failed', 'error'); }
} catch (e) { showToast('Network error', 'error'); }
}

function openEditModal(userId, username, credits) {
currentEditUserId = userId;
document.getElementById('edit-username').textContent = username;
document.getElementById('edit-basic').value = credits;
document.getElementById('edit-credits-modal').style.display = 'flex';
}
function closeEditModal() { document.getElementById('edit-credits-modal').style.display = 'none'; currentEditUserId = null; }

async function saveUserCredits() {
const credits = parseInt(document.getElementById('edit-basic').value) || 0;
try {
const res = await fetch('/api/admin/update-credits', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ user_id: currentEditUserId, basic_credits: credits, premium_credits: 0 }) });
const data = await res.json();
if (data.success) { showToast('Credits updated!', 'success'); closeEditModal(); loadUsers(); }
else { showToast(data.error || 'Failed', 'error'); }
} catch (e) { showToast('Network error', 'error'); }
}

async function generateAdminCoupon() {
const basic = parseInt(document.getElementById('coupon-basic').value) || 0;
if (basic === 0) { showToast('Enter at least 1 credit', 'error'); return; }
try {
const res = await fetch('/api/admin/create-coupon', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ basic_credits: basic, premium_credits: 0 }) });
const data = await res.json();
if (data.success) {
document.getElementById('coupon-code-display').textContent = data.coupon.code;
document.getElementById('generated-coupon').classList.remove('hidden');
showToast('Coupon generated!', 'success');
document.getElementById('coupon-basic').value = 1;
} else { showToast(data.error || 'Failed', 'error'); }
} catch (e) { showToast('Network error', 'error'); }
}

function copyCouponCode() {
navigator.clipboard.writeText(document.getElementById('coupon-code-display').textContent);
showToast('Copied!', 'success');
}

let currentTab = 'dashboard';
function switchTab(tab) {
currentTab = tab;
document.querySelectorAll('.tab-content').forEach(t => t.classList.add('hidden'));
document.getElementById(`tab-${tab}`).classList.remove('hidden');
document.querySelectorAll('.nav-item').forEach(item => { item.classList.remove('nav-item-active'); item.classList.remove('text-blue-400'); item.classList.add('text-gray-400'); });
const sidebarBtn = document.getElementById(`sidebar-${tab}`);
if (sidebarBtn) { sidebarBtn.classList.add('nav-item-active'); sidebarBtn.classList.remove('text-gray-400'); }
document.querySelectorAll('.mob-nav-item').forEach(item => { item.classList.remove('text-blue-400'); item.classList.add('text-gray-400'); });
const mobBtn = document.getElementById(`mob-${tab}`);
if (mobBtn) { mobBtn.classList.remove('text-gray-400'); mobBtn.classList.add('text-blue-400'); }
if (tab === 'dashboard') loadStats();
if (tab === 'glory') loadActiveGroups();
if (tab === 'orders') loadOrders();
if (tab === 'users') loadUsers();
if (tab === 'groups') loadGroups();
if (tab === 'transactions') loadTransactions();
}

document.getElementById('logout-btn')?.addEventListener('click', async () => {
await fetch('/api/auth/logout', { method: 'POST' });
window.location.href = '/login';
});

async function init() {
const isAdmin = await checkAuth();
if (!isAdmin) return;
loadStats();
switchTab('dashboard');
setInterval(() => { if (currentTab === 'glory') loadActiveGroups(); }, 15000);
setInterval(() => { if (currentTab === 'groups') loadGroups(); }, 15000);
setInterval(() => { if (currentTab === 'orders') loadOrders(); }, 15000);
setInterval(() => { loadStats(); }, 20000);
}

window.switchTab = switchTab;
window.loadUsers = loadUsers;
window.loadGroups = loadGroups;
window.loadTransactions = loadTransactions;
window.approveTransaction = approveTransaction;
window.rejectTransaction = rejectTransaction;
window.openEditModal = openEditModal;
window.closeEditModal = closeEditModal;
window.saveUserCredits = saveUserCredits;
window.generateAdminCoupon = generateAdminCoupon;
window.copyCouponCode = copyCouponCode;
window.openApproveModal = openApproveModal;
window.closeApproveModal = closeApproveModal;
window.rejectOrder = rejectOrder;
window.openUpdateGloryModal = openUpdateGloryModal;
window.closeUpdateGloryModal = closeUpdateGloryModal;
window.loadActiveGroups = loadActiveGroups;
window.copyText = copyText;

init();
</script>
</body>
</html>"""

# ==================== PERSISTENT DEVICE ====================
def get_device_for_account(acc_id):
    devices = {}
    if os.path.exists(DEVICES_FILE):
        try:
            with open(DEVICES_FILE, 'r', encoding='utf-8') as f: devices = json.load(f)
        except: pass
    k = str(acc_id)
    if k in devices: return devices[k]
    dl = [('Samsung','SM-G998B','Adreno (TM) 660','Android OS 12 / API-31'),
          ('Xiaomi','2201122G','Adreno (TM) 730','Android OS 13 / API-33'),
          ('Realme','RMX3700','Mali-G710','Android OS 14 / API-34'),
          ('OnePlus','CPH2451','Adreno (TM) 740','Android OS 13 / API-33'),
          ('OPPO','CPH2611','Adreno (TM) 720','Android OS 14 / API-34'),
          ('Vivo','V2203','Mali-G710','Android OS 12 / API-31'),
          ('Poco','M2102J20SG','Adreno (TM) 660','Android OS 13 / API-33')]
    b, m, g, o = random.choice(dl)
    nd = {'unique_device_id': f'Google|{uuid.uuid4()}', 'brand': b, 'model': m, 'gpu_renderer': g,
          'system_software': o, 'screen_width': random.choice([1080,1440,720,1280]),
          'screen_height': random.choice([2400,3200,1600,2400]), 'screen_dpi': str(random.randint(300,420)),
          'memory': random.randint(2800,6500),
          'processor_details': f'ARM64 FP ASIMD AES VMH | {random.randint(2200,3200)} | {random.randint(6,12)}',
          'client_ip': f'{random.randint(103,223)}.{random.randint(10,250)}.{random.randint(10,250)}.{random.randint(10,250)}'}
    devices[k] = nd
    try:
        with open(DEVICES_FILE, 'w', encoding='utf-8') as f: json.dump(devices, f, indent=4)
    except: pass
    return nd

# ==================== DNS ====================
_DNS_CACHE = {}
async def resolve_host_cloudflare(hostname):
    if not hostname: return hostname
    parts = hostname.split('.')
    if len(parts)==4 and all(p.isdigit() for p in parts): return hostname
    now = time.time()
    if hostname in _DNS_CACHE:
        ip, exp = _DNS_CACHE[hostname]
        if now < exp: return ip
    try:
        loop = asyncio.get_running_loop()
        info = await loop.getaddrinfo(hostname, None, family=socket.AF_INET)
        if info:
            _DNS_CACHE[hostname] = (info[0][4][0], now+300)
            return info[0][4][0]
    except: pass
    return hostname

def optimize_tcp_socket(s):
    try:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except: pass

def optimize_udp_socket(s):
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)
    except: pass

# ==================== HTTP / CRYPTO ====================
client = httpx.AsyncClient(verify=False, timeout=10.0,
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=50))

headers = {'User-Agent':'UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)',
    'Connection':'Keep-Alive','Accept-Encoding':'gzip','Content-Type':'application/x-www-form-urlencoded',
    'Expect':'100-continue','X-Unity-Version':'2018.4.12f1','X-GA-SV':'1789535859','X-GA':'v1 1','ReleaseVersion':'OB55'}

AES_KEY = b'Yg&tc%DEuh6%Zc^8'
AES_IV = b'6oyZDr22E3ychjM%'

CRC7_TABLE = bytes([0,9,18,27,36,45,54,63,72,65,90,83,108,101,126,119,25,16,11,2,61,52,47,38,81,88,67,74,117,124,103,110,50,59,32,41,22,31,4,13,122,115,104,97,94,87,76,69,43,34,57,48,15,6,29,20,99,106,113,120,71,78,85,92,100,109,118,127,64,73,82,91,44,37,62,55,8,1,26,19,125,116,111,102,89,80,75,66,53,60,39,46,17,24,3,10,86,95,68,77,114,123,96,105,30,23,12,5,58,51,40,33,79,70,93,84,107,98,121,112,7,14,21,28,35,42,49,56,65,72,83,90,101,108,119,126,9,0,27,18,45,36,63,54,88,81,74,67,124,117,110,103,16,25,2,11,52,61,38,47,115,122,97,104,87,94,69,76,59,50,41,32,31,22,13,4,106,99,120,113,78,71,92,85,34,43,48,57,6,15,20,29,37,44,55,62,1,8,19,26,109,100,127,118,73,64,91,82,60,53,46,39,24,17,10,3,116,125,102,111,80,89,66,75,23,30,5,12,51,58,33,40,95,86,77,68,123,114,105,96,14,7,28,21,42,35,56,49,70,79,84,93,98,107,112,121])

_DELTA = 0x9E3779B9
_ROUNDS = 16
_FIELD_SIZES = {0:1, 1:2, 2:2, 3:1, 4:2}
_FIELD_NAMES = {0:'sendOption', 1:'cmd', 2:'orderId', 3:'flags', 4:'length'}

def print_success(t): print(f'\033[92m[+] {t}\033[0m'); bot_state.log(t, 'success')
def print_error(t): print(f'\033[91m[-] {t}\033[0m'); bot_state.log(t, 'error')
def print_warning(t): print(f'\033[93m[!] {t}\033[0m'); bot_state.log(t, 'warning')
def print_info(t): print(f'\033[96m[i] {t}\033[0m'); bot_state.log(t, 'info')

def get_proto_field(d, key, default=None):
    if not d or not isinstance(d, dict): return default
    if key in d:
        v = d[key].get('data'); return v if v is not None else default
    if str(key) in d:
        v = d[str(key)].get('data'); return v if v is not None else default
    return default

# ==================== TOKEN CACHE ====================
_token_cache_memo = {}
_token_cache_memo_time = 0.0

def _js(o):
    if isinstance(o, (bytes, bytearray)): return {'__bytes_hex__': bytes(o).hex()}
    raise TypeError()

def _jd(o):
    if isinstance(o, dict):
        if '__bytes_hex__' in o and len(o)==1:
            try: return bytes.fromhex(o['__bytes_hex__'])
            except: return b''
        return {k:_jd(v) for k,v in o.items()}
    if isinstance(o, list): return [_jd(x) for x in o]
    return o

def _load_cache():
    global _token_cache_memo, _token_cache_memo_time
    now = time.time()
    if _token_cache_memo and (now-_token_cache_memo_time) < 5: return _token_cache_memo
    if not os.path.exists(TOKEN_CACHE_FILE): return {}
    try:
        with open(TOKEN_CACHE_FILE, 'r', encoding='utf-8') as f: c = f.read().strip()
        if not c: return {}
        d = json.loads(c)
        parsed = _jd(d)
        _token_cache_memo = parsed
        _token_cache_memo_time = now
        return parsed
    except:
        try: os.remove(TOKEN_CACHE_FILE)
        except: pass
        return {}

def _save_cache(c):
    global _token_cache_memo, _token_cache_memo_time
    try:
        tmp = TOKEN_CACHE_FILE+'.tmp'
        with open(tmp,'w',encoding='utf-8') as f: json.dump(c, f, indent=2, default=_js)
        os.replace(tmp, TOKEN_CACHE_FILE)
        _token_cache_memo = c
        _token_cache_memo_time = time.time()
    except: pass

def cache_get(uid):
    c = _load_cache(); e = c.get(str(uid))
    if not e: return None
    if time.time() - e.get('cached_at', 0) > TOKEN_CACHE_TTL:
        cache_invalidate(uid); return None
    if str(e.get('account_id','')).isdigit(): e['account_id'] = int(e['account_id'])
    if not isinstance(e.get('login_payload_data'), (bytes, bytearray)):
        cache_invalidate(uid); return None
    return e

def cache_set(uid, d):
    c = _load_cache(); e = dict(d); e['cached_at'] = time.time(); c[str(uid)] = e
    _save_cache(c)

def cache_invalidate(uid):
    c = _load_cache()
    if str(uid) in c:
        del c[str(uid)]; _save_cache(c)

# ==================== ENCRYPTION / PROTOBUF ====================
async def aes_encrypt(p, k, iv):
    return AES.new(k, AES.MODE_CBC, iv).encrypt(pad(p, AES.block_size))

async def get_playstore_version():
    loop = asyncio.get_event_loop()
    r = await loop.run_in_executor(None, lambda: play_scraper('com.dts.freefireth', lang='hi', country='id'))
    return r.get('version')

async def version_config():
    v = await get_playstore_version()
    url = f'https://version.ggwhitehawk.com/live/ver.php?version={v}&lang=hi&device=android&channel=android&appstore=googleplay&region=BD&whitelist_version=1.3.0&whitelist_sp_version=1.0.0'
    try:
        r = await client.get(url); r.raise_for_status()
        d = r.json()
        su, rv, lrv = d.get('server_url'), d.get('remote_version'), d.get('latest_release_version')
        if not su or not rv or not lrv: return None
        return lrv, rv, su
    except: return None

async def get_access_token(uid, pwd):
    url = 'https://100067.connect.garena.com/oauth/guest/token/grant'
    hdrs = {'Host':'100067.connect.garena.com','User-Agent':'Dalvik/2.1.0 (Linux; U; Android 12; SM-G998B Build/SP1A.210812.016)',
            'Content-Type':'application/x-www-form-urlencoded','Connection':'close'}
    data = {'uid':uid,'password':pwd,'response_type':'token','client_type':'2',
            'client_secret':'2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3','client_id':'100067'}
    for _ in range(5):
        try:
            r = await client.post(url, headers=hdrs, data=data)
            if r.status_code == 200:
                d = r.json()
                oid, at, pl = d.get('open_id'), d.get('access_token'), d.get('platform', 4)
                if oid and at: return oid, at, pl
            if r.status_code == 429:
                await asyncio.sleep(1); continue
        except: pass
        await asyncio.sleep(0.5)
    return None

async def parse_results(pr):
    rd = {}
    for r in pr:
        fd = {'wire_type': r.wire_type}
        if r.wire_type == 'varint': fd['data'] = r.data
        elif r.wire_type == 'string': fd['data'] = r.data
        elif r.wire_type == 'bytes': fd['data'] = r.data
        elif r.wire_type == 'length_delimited': fd['data'] = await parse_results(r.data.results)
        rd[r.field] = fd
    return rd

async def decode_protobuf(data):
    return json.dumps(await parse_results(Parser().parse(data)))

async def build_majorlogin_payload(oid, at, pl, cv, dev):
    try:
        p = thunderFF_pb2.MajorLoginReq()
        p.event_time = str(datetime.now())[:-7]
        p.game_name = 'free fire'
        p.platform_id = 1 if str(pl) in ['1','4'] else int(pl)
        p.client_version = cv
        p.client_version_code = '2019121229'
        p.system_software = dev.get('system_software', 'Android OS 12 / API-31')
        p.system_hardware = dev.get('brand', 'Handheld')
        p.device_type = dev.get('model', 'Handheld')
        p.screen_width = int(dev.get('screen_width', 1600))
        p.screen_height = int(dev.get('screen_height', 900))
        p.screen_dpi = str(dev.get('screen_dpi', '300'))
        p.processor_details = dev.get('processor_details', 'x86-64')
        p.memory = int(dev.get('memory', 5951))
        p.gpu_renderer = dev.get('gpu_renderer', 'Adreno')
        p.unique_device_id = dev.get('unique_device_id', 'Google|xxxx')
        p.client_ip = dev.get('client_ip', '1.1.1.1')
        p.telecom_operator = 'Citycell'
        p.network_operator_a = 'Citycell'
        p.network_type = 'WIFI'
        p.network_type_a = 'WIFI'
        p.cpu_type = 2
        p.cpu_architecture = '64'
        p.gpu_version = 'OpenGL ES 3.2'
        p.graphics_api = 'OpenGLES2'
        p.language = 'en'
        p.open_id = oid
        p.open_id_type = str(pl)
        p.login_open_id_type = int(pl)
        p.access_token = at
        p.login_by = 3
        p.platform_sdk_id = 2
        p.origin_platform_type = str(pl)
        p.primary_platform_type = str(pl)
        p.reg_avatar = 1
        p.channel_type = 3
        m = p.memory_available
        m.version = 55
        m.hidden_value = 81
        p.external_storage_total = 34308
        p.external_storage_available = 30777
        p.internal_storage_total = 2519
        p.internal_storage_available = 243
        p.game_disk_storage_total = 34308
        p.game_disk_storage_available = 32224
        p.external_sdcard_total_storage = 34308
        p.external_sdcard_avail_storage = 32224
        p.library_path = '/data/app/~~UKDdGuy32C5yOa0KZe_ROA==/com.dts.freefireth-UAKF1gjDbXSGfpA07JDTKQ==/lib/arm64'
        p.library_token = 'b8e0cd5e295eee42f5860d3c86e483dd|/data/app/~~UKDdGuy32C5yOa0KZe_ROA==/com.dts.freefireth-UAKF1gjDbXSGfpA07JDTKQ==/base.apk'
        p.client_using_version = '7428b253defc164018c604a1ebbfebdf'
        p.supported_astc_bitset = 4095
        p.analytics_detail = b'FwQVTgUPX1UaUllDDwcWCRBpWAUOUgsvA1snWlBaO1kFYg=='
        p.loading_time = 14582
        p.release_channel = 'android'
        p.extra_info = 'KqsHT4tDHGqm9PQ3syB24XA4N6SWy/Q/HfMFTQM+SgxmVqsgPK138ajtCFyVNW/Q7p6hxoenpRjeZ2NphiIosCZ3YDkONB5NAa+zTwNo7iabx/mj'
        p.android_engine_init_flag = 111207
        p.if_push = 1
        p.is_vpn = 0
        return await aes_encrypt(p.SerializeToString(), AES_KEY, AES_IV)
    except: return None

async def send_majorlogin(data, rv, su):
    try:
        url = f'{su}MajorLogin'
        h = headers.copy(); h['ReleaseVersion'] = rv
        r = await client.post(url, headers=h, data=data)
        if r.status_code != 200: return None
        c = r.content
        if len(c) < 40: return None
        p = thunderFF_pb2.MajorLoginRes()
        try:
            p.ParseFromString(c)
            if p.region and p.token: return p
        except: pass
        for off in range(min(128, len(c))):
            try:
                cand = thunderFF_pb2.MajorLoginRes()
                cand.ParseFromString(c[off:])
                if cand.region and cand.token: return cand
            except: pass
        p2 = thunderFF_pb2.MajorLoginRes()
        p2.ParseFromString(c)
        return p2
    except: return None

async def send_getlogin(data, base, tok, rv):
    try:
        url = f"{base.rstrip('/')}/GetLoginData"
        h = headers.copy()
        h['ReleaseVersion'] = rv
        h['Authorization'] = f'Bearer {tok}'
        h['Host'] = 'clientbp.ppmainecoonghj.com'
        r = await client.post(url, headers=h, data=data)
        if r.status_code != 200: return None
        c = r.content
        p = thunderFF_pb2.GetLoginDataRes()
        ok = False
        try:
            p.ParseFromString(c)
            if p.functional_addrs or p.informational_addrs: ok = True
        except: pass
        if not ok:
            for off in range(min(128, len(c))):
                try:
                    cand = thunderFF_pb2.GetLoginDataRes()
                    cand.ParseFromString(c[off:])
                    if cand.functional_addrs or cand.informational_addrs:
                        p = cand; break
                except: pass
        dr = {}
        try: dr = await parse_results(Parser().parse(c.hex()))
        except: pass
        return p, dr
    except: return None

async def build_tcp_startup_packet(aid, tok, st, k, iv, region='BD', typ='OnLine'):
    uh = f'{int(aid):016x}'
    th = f'{int(st):08x}'
    ep = (await aes_encrypt(tok.encode(), k, iv)).hex()
    epl = f'{len(ep)//2:08x}'
    reg = str(region).upper() if region else 'BD'
    if typ == 'OnLine':
        pre = '7119' if reg == 'BD' else ('7114' if reg == 'IND' else '7115')
        return f'{pre}{uh}{th}00000000{epl}{ep}'
    pre = '9219' if reg == 'BD' else ('9214' if reg == 'IND' else '9215')
    return f'{pre}{uh}{th}{epl}{ep}'

async def send_keep_alive(region='BD'):
    try:
        reg = str(region).upper() if region else 'BD'
        ka = '0219' if reg == 'BD' else ('0214' if reg == 'IND' else '0215')
        return bytes.fromhex(ka)
    except: return bytes.fromhex('0219')

async def start_game_lone_wolf(region, cv, w, k, iv):
    pkt = bytes.fromhex('080112800a0a010b102b3a110a044944433110aa011a064555524f50453a100a044944433210311a064555524f504540014a0801090a0b1219202758016291090a8001303838463832424630324139363736373032303130313030303030303030303030303136303030313030313530303032323246393745454530463030303030303436373632353134303030303030303030303030303030303030303030303030303030303030303030303030303066663030303030303030636163666131366410241afb02735d5e571400024a775d45414d1a041b1c001f11010449715f4243481a001e1d071c1703004b1a4066785c524570735c51486775421b5c5a4c07504042685a63610816054e19025e75196001477c015165406370195f5547404e4550640103020f1304064863754268676c755f65576e40467e5f0a417a4701026d675d6e73670b1108495a4c6a0b78470b740065645e525a057258425f584a447d4e6759440c11044e7c596d7f4b625f7d04055a47505c4e1d6b5b4107447d7201057d7f0f14084e430457674f7e517d72015172415d027473577c4d615f79535256780911030f4d5e027a797f614165067806505d53777750475e75064257076500460817014e741e7e5078487e7a7c465e7669767153497064605a7376677773550d160148037e18675966787f4c42607a645f577e7b441b460776026b18685d0b110205490060020f70676175654674706671797f41067346677c4e06585e780f15074c57047b40517075415f6364027259674b5b0166407f7340600407770a22047a5d5c52300b3a0a167305067162727516134208312e3133302e3232480350015ae90403626253513635686e556f4e36416456324b796f566c636f477776484f624e56526c4d727073504b4f43654177616848494176795556497273743752737149734a7a786b3247525268377a2f637664626d504f6a73552f79626d38547a4c69586d2f474351696d494b53486833447955726f39515152756c34545350626d6d624b7949565937545671577059455372323646572f59624578507338514f706d317372785455736c30796a434144444d4f34616a654b615753366361496c554b4963797a494e396d52516f715277687939797257476d337a644345337a6a61436f492f5a585233656f65365a42647a64677654636b6b665733356e4d4c6a6a565072564b6433523172756174394e50514150724a5546627859696c4c5a3859707336654d5447666b6649793574666a526c314d4648706b51774c6373374439656378566c41636f374e664f6d2b30654756466c4434744478706771385533595973587645384842502f70666c767a737138316a32524f4d7857437556445442492f684735625462773166456e4249725162762b636144775147696f74554e316d4c4b77734379456f4766706746614251457645672b736a764c4c78704743334c304a5344532f74526169504354553344374e6249306547516651622f5a466f4c36455630775a324d6f583932414c572f5049752f56634663584e70596b356f7966326151416a536971486a2f363276354843644f525551303578754e6171795251625653704654303137655237675255636b4966366c6f447476342b514e4a4670766d74757077707774396a5a5974437a4b56743657726d6e36785837706658456251555434684f3758a201050803108703a201050804108103a20105080510c001a20105081d10cc01a2010408161078a20105080e10af01a201020815')
    p = thunderFF_pb2.StartMatch()
    p.ParseFromString(pkt)
    if hasattr(p.main, 'region_list') and len(p.main.region_list) > 0:
        p.main.region_list[0].region = region
        if len(p.main.region_list) > 1: p.main.region_list[1].region = region
    if hasattr(p.main, 'client_version'): p.main.client_version.remote_version = cv
    pkt = p.SerializeToString()
    ep = (await aes_encrypt(pkt, k, iv)).hex()
    pl = len(ep)//2
    hl = hex(pl)[2:]
    hl = hl if len(hl) > 1 else '0' + hl
    fp = '031400' + '0' * (6 - len(hl)) + hl + ep
    w.write(bytes.fromhex(fp))
    await w.drain()

async def has_ssan_zig(n):
    z = (n << 1) & 0xFFFFFFFFFFFFFFFF
    o = bytearray()
    while z >= 0x80:
        o.append((z & 0x7F) | 0x80)
        z >>= 7
    o.append(z)
    return bytes(o)

async def uleb_encode(n):
    o = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n: b |= 0x80
        o.append(b)
        if not n: break
    return bytes(o)

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
    k0, k1, k2, k3 = (struct.unpack_from('<I', kb, o)[0] for o in (0,4,8,12))
    out = bytearray(len(padded))
    pc = bytearray(8)
    pi = bytearray(8)
    for i in range(0, len(padded), 8):
        x = bytearray(8)
        for j in range(8): x[j] = padded[i+j] ^ pc[j]
        e0, e1 = await tea_enc(struct.unpack_from('<I', x, 0)[0], struct.unpack_from('<I', x, 4)[0], k0, k1, k2, k3)
        e = bytearray(8)
        struct.pack_into('<I', e, 0, e0)
        struct.pack_into('<I', e, 4, e1)
        for j in range(8): out[i+j] = e[j] ^ pi[j]
        pc[:] = out[i:i+8]
        pi[:] = x
    return bytes(out)

async def build_padded(c):
    pl = (8 - (len(c) + 10) % 8) % 8
    return bytes([pl,0,0]) + b'\x00'*pl + c + b'\x00'*7

async def encode_header(layout, so, cmd, oid, fl, ln, k, v80):
    o = bytearray()
    for code in layout:
        v = {0:so, 1:cmd, 2:oid, 3:fl, 4:ln}[code]
        if _FIELD_SIZES[code] == 1: o.append((v & 0xFF) ^ k)
        else:
            x = ((v & 0xFFFF) ^ v80) & 0xFFFF
            o.append(x & 0xFF)
            o.append((x >> 8) & 0xFF)
    return bytes(o)

async def crc7_buff(crc, buf):
    c = crc & 0x7F
    for b in buf: c = CRC7_TABLE[((2 * (c & 0xFF)) ^ (b & 0xFF)) & 0xFF] & 0x7F
    return c & 0x7F

async def sv_frame(mk, layout, so, cmd, oid, fl, c, k, enc=True):
    kk = k[0]
    v80 = ((kk << 8) | kk) & 0xFFFF
    body = await tea_cbc_encrypt(await build_padded(c), k) if enc else c
    hdr = bytearray([mk, 0]) + await encode_header(layout, so, cmd, oid, fl, len(body), kk, v80)
    p = bytearray(hdr + body)
    p[1] = await crc7_buff(0, bytes(p[2:])) & 0x7F
    return bytes(p)

async def build_match_startup_packets(tok, udp_key, mc, aid, bv, sip='', region='BD', cv='1.132.8', cvc='2019121229', at=''):
    tok = tok.strip()
    udp_key = bytes.fromhex(udp_key)
    mc = [int(ch) for ch in str(mc).strip()]
    tj = tok[:660] if len(tok) > 660 else tok
    sj = tok[660:] if len(tok) > 660 else ''
    etj = tj.encode() if isinstance(tj, str) else tj
    esj = sj.encode() if isinstance(sj, str) else sj
    g420 = await has_ssan_zig(len(etj)) + etj
    reg = str(region).upper() if region else 'BD'
    cs = bytes.fromhex('ca0163736f7665727365612e7374726f6e67686f6c642e66726565666972656d6f62696c652e636f6d3b302e302e302e303b33342e3132362e37362e34353b33342e38372e3137372e31343b33342e38372e3137302e3233303b33352e3138352e3138332e3537000000000000010000000000000000000000000100000800000100000000000100a8a2d7bebd8d8bdf110200')
    mid = bytes.fromhex('0000000001000102030101') + await has_ssan_zig(len(reg)) + reg.encode()
    mid += bytes.fromhex('0001030003000004')
    mid += await has_ssan_zig(len(cv)) + cv.encode()
    mid += await has_ssan_zig(len(cvc)) + cvc.encode()
    mid += cs
    cip = sip.split(':')[0] if sip else '0.0.0.0'
    mid += await has_ssan_zig(len(cip)) + cip.encode()
    cat = at.strip() if at else ''
    if cat: mid += await has_ssan_zig(len(cat)) + cat.encode()
    mid += await has_ssan_zig(len(esj)) + esj
    tg = (await uleb_encode(int(aid)) + await uleb_encode(int(bv)) + await uleb_encode(1) +
          await uleb_encode(43) + await uleb_encode(int(bv)) + await uleb_encode(11) + mid)
    proc = await sv_frame(0x5E, mc, 2, 447, 0, 1, g420, udp_key)
    load = await sv_frame(0x5A, mc, 2, 448, 1, 1, tg, udp_key)
    return proc.hex(), load.hex()

async def produce_xor_key(sk):
    k = sk[0] if sk and len(sk) > 0 else 10
    return k, ((k << 8) | k) & 0xFFFF

async def parse_layout(l):
    if isinstance(l, str): return [int(ch) for ch in l.strip()]
    return list(l)

async def tea_cbc_decrypt(body, kb):
    k0, k1, k2, k3 = (struct.unpack_from('<I', kb, o)[0] for o in (0,4,8,12))
    out = bytearray(len(body))
    pi = bytearray(8)
    pc = bytearray(8)
    x = bytearray(8)
    d = bytearray(8)
    for i in range(0, len(body), 8):
        for j in range(8): x[j] = body[i+j] ^ pi[j]
        d0, d1 = await tea_dec(struct.unpack_from('<I', x, 0)[0], struct.unpack_from('<I', x, 4)[0], k0, k1, k2, k3)
        struct.pack_into('<I', d, 0, d0)
        struct.pack_into('<I', d, 4, d1)
        for j in range(8): out[i+j] = d[j] ^ pc[j]
        pc[:] = body[i:i+8]
        pi[:] = d
    return bytes(out)

async def build_hello_packet(text, k, layout):
    d = text.encode('utf-8')
    if len(d) > 25: raise ValueError(f'Text too long ({len(d)})')
    c = b'\x10\x00\x00\x00' + d + b'\x00' * (29 - 4 - len(d))
    kk, v80 = await produce_xor_key(k)
    layout = await parse_layout(layout)
    padded = await build_padded(c)
    eb = await tea_cbc_encrypt(padded, k)
    hb = await encode_header(layout, 1, 1, 0, 1, len(eb), kk, v80)
    p = bytearray([0x63, 0x00]) + hb + eb
    p[1] = await crc7_buff(0, p[2:]) & 0x7F
    return bytes(p).hex()

async def classify(f):
    cmd = f['cmd']
    mn = MESSAGE_ID_TO_NAME.get(cmd, f'UNKNOWN_{cmd}')
    if mn == 'UDP_HELLO': return 'HELLO'
    if mn == 'UDP_ACK': return 'ACK'
    if mn == 'UDP_PING': return 'PING'
    if mn == 'RUDP_JOIN_MATCH': return 'JOIN_MATCH'
    if mn.startswith('RUDP_'): return mn
    if mn.startswith('UDP_'): return mn
    return 'DATA'

async def build_packet(mk, layout, so, cmd, oid, fl, c, k, enc=True):
    kk = k[0]
    v80 = ((kk << 8) | kk) & 0xFFFF
    body = await tea_cbc_encrypt(await build_padded(c), k) if enc else c
    hdr = bytearray([mk, 0])
    for code in layout:
        v = {0:so, 1:cmd, 2:oid, 3:fl, 4:len(body)}[code]
        if _FIELD_SIZES[code] == 1: hdr.append((v & 0xFF) ^ kk)
        else:
            x = ((v & 0xFFFF) ^ v80) & 0xFFFF
            hdr.append(x & 0xFF)
            hdr.append((x >> 8) & 0xFF)
    p = bytearray(hdr + body)
    p[1] = await crc7_buff(0, bytes(p[2:])) & 0x7F
    return bytes(p)

async def layouts_from_mask(m):
    ru = [int(c) for c in str(m).strip()]
    nr = [c for c in ru if c != 2]
    return ru, nr

async def reply_for(f, k, mask, ack=0x68, ping=0x6D, hello=0x5B, style='short'):
    ru, nr = await layouts_from_mask(mask)
    t = await classify(f)
    if t == 'HELLO':
        if style == 'echo':
            c = f['content'] if f['content'] else b'\x10\x00\x00\x00'
            return t, await build_packet(hello, nr, 1, 1, None, 1, c, k)
        return t, await build_packet(ack, nr, 0, 2, None, 1, b'\x01\x00', k)
    if t == 'ACK':
        c = f['content'] if f['content'] else b'\x01\x00'
        return t, await build_packet(ack, nr, 0, 2, None, 1, c, k)
    if t == 'PING':
        c = f['content']
        cnt = c[:4] if len(c) >= 4 else c
        return t, await build_packet(ping, nr, 0, 3, None, 0, cnt + b'\x00\x00\x00', k, enc=False)
    if t == 'JOIN_MATCH':
        return t, await build_packet(ack, nr, 0, 2, None, 1, b'\x02\x00', k)
    return t, None

async def keepalive_ping(s, ip, port, kb, mask, se):
    nr = (await layouts_from_mask(mask))[1]
    pk = [0x66, 0x6D, 0x69, 0x6C, 0x6B, 0x6E, 0x6F, 0x70]
    loop = asyncio.get_event_loop()
    i = 0
    while not se.is_set():
        k = pk[i % len(pk)]
        cnt = int(time.time() * 1000) & 0xFFFFFFFF
        p = await build_packet(k, nr, 0, 3, None, 0, struct.pack('<I', cnt) + b'\x00\x00\x00', kb, enc=False)
        try: await loop.sock_sendto(s, p, (ip, port))
        except: pass
        i += 1
        try: await asyncio.wait_for(se.wait(), timeout=3.0)
        except asyncio.TimeoutError: pass

async def try_header(buf, layout, k, v80):
    off = 2
    o = {}
    for code in layout:
        sz = _FIELD_SIZES[code]
        if off + sz > len(buf): return None
        o[_FIELD_NAMES[code]] = (buf[off] ^ k) if sz == 1 else ((buf[off] | (buf[off+1] << 8)) ^ v80) & 0xFFFF
        off += sz
    o['headerLen'] = off
    return o

async def oicq_unpad(p):
    if not p or len(p) < 8: return None
    if not all(p[-1-i] == 0 for i in range(7)): return None
    pl = p[0] & 0x07
    s = 3 + pl
    e = len(p) - 7
    return p[s:e] if s < e else b''

async def decode_packet(pkt, k, mask=None):
    data = bytes(pkt) if isinstance(pkt, bytes) else bytes.fromhex(pkt)
    if len(data) < 8: return None
    kk = k[0]
    v80 = ((kk << 8) | kk) & 0xFFFF
    crc_ok = (data[1] & 0x7F) == await crc7_buff(0, data[2:])
    cands = []
    if mask:
        ru, nr = await layouts_from_mask(mask)
        layouts = [('RUDP', ru), ('nonRUDP', nr)]
    else:
        layouts = [('RUDP', list(p)) for p in itertools.permutations([0,1,2,3,4])]
        layouts += [('nonRUDP', list(p)) for p in itertools.permutations([0,1,3,4])]
    for kind, layout in layouts:
        f = await try_header(data, layout, kk, v80)
        if not f: continue
        if f['flags'] > 7 or f['sendOption'] > 7: continue
        if f['length'] != len(data) - f['headerLen']: continue
        body = data[f['headerLen']:f['headerLen']+f['length']]
        content = None
        padded = None
        if f['flags'] & 1:
            if len(body) < 8 or len(body) % 8 != 0: continue
            padded = await tea_cbc_decrypt(body, k)
            content = await oicq_unpad(padded)
            if content is None: continue
        else: content = body
        score = (1 if crc_ok else 0) + (1 if content is not None else 0)
        cands.append({'kind':kind, 'layout':layout, 'headerLen':f['headerLen'], 'msgKey':data[0],
            'cmd':f['cmd'], 'flags':f['flags'], 'sendOption':f['sendOption'],
            'orderId':f.get('orderId'), 'length':f['length'], 'content':content, 'crcOk':crc_ok,
            'padded':padded, 'score':score, 'total':len(data)})
    if not cands: return None
    cands.sort(key=lambda c: (c['kind'] in ('RUDP','nonRUDP'), c['score']), reverse=True)
    return cands[0]

# ==================== MATCH COUNTER ====================
_match_counters = {}
_match_counter_lock = asyncio.Lock()

async def _inc_match(uid):
    async with _match_counter_lock:
        _match_counters[uid] = _match_counters.get(uid, 0) + 1
        return _match_counters[uid]

async def _dec_match(uid):
    async with _match_counter_lock:
        if uid in _match_counters and _match_counters[uid] > 0: _match_counters[uid] -= 1
        return _match_counters.get(uid, 0)

async def _get_match_count(uid):
    async with _match_counter_lock: return _match_counters.get(uid, 0)

async def _get_total_match_count():
    async with _match_counter_lock: return sum(_match_counters.values())

# ==================== AUTO DELETE ====================
async def auto_delete_completed_account(card_key):
    try:
        ck = str(card_key)
        gu = None
        gid = None
        cred = bot_state.account_credentials.get(ck)
        if cred:
            if cred.get('auth_uid'): gu = str(cred['auth_uid'])
            if cred.get('account_id'): gid = str(cred.get('account_id'))
        else: gu = ck
        all_accs = load_accounts()
        new_list = []
        for a in all_accs:
            au = str(a.get('uid', '')).strip()
            ag = str(a.get('account_id', '')).strip()
            if au and (au == ck or au == gu): continue
            if gid and ag == gid: continue
            new_list.append(a)
        _save_accounts_list(new_list)
        if os.path.exists(TOKEN_CACHE_FILE):
            try:
                with open(TOKEN_CACHE_FILE, 'r', encoding='utf-8') as f: c = f.read().strip()
                cd = json.loads(c) if c else {}
                del_k = []
                for k in list(cd.keys()):
                    if k in (ck, gu, gid):
                        del_k.append(k); continue
                    e = cd[k]
                    if isinstance(e, dict):
                        if gid and str(e.get('account_id')) == gid: del_k.append(k)
                        elif gu and str(e.get('auth_uid')) == gu: del_k.append(k)
                for k in del_k: del cd[k]
                tmp = TOKEN_CACHE_FILE + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f: json.dump(cd, f, indent=2)
                os.replace(tmp, TOKEN_CACHE_FILE)
                global _token_cache_memo, _token_cache_memo_time
                _token_cache_memo = {}
                _token_cache_memo_time = 0.0
            except: pass
        for k in (ck, gu, gid):
            if not k: continue
            bot_state.accounts.pop(k, None)
            bot_state.account_credentials.pop(k, None)
            bot_state.account_states.pop(k, None)
            if k in bot_state.account_workers:
                try: bot_state.account_workers[k].cancel()
                except: pass
                bot_state.account_workers.pop(k, None)
        print_success(f'[AUTO-DELETE] {ck} removed')
    except Exception as e: print_error(f'[AUTO-DELETE] {e}')

# ==================== PLAY GAME ====================
async def play_game(sipp, thunder, sharma, udp_key, mc, aid, preg, cv, k, iv, mi, card_key=None):
    mst = time.time()
    pt = None
    sock = None
    ps = asyncio.Event()
    us = str(aid)
    sk = card_key or us
    cc = False
    try:
        ip, port = sipp.split(':')
        port = int(port)
        rip = await resolve_host_cloudflare(ip)
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        optimize_udp_socket(sock)
        sock.setblocking(False)
        ukb = bytes.fromhex(udp_key)
        hp = await build_hello_packet(f'{aid}_2585', ukb, mc)
        await loop.sock_sendto(sock, bytes.fromhex(hp), (rip, port))
        ack_state = 'waiting_for_hello_reply'
        ts = False
        ss = False
        jmr = False
        lc = False
        sl = asyncio.Lock()
        pt = asyncio.create_task(keepalive_ping(sock, rip, port, ukb, mc, ps))
        la = time.time()
        MAXI = 7.0
        print_info(f'[MATCH #{mi}] UDP started → {sipp}')

        async def send_ts():
            nonlocal ack_state, ts, ss
            if ts: return
            async with sl:
                if ts: return
                try:
                    await loop.sock_sendto(sock, bytes.fromhex(thunder), (rip, port))
                    ts = True
                    await asyncio.sleep(0.1)
                    pa = await build_packet(0x68, (await layouts_from_mask(mc))[1], 0, 2, None, 1, b'\x01\x00', ukb)
                    await loop.sock_sendto(sock, pa, (rip, port))
                    await asyncio.sleep(0.2)
                    await loop.sock_sendto(sock, bytes.fromhex(sharma), (rip, port))
                    ss = True
                    ack_state = 'thunder_sharma_sent'
                    print_success(f'[MATCH #{mi}] Thunder+Sharma sent!')
                except Exception as e: print_error(f'[MATCH #{mi}] send err: {e}')

        while not lc:
            if time.time() - mst > MAX_MATCH_DURATION: break
            try:
                resp, sa = await asyncio.wait_for(loop.sock_recvfrom(sock, 65535), timeout=1.5)
                if resp:
                    la = time.time()
                    f = await decode_packet(resp, ukb, mc)
                    if f:
                        ptype = await classify(f)
                        if f['cmd'] in (103, 107):
                            print_success(f'[MATCH #{mi}] Completed (cmd {f["cmd"]})')
                            cc = True
                            lc = True
                            continue
                        if f['cmd'] == 101:
                            try:
                                ap = await build_packet(0x68, (await layouts_from_mask(mc))[1], 0, 2, None, 1, b'\x01\x00', ukb)
                                await loop.sock_sendto(sock, ap, sa)
                            except: pass
                            continue
                        if ptype in ('ACK','PING','HELLO','JOIN_MATCH'):
                            if ptype == 'HELLO' and ack_state == 'waiting_for_hello_reply':
                                t, r = await reply_for(f, ukb, mc, style='short')
                                if r: await loop.sock_sendto(sock, r, sa)
                                ack_state = 'ack_sent_waiting'
                            elif ptype == 'ACK':
                                if ack_state == 'waiting_for_hello_reply':
                                    t, r = await reply_for(f, ukb, mc)
                                    if r: await loop.sock_sendto(sock, r, sa)
                                    ack_state = 'ready_to_send_thunder'
                                elif ack_state == 'ack_sent_waiting':
                                    ack_state = 'ready_to_send_thunder'
                                else:
                                    t, r = await reply_for(f, ukb, mc)
                                    if r: await loop.sock_sendto(sock, r, sa)
                            elif ptype == 'PING':
                                t, r = await reply_for(f, ukb, mc)
                                if r: await loop.sock_sendto(sock, r, sa)
                            elif ptype == 'JOIN_MATCH' and not jmr:
                                t, r = await reply_for(f, ukb, mc)
                                if r:
                                    await loop.sock_sendto(sock, r, sa)
                                    jmr = True
            except asyncio.TimeoutError:
                if ack_state == 'ready_to_send_thunder' and not ts:
                    await send_ts()
                elif ack_state == 'waiting_for_hello_reply':
                    if (time.time() - la) > MAXI:
                        try:
                            p = await build_hello_packet(f'{aid}_2585', ukb, mc)
                            await loop.sock_sendto(sock, bytes.fromhex(p), (rip, port))
                        except: pass
                        la = time.time()
                    if (time.time() - mst) > 25.0:
                        print_warning(f'[MATCH #{mi}] Handshake timeout')
                        break
                elif ack_state == 'thunder_sharma_sent':
                    if (time.time() - la) > MATCH_IDLE_TIMEOUT:
                        print_success(f'[MATCH #{mi}] Finished naturally')
                        cc = True
                        break
                continue
            except (BlockingIOError, OSError):
                await asyncio.sleep(0.1)
                continue
            except:
                await asyncio.sleep(0.5)
                continue
            if ack_state == 'ready_to_send_thunder' and not ts:
                await send_ts()
        return f'match #{mi} done'
    except Exception as e:
        print_error(f'[MATCH #{mi}] err: {e}')
        return f'match #{mi} err'
    finally:
        if cc:
            try: bot_state.increment_match(sk)
            except: pass
            try:
                cred = bot_state.account_credentials.get(sk)
                if cred:
                    asyncio.create_task(refresh_account_profile(cred))
                    if bot_state.is_target_reached(sk):
                        asyncio.create_task(auto_delete_completed_account(sk))
            except: pass
        ps.set()
        if pt:
            pt.cancel()
            try: await pt
            except: pass
        if sock:
            try: sock.close()
            except: pass
        rem = await _dec_match(sk)
        try:
            if sk in bot_state.accounts:
                bot_state.update_status(sk, 'IN_MATCH' if rem > 0 else 'ONLINE', rem)
        except: pass

# ==================== FUNCTIONAL LONE WOLF ====================
async def functional_lone_wolf(addrs, starter, region, cv, k, iv, aid='', adata=None, mr=10, card_key=None):
    rc = 0
    ip, port = addrs.split(':')
    pm = []
    nrc = 0
    sa = 0
    lst = 0.0
    us = str(aid)
    sk = card_key or us
    cpf = 0
    ct = starter
    ck = k
    civ = iv
    cad = adata
    try:
        while True:
            if sk not in bot_state.accounts:
                print_warning(f'[FUNC] {sk} removed → stop')
                break
            w = None
            try:
                if cad:
                    fr = None
                    if cad.get('auth_type') == 'guest' and cad.get('auth_uid'):
                        fr = cache_get(str(cad['auth_uid']))
                    if fr:
                        cad = fr
                        ck = fr['aes_ak']
                        civ = fr['iv_i']
                        ct = await build_tcp_startup_packet(fr['account_id'], fr['token'], fr['server_time'], ck, civ, region=fr.get('region', region), typ='OnLine')
                    else:
                        raise ConnectionError('Cache expired')
                rip = await resolve_host_cloudflare(ip)
                r, w = await asyncio.open_connection(rip, int(port))
                rs = w.get_extra_info('socket')
                if rs: optimize_tcp_socket(rs)
                w.write(bytes.fromhex(ct))
                await w.drain()
                try:
                    ik = await send_keep_alive(region)
                    if ik and w and not w.is_closing():
                        w.write(ik)
                        await asyncio.wait_for(w.drain(), timeout=3)
                except: pass
                print_success(f'[FUNC] TCP Connected: {us}')
                rc = 0
                nrc = 0
                lst = 0.0

                async def ssm():
                    nonlocal sa, lst
                    sa += 1
                    try:
                        await asyncio.sleep(random.uniform(0.3, 0.6))
                        await start_game_lone_wolf('BD', cv, w, ck, civ)
                        a = await _get_match_count(sk)
                        try: bot_state.update_status(sk, 'SEARCHING', a)
                        except: pass
                    except Exception as e: print_error(f'start_game_lone_wolf: {e}')
                    lst = asyncio.get_running_loop().time()

                await ssm()
                while True:
                    if sk not in bot_state.accounts: raise asyncio.CancelledError()
                    pm[:] = [m for m in pm if not m.done()]
                    ac = await _get_match_count(sk)
                    try: bot_state.update_status(sk, 'ONLINE' if ac == 0 else 'IN_MATCH', ac)
                    except: pass
                    nw = asyncio.get_running_loop().time()
                    if nw - lst >= START_MATCH_INTERVAL: await ssm()
                    try: data = await asyncio.wait_for(r.read(8192), timeout=0.5)
                    except asyncio.TimeoutError:
                        nrc += 1
                        if nrc > 80: raise ConnectionError('idle')
                        continue
                    if not data: raise ConnectionError('closed')
                    hd = data.hex()
                    pl = len(data)
                    nrc = 0
                    if hd.startswith('0300') and 10 < pl < 30: continue
                    if hd.startswith('0300') and pl >= 300:
                        try:
                            res = json.loads(await decode_protobuf(hd[10:]))
                            tok = None
                            udk = None
                            mc = None
                            sipp = None
                            maid = None
                            bv = None
                            if '42' in res and 'data' in res['42']: mc = res['42']['data']
                            if '5' in res and 'data' in res['5']:
                                r5 = res['5']['data']
                                sipp = r5.get('2', {}).get('data')
                                udk = r5.get('3', {}).get('data')
                                tok = r5.get('4', {}).get('data')
                                if '42' in r5: mc = r5['42']['data']
                            if '1' in res and 'data' in res['1']: maid = res['1']['data']
                            if '5' in res and 'data' in res['5']: bv = res['5']['data'].get('1', {}).get('data')
                            eai = maid or aid or 'BD'
                            if tok and udk and mc and sipp:
                                at = ''
                                if cad: at = cad.get('access_token', '') or ''
                                th, sh = await build_match_startup_packets(tok, udk, mc, eai, bv or 0, sip=sipp, region=region, cv=cv, at=at)
                                mi = await _inc_match(sk)
                                nm = asyncio.create_task(play_game(sipp, th, sh, udk, mc, eai, 'BD', cv, ck, civ, mi, card_key=sk))
                                pm.append(nm)
                                cpf = 0
                                try:
                                    w.close()
                                    await w.wait_closed()
                                except: pass
                                await asyncio.sleep(NEW_MATCH_DELAY)
                                rc = 0
                                break
                            else:
                                cpf += 1
                                if cpf >= MAX_CONSECUTIVE_PARSE_FAILURES: cpf = 0
                                try:
                                    w.close()
                                    await w.wait_closed()
                                except: pass
                                await asyncio.sleep(NON_MATCH_RECONNECT_DELAY)
                                break
                        except Exception as e:
                            print_error(f'[FUNC] pkt: {e}')
                            cpf += 1
                            if cpf >= MAX_CONSECUTIVE_PARSE_FAILURES: cpf = 0
                            try:
                                w.close()
                                await w.wait_closed()
                            except: pass
                            await asyncio.sleep(NON_MATCH_RECONNECT_DELAY)
                            break
                    if 30 <= pl <= 40: continue
            except asyncio.CancelledError:
                for m in pm:
                    if not m.done(): m.cancel()
                if pm: await asyncio.gather(*pm, return_exceptions=True)
                pm.clear()
                raise
            except Exception as e:
                print_error(f'[FUNC] TCP ({us}): {e}')
                pm[:] = [m for m in pm if not m.done()]
                if w:
                    try:
                        w.close()
                        await w.wait_closed()
                    except: pass
                if 'Cache expired' in str(e): break
                rc += 1
                if rc > mr:
                    rc = 0
                    await asyncio.sleep(3)
                    continue
                await asyncio.sleep(min(rc, 2))
    except asyncio.CancelledError:
        for m in pm:
            if not m.done(): m.cancel()
        if pm: await asyncio.gather(*pm, return_exceptions=True)
        pm.clear()
        raise

async def informational(addrs, starter, k, iv, region='BD', mr=3):
    rc = 0
    ip, port = addrs.split(':')
    while True:
        w = None
        pt = None
        try:
            rip = await resolve_host_cloudflare(ip)
            r, w = await asyncio.open_connection(rip, int(port))
            rs = w.get_extra_info('socket')
            if rs: optimize_tcp_socket(rs)
            w.write(bytes.fromhex(starter))
            await w.drain()
            rc = 0
            try:
                ik = await send_keep_alive(region)
                if ik and w and not w.is_closing():
                    w.write(ik)
                    await asyncio.wait_for(w.drain(), timeout=3)
            except: pass

            async def kp():
                kb = await send_keep_alive(region)
                while True:
                    await asyncio.sleep(5)
                    try:
                        if w and not w.is_closing():
                            w.write(kb)
                            await w.drain()
                    except: break
            pt = asyncio.create_task(kp())
            while True:
                d = await r.read(8192)
                if not d: raise ConnectionError('closed')
        except asyncio.CancelledError:
            if pt: pt.cancel()
            if w:
                try:
                    w.close()
                    await w.wait_closed()
                except: pass
            raise
        except:
            if pt: pt.cancel()
            if w:
                try:
                    w.close()
                    await w.wait_closed()
                except: pass
            rc += 1
            if rc > mr:
                await asyncio.sleep(3)
                rc = 0
            else:
                await asyncio.sleep(1)

# ==================== ACCOUNT PROCESSORS ====================
def _reg_cred(ad):
    try:
        ai = str(ad['account_id'])
        bot_state.account_credentials[ai] = ad
        if ad.get('auth_uid'): bot_state.account_credentials[str(ad['auth_uid'])] = ad
    except: pass

async def refresh_account_profile(ad_or_uid):
    try:
        if isinstance(ad_or_uid, str):
            uid = str(ad_or_uid)
            ad = bot_state.account_credentials.get(uid)
        else:
            ad = ad_or_uid
            uid = str(ad.get('account_id'))
        if not ad: return
        url = ad.get('server_url')
        tok = ad.get('token')
        rv = ad.get('release_version')
        pl = ad.get('login_payload_data')
        if not (url and tok and rv and pl): return
        res = await send_getlogin(pl, url, tok, rv)
        if res:
            rp, dr = res
            lvl = int(get_proto_field(dr, 6, 1))
            exp = int(get_proto_field(dr, 7, 0))
            nick = rp.nickname or get_proto_field(dr, 4, '')
            ai = str(ad['account_id'])
            ck = str(ad['auth_uid']) if ad.get('auth_uid') else ai
            if exp > 0: bot_state.update_exp(ck, exp, lvl)
            if nick and ck in bot_state.accounts: bot_state.accounts[ck]['nickname'] = nick
            try:
                with flask_app.app_context():
                    g = LevelUpGroup.query.filter_by(guest_uid=ck).first()
                    if g:
                        if (not g.initial_exp or g.initial_exp == 0) and exp > 0:
                            g.initial_exp = exp
                        g.current_level = lvl
                        g.current_exp = exp
                        g.gained_exp = max(0, exp - (g.initial_exp or 0))
                        g.nickname = nick or g.nickname
                        g.game_uid = ai
                        # sync live status into DB loosely as running while active
                        if g.status not in ('stopped', 'completed'):
                            g.status = 'running'
                        db.session.commit()
            except: pass
            if bot_state.is_target_reached(ck):
                tgt = bot_state.accounts[ck].get('target_level', 0)
                bot_state.update_status(ck, 'COMPLETE')
                print_success(f'[TARGET] {ck} reached Lv{tgt} → AUTO DELETE')
                await auto_delete_completed_account(ck)
                try:
                    with flask_app.app_context():
                        g = LevelUpGroup.query.filter_by(guest_uid=ck).first()
                        if g:
                            g.status = 'completed'
                            db.session.commit()
                except: pass
                return
    except Exception as e: print_error(f'refresh error: {e}')

async def process_account_uid_pass(uid, pwd, register_key=None):
    c = cache_get(uid)
    if c:
        ai = str(c['account_id'])
        k = register_key or uid
        tgt = 0
        for a in load_accounts():
            if str(a.get('uid')) == uid:
                tgt = int(a.get('target_level', 0) or 0); break
        bot_state.register_account(k, c.get('nickname', f'Player_{ai}'), c.get('region', 'BD'),
            c.get('level', 1), c.get('exp', 0), c.get('likes', 0), tgt, ai)
        _reg_cred(c)
        return c
    print_info(f'[LOGIN] {uid}...')
    try:
        v = await version_config()
        if not v: return None
        rv, cv, su = v
        tg = await get_access_token(uid, pwd)
        if not tg: return None
        oid, at, pl = tg
        di = get_device_for_account(uid)
        lpd = await build_majorlogin_payload(oid, at, pl, cv, di)
        if not lpd: return None
        mlr = await send_majorlogin(lpd, rv, su)
        if not mlr: return None
        glr = await send_getlogin(lpd, mlr.url, mlr.token, rv)
        if not glr: return None
        rp, dr = glr
        ai = str(mlr.account_id)
        lvl = int(get_proto_field(dr, 6, 1))
        exp = int(get_proto_field(dr, 7, 0))
        likes = int(get_proto_field(dr, 8, 0))
        nick = rp.nickname or get_proto_field(dr, 4, f'Player_{ai}')
        reg = mlr.region or get_proto_field(dr, 3, 'BD')
        tgt = 0
        for a in load_accounts():
            if str(a.get('uid')) == uid:
                tgt = int(a.get('target_level', 0) or 0); break
        k = register_key or uid
        bot_state.register_account(k, nick, reg, lvl, exp, likes, tgt, ai)
        ad = {'account_id': mlr.account_id, 'nickname': nick, 'region': reg, 'level': lvl, 'exp': exp,
              'likes': likes, 'open_id': oid, 'access_token': at, 'platform': str(pl),
              'token': mlr.token, 'server_time': mlr.server_time, 'aes_ak': mlr.aes_ak, 'iv_i': mlr.iv_i,
              'functional_addrs': rp.functional_addrs or get_proto_field(dr, 14),
              'informational_addrs': rp.informational_addrs or get_proto_field(dr, 32),
              'release_version': rv, 'client_version': cv, 'server_url': mlr.url,
              'login_payload_data': lpd, 'auth_type': 'guest', 'auth_uid': uid, 'auth_password': pwd}
        _reg_cred(ad)
        cache_set(uid, ad)
        print_success(f'[LOGIN] ✅ {uid} → {nick} (Lv{lvl})')
        return ad
    except Exception as e:
        print_error(f'login err: {e}')
        return None

async def run_account_worker(ad, label, card_key=None):
    ai = str(ad['account_id'])
    sk = card_key or ai
    it = None
    et = None
    try:
        reg = ad.get('region', 'BD')
        tpo = await build_tcp_startup_packet(ad['account_id'], ad['token'], ad['server_time'], ad['aes_ak'], ad['iv_i'], region=reg, typ='OnLine')
        tpc = await build_tcp_startup_packet(ad['account_id'], ad['token'], ad['server_time'], ad['aes_ak'], ad['iv_i'], region=reg, typ='ChaT')
        it = asyncio.create_task(informational(ad['informational_addrs'], tpc, ad['aes_ak'], ad['iv_i'], region=reg))

        async def er():
            while True:
                await asyncio.sleep(EXP_REFRESH_INTERVAL)
                if sk not in bot_state.accounts: break
                fr = bot_state.account_credentials.get(sk)
                if fr:
                    try: await refresh_account_profile(fr)
                    except: pass
                if bot_state.is_target_reached(sk): break
        et = asyncio.create_task(er())
        ft = asyncio.create_task(functional_lone_wolf(ad['functional_addrs'], tpo, ad['region'], ad['client_version'],
            ad['aes_ak'], ad['iv_i'], aid=ai, adata=ad, card_key=sk))
        await ft
    except asyncio.CancelledError: raise
    except Exception as e: print_error(f'worker {label}: {e}')
    finally:
        for t in (it, et):
            if t and not t.done(): t.cancel()
        for t in (it, et):
            if t:
                try: await t
                except: pass

async def account_loop_guest(uid, pwd, card_key=None):
    sk = card_key or uid
    while True:
        try:
            if sk not in bot_state.accounts: return
            if bot_state.account_states.get(sk) == 'stopped': return
            ad = await process_account_uid_pass(uid, pwd, register_key=sk)
            if not ad:
                await asyncio.sleep(15)
                continue
            ai = str(ad['account_id'])
            if bot_state.is_target_reached(sk):
                bot_state.update_status(sk, 'COMPLETE')
                await auto_delete_completed_account(sk)
                return
            await run_account_worker(ad, uid, card_key=sk)
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            try:
                if sk in bot_state.accounts: bot_state.update_status(sk, 'STOPPED')
            except: pass
            break
        except Exception as e:
            print_error(f'loop {uid}: {e}')
            await asyncio.sleep(10)

# ==================== ACCOUNTS LOADER ====================
def load_accounts():
    a = []
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, 'r', encoding='utf-8-sig') as f: c = f.read().strip()
            if c:
                d = json.loads(c)
                if isinstance(d, list): a = d
        except Exception as e: print_error(f'load accs: {e}')
    return a

def _save_accounts_list(a):
    try:
        tmp = ACCOUNTS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f: json.dump(a, f, indent=2)
        os.replace(tmp, ACCOUNTS_FILE)
    except Exception as e: print_error(f'save accs: {e}')

def register_placeholder_accounts(accounts):
    for acc in accounts:
        if 'uid' in acc and acc.get('uid'):
            gu = str(acc['uid']).strip()
            tl = int(acc.get('target_level', 0) or 0)
            if gu not in bot_state.accounts:
                bot_state.accounts[gu] = {'uid': gu, 'game_uid': str(acc.get('account_id', gu)),
                    'nickname': acc.get('nickname') or f'Player_{gu[-6:]}', 'region': '—',
                    'level': 0, 'initial_exp': 0, 'current_exp': 0, 'gained_exp': 0, 'likes': 0,
                    'status': 'OFFLINE', 'target_level': tl, 'matches_played': 0, 'active_matches': 0,
                    'last_match_time': None, 'last_updated': time.strftime('%H:%M:%S'), 'completed_at': None,
                    'user_id': acc.get('user_id'), 'group_id': acc.get('group_id')}

# ==================== BOT CALLBACKS ====================
async def on_start_account_handler(uid):
    us = str(uid)
    if us in bot_state.account_workers:
        t = bot_state.account_workers[us]
        if not t.done(): return True
    aa = load_accounts()
    match = None
    ck = None
    for a in aa:
        if str(a.get('uid')) == us:
            match = a; ck = us; break
        if str(a.get('account_id', '')) == us:
            match = a
            ck = str(a.get('uid')) if a.get('uid') else None
            break
    if not match:
        print_error(f'[START] {us} not found')
        return False
    if not match.get('uid'): return False
    ad = await process_account_uid_pass(match['uid'], match['password'], register_key=ck)
    if not ad: return False
    rai = str(ad['account_id'])
    if rai != ck and rai in bot_state.accounts: del bot_state.accounts[rai]
    for a in aa:
        if a.get('uid') and str(a.get('uid')) == match.get('uid'):
            a['account_id'] = rai
            if ad.get('nickname'): a['nickname'] = ad['nickname']
            break
    _save_accounts_list(aa)
    bot_state.account_credentials[ck] = ad
    bot_state.account_credentials[rai] = ad
    bot_state.account_states[ck] = 'running'
    bot_state.update_status(ck, 'ONLINE')
    task = asyncio.create_task(account_loop_guest(match['uid'], match['password'], card_key=ck))
    bot_state.account_workers[ck] = task
    return True

async def on_stop_account_handler(uid):
    us = str(uid)
    bot_state.account_states[us] = 'stopped'
    for k in list(bot_state.account_workers.keys()):
        sc = (k == us)
        if not sc:
            cr = bot_state.account_credentials.get(k)
            if cr and (str(cr.get('account_id')) == us or str(cr.get('auth_uid')) == us):
                sc = True
        if sc:
            try: bot_state.account_workers[k].cancel()
            except: pass
            del bot_state.account_workers[k]
    if us in bot_state.accounts:
        if bot_state.accounts[us].get('status') != 'COMPLETE':
            bot_state.accounts[us]['status'] = 'STOPPED'
    return True

async def on_admin_approve_handler(guest_uid, password, target_level, group_id, user_id):
    existing = load_accounts()
    existing = [a for a in existing if str(a.get('uid')) != guest_uid]
    existing.append({'uid': guest_uid, 'password': password, 'target_level': target_level,
                     'group_id': group_id, 'user_id': user_id, 'status': 'pending_start'})
    _save_accounts_list(existing)
    if guest_uid not in bot_state.accounts:
        bot_state.accounts[guest_uid] = {'uid': guest_uid, 'game_uid': guest_uid,
            'nickname': f'Player_{guest_uid[-6:]}', 'region': '—', 'level': 0, 'initial_exp': 0,
            'current_exp': 0, 'gained_exp': 0, 'likes': 0, 'status': 'OFFLINE',
            'target_level': target_level, 'matches_played': 0, 'active_matches': 0,
            'last_match_time': None, 'last_updated': time.strftime('%H:%M:%S'), 'completed_at': None,
            'user_id': user_id, 'group_id': group_id}
    await on_start_account_handler(guest_uid)

async def on_admin_delete_handler(guest_uid):
    existing = load_accounts()
    existing = [a for a in existing if str(a.get('uid')) != guest_uid]
    _save_accounts_list(existing)
    bot_state.accounts.pop(guest_uid, None)
    if guest_uid in bot_state.account_workers:
        try: bot_state.account_workers[guest_uid].cancel()
        except: pass
        del bot_state.account_workers[guest_uid]
    bot_state.account_credentials.pop(guest_uid, None)
    bot_state.account_states.pop(guest_uid, None)

# ==================== FLASK ROUTES ====================
@flask_app.route('/')
def idx():
    t = request.cookies.get('auth_token')
    if t:
        uid = verify_token(t)
        if uid:
            u = db.session.get(User, uid)
            if u:
                if u.role == 'admin': return redirect('/admin')
                return redirect('/home')
    return render_template_string(INDEX_HTML)

@flask_app.route('/login')
def login_pg():
    return render_template_string(LOGIN_HTML)

@flask_app.route('/home')
def home_pg():
    t = request.cookies.get('auth_token')
    if not t: return redirect('/login')
    uid = verify_token(t)
    if not uid: return redirect('/login')
    u = db.session.get(User, uid)
    if not u: return redirect('/login')
    if u.role == 'admin': return redirect('/admin')
    return render_template_string(HOME_HTML)

@flask_app.route('/admin')
def admin_pg():
    t = request.cookies.get('auth_token')
    if not t: return redirect('/login')
    uid = verify_token(t)
    if not uid: return redirect('/login')
    u = db.session.get(User, uid)
    if not u or u.role != 'admin': return redirect('/home')
    return render_template_string(ADMIN_HTML)

# ==================== AUTH API ====================
@flask_app.route('/api/auth/me', methods=['GET'])
@api_login_required
def api_me(user):
    return jsonify(user.to_dict()), 200

@flask_app.route('/api/auth/register', methods=['POST'])
def api_reg():
    d = request.get_json()
    un = d.get('username')
    em = d.get('email')
    pw = d.get('password')
    if not un or not em or not pw: return jsonify({'error': 'All fields required'}), 400
    if len(pw) < 6: return jsonify({'error': 'Password min 6'}), 400
    if not re.match(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$', em):
        return jsonify({'error': 'Invalid email'}), 400
    if User.query.filter_by(email=em).first(): return jsonify({'error': 'Email taken'}), 400
    if User.query.filter_by(username=un).first(): return jsonify({'error': 'Username taken'}), 400
    u = User(username=un, email=em)
    u.set_password(pw)
    db.session.add(u)
    db.session.commit()
    return jsonify({'success': True}), 200

@flask_app.route('/api/auth/login', methods=['POST'])
def api_log():
    d = request.get_json()
    em = d.get('email')
    pw = d.get('password')
    if not em or not pw: return jsonify({'error': 'Required'}), 400
    u = User.query.filter_by(email=em).first()
    if not u or not u.check_password(pw): return jsonify({'error': 'Invalid credentials'}), 401
    t = generate_token(u.id)
    r = jsonify({'success': True, 'user': u.to_dict()})
    r.set_cookie('auth_token', t, httponly=True, samesite='Lax', max_age=86400)
    return r, 200

@flask_app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    r = jsonify({'success': True})
    r.set_cookie('auth_token', '', expires=0, path='/')
    return r, 200

@flask_app.route('/api/auth/change-password', methods=['POST'])
@api_login_required
def api_chpw(user):
    d = request.get_json()
    cur = d.get('current_password')
    nw = d.get('new_password')
    if not cur or not nw: return jsonify({'error': 'All fields'}), 400
    if len(nw) < 6: return jsonify({'error': 'Min 6'}), 400
    if not user.check_password(cur): return jsonify({'error': 'Wrong password'}), 401
    user.set_password(nw)
    db.session.commit()
    return jsonify({'success': True}), 200

@flask_app.route('/api/public/regions', methods=['GET'])
def api_regions():
    return jsonify({'regions': [
        {'id': 'bd', 'region_name': 'Bangladesh', 'tier': 'credit', 'price': 1, 'enabled': True},
        {'id': 'in', 'region_name': 'India', 'tier': 'credit', 'price': 1, 'enabled': True},
        {'id': 'indo', 'region_name': 'Indonesia', 'tier': 'credit', 'price': 1, 'enabled': True},
        {'id': 'br', 'region_name': 'Brazil', 'tier': 'credit', 'price': 1, 'enabled': True},
        {'id': 'me', 'region_name': 'Middle East', 'tier': 'credit', 'price': 1, 'enabled': True},
        {'id': 'ghrab', 'region_name': 'MENA', 'tier': 'credit', 'price': 1, 'enabled': True}
    ]}), 200

# ==================== CLIENT LEVELUP API ====================
@flask_app.route('/api/client/levelup/pending', methods=['GET'])
@api_login_required
def api_lv_pending(user):
    o = LevelUpOrder.query.filter_by(user_id=user.id, status='pending').order_by(LevelUpOrder.id.desc()).all()
    return jsonify([x.to_dict() for x in o]), 200

@flask_app.route('/api/client/levelup/launch', methods=['POST'])
@api_login_required
def api_lv_launch(user):
    d = request.get_json()
    gu = (d.get('guest_uid') or '').strip()
    pw = (d.get('password') or '').strip()
    reg = (d.get('region') or '').strip()
    tl = int(d.get('target_level') or 10)
    if not gu or not pw or not reg: return jsonify({'error': 'All required'}), 400
    if tl < 2 or tl > 100: return jsonify({'error': 'Target 2-100'}), 400
    if user.total_credits() < 1:
        return jsonify({'error': 'Need 1 Credit'}), 400
    if not user.deduct_credit(1):
        return jsonify({'error': 'Need 1 Credit'}), 400
    g = LevelUpGroup(
        user_id=user.id, guest_uid=gu, password=pw,
        game_uid='', nickname=f'Player_{gu[-6:]}', region=reg,
        region_tier='credit', target_level=tl, status='running',
        started_at=datetime.utcnow()
    )
    db.session.add(g)
    db.session.commit()
    schedule_async(on_admin_approve_handler(gu, pw, tl, g.id, user.id))
    return jsonify({'success': True, 'group': g.to_dict(), 'message': 'Bot started!'}), 200

@flask_app.route('/api/client/levelup/groups', methods=['GET'])
@api_login_required
def api_lv_groups(user):
    # Read-only snapshot for UI. Does not start/stop/cancel bot tasks.
    gs = LevelUpGroup.query.filter_by(user_id=user.id).order_by(LevelUpGroup.id.desc()).all()
    out = []
    for g in gs:
        d = g.to_dict()
        live = bot_state.accounts.get(g.guest_uid)
        if not live and g.game_uid:
            live = bot_state.accounts.get(str(g.game_uid))
        if live:
            d['current_level'] = live.get('level', d['current_level'])
            d['current_exp'] = live.get('current_exp', d['current_exp'])
            # fix gained when initial was 0
            init_exp = int(d.get('initial_exp') or 0)
            live_init = int(live.get('initial_exp') or 0)
            if init_exp <= 0 and live_init > 0:
                init_exp = live_init
                d['initial_exp'] = init_exp
            elif init_exp <= 0 and int(live.get('current_exp') or 0) > 0:
                # keep DB initial if set later via refresh
                pass
            cur = int(live.get('current_exp') or d.get('current_exp') or 0)
            d['current_exp'] = cur
            if init_exp > 0:
                d['gained_exp'] = max(0, cur - init_exp)
            else:
                d['gained_exp'] = live.get('gained_exp', d.get('gained_exp') or 0)
            d['matches_played'] = live.get('matches_played', d['matches_played'])
            st = (live.get('status') or d['status'] or 'offline')
            d['status'] = str(st).lower().replace(' ', '_')
            d['nickname'] = live.get('nickname') or d['nickname']
            d['game_uid'] = live.get('game_uid') or d['game_uid']
            d['active_matches'] = live.get('active_matches', 0)
        else:
            # no live worker → offline unless DB says completed/stopped
            st = (d.get('status') or 'offline').lower()
            if st in ('running',):
                d['status'] = 'offline'
        out.append(d)
    return jsonify(out), 200

@flask_app.route('/api/client/levelup/action', methods=['POST'])
@api_login_required
def api_lv_action(user):
    d = request.get_json()
    gid = d.get('group_id')
    act = d.get('action')
    g = LevelUpGroup.query.filter_by(id=gid, user_id=user.id).first()
    if not g: return jsonify({'error': 'Group not found'}), 404
    if act == 'stop':
        g.status = 'stopped'
        db.session.commit()
        schedule_async(on_stop_account_handler(g.guest_uid))
        return jsonify({'success': True, 'message': 'Stopped'}), 200
    elif act == 'start':
        g.status = 'running'
        db.session.commit()
        schedule_async(on_start_account_handler(g.guest_uid))
        return jsonify({'success': True, 'message': 'Started'}), 200
    elif act == 'restart':
        g.status = 'running'
        g.gained_exp = 0
        g.started_at = datetime.utcnow()
        db.session.commit()
        async def _rs():
            await on_stop_account_handler(g.guest_uid)
            await asyncio.sleep(1)
            await on_start_account_handler(g.guest_uid)
        schedule_async(_rs())
        return jsonify({'success': True, 'message': 'Restarted'}), 200
    elif act == 'refund':
        if g.current_level < g.target_level:
            user.add_credit(1)
        schedule_async(on_admin_delete_handler(g.guest_uid))
        db.session.delete(g)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Refunded'}), 200
    elif act == 'delete':
        schedule_async(on_admin_delete_handler(g.guest_uid))
        db.session.delete(g)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Deleted'}), 200
    return jsonify({'error': 'Invalid'}), 400

@flask_app.route('/api/client/transactions', methods=['GET'])
@api_login_required
def api_txs(user):
    ts = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.id.desc()).all()
    return jsonify([t.to_dict() for t in ts]), 200

@flask_app.route('/api/client/buy-credits', methods=['POST'])
@api_login_required
def api_buy(user):
    d = request.get_json()
    b = d.get('basic_credits', 0)
    p = d.get('premium_credits', 0)
    oid = d.get('order_id')
    if b == 0 and p == 0: return jsonify({'error': 'Select at least 1'}), 400
    if not oid: return jsonify({'error': 'Order ID required'}), 400
    amount = (b * 1.7) + (p * 20)
    t = Transaction(user_id=user.id, amount=amount, basic_credits=b, premium_credits=p,
                    order_id=oid, status='pending')
    db.session.add(t)
    db.session.commit()
    return jsonify({'success': True, 'transaction_id': t.id}), 200

@flask_app.route('/api/client/coupons/create', methods=['POST'])
@api_login_required
def api_cc(user):
    d = request.get_json() or {}
    b = int(d.get('basic_credits', 0) or 0)
    p = int(d.get('premium_credits', 0) or 0)
    if d.get('credits') is not None:
        b = int(d.get('credits') or 0)
        p = 0
    total = b + p
    if total <= 0: return jsonify({'error': 'Select at least 1 Credit'}), 400
    if user.total_credits() < total: return jsonify({'error': f'Need {total} Credit'}), 400
    if not user.deduct_credit(total): return jsonify({'error': f'Need {total} Credit'}), 400
    code = gen_code()
    while Coupon.query.filter_by(code=code).first(): code = gen_code()
    c = Coupon(code=code, created_by=user.id, basic_credits=total, premium_credits=0, status='active')
    db.session.add(c)
    db.session.commit()
    return jsonify({'success': True, 'coupon': c.to_dict()}), 200

@flask_app.route('/api/client/coupons/redeem', methods=['POST'])
@api_login_required
def api_rc(user):
    d = request.get_json() or {}
    code = (d.get('code') or '').upper().strip()
    c = Coupon.query.filter_by(code=code, status='active').first()
    if not c: return jsonify({'error': 'Invalid or used code'}), 400
    gained = int(c.basic_credits or 0) + int(c.premium_credits or 0)
    user.add_credit(gained)
    c.status = 'used'
    c.used_by = user.id
    c.used_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True, 'message': 'Redeemed',
                    'credits': gained, 'basic_credits': gained, 'premium_credits': 0}), 200

@flask_app.route('/api/client/coupons/my', methods=['GET'])
@api_login_required
def api_mc(user):
    cs = Coupon.query.filter_by(created_by=user.id).order_by(Coupon.id.desc()).all()
    return jsonify([c.to_dict() for c in cs]), 200

@flask_app.route('/api/levelup/stats', methods=['GET'])
@api_login_required
def api_lv_stats(user):
    gs = LevelUpGroup.query.filter_by(user_id=user.id).all()
    out = []
    for g in gs:
        live = bot_state.accounts.get(g.guest_uid)
        if live:
            item = dict(live)
        else:
            item = {'uid': g.guest_uid, 'game_uid': g.game_uid or g.guest_uid,
                'nickname': g.nickname, 'region': g.region, 'level': g.current_level,
                'initial_exp': g.initial_exp, 'current_exp': g.current_exp,
                'gained_exp': g.gained_exp, 'target_level': g.target_level,
                'status': (g.status or 'offline').upper(), 'matches_played': g.matches_played,
                'active_matches': 0, 'last_match_time': None, 'completed_at': None}
        item['group_id'] = g.id
        out.append(item)
    return jsonify({
        'total_accounts': len(out),
        'total_matches': sum(x.get('matches_played', 0) for x in out),
        'total_gained_exp': sum(x.get('gained_exp', 0) for x in out),
        'accounts': out
    }), 200

# ==================== ADMIN API ====================
@flask_app.route('/api/admin/stats', methods=['GET'])
@admin_required
def admin_stats(admin):
    return jsonify({
        'total_users': User.query.count(),
        'active_groups': LevelUpGroup.query.filter_by(status='running').count(),
        'total_glory': db.session.query(db.func.sum(LevelUpGroup.gained_exp)).scalar() or 0,
        'pending_transactions': Transaction.query.filter_by(status='pending').count(),
        'pending_orders': LevelUpOrder.query.filter_by(status='pending').count()
    }), 200

@flask_app.route('/api/admin/levelup/orders', methods=['GET'])
@admin_required
def admin_lv_orders(admin):
    o = LevelUpOrder.query.filter_by(status='pending').order_by(LevelUpOrder.id.desc()).all()
    return jsonify([x.to_dict() for x in o]), 200

@flask_app.route('/api/admin/levelup/groups', methods=['GET'])
@admin_required
def admin_lv_groups(admin):
    gs = LevelUpGroup.query.order_by(LevelUpGroup.id.desc()).all()
    out = []
    for g in gs:
        d = g.to_dict()
        d['username'] = g.user.username if g.user else 'Unknown'
        d['email'] = g.user.email if g.user else 'Unknown'
        d['password'] = g.password  # UID + password admin panel-এ দেখাবে
        live = bot_state.accounts.get(g.guest_uid)
        if not live and g.game_uid:
            live = bot_state.accounts.get(str(g.game_uid))
        if live:
            d['current_level'] = live.get('level', d['current_level'])
            d['current_exp'] = live.get('current_exp', d['current_exp'])
            d['gained_exp'] = live.get('gained_exp', d['gained_exp'])
            d['matches_played'] = live.get('matches_played', d['matches_played'])
            d['status'] = (live.get('status') or d['status']).lower()
            d['nickname'] = live.get('nickname') or d['nickname']
            d['game_uid'] = live.get('game_uid') or d['game_uid']
            d['active_matches'] = live.get('active_matches', 0)
            d['region'] = live.get('region', d['region'])
        else:
            d['active_matches'] = 0
        out.append(d)
    return jsonify(out), 200

@flask_app.route('/api/admin/levelup/approve', methods=['POST'])
@admin_required
def admin_lv_approve(admin):
    d = request.get_json()
    oid = d.get('order_id')
    nick = (d.get('nickname') or '').strip()
    guid = (d.get('game_uid') or '').strip()
    o = db.session.get(LevelUpOrder, oid)
    if not o: return jsonify({'error': 'Not found'}), 404
    if o.status != 'pending': return jsonify({'error': 'Processed'}), 400
    g = LevelUpGroup(user_id=o.user_id, guest_uid=o.guest_uid, password=o.password,
        game_uid=guid, nickname=nick or f'Player_{o.guest_uid[-6:]}', region=o.region,
        region_tier=o.region_tier, target_level=o.target_level, status='running', started_at=datetime.utcnow())
    db.session.add(g)
    db.session.commit()
    o.status = 'approved'
    db.session.commit()
    schedule_async(on_admin_approve_handler(o.guest_uid, o.password, o.target_level, g.id, o.user_id))
    return jsonify({'success': True}), 200

@flask_app.route('/api/admin/levelup/reject', methods=['POST'])
@admin_required
def admin_lv_reject(admin):
    d = request.get_json()
    o = db.session.get(LevelUpOrder, d.get('order_id'))
    if not o: return jsonify({'error': 'Not found'}), 404
    if o.status != 'pending': return jsonify({'error': 'Processed'}), 400
    u = db.session.get(User, o.user_id)
    if u:
        u.add_credit(1)
    o.status = 'rejected'
    db.session.commit()
    return jsonify({'success': True}), 200

@flask_app.route('/api/admin/levelup/update-progress', methods=['POST'])
@admin_required
def admin_lv_prog(admin):
    d = request.get_json()
    g = db.session.get(LevelUpGroup, d.get('group_id'))
    if not g: return jsonify({'error': 'Not found'}), 404
    if d.get('current_level') is not None: g.current_level = int(d['current_level'])
    if d.get('current_exp') is not None:
        g.current_exp = int(d['current_exp'])
        g.gained_exp = max(0, g.current_exp - g.initial_exp)
    db.session.commit()
    return jsonify({'success': True}), 200

@flask_app.route('/api/admin/users', methods=['GET'])
@admin_required
def admin_users(admin):
    us = User.query.order_by(User.id.desc()).all()
    return jsonify([{'id': u.id, 'username': u.username, 'email': u.email,
        'basic_credits': u.basic_credits, 'premium_credits': u.premium_credits,
        'role': u.role, 'group_count': LevelUpGroup.query.filter_by(user_id=u.id).count()} for u in us]), 200

@flask_app.route('/api/admin/update-credits', methods=['POST'])
@admin_required
def admin_uc(admin):
    d = request.get_json()
    u = db.session.get(User, d.get('user_id'))
    if not u: return jsonify({'error': 'Not found'}), 404
    u.basic_credits = d.get('basic_credits', 0)
    u.premium_credits = d.get('premium_credits', 0)
    db.session.commit()
    return jsonify({'success': True}), 200

@flask_app.route('/api/admin/transactions', methods=['GET'])
@admin_required
def admin_txs(admin):
    ts = Transaction.query.order_by(Transaction.id.desc()).all()
    out = []
    for t in ts:
        u = db.session.get(User, t.user_id)
        out.append({'id': t.id, 'user_id': t.user_id, 'username': u.username if u else '?',
            'amount': t.amount, 'basic_credits': t.basic_credits, 'premium_credits': t.premium_credits,
            'order_id': t.order_id, 'status': t.status,
            'created_at': t.created_at.isoformat() if t.created_at else None})
    return jsonify(out), 200

@flask_app.route('/api/admin/transaction/approve', methods=['POST'])
@admin_required
def admin_ta(admin):
    d = request.get_json()
    t = db.session.get(Transaction, d.get('transaction_id'))
    if not t or t.status != 'pending': return jsonify({'error': 'Invalid'}), 400
    u = db.session.get(User, t.user_id)
    if not u: return jsonify({'error': 'No user'}), 404
    u.basic_credits += t.basic_credits
    u.premium_credits += t.premium_credits
    t.status = 'completed'
    db.session.commit()
    return jsonify({'success': True}), 200

@flask_app.route('/api/admin/transaction/reject', methods=['POST'])
@admin_required
def admin_tr(admin):
    d = request.get_json()
    t = db.session.get(Transaction, d.get('transaction_id'))
    if not t or t.status != 'pending': return jsonify({'error': 'Invalid'}), 400
    t.status = 'failed'
    db.session.commit()
    return jsonify({'success': True}), 200

@flask_app.route('/api/admin/create-coupon', methods=['POST'])
@admin_required
def admin_cr(admin):
    d = request.get_json()
    b = d.get('basic_credits', 0)
    p = d.get('premium_credits', 0)
    if not b and not p: return jsonify({'error': 'Select at least 1'}), 400
    code = gen_code()
    while Coupon.query.filter_by(code=code).first(): code = gen_code()
    c = Coupon(code=code, created_by=admin.id, basic_credits=b, premium_credits=p, status='active')
    db.session.add(c)
    db.session.commit()
    return jsonify({'success': True, 'coupon': c.to_dict()}), 200

# ==================== CREATE ADMIN ====================
def create_admin():
    with flask_app.app_context():
        em = 'shirena857@gmail.com'
        a = User.query.filter_by(email=em).first()
        if not a:
            a = User(username='shihab_ff_857', email=em, role='admin',
                     basic_credits=9999, premium_credits=0)
            a.set_password('shihab_ff_857')
            db.session.add(a)
            db.session.commit()
            print(f'✅ Admin: {em} / shihab_ff_857')

# ==================== MAIN ====================
async def bot_main():
    global _bot_loop
    _bot_loop = asyncio.get_running_loop()
    print('\033[96m' + '='*60 + '\033[0m')
    print('\033[92m   TEAM 84FF — Level Up Bot + Marketplace (Port 5000)\033[0m')
    print('\033[96m' + '='*60 + '\033[0m')
    bot_state.refresh_callbacks['on_start_account'] = on_start_account_handler
    bot_state.refresh_callbacks['on_stop_account'] = on_stop_account_handler
    bot_state.refresh_callbacks['on_refresh_account'] = refresh_account_profile
    accounts = load_accounts()
    register_placeholder_accounts(accounts)
    print_info(f'Loaded {len(accounts)} accounts (auto-start OFF)')
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print_warning('[STOP] Shutting down...')
        for t in list(bot_state.account_workers.values()):
            t.cancel()
        await asyncio.gather(*bot_state.account_workers.values(), return_exceptions=True)

def run_flask():
    # Web UI runs in its own thread — completely separate from asyncio bot workers.
    # HTTP polling only reads DB / bot_state snapshots; it never cancels match tasks.
    flask_app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'connect_args': {'timeout': 15, 'check_same_thread': False},
        'pool_pre_ping': True,
    }
    flask_app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False, threaded=True)

if __name__ == '__main__':
    with flask_app.app_context():
        db.create_all()
    create_admin()
    ft = threading.Thread(target=run_flask, daemon=True)
    ft.start()
    time.sleep(1.5)
    print_success(f'🌐 Web: http://localhost:{WEB_PORT}')
    try:
        asyncio.run(bot_main())
    except KeyboardInterrupt:
        print_warning('\nProgram stopped.')