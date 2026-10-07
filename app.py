# ==================== STANDARD IMPORTS ====================
import sys
import asyncio
import httpx
import random
import json
import socket
import struct
import time
import os
import uuid
import itertools
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from google_play_scraper import app as play_scraper
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from protobuf_decoder.protobuf_decoder import Parser
from message_ids import MESSAGE_ID_TO_NAME
import thunderFF_pb2

from dashboard_server import bot_state, start_web_dashboard

# ==================== CONFIGURATION ====================
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
PRIORITY_REGIONS = ["BD", "IND", "SG", "TH", "PH", "VN", "MY", "ID", "HK", "TW"]

MAX_CONSECUTIVE_PARSE_FAILURES = 5.0
NON_MATCH_RECONNECT_DELAY = 1.0

EXP_REFRESH_INTERVAL = 20.0

# 🔥 BR / LW auto switch
BR_TO_LW_LEVEL = 3
MODE_ID_BR = 1
MAP_ID_BR = 1
MODE_ID_LW = 43
MAP_ID_LW = 11

FALLBACK_UID = ""
FALLBACK_PASSWORD = ""


# ==================== DEVICE HELPER ====================
def get_device_for_account(account_identifier: str) -> dict:
    devices = {}
    if os.path.exists(DEVICES_FILE):
        try:
            with open(DEVICES_FILE, "r", encoding="utf-8") as f:
                devices = json.load(f)
        except Exception:
            pass
    acc_key = str(account_identifier)
    if acc_key in devices:
        return devices[acc_key]
    device_list = [
        ("Samsung", "SM-G998B", "Adreno (TM) 660", "Android OS 12 / API-31"),
        ("Xiaomi", "2201122G", "Adreno (TM) 730", "Android OS 13 / API-33"),
        ("Realme", "RMX3700", "Mali-G710", "Android OS 14 / API-34"),
        ("OnePlus", "CPH2451", "Adreno (TM) 740", "Android OS 13 / API-33"),
        ("OPPO", "CPH2611", "Adreno (TM) 720", "Android OS 14 / API-34"),
        ("Vivo", "V2203", "Mali-G710", "Android OS 12 / API-31"),
        ("Poco", "M2102J20SG", "Adreno (TM) 660", "Android OS 13 / API-33"),
    ]
    brand, model, gpu, os_ver = random.choice(device_list)
    new_device = {
        "unique_device_id": f"Google|{str(uuid.uuid4())}",
        "brand": brand, "model": model, "gpu_renderer": gpu,
        "system_software": os_ver,
        "screen_width": random.choice([1080, 1440, 720, 1280]),
        "screen_height": random.choice([2400, 3200, 1600, 2400]),
        "screen_dpi": str(random.randint(300, 420)),
        "memory": random.randint(2800, 6500),
        "processor_details": f"ARM64 FP ASIMD AES VMH | {random.randint(2200, 3200)} | {random.randint(6, 12)}",
        "client_ip": f"{random.randint(103, 223)}.{random.randint(10, 250)}.{random.randint(10, 250)}.{random.randint(10, 250)}"
    }
    devices[acc_key] = new_device
    try:
        with open(DEVICES_FILE, "w", encoding="utf-8") as f:
            json.dump(devices, f, indent=4)
    except Exception as e:
        print_error(f"Failed to save device: {e}")
    return new_device


# ==================== CLOUDFLARE DNS ====================
CLOUDFLARE_PRIMARY_DNS = "1.1.1.1"
CLOUDFLARE_SECONDARY_DNS = "1.0.0.1"
_DNS_CACHE: Dict[str, Tuple[str, float]] = {}
_DNS_CACHE_TTL = 300.0


async def resolve_host_cloudflare(hostname: str) -> str:
    if not hostname:
        return hostname
    parts = hostname.split('.')
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return hostname
    now = time.time()
    if hostname in _DNS_CACHE:
        ip, exp = _DNS_CACHE[hostname]
        if now < exp:
            return ip

    def _query_cloudflare(server_ip: str) -> Optional[str]:
        s = None
        try:
            tx_id = random.randint(1000, 65535)
            header = struct.pack(">HHHHHH", tx_id, 0x0100, 1, 0, 0, 0)
            qname = b"".join(bytes([len(p)]) + p.encode('ascii') for p in hostname.split('.')) + b"\x00"
            query_pkt = header + qname + struct.pack(">HH", 1, 1)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.2)
            s.sendto(query_pkt, (server_ip, 53))
            resp, _ = s.recvfrom(1024)
            if len(resp) >= 12:
                ancount = struct.unpack(">H", resp[6:8])[0]
                if ancount > 0:
                    offset = 12 + len(qname) + 4
                    for _ in range(ancount):
                        if offset >= len(resp):
                            break
                        if (resp[offset] & 0xC0) == 0xC0:
                            offset += 2
                        else:
                            while offset < len(resp) and resp[offset] != 0:
                                offset += 1 + resp[offset]
                            offset += 1
                        if offset + 10 > len(resp):
                            break
                        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", resp[offset:offset + 10])
                        offset += 10
                        if rtype == 1 and rdlen == 4 and offset + 4 <= len(resp):
                            return socket.inet_ntoa(resp[offset:offset + 4])
                        offset += rdlen
        except Exception:
            pass
        finally:
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        return None

    loop = asyncio.get_running_loop()
    ip = await loop.run_in_executor(None, _query_cloudflare, CLOUDFLARE_PRIMARY_DNS)
    if not ip:
        ip = await loop.run_in_executor(None, _query_cloudflare, CLOUDFLARE_SECONDARY_DNS)
    if not ip:
        try:
            ip_info = await loop.getaddrinfo(hostname, None, family=socket.AF_INET)
            if ip_info:
                ip = ip_info[0][4][0]
        except Exception:
            ip = hostname
    if ip:
        _DNS_CACHE[hostname] = (ip, now + _DNS_CACHE_TTL)
    return ip or hostname


def optimize_tcp_socket(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
    except Exception:
        pass


def optimize_udp_socket(sock):
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)
        if hasattr(socket, 'SIO_UDP_CONNRESET') and os.name == 'nt':
            try:
                sock.ioctl(socket.SIO_UDP_CONNRESET, False)
            except Exception:
                pass
    except Exception:
        pass


# ==================== NETWORK & CRYPTO ====================
client = httpx.AsyncClient(
    verify=False, timeout=10.0,
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=50)
)

headers = {
    'User-Agent': 'UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)',
    'Connection': 'Keep-Alive',
    'Accept-Encoding': 'gzip',
    'Content-Type': 'application/x-www-form-urlencoded',
    'Expect': '100-continue',
    'X-Unity-Version': '2018.4.12f1',
    'X-GA-SV': '1789535859',
    'X-GA': 'v1 1',
    'ReleaseVersion': 'OB55'
}

AES_KEY = b'Yg&tc%DEuh6%Zc^8'
AES_IV = b'6oyZDr22E3ychjM%'

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

sai_tail_dul = bytes.fromhex(
    "0101030101045452000103000100000410312e3133302e3232"
    "1432303139313231303430ca0163736f7665727365612e737472"
    "6f6e67686f6c642e66726565666972656d6f62696c652e636f6d"
    "3b302e302e302e303b33342e3132362e37362e34353b33342e38"
    "372e3137372e31343b33342e38372e3137302e3233303b33352e"
    "3138352e3138332e353700000000000001000000000000000000"
    "0000000100000000000100000000000100b8eeec91c5d7ffde110200"
)


class Colors:
    HEADER = '\033[95m'
    GREEN = '\033[92m'
    FAIL = '\033[91m'
    WARNING = '\033[93m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    WHITE = '\033[97m'
    ENDC = '\033[0m'


def print_colored(text, color=Colors.WHITE):
    try:
        print(f"{color}{text}{Colors.ENDC}")
    except Exception:
        try:
            print(f"{color}{text.encode('ascii', errors='replace').decode('ascii')}{Colors.ENDC}")
        except Exception:
            pass


def print_success(text):
    print_colored(f"[+] {text}", Colors.GREEN)
    try:
        bot_state.log(text, "success")
    except Exception:
        pass


def print_error(text):
    print_colored(f"[-] {text}", Colors.FAIL)
    try:
        bot_state.log(text, "error")
    except Exception:
        pass


def print_warning(text):
    print_colored(f"[!] {text}", Colors.WARNING)
    try:
        bot_state.log(text, "warning")
    except Exception:
        pass


def print_info(text):
    print_colored(f"[i] {text}", Colors.CYAN)
    try:
        bot_state.log(text, "info")
    except Exception:
        pass


def get_proto_field(d, key, default=None):
    if not d or not isinstance(d, dict):
        return default
    if key in d:
        val = d[key].get('data')
        return val if val is not None else default
    if str(key) in d:
        val = d[str(key)].get('data')
        return val if val is not None else default
    return default


def _pb_varint(n):
    if n < 0:
        n = (1 << 64) + n
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            break
    return bytes(out)


def _pb_tag(f, w):
    return _pb_varint((f << 3) | w)


def _pb_field(f, v):
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, int):
        return _pb_tag(f, 0) + _pb_varint(v)
    if isinstance(v, str):
        d = v.encode('utf-8')
        return _pb_tag(f, 2) + _pb_varint(len(d)) + d
    if isinstance(v, (bytes, bytearray)):
        d = bytes(v)
        return _pb_tag(f, 2) + _pb_varint(len(d)) + d
    return b""


# ==================== PER-ACCOUNT MATCH COUNTER ====================
_match_counters: Dict[str, int] = {}
_match_counter_lock = asyncio.Lock()


async def _inc_match(uid: str) -> int:
    async with _match_counter_lock:
        _match_counters[uid] = _match_counters.get(uid, 0) + 1
        return _match_counters[uid]


async def _dec_match(uid: str) -> int:
    async with _match_counter_lock:
        if uid in _match_counters and _match_counters[uid] > 0:
            _match_counters[uid] -= 1
        return _match_counters.get(uid, 0)


async def _get_match_count(uid: str) -> int:
    async with _match_counter_lock:
        return _match_counters.get(uid, 0)


async def _get_total_match_count() -> int:
    async with _match_counter_lock:
        return sum(_match_counters.values())


# ==================== TOKEN CACHE ====================
_token_cache_memo: Dict[str, Any] = {}
_token_cache_memo_time: float = 0.0
_TOKEN_CACHE_MEMO_TTL = 5.0


def _json_serializer(obj):
    if isinstance(obj, (bytes, bytearray)):
        return {"__bytes_hex__": bytes(obj).hex()}
    raise TypeError(f"Type {type(obj)} not serializable")


def _json_deserializer(obj):
    if isinstance(obj, dict):
        if "__bytes_hex__" in obj and len(obj) == 1:
            try:
                return bytes.fromhex(obj["__bytes_hex__"])
            except Exception:
                return b""
        return {k: _json_deserializer(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_deserializer(x) for x in obj]
    return obj


def _load_token_cache() -> Dict[str, Any]:
    global _token_cache_memo, _token_cache_memo_time
    now = time.time()
    if _token_cache_memo and (now - _token_cache_memo_time) < _TOKEN_CACHE_MEMO_TTL:
        return _token_cache_memo
    if not os.path.exists(TOKEN_CACHE_FILE):
        return {}
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return {}
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("Cache root must be dict")
        parsed = _json_deserializer(data)
        _token_cache_memo = parsed
        _token_cache_memo_time = now
        return parsed
    except Exception as e:
        print_error(f"Token cache corrupt → deleting: {e}")
        try:
            os.remove(TOKEN_CACHE_FILE)
        except Exception:
            pass
        return {}


def _save_token_cache(cache: Dict[str, Any]):
    global _token_cache_memo, _token_cache_memo_time
    try:
        tmp_file = TOKEN_CACHE_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, default=_json_serializer)
        os.replace(tmp_file, TOKEN_CACHE_FILE)
        _token_cache_memo = cache
        _token_cache_memo_time = time.time()
    except Exception as e:
        print_error(f"Token cache save error: {e}")


def cache_get(uid: str) -> Optional[Dict]:
    """STRICT validation — corrupt/invalid entries auto-invalidate"""
    cache = _load_token_cache()
    entry = cache.get(str(uid))
    if not entry:
        return None
    if time.time() - entry.get("cached_at", 0) > TOKEN_CACHE_TTL:
        print_info(f"[CACHE] UID {uid} expired → re-login")
        cache_invalidate(uid)
        return None
    acc_id_raw = str(entry.get("account_id", "")).strip()
    if not acc_id_raw or acc_id_raw in ("0", "None", "null", ""):
        print_warning(f"[CACHE] UID {uid} invalid account_id='{acc_id_raw}' → invalidating")
        cache_invalidate(uid)
        return None
    if not acc_id_raw.isdigit():
        print_warning(f"[CACHE] UID {uid} account_id not numeric → invalidating")
        cache_invalidate(uid)
        return None
    entry["account_id"] = int(acc_id_raw)
    if not entry.get("aes_ak") or not entry.get("iv_i"):
        print_warning(f"[CACHE] UID {uid} missing crypto keys → invalidating")
        cache_invalidate(uid)
        return None
    if not isinstance(entry.get("login_payload_data"), (bytes, bytearray)):
        print_warning(f"[CACHE] UID {uid} missing payload → invalidating")
        cache_invalidate(uid)
        return None
    return entry


def cache_set(uid: str, account_data: Dict):
    cache = _load_token_cache()
    entry = dict(account_data)
    entry["cached_at"] = time.time()
    cache[str(uid)] = entry
    _save_token_cache(cache)
    print_success(f"[CACHE] Saved credentials for UID {uid}")


def cache_invalidate(uid: str):
    cache = _load_token_cache()
    if str(uid) in cache:
        del cache[str(uid)]
        _save_token_cache(cache)
        print_warning(f"[CACHE] Invalidated: {uid}")


# ==================== ENCRYPTION & LOGIN ====================
async def aes_encrypt(payload, key, iv):
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.encrypt(pad(payload, AES.block_size))


async def get_playstore_version():
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, lambda: play_scraper('com.dts.freefireth', lang='hi', country='id')
    )
    return result.get("version")


async def version_config():
    app_version = await get_playstore_version()
    api_url = (
        "https://version.ggwhitehawk.com/live/ver.php"
        f"?version={app_version}"
        "&lang=hi&device=android&channel=android"
        "&appstore=googleplay&region=BD"
        "&whitelist_version=1.3.0&whitelist_sp_version=1.0.0"
    )
    try:
        response = await client.get(api_url)
        response.raise_for_status()
        data = response.json()
        server_url = data.get("server_url")
        remote_version = data.get("remote_version")
        latest_release_version = data.get("latest_release_version")
        if not server_url or not remote_version or not latest_release_version:
            return None
        return latest_release_version, remote_version, server_url
    except Exception:
        return None


async def get_access_token(uid, password):
    url = "https://100067.connect.garena.com/oauth/guest/token/grant"
    hdrs = {
        "Host": "100067.connect.garena.com",
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; SM-G998B Build/SP1A.210812.016)",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "close"
    }
    data = {
        "uid": uid, "password": password,
        "response_type": "token", "client_type": "2",
        "client_secret": "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3",
        "client_id": "100067"
    }
    for _ in range(5):
        try:
            response = await client.post(url, headers=hdrs, data=data)
            if response.status_code == 200:
                rd = response.json()
                oi = rd.get("open_id"); at = rd.get("access_token"); pl = rd.get("platform", 4)
                if oi and at:
                    return oi, at, pl
            if response.status_code == 429:
                await asyncio.sleep(1)
                continue
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return None


async def parse_results(parsed_results):
    result_dict = {}
    for result in parsed_results:
        field_data = {"wire_type": result.wire_type}
        if result.wire_type in ("varint", "string", "bytes"):
            field_data["data"] = result.data
        elif result.wire_type == "length_delimited":
            field_data["data"] = await parse_results(result.data.results)
        result_dict[result.field] = field_data
    return result_dict


async def decode_protobuf(data):
    parsed_results = Parser().parse(data)
    return json.dumps(await parse_results(parsed_results))


async def build_majorlogin_payload(open_id, access_token, platform, client_version, device_info, verr=None):
    try:
        if verr is None:
            verr = client_version
        p = thunderFF_pb2.MajorLoginReq()
        p.event_time = str(datetime.now())[:-7]
        p.game_name = "free fire"
        p.platform_id = 1
        p.client_version = str(verr)
        p.client_version_code = "2019121229"
        p.system_software = "Android OS 15 / API-35 (AP3A.240905.015.A2/185014)"
        p.system_hardware = "Handheld"
        p.device_type = "Handheld"
        p.screen_width = 1600
        p.screen_height = 719
        p.screen_dpi = "234"
        p.processor_details = "ARM64 FP ASIMD AES | 1820 | 8"
        p.memory = 2798
        p.gpu_renderer = "Mali-G57"
        p.gpu_version = "OpenGL ES 3.2 v1.r49p1-04eac0.2848c17a2fd4e9340e06555168eaa3c9"
        p.unique_device_id = "Google|f744e396-5694-4e65-995d-97a958f2bd1f"
        p.client_ip = "197.0.137.129"
        p.language = "pt-br"
        p.open_id = str(open_id)
        p.open_id_type = "4"
        p.login_open_id_type = 4
        p.access_token = str(access_token)
        p.login_by = 2
        p.platform_sdk_id = 1
        p.origin_platform_type = "4"
        p.primary_platform_type = "4"
        p.reg_avatar = 1
        p.channel_type = 3
        p.telecom_operator = "TUNTEL"
        p.network_operator_a = "TUNTEL"
        p.network_type = "WIFI"
        p.network_type_a = "WIFI"
        p.cpu_type = 2
        p.cpu_architecture = "64"
        p.graphics_api = "OpenGLES2"
        p.supported_astc_bitset = 8191
        p.client_using_version = "7428b253defc164018c604a1ebbfebdf"
        p.loading_time = 15078
        p.release_channel = "android"
        p.extra_info = "KqsHTx3+QOmBRR1WKvaWewlcpqJBfjki+PPHQoQG8+0yV+Uos7gUFFjHMQ/e7u6han6Fl77r7c3vMN3p8UbKgN+nfycQCgwBmWgBzomx2gj84c+p"
        p.android_engine_init_flag = 111207
        p.if_push = 1
        p.is_vpn = 0
        ma = p.memory_available
        ma.version = 55
        ma.hidden_value = 81
        p.external_storage_total = 49973
        p.external_storage_available = 11338
        p.internal_storage_total = 854
        p.internal_storage_available = 11466
        p.game_disk_storage_total = 49973
        p.game_disk_storage_available = 11466
        p.external_sdcard_total_storage = 49973
        p.external_sdcard_avail_storage = 11466
        p.library_path = "/data/app/~~lHFxTCCbupG2QVJmsURtZw==/com.dts.freefireth-N3aCHpHNXpdxjD80uIIbww==/lib/arm64"
        p.library_token = "b8e0cd5e295eee42f5860d3c86e483dd|/data/app/~~lHFxTCCbupG2QVJmsURtZw==/com.dts.freefireth-N3aCHpHNXpdxjD80uIIbww==/base.apk"
        bp = p.SerializeToString()
        ex = b""
        ex += _pb_field(96, '{"cur_rate":[90,60,120],"support_etc2":false}')
        ex += _pb_field(97, 1)
        ex += _pb_field(99, "4")
        ex += _pb_field(100, "4")
        ex += _pb_field(102, b"\x17]ENWU\x0eR5")
        ex += _pb_field(104, 52882)
        ex += _pb_field(105, 1)
        ex += _pb_field(106, "https://dl.ak.freefiremobile.com/live/ABHotUpdates/|https://core-ak.freefiremobile.com/live/ABHotUpdates/|6b2078db9d22dd98f8e9386a39af8462")
        ex += _pb_field(107, "c8e41b7a93f02d56e1a94c7b8203f5d1")
        return await aes_encrypt(bp + ex, AES_KEY, AES_IV)
    except Exception as e:
        print(f"[DEBUG] build_majorlogin_payload error: {e}")
        return None


async def send_majorlogin(data, release_version, server_url):
    try:
        url = f"{server_url}MajorLogin" if server_url.endswith('/') else f"{server_url}/MajorLogin"
        req_headers = headers.copy()
        req_headers["ReleaseVersion"] = str(release_version)
        response = await client.post(url, headers=req_headers, data=data)
        if response.status_code != 200:
            return None
        rc = response.content
        if len(rc) < 40:
            return None
        res_proto = thunderFF_pb2.MajorLoginRes()
        try:
            res_proto.ParseFromString(rc)
        except Exception:
            pass
        dr = {}
        try:
            parsed = Parser().parse(rc.hex())
            dr = await parse_results(parsed)
        except Exception:
            pass
        kv = get_proto_field(dr, 22)
        ivv = get_proto_field(dr, 23)
        if not kv:
            kv = res_proto.aes_ak
        if not ivv:
            ivv = res_proto.iv_i
        if isinstance(kv, str):
            try:
                kv = bytes.fromhex(kv)
            except Exception:
                pass
        if isinstance(ivv, str):
            try:
                ivv = bytes.fromhex(ivv)
            except Exception:
                pass
        if not kv or not ivv:
            for off in range(min(128, len(rc))):
                try:
                    c = thunderFF_pb2.MajorLoginRes()
                    c.ParseFromString(rc[off:])
                    if c.region and c.token:
                        res_proto = c
                        break
                except Exception:
                    pass
        if kv:
            try:
                res_proto.aes_ak = kv
            except Exception:
                pass
        if ivv:
            try:
                res_proto.iv_i = ivv
            except Exception:
                pass
        return res_proto
    except Exception as e:
        print(f"[DEBUG] send_majorlogin exception: {e}")
        return None


async def send_getlogin(data, base_url, token, release_version):
    try:
        url = f"{base_url.rstrip('/')}/GetLoginData"
        req_headers = headers.copy()
        req_headers["ReleaseVersion"] = release_version
        req_headers['Authorization'] = f"Bearer {token}"
        req_headers['Host'] = "clientbp.ppmainecoonghj.com"
        response = await client.post(url, headers=req_headers, data=data)
        if response.status_code != 200:
            return None
        rc = response.content
        res_proto = thunderFF_pb2.GetLoginDataRes()
        try:
            res_proto.ParseFromString(rc)
        except Exception:
            pass

        def _ea(rb, fn):
            try:
                pos = 0
                n = len(rb)

                def rv2(b, p):
                    res = 0
                    sh = 0
                    while p < len(b):
                        x = b[p]
                        p += 1
                        res |= (x & 0x7F) << sh
                        if not (x & 0x80):
                            break
                        sh += 7
                    return res, p

                fs = {}
                while pos < n:
                    try:
                        k, pos = rv2(rb, pos)
                    except Exception:
                        break
                    fnn = k >> 3
                    wt = k & 0x07
                    try:
                        if wt == 0:
                            v, pos = rv2(rb, pos)
                        elif wt == 2:
                            ln, pos = rv2(rb, pos)
                            v = rb[pos:pos + ln]
                            pos += ln
                        elif wt == 5:
                            v = rb[pos:pos + 4]
                            pos += 4
                        elif wt == 1:
                            v = rb[pos:pos + 8]
                            pos += 8
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
                fa = _ea(rc[off:], 14)
                ia = _ea(rc[off:], 32)
                if fa and ":" in fa and ia and ":" in ia:
                    res_proto.functional_addrs = fa
                    res_proto.informational_addrs = ia
                    break
        except Exception:
            pass
        dict_res = {}
        try:
            parsed = Parser().parse(rc.hex())
            dict_res = await parse_results(parsed)
        except Exception:
            pass
        return res_proto, dict_res
    except Exception:
        return None


async def build_tcp_startup_packet(account_id, token, server_time, key, iv, region="BD", typ='OnLine'):
    uid_hex = f"{int(account_id):016x}"
    timestamp_hex = f"{int(server_time):08x}"
    encode_token = token.encode()
    encrypted_packet = (await aes_encrypt(encode_token, key, iv)).hex()
    encrypted_packet_length = f"{len(encrypted_packet) // 2:08x}"
    reg = str(region).upper() if region else "BD"
    if typ == 'OnLine':
        prefix = "7219" if reg in ("BD", "ME") else ("7214" if reg == "IND" else "7215")
        return f"{prefix}{uid_hex}{timestamp_hex}00000000{encrypted_packet_length}{encrypted_packet}"
    else:
        prefix = "8119" if reg in ("BD", "ME") else ("8114" if reg == "IND" else "8115")
        return f"{prefix}{uid_hex}{timestamp_hex}{encrypted_packet_length}{encrypted_packet}"


async def send_keep_alive(region="BD"):
    try:
        reg = str(region).upper() if region else "BD"
        ka_hex = "0219" if reg == "BD" else ("0214" if reg == "IND" else "0215")
        return bytes.fromhex(ka_hex)
    except Exception:
        return bytes.fromhex("0219")


# ==================== BR / LW AUTO SWITCH ====================
def resolve_match_mode(uid: str, mode_override: str = "") -> str:
    mo = (mode_override or "AUTO").upper().strip()
    if mo == "BR":
        return "BR"
    if mo in ("LW", "LONE_WOLF", "LONE"):
        return "LW"
    try:
        uid_str = str(uid)
        acc = bot_state.accounts.get(uid_str)
        if acc is None:
            for v in bot_state.accounts.values():
                if (str(v.get("auth_uid")) == uid_str or
                        str(v.get("uid")) == uid_str or
                        str(v.get("game_uid")) == uid_str):
                    acc = v
                    break
        lvl = int(acc.get("level", 1) or 1) if acc else 1
    except Exception:
        lvl = 1
    return "BR" if lvl < BR_TO_LW_LEVEL else "LW"


# ═══════════════════════════════════════════════════════════════
#  🔥 VERIFIED LW BASE HEX (from working original main.py)
# ═══════════════════════════════════════════════════════════════
_BASE_LW_HEX = (
    "080112800a0a010b102b3a110a044944433110aa011a064555524f50453a100a044944433210311a064555524f504540014a0801090a0b1219202758016291090a8001303838463832424630324139363736373032303130313030303030303030303030303136303030313030313530303032323246393745454530463030303030303436373632353134303030303030303030303030303030303030303030303030303030303030303030303030303066663030303030303030636163666131366410241afb02735d5e571400024a775d45414d1a041b1c001f11010449715f4243481a001e1d071c1703004b1a4066785c524570735c51486775421b5c5a4c07504042685a63610816054e19025e75196001477c015165406370195f5547404e4550640103020f1304064863754268676c755f65576e40467e5f0a417a4701026d675d6e73670b1108495a4c6a0b78470b740065645e525a057258425f584a447d4e6759440c11044e7c596d7f4b625f7d04055a47505c4e1d6b5b4107447d7201057d7f0f14084e430457674f7e517d72015172415d027473577c4d615f79535256780911030f4d5e027a797f614165067806505d53777750475e75064257076500460817014e741e7e5078487e7a7c465e7669767153497064605a7376677773550d160148037e18675966787f4c42607a645f577e7b441b460776026b18685d0b110205490060020f70676175654674706671797f41067346677c4e06585e780f15074c57047b40517075415f6364027259674b5b0166407f7340600407770a22047a5d5c52300b3a0a167305067162727516134208312e3133302e3232480350015ae90403626253513635686e556f4e36416456324b796f566c636f477776484f624e56526c4d727073504b4f43654177616848494176795556497273743752737149734a7a786b3247525268377a2f637664626d504f6a73552f79626d38547a4c69586d2f474351696d494b53486833447955726f39515152756c34545350626d6d624b7949565937545671577059455372323646572f59624578507338514f706d317372785455736c30796a434144444d4f34616a654b615753366361496c554b4963797a494e396d52516f715277687939797257476d337a644345337a6a61436f492f5a585233656f65365a42647a64677654636b6b665733356e4d4c6a6a565072564b6433523172756174394e50514150724a5546627859696c4c5a3859707336654d5447666b6649793574666a526c314d4648706b51774c6373374439656378566c41636f374e664f6d2b30654756466c4434744478706771385533595973587645384842502f70666c767a737138316a32524f4d7857437556445442492f684735625462773166456e4249725162762b636144775147696f74554e316d4c4b77734379456f4766706746614251457645672b736a764c4c78704743334c304a5344532f74526169504354553344374e6249306547516651622f5a466f4c36455630775a324d6f583932414c572f5049752f56634663584e70596b356f7966326151416a536971486a2f363276354843644f525551303578754e6171795251625653704654303137655237675255636b4966366c6f447476342b514e4a4670766d74757077707774396a5a5974437a4b56743657726d6e36785837706658456251555434684f3758a201050803108703a201050804108103a20105080510c001a20105081d10cc01a2010408161078a20105080e10af01a201020815"
)


async def _send_start_match_packet(region, client_version, mode, writer, key, iv):
    """Parse → modify → re-serialize → 215-byte compact packet"""
    hex_str = _BASE_LW_HEX

    # 🔥 BR mode swap: mode 0x0b→0x01, map 0x2b→0x01
    if str(mode).upper() == "BR":
        hex_str = hex_str.replace("0a010b102b", "0a01011001", 1)

    try:
        raw = bytes.fromhex(hex_str)
    except ValueError as e:
        print_error(f"[{mode}] HEX decode fail: {e}")
        return

    try:
        proto = thunderFF_pb2.StartMatch()
        proto.ParseFromString(raw)
    except Exception as e:
        print_error(f"[{mode}] ParseFromString FAIL: {e}")
        return

    reg = str(region).upper() if region else "BD"
    if hasattr(proto.main, 'region_list') and len(proto.main.region_list) > 0:
        proto.main.region_list[0].region = reg
        if len(proto.main.region_list) > 1:
            proto.main.region_list[1].region = reg
    if hasattr(proto.main, 'client_version'):
        proto.main.client_version.remote_version = client_version

    serialized = proto.SerializeToString()
    encrypted_packet = (await aes_encrypt(serialized, key, iv)).hex()
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
    try:
        writer.write(bytes.fromhex(final_packet))
        await writer.drain()
        print_info(f"[{mode}] Match search sent ({packet_length} bytes) | Region: {reg} | Prefix: {reg_prefix}")
    except Exception as e:
        print_error(f"[{mode}] Write fail: {e}")


async def start_game_lone_wolf(region, client_version, writer, key, iv):
    await _send_start_match_packet(region, client_version, "LW", writer, key, iv)


async def start_game_battle_royale(region, client_version, writer, key, iv):
    await _send_start_match_packet(region, client_version, "BR", writer, key, iv)


# ==================== TEA / FRAME ====================
async def has_ssan_zig(n):
    z = (n << 1) & 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while z >= 0x80:
        out.append((z & 0x7F) | 0x80)
        z >>= 7
    out.append(z)
    return bytes(out)


async def uleb_encode(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            break
    return bytes(out)


async def tea_enc(v0, v1, k0, k1, k2, k3):
    s = 0
    for _ in range(_ROUNDS):
        s = (s + _DELTA) & 0xFFFFFFFF
        v0 = (v0 + (((((v1 << 4) & 0xFFFFFFFF) + k0) & 0xFFFFFFFF ^
                     ((v1 + s) & 0xFFFFFFFF) ^
                     (((v1 >> 5) + k1) & 0xFFFFFFFF)))) & 0xFFFFFFFF
        v1 = (v1 + (((((v0 << 4) & 0xFFFFFFFF) + k2) & 0xFFFFFFFF ^
                     ((v0 + s) & 0xFFFFFFFF) ^
                     (((v0 >> 5) + k3) & 0xFFFFFFFF)))) & 0xFFFFFFFF
    return v0, v1


async def tea_dec(v0, v1, k0, k1, k2, k3):
    s = (_DELTA * _ROUNDS) & 0xFFFFFFFF
    for _ in range(_ROUNDS):
        v1 = (v1 - (((((v0 << 4) & 0xFFFFFFFF) + k2) & 0xFFFFFFFF ^
                     ((v0 + s) & 0xFFFFFFFF) ^
                     (((v0 >> 5) + k3) & 0xFFFFFFFF)))) & 0xFFFFFFFF
        v0 = (v0 - (((((v1 << 4) & 0xFFFFFFFF) + k0) & 0xFFFFFFFF ^
                     ((v1 + s) & 0xFFFFFFFF) ^
                     (((v1 >> 5) + k1) & 0xFFFFFFFF)))) & 0xFFFFFFFF
        s = (s - _DELTA) & 0xFFFFFFFF
    return v0, v1


async def tea_cbc_encrypt(padded, key_bytes):
    k0, k1, k2, k3 = (struct.unpack_from("<I", key_bytes, o)[0] for o in (0, 4, 8, 12))
    out = bytearray(len(padded))
    prev_cipher = bytearray(8)
    prev_intermediate = bytearray(8)
    for i in range(0, len(padded), 8):
        xored = bytearray(8)
        for j in range(8):
            xored[j] = padded[i + j] ^ prev_cipher[j]
        e0, e1 = await tea_enc(
            struct.unpack_from("<I", xored, 0)[0],
            struct.unpack_from("<I", xored, 4)[0],
            k0, k1, k2, k3,
        )
        enc = bytearray(8)
        struct.pack_into("<I", enc, 0, e0)
        struct.pack_into("<I", enc, 4, e1)
        for j in range(8):
            out[i + j] = enc[j] ^ prev_intermediate[j]
        prev_cipher[:] = out[i:i + 8]
        prev_intermediate[:] = xored
    return bytes(out)


async def tea_cbc_decrypt(body, key_bytes):
    k0, k1, k2, k3 = (struct.unpack_from("<I", key_bytes, o)[0] for o in (0, 4, 8, 12))
    out = bytearray(len(body))
    prev_intermediate = bytearray(8)
    prev_cipher = bytearray(8)
    xored = bytearray(8)
    dec = bytearray(8)
    for i in range(0, len(body), 8):
        for j in range(8):
            xored[j] = body[i + j] ^ prev_intermediate[j]
        d0, d1 = await tea_dec(
            struct.unpack_from("<I", xored, 0)[0],
            struct.unpack_from("<I", xored, 4)[0],
            k0, k1, k2, k3
        )
        struct.pack_into("<I", dec, 0, d0)
        struct.pack_into("<I", dec, 4, d1)
        for j in range(8):
            out[i + j] = dec[j] ^ prev_cipher[j]
        prev_cipher[:] = body[i:i + 8]
        prev_intermediate[:] = dec
    return bytes(out)


async def build_padded(content):
    pad_len = (8 - (len(content) + 10) % 8) % 8
    return bytes([pad_len, 0, 0]) + b"\x00" * pad_len + content + b"\x00" * 7


async def encode_header(layout, send_option, cmd, order_id, flags, length, k, v80):
    out = bytearray()
    for code in layout:
        value = {0: send_option, 1: cmd, 2: order_id, 3: flags, 4: length}[code]
        if _FIELD_SIZES[code] == 1:
            out.append((value & 0xFF) ^ k)
        else:
            v = ((value & 0xFFFF) ^ v80) & 0xFFFF
            out.append(v & 0xFF)
            out.append((v >> 8) & 0xFF)
    return bytes(out)


async def crc7_buff(crc, buf):
    c = crc & 0x7F
    for b in buf:
        c = CRC7_TABLE[((2 * (c & 0xFF)) ^ (b & 0xFF)) & 0xFF] & 0x7F
    return c & 0x7F


async def sv_frame(msg_key, layout, send_option, cmd, order_id, flags, content, key, encrypted=True):
    k = key[0]
    v80 = ((k << 8) | k) & 0xFFFF
    body = await tea_cbc_encrypt(await build_padded(content), key) if encrypted else content
    hdr = bytearray([msg_key, 0]) + await encode_header(layout, send_option, cmd, order_id, flags, len(body), k, v80)
    packet = bytearray(hdr + body)
    packet[1] = await crc7_buff(0, bytes(packet[2:])) & 0x7F
    return bytes(packet)


async def build_match_startup_packets(token, udp_key, match_code, account_id, block_val,
                                      server_ip="", region="BD", client_version="1.132.6",
                                      client_version_code="2019121229", access_token="",
                                      mode="LW"):
    token = token.strip()
    udp_key = bytes.fromhex(udp_key)
    match_code = [int(ch) for ch in str(match_code).strip()]

    thunder_jwt = token[:660] if len(token) > 660 else token
    sharma_jwt = token[660:] if len(token) > 660 else ""
    encoded_thunder_jwt = thunder_jwt.encode() if isinstance(thunder_jwt, str) else thunder_jwt
    encoded_sharma_jwt = sharma_jwt.encode() if isinstance(sharma_jwt, str) else sharma_jwt

    garena420 = await has_ssan_zig(len(encoded_thunder_jwt)) + encoded_thunder_jwt

    reg = str(region).upper() if region else "BD"
    csoversea_block = bytes.fromhex(
        "ca0163736f7665727365612e7374726f6e67686f6c642e66726565666972656d6f62696c652e636f6d"
        "3b302e302e302e303b33342e3132362e37362e34353b33342e38372e3137372e31343b33342e38372e"
        "3137302e3233303b33352e3138352e3138332e35370000000000000100000000000000000000000001"
        "00000800000100000000000100a8a2d7bebd8d8bdf110200"
    )

    mid = bytes.fromhex('0000000001000102030101') + await has_ssan_zig(len(reg)) + reg.encode()
    mid += bytes.fromhex('0001030003000004')
    mid += await has_ssan_zig(len(client_version)) + client_version.encode()
    mid += await has_ssan_zig(len(client_version_code)) + client_version_code.encode()
    mid += csoversea_block

    clean_ip = server_ip.split(':')[0] if server_ip else "0.0.0.0"
    mid += await has_ssan_zig(len(clean_ip)) + clean_ip.encode()

    clean_acc_tok = access_token.strip() if access_token else ""
    if clean_acc_tok:
        mid += await has_ssan_zig(len(clean_acc_tok)) + clean_acc_tok.encode()

    mid += await has_ssan_zig(len(encoded_sharma_jwt)) + encoded_sharma_jwt

    if str(mode).upper() == "LW":
        _mode_byte = MODE_ID_LW
        _map_byte = MAP_ID_LW
    else:
        _mode_byte = MODE_ID_BR
        _map_byte = MAP_ID_BR

    tg_garena420 = (
            await uleb_encode(int(account_id)) +
            await uleb_encode(int(block_val)) +
            await uleb_encode(1) +
            await uleb_encode(_mode_byte) +
            await uleb_encode(int(block_val)) +
            await uleb_encode(_map_byte) +
            mid
    )

    process = await sv_frame(0x5E, match_code, 2, 447, 0, 1, garena420, udp_key)
    loading = await sv_frame(0x5A, match_code, 2, 448, 1, 1, tg_garena420, udp_key)
    return process.hex(), loading.hex()


async def produce_xor_key(secret_key):
    k = secret_key[0] if secret_key and len(secret_key) > 0 else 10
    return k, ((k << 8) | k) & 0xFFFF


async def parse_layout(layout):
    if isinstance(layout, str):
        return [int(ch) for ch in layout.strip()]
    return list(layout)


async def build_hello_packet(text, key, layout):
    data = text.encode("utf-8")
    if len(data) > 25:
        raise ValueError(f"Text too long ({len(data)})")
    content = b"\x10\x00\x00\x00" + data + b"\x00" * (29 - 4 - len(data))
    k, v80 = await produce_xor_key(key)
    layout = await parse_layout(layout)
    padded = await build_padded(content)
    enc_body = await tea_cbc_encrypt(padded, key)
    header_bytes = await encode_header(layout, 1, 1, 0, 1, len(enc_body), k, v80)
    packet = bytearray([0x63, 0x00]) + header_bytes + enc_body
    packet[1] = await crc7_buff(0, packet[2:]) & 0x7F
    return bytes(packet).hex()


async def classify(frame):
    cmd = frame["cmd"]
    msg_name = MESSAGE_ID_TO_NAME.get(cmd, f"UNKNOWN_{cmd}")
    if msg_name == "UDP_HELLO":
        return "HELLO"
    if msg_name == "UDP_ACK":
        return "ACK"
    if msg_name == "UDP_PING":
        return "PING"
    if msg_name == "RUDP_JOIN_MATCH":
        return "JOIN_MATCH"
    if msg_name.startswith("RUDP_"):
        return msg_name
    if msg_name.startswith("UDP_"):
        return msg_name
    return "DATA"


async def build_packet(msg_key, layout, send_option, cmd, order_id, flags, content, key, encrypted=True):
    k = key[0]
    v80 = ((k << 8) | k) & 0xFFFF
    body = await tea_cbc_encrypt(await build_padded(content), key) if encrypted else content
    hdr = bytearray([msg_key, 0])
    for code in layout:
        value = {0: send_option, 1: cmd, 2: order_id, 3: flags, 4: len(body)}[code]
        if _FIELD_SIZES[code] == 1:
            hdr.append((value & 0xFF) ^ k)
        else:
            v = ((value & 0xFFFF) ^ v80) & 0xFFFF
            hdr.append(v & 0xFF)
            hdr.append((v >> 8) & 0xFF)
    packet = bytearray(hdr + body)
    packet[1] = await crc7_buff(0, bytes(packet[2:])) & 0x7F
    return bytes(packet)


async def layouts_from_mask(mask):
    ru = [int(c) for c in str(mask).strip()]
    nr = [c for c in ru if c != 2]
    return ru, nr


async def reply_for(frame, key, mask, ack_key=0x68, ping_key=0x6D, hello_key=0x5B, ack_style="short"):
    ru, nr = await layouts_from_mask(mask)
    typ = await classify(frame)
    if typ == "HELLO":
        if ack_style == "echo":
            content = frame["content"] if frame["content"] else b"\x10\x00\x00\x00"
            return typ, await build_packet(hello_key, nr, 1, 1, None, 1, content, key)
        return typ, await build_packet(ack_key, nr, 0, 2, None, 1, b"\x01\x00", key)
    if typ == "ACK":
        content = frame["content"] if frame["content"] else b"\x01\x00"
        return typ, await build_packet(ack_key, nr, 0, 2, None, 1, content, key)
    if typ == "PING":
        c = frame["content"]
        counter = c[:4] if len(c) >= 4 else c
        return typ, await build_packet(ping_key, nr, 0, 3, None, 0, counter + b"\x00\x00\x00", key, encrypted=False)
    if typ == "JOIN_MATCH":
        return typ, await build_packet(ack_key, nr, 0, 2, None, 1, b"\x02\x00", key)
    return typ, None


async def keepalive_ping(sock, ip, port, key_bytes, mask, stop_event):
    nr = (await layouts_from_mask(mask))[1]
    ping_keys = [0x66, 0x6D, 0x69, 0x6C, 0x6B, 0x6E, 0x6F, 0x70]
    loop = asyncio.get_event_loop()
    i = 0
    while not stop_event.is_set():
        pk = ping_keys[i % len(ping_keys)]
        counter = int(time.time() * 1000) & 0xFFFFFFFF
        pkt = await build_packet(pk, nr, 0, 3, None, 0, struct.pack("<I", counter) + b"\x00\x00\x00",
                                 key_bytes, encrypted=False)
        try:
            await loop.sock_sendto(sock, pkt, (ip, port))
        except Exception:
            pass
        i += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pass


async def try_header(buf, layout, k, v80):
    off = 2
    out = {}
    for code in layout:
        size = _FIELD_SIZES[code]
        if off + size > len(buf):
            return None
        out[_FIELD_NAMES[code]] = (buf[off] ^ k) if size == 1 else ((buf[off] | (buf[off + 1] << 8)) ^ v80) & 0xFFFF
        off += size
    out["headerLen"] = off
    return out


async def oicq_unpad(padded):
    if not padded or len(padded) < 8:
        return None
    if not all(padded[-1 - i] == 0 for i in range(7)):
        return None
    pad_len = padded[0] & 0x07
    s = 3 + pad_len
    e = len(padded) - 7
    return padded[s:e] if s < e else b""


async def decode_packet(packet, key, mask=None):
    data = bytes(packet) if isinstance(packet, bytes) else bytes.fromhex(packet)
    if len(data) < 8:
        return None
    k = key[0]
    v80 = ((k << 8) | k) & 0xFFFF
    crc_ok = (data[1] & 0x7F) == await crc7_buff(0, data[2:])
    candidates = []
    if mask:
        ru, nr = await layouts_from_mask(mask)
        layouts = [("RUDP", ru), ("nonRUDP", nr)]
    else:
        layouts = [("RUDP", list(p)) for p in itertools.permutations([0, 1, 2, 3, 4])]
        layouts += [("nonRUDP", list(p)) for p in itertools.permutations([0, 1, 3, 4])]
    for kind, layout in layouts:
        f = await try_header(data, layout, k, v80)
        if not f:
            continue
        if f["flags"] > 7 or f["sendOption"] > 7:
            continue
        if f["length"] != len(data) - f["headerLen"]:
            continue
        body = data[f["headerLen"]:f["headerLen"] + f["length"]]
        content = None
        padded = None
        if f["flags"] & 1:
            if len(body) < 8 or len(body) % 8 != 0:
                continue
            padded = await tea_cbc_decrypt(body, key)
            content = await oicq_unpad(padded)
            if content is None:
                continue
        else:
            content = body
        score = (1 if crc_ok else 0) + (1 if content is not None else 0)
        candidates.append({
            "kind": kind, "layout": layout, "headerLen": f["headerLen"],
            "msgKey": data[0], "cmd": f["cmd"], "flags": f["flags"],
            "sendOption": f["sendOption"], "orderId": f.get("orderId"),
            "length": f["length"], "content": content, "crcOk": crc_ok,
            "padded": padded, "score": score, "total": len(data),
        })
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c["kind"] == "RUDP" or c["kind"] == "nonRUDP", c["score"]), reverse=True)
    return candidates[0]


# ==================== AUTO DELETE ON TARGET ====================
async def auto_delete_completed_account(card_key: str):
    try:
        card_key = str(card_key)
        guest_uid = None
        token_key = None
        game_uid = None
        cred = bot_state.account_credentials.get(card_key)
        if cred:
            if cred.get('auth_type') == 'guest' and cred.get('auth_uid'):
                guest_uid = str(cred['auth_uid'])
            elif cred.get('auth_type') == 'token' and cred.get('auth_token'):
                token_key = f"tok_{cred['auth_token'][:20]}"
            if cred.get('account_id'):
                game_uid = str(cred.get('account_id'))
        else:
            guest_uid = card_key

        all_accs = load_accounts()
        new_list = []
        for a in all_accs:
            acc_uid = str(a.get("uid", "")).strip()
            acc_token = str(a.get("token", "")).strip()
            acc_game_id = str(a.get("account_id", "")).strip()
            if acc_uid and (acc_uid == card_key or acc_uid == guest_uid):
                continue
            if acc_token and token_key and acc_token[:20] == token_key.replace("tok_", "")[:20]:
                continue
            if acc_token and f"tok_{acc_token[:20]}" == card_key:
                continue
            if game_uid and acc_game_id == game_uid:
                continue
            new_list.append(a)
        _save_accounts_list(new_list)

        if os.path.exists(TOKEN_CACHE_FILE):
            try:
                with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
                    c = f.read().strip()
                cache_data = json.loads(c) if c else {}
                keys_to_del = []
                for k in list(cache_data.keys()):
                    if k == card_key or k == guest_uid or k == game_uid or (token_key and k == token_key):
                        keys_to_del.append(k)
                        continue
                    entry = cache_data[k]
                    if isinstance(entry, dict):
                        if game_uid and str(entry.get('account_id')) == game_uid:
                            keys_to_del.append(k)
                        elif guest_uid and str(entry.get('auth_uid')) == guest_uid:
                            keys_to_del.append(k)
                        elif str(entry.get('account_id')) == card_key:
                            keys_to_del.append(k)
                for k in keys_to_del:
                    del cache_data[k]
                tmp = TOKEN_CACHE_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(cache_data, f, indent=2)
                os.replace(tmp, TOKEN_CACHE_FILE)
                global _token_cache_memo, _token_cache_memo_time
                _token_cache_memo = {}
                _token_cache_memo_time = 0.0
            except Exception as e:
                print_error(f"[AUTO-DELETE] token_cache error: {e}")

        for k in [card_key, guest_uid, token_key, game_uid]:
            if not k:
                continue
            bot_state.accounts.pop(k, None)
            bot_state.account_credentials.pop(k, None)
            bot_state.account_states.pop(k, None)
            if k in bot_state.account_workers:
                try:
                    bot_state.account_workers[k].cancel()
                except Exception:
                    pass
                bot_state.account_workers.pop(k, None)

        bot_state.log(f"🎯 Target complete → AUTO DELETED {card_key}", "success", card_key)
        print_success(f"[AUTO-DELETE] ✅ {card_key} removed (target reached)")
    except Exception as e:
        print_error(f"[AUTO-DELETE] error: {e}")


# ==================== PLAY GAME (UDP) ====================
async def play_game(server_ip_port, thunder, sharma, udp_key, match_code,
                    account_id, player_region, client_version, key, iv,
                    match_index: int, card_key: str = None):
    match_start_time = time.time()
    ping_task = None
    sock = None
    ping_stop = asyncio.Event()
    uid_str = str(account_id)
    stat_key = card_key or uid_str
    completed_cleanly = False

    try:
        ip, port = server_ip_port.split(":")
        port = int(port)
        resolved_ip = await resolve_host_cloudflare(ip)

        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        optimize_udp_socket(sock)
        sock.setblocking(False)

        udp_key_bytes = bytes.fromhex(udp_key)
        hello_packet = await build_hello_packet(f"{account_id}_2585", udp_key_bytes, match_code)
        await loop.sock_sendto(sock, bytes.fromhex(hello_packet), (resolved_ip, port))

        ack_state = "waiting_for_hello_reply"
        thunder_sent = False
        sharma_sent = False
        join_match_received = False
        local_closed = False
        send_lock = asyncio.Lock()

        ping_task = asyncio.create_task(
            keepalive_ping(sock, resolved_ip, port, udp_key_bytes, match_code, ping_stop)
        )
        last_activity = time.time()
        MAX_IDLE_BEFORE_HELLO_RESEND = 7.0

        print_colored(f"🎮 [MATCH #{match_index}] UDP → {server_ip_port} (DNS: {resolved_ip})", Colors.MAGENTA)

        async def send_thunder_sharma_inline():
            nonlocal ack_state, thunder_sent, sharma_sent
            if thunder_sent:
                return
            async with send_lock:
                if thunder_sent:
                    return
                try:
                    await loop.sock_sendto(sock, bytes.fromhex(thunder), (resolved_ip, port))
                    thunder_sent = True
                    await asyncio.sleep(0.1)
                    prepare_ack = await build_packet(
                        0x68, (await layouts_from_mask(match_code))[1],
                        0, 2, None, 1, b"\x01\x00", udp_key_bytes
                    )
                    await loop.sock_sendto(sock, prepare_ack, (resolved_ip, port))
                    await asyncio.sleep(0.2)
                    await loop.sock_sendto(sock, bytes.fromhex(sharma), (resolved_ip, port))
                    sharma_sent = True
                    ack_state = "thunder_sharma_sent"
                    print_success(f"[MATCH #{match_index}] Thunder+Sharma sent!")
                except Exception as e:
                    print_error(f"[MATCH #{match_index}] send error: {e}")

        while not local_closed:
            if time.time() - match_start_time > MAX_MATCH_DURATION:
                break
            try:
                response, server_addr = await asyncio.wait_for(
                    loop.sock_recvfrom(sock, 65535), timeout=1.5
                )
                if response:
                    last_activity = time.time()
                    frame = await decode_packet(response, udp_key_bytes, match_code)
                    if frame:
                        ptype = await classify(frame)
                        if frame['cmd'] in [103, 107]:
                            print_success(f"[MATCH #{match_index}] Completed (cmd {frame['cmd']})")
                            completed_cleanly = True
                            local_closed = True
                            continue
                        if frame['cmd'] == 101:
                            try:
                                ack_pkt = await build_packet(
                                    0x68, (await layouts_from_mask(match_code))[1],
                                    0, 2, None, 1, b"\x01\x00", udp_key_bytes
                                )
                                await loop.sock_sendto(sock, ack_pkt, server_addr)
                            except Exception:
                                pass
                            continue
                        if ptype in ["ACK", "PING", "HELLO", "JOIN_MATCH"]:
                            if ptype == "HELLO" and ack_state == "waiting_for_hello_reply":
                                typ, reply = await reply_for(frame, udp_key_bytes, match_code, ack_style="short")
                                if reply:
                                    await loop.sock_sendto(sock, reply, server_addr)
                                ack_state = "ack_sent_waiting"
                            elif ptype == "ACK":
                                if ack_state == "waiting_for_hello_reply":
                                    typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                    if reply:
                                        await loop.sock_sendto(sock, reply, server_addr)
                                    ack_state = "ready_to_send_thunder"
                                elif ack_state == "ack_sent_waiting":
                                    ack_state = "ready_to_send_thunder"
                                else:
                                    typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                    if reply:
                                        await loop.sock_sendto(sock, reply, server_addr)
                            elif ptype == "PING":
                                typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                if reply:
                                    await loop.sock_sendto(sock, reply, server_addr)
                            elif ptype == "JOIN_MATCH" and not join_match_received:
                                typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                if reply:
                                    await loop.sock_sendto(sock, reply, server_addr)
                                    join_match_received = True
            except asyncio.TimeoutError:
                if ack_state == "ready_to_send_thunder" and not thunder_sent:
                    await send_thunder_sharma_inline()
                elif ack_state == "waiting_for_hello_reply":
                    if (time.time() - last_activity) > MAX_IDLE_BEFORE_HELLO_RESEND:
                        try:
                            pkt = await build_hello_packet(f"{account_id}_2585", udp_key_bytes, match_code)
                            await loop.sock_sendto(sock, bytes.fromhex(pkt), (resolved_ip, port))
                        except Exception:
                            pass
                        last_activity = time.time()
                    if (time.time() - match_start_time) > 25.0:
                        break
                elif ack_state == "thunder_sharma_sent":
                    if (time.time() - last_activity) > MATCH_IDLE_TIMEOUT:
                        print_success(f"[MATCH #{match_index}] Finished naturally")
                        completed_cleanly = True
                        break
                continue
            except BlockingIOError:
                await asyncio.sleep(0.05)
            except OSError:
                await asyncio.sleep(0.5)
                continue
            except Exception:
                await asyncio.sleep(0.5)
                continue
            if ack_state == "ready_to_send_thunder" and not thunder_sent:
                await send_thunder_sharma_inline()
        return f"match #{match_index} finished"
    except Exception as e:
        print_error(f"[MATCH #{match_index}] error: {e}")
        return f"match #{match_index} error"
    finally:
        if completed_cleanly:
            try:
                bot_state.increment_match(stat_key)
            except Exception:
                pass
            try:
                cred = bot_state.account_credentials.get(stat_key)
                if cred:
                    asyncio.create_task(refresh_account_profile(cred))
                    if bot_state.is_target_reached(stat_key):
                        print_success(f"[TARGET] {stat_key} target reached → AUTO DELETE")
                        asyncio.create_task(auto_delete_completed_account(stat_key))
            except Exception as e:
                print_error(f"[AUTO-REFRESH] error: {e}")
        ping_stop.set()
        if ping_task:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        remaining = await _dec_match(stat_key)
        total = await _get_total_match_count()
        print_info(f"[MATCH #{match_index}] Closed. Card active: {remaining} | Total: {total}")
        try:
            if stat_key in bot_state.accounts:
                bot_state.update_status(stat_key, "IN_MATCH" if remaining > 0 else "ONLINE", remaining)
        except Exception:
            pass


# ==================== FUNCTIONAL WORKER ====================
async def functional_lone_wolf(addrs, starter_packet, account_region, client_version,
                                key, iv, account_id="", account_data=None,
                                max_reconnects=10, card_key: str = None):
    reconnects = 0
    ip, port = addrs.split(":")
    play_matches: List[asyncio.Task] = []
    no_response_count = 0
    search_attempts = 0
    last_start_time = 0.0
    uid_str = str(account_id)
    stat_key = card_key or uid_str

    if not uid_str or uid_str == "0" or not uid_str.isdigit():
        print_error(f"[FUNCTIONAL] Invalid account_id='{uid_str}' — aborting")
        return

    consecutive_parse_failures = 0
    current_token = starter_packet
    current_key = key
    current_iv = iv
    current_account_data = account_data

    try:
        while True:
            if stat_key not in bot_state.accounts:
                print_warning(f"[FUNCTIONAL] {stat_key} removed → stopping worker")
                break
            writer = None
            try:
                if current_account_data:
                    fresh = None
                    if current_account_data.get('auth_type') == 'guest' and current_account_data.get('auth_uid'):
                        fresh = cache_get(str(current_account_data['auth_uid']))
                    elif current_account_data.get('auth_type') == 'token' and current_account_data.get('auth_token'):
                        fresh = cache_get(f"tok_{current_account_data['auth_token'][:20]}")
                    if fresh:
                        current_account_data = fresh
                        current_key = fresh['aes_ak']
                        current_iv = fresh['iv_i']
                        current_token = await build_tcp_startup_packet(
                            fresh['account_id'], fresh['token'], fresh['server_time'],
                            current_key, current_iv,
                            region=fresh.get('region', account_region),
                            typ='OnLine'
                        )
                    else:
                        print_warning(f"[FUNCTIONAL] Cache miss for {uid_str} → re-login")
                        try:
                            if current_account_data.get('auth_uid'):
                                cache_invalidate(str(current_account_data['auth_uid']))
                            if current_account_data.get('auth_token'):
                                cache_invalidate(f"tok_{current_account_data['auth_token'][:20]}")
                        except Exception:
                            pass
                        raise ConnectionError("Cache expired, triggering fresh login")

                resolved_ip = await resolve_host_cloudflare(ip)
                reader, writer = await asyncio.open_connection(resolved_ip, int(port))
                raw_sock = writer.get_extra_info('socket')
                if raw_sock:
                    optimize_tcp_socket(raw_sock)
                writer.write(bytes.fromhex(current_token))
                await writer.drain()
                try:
                    init_ka = await send_keep_alive(account_region)
                    if init_ka and writer and not writer.is_closing():
                        writer.write(init_ka)
                        await asyncio.wait_for(writer.drain(), timeout=3)
                except Exception:
                    pass

                print_success(f"[FUNCTIONAL] TCP Gateway Connected for UID: {uid_str} (DNS: {resolved_ip})")
                reconnects = 0
                no_response_count = 0
                last_start_time = 0.0

                async def send_start_match():
                    nonlocal search_attempts, last_start_time
                    search_attempts += 1
                    current_region = "BD"

                    mo = ""
                    if current_account_data:
                        mo = str(current_account_data.get("mode_override") or "")
                    mode = resolve_match_mode(stat_key, mo)

                    # 🔥 BR safety: skip if any match already active
                    if mode == "BR":
                        active_udp = [m for m in play_matches if not m.done()]
                        if active_udp:
                            print_info(f"[BR] Match in progress ({len(active_udp)}) — skip new StartMatch")
                            last_start_time = asyncio.get_running_loop().time()
                            return

                    print_info(f"[{mode}] UID {uid_str} -> Match #{search_attempts} | Region: {current_region} | Mode: {mo or 'AUTO'}")
                    try:
                        await asyncio.sleep(random.uniform(0.3, 0.6))
                        if mode == "LW":
                            await start_game_lone_wolf(current_region, client_version, writer, current_key, current_iv)
                        else:
                            await start_game_battle_royale(current_region, client_version, writer, current_key, current_iv)
                        print_success(f"[{mode}] Match search packet sent")
                        active = await _get_match_count(stat_key)
                        try:
                            bot_state.update_status(stat_key, "SEARCHING", active)
                        except Exception:
                            pass
                    except Exception as e:
                        print_error(f"start_game error: {e}")
                    last_start_time = asyncio.get_running_loop().time()

                await send_start_match()

                while True:
                    if stat_key not in bot_state.accounts:
                        print_warning(f"[FUNCTIONAL] {stat_key} removed → stopping worker")
                        raise asyncio.CancelledError()
                    play_matches[:] = [m for m in play_matches if not m.done()]
                    active_count = await _get_match_count(stat_key)
                    try:
                        bot_state.update_status(stat_key,
                                                "ONLINE" if active_count == 0 else "IN_MATCH",
                                                active_count)
                    except Exception:
                        pass
                    now = asyncio.get_running_loop().time()
                    if now - last_start_time >= START_MATCH_INTERVAL:
                        await send_start_match()
                    try:
                        data = await asyncio.wait_for(reader.read(8192), timeout=0.5)
                    except asyncio.TimeoutError:
                        no_response_count += 1
                        if no_response_count > 80:
                            print_warning(f"[FUNCTIONAL] Gateway silent ({uid_str}). Reconnecting...")
                            raise ConnectionError("Gateway idle timeout")
                        continue
                    if not data:
                        raise ConnectionError("Connection closed by server")
                    hex_data = data.hex()
                    packet_length = len(data)
                    no_response_count = 0
                    if hex_data.startswith("0300") and 10 < packet_length < 30:
                        print_info("Match starting, please wait...")
                        continue
                    if hex_data.startswith("0300") and packet_length >= 300:
                        print_colored("=" * 60, Colors.GREEN)
                        print_colored(f"MATCH FOUND! Loading...", Colors.GREEN)
                        print_colored("=" * 60, Colors.GREEN)
                        try:
                            res = json.loads(await decode_protobuf(hex_data[10:]))
                            token = None
                            udp_key = None
                            match_code = None
                            server_ip_port = None
                            match_account_id = None
                            block_val = None
                            if '42' in res and 'data' in res['42']:
                                match_code = res['42']['data']
                            if '5' in res and 'data' in res['5']:
                                res_field5 = res['5']['data']
                                server_ip_port = res_field5.get('2', {}).get('data')
                                udp_key = res_field5.get('3', {}).get('data')
                                token = res_field5.get('4', {}).get('data')
                                if '42' in res_field5:
                                    match_code = res_field5['42']['data']
                            if '1' in res and 'data' in res['1']:
                                match_account_id = res['1']['data']
                            if '5' in res and 'data' in res['5']:
                                block_val = res['5']['data'].get('1', {}).get('data')
                            effective_acc_id = match_account_id or account_id or "BD_BOT"
                            if token and udp_key and match_code and server_ip_port:
                                mo = ""
                                if current_account_data:
                                    mo = str(current_account_data.get("mode_override") or "")
                                cur_mode = resolve_match_mode(stat_key, mo)
                                acc_tok = ""
                                if current_account_data:
                                    acc_tok = current_account_data.get('access_token', '') or ""
                                thunder, sharma = await build_match_startup_packets(
                                    token, udp_key, match_code, effective_acc_id, block_val or 0,
                                    server_ip=server_ip_port,
                                    region=account_region,
                                    client_version=client_version,
                                    access_token=acc_tok,
                                    mode=cur_mode
                                )
                                match_index = await _inc_match(stat_key)
                                total = await _get_total_match_count()
                                print_colored(f"🚀 [{cur_mode}] MATCH #{match_index} UDP starting → {server_ip_port}", Colors.CYAN)
                                print_success(f"[FUNCTIONAL] UDP task started ({cur_mode}). Card active: {match_index} | Total: {total}")
                                new_match = asyncio.create_task(
                                    play_game(
                                        server_ip_port, thunder, sharma, udp_key, match_code,
                                        effective_acc_id, "BD", client_version,
                                        current_key, current_iv,
                                        match_index=match_index, card_key=stat_key
                                    )
                                )
                                play_matches.append(new_match)
                                consecutive_parse_failures = 0
                                try:
                                    writer.close()
                                    await writer.wait_closed()
                                except Exception:
                                    pass
                                print_info(f"[OFFLINE] {NEW_MATCH_DELAY}s offline → reload token → new StartMatch")

                                # 🔥 BR = SEQUENTIAL | LW = PARALLEL
                                if cur_mode == "BR":
                                    print_info(f"[BR] Waiting for Match #{match_index} to finish (sequential mode)...")
                                    try:
                                        await new_match
                                    except Exception as ex:
                                        print_error(f"[BR] wait error: {ex}")
                                    play_matches[:] = [m for m in play_matches if not m.done()]
                                    print_success(f"[BR] Match #{match_index} finished → next search")
                                else:
                                    print_info(f"[LW] Match #{match_index} running in parallel (background)")

                                await asyncio.sleep(NEW_MATCH_DELAY)
                                reconnects = 0
                                break
                            else:
                                consecutive_parse_failures += 1
                                print_warning(f"[FUNCTIONAL] Non-match big packet (#{consecutive_parse_failures}/{MAX_CONSECUTIVE_PARSE_FAILURES}) → reconnecting")
                                if consecutive_parse_failures >= MAX_CONSECUTIVE_PARSE_FAILURES:
                                    print_error(f"[FUNCTIONAL] {MAX_CONSECUTIVE_PARSE_FAILURES}x parse failures → invalidating cache")
                                    if current_account_data:
                                        try:
                                            if current_account_data.get('auth_uid'):
                                                cache_invalidate(str(current_account_data['auth_uid']))
                                            if current_account_data.get('auth_token'):
                                                cache_invalidate(f"tok_{current_account_data['auth_token'][:20]}")
                                        except Exception:
                                            pass
                                    consecutive_parse_failures = 0
                                try:
                                    writer.close()
                                    await writer.wait_closed()
                                except Exception:
                                    pass
                                await asyncio.sleep(NON_MATCH_RECONNECT_DELAY)
                                break
                        except Exception as e:
                            print_error(f"[FUNCTIONAL] Match packet error: {e}")
                            consecutive_parse_failures += 1
                            if consecutive_parse_failures >= MAX_CONSECUTIVE_PARSE_FAILURES:
                                if current_account_data:
                                    try:
                                        if current_account_data.get('auth_uid'):
                                            cache_invalidate(str(current_account_data['auth_uid']))
                                        if current_account_data.get('auth_token'):
                                            cache_invalidate(f"tok_{current_account_data['auth_token'][:20]}")
                                    except Exception:
                                        pass
                                consecutive_parse_failures = 0
                            try:
                                writer.close()
                                await writer.wait_closed()
                            except Exception:
                                pass
                            await asyncio.sleep(NON_MATCH_RECONNECT_DELAY)
                            break
                    if 30 <= packet_length <= 40:
                        continue
            except asyncio.CancelledError:
                print_warning(f"[FUNCTIONAL] Cancelled — cancelling {len(play_matches)} UDP matches")
                for m in play_matches:
                    if not m.done():
                        m.cancel()
                if play_matches:
                    await asyncio.gather(*play_matches, return_exceptions=True)
                play_matches.clear()
                raise
            except Exception as e:
                print_error(f"[FUNCTIONAL] TCP state ({uid_str}): {e}")
                play_matches[:] = [m for m in play_matches if not m.done()]
                if writer:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                if "Cache expired" in str(e):
                    print_warning(f"[FUNCTIONAL] Triggering re-login for {uid_str}")
                    break
                reconnects += 1
                if reconnects > max_reconnects:
                    print_error("[FUNCTIONAL] Max reconnects reached, retrying...")
                    reconnects = 0
                    await asyncio.sleep(3)
                    continue
                await asyncio.sleep(min(reconnects, 2))
    except asyncio.CancelledError:
        print_warning(f"[FUNCTIONAL] Outer cancelled. {len(play_matches)} UDP matches still running.")
        for m in play_matches:
            if not m.done():
                m.cancel()
        if play_matches:
            await asyncio.gather(*play_matches, return_exceptions=True)
        play_matches.clear()
        raise


async def informational(addrs, starter_packet, key, iv, region="BD", max_reconnects=3):
    reconnects = 0
    ip, port = addrs.split(":")
    while True:
        writer = None
        ping_task = None
        try:
            resolved_ip = await resolve_host_cloudflare(ip)
            reader, writer = await asyncio.open_connection(resolved_ip, int(port))
            raw_sock = writer.get_extra_info('socket')
            if raw_sock:
                optimize_tcp_socket(raw_sock)
            writer.write(bytes.fromhex(starter_packet))
            await writer.drain()
            reconnects = 0
            try:
                init_ka = await send_keep_alive(region)
                if init_ka and writer and not writer.is_closing():
                    writer.write(init_ka)
                    await asyncio.wait_for(writer.drain(), timeout=3)
            except Exception:
                pass

            async def info_keepalive():
                ka_bytes = await send_keep_alive(region)
                while True:
                    await asyncio.sleep(5)
                    try:
                        if writer and not writer.is_closing():
                            writer.write(ka_bytes)
                            await writer.drain()
                    except Exception:
                        break

            ping_task = asyncio.create_task(info_keepalive())
            while True:
                data = await reader.read(8192)
                if not data:
                    raise ConnectionError("Connection closed")
        except asyncio.CancelledError:
            if ping_task:
                ping_task.cancel()
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
            raise
        except Exception:
            if ping_task:
                ping_task.cancel()
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
            reconnects += 1
            if reconnects > max_reconnects:
                await asyncio.sleep(3)
                reconnects = 0
            else:
                await asyncio.sleep(1)


def _register_credentials(account_data: Dict):
    try:
        acc_id = str(account_data['account_id'])
        bot_state.account_credentials[acc_id] = account_data
        if account_data.get('auth_uid'):
            bot_state.account_credentials[str(account_data['auth_uid'])] = account_data
        if account_data.get('auth_token'):
            bot_state.account_credentials[f"tok_{account_data['auth_token'][:20]}"] = account_data
    except Exception:
        pass


async def refresh_account_profile(account_data_or_uid: Any):
    try:
        if isinstance(account_data_or_uid, str):
            uid = str(account_data_or_uid)
            account_data = bot_state.account_credentials.get(uid)
        else:
            account_data = account_data_or_uid
            uid = str(account_data.get('account_id')) if account_data else ""
        if not account_data:
            return
        url = account_data.get('server_url')
        token = account_data.get('token')
        release_version = account_data.get('release_version')
        payload = account_data.get('login_payload_data')
        if not (url and token and release_version and payload):
            return
        res = await send_getlogin(payload, url, token, release_version)
        if res:
            res_proto, dict_res = res
            level = int(get_proto_field(dict_res, 6, 1))
            exp = int(get_proto_field(dict_res, 7, 0))
            likes = int(get_proto_field(dict_res, 8, 0))
            nickname = res_proto.nickname or get_proto_field(dict_res, 4, "")
            acc_id = str(account_data['account_id'])
            card_key = None
            if account_data.get('auth_uid'):
                card_key = str(account_data['auth_uid'])
            elif account_data.get('auth_token'):
                card_key = f"tok_{account_data['auth_token'][:20]}"
            else:
                card_key = acc_id
            old_lvl = 1
            old_exp = 0
            if card_key in bot_state.accounts:
                old_lvl = bot_state.accounts[card_key].get("level", 1)
                old_exp = bot_state.accounts[card_key].get("current_exp", 0)
            if exp > 0:
                bot_state.update_exp(card_key, exp, level)
            if likes > 0 and card_key in bot_state.accounts:
                bot_state.accounts[card_key]["likes"] = likes
            if nickname and card_key in bot_state.accounts:
                bot_state.accounts[card_key]["nickname"] = nickname
            lvl_changed = level > old_lvl
            exp_changed = exp > old_exp
            if lvl_changed:
                print_success(f"🎉 LEVEL UP! {nickname} → Level {level} (+{exp - old_exp} EXP)")
            elif exp_changed:
                print_success(f"✅ {nickname} +{exp - old_exp} EXP (Lv{level}, Total {exp})")
            else:
                print_info(f"[EXP-REFRESH] {card_key} -> Level: {level}, EXP: {exp} (no change)")
            if bot_state.is_target_reached(card_key):
                target = bot_state.accounts[card_key].get("target_level", 0)
                nickname2 = bot_state.accounts[card_key].get("nickname", card_key)
                bot_state.update_status(card_key, "COMPLETE")
                bot_state.log(f"🎉 {nickname2} reached target Lv{target} — AUTO DELETED", "success", card_key)
                print_success(f"[TARGET] {card_key} ({nickname2}) reached Level {target} → AUTO DELETING")
                for key in list(bot_state.account_workers.keys()):
                    should_cancel = False
                    if key == card_key:
                        should_cancel = True
                    else:
                        cred2 = bot_state.account_credentials.get(key)
                        if cred2 and str(cred2.get('account_id')) == acc_id:
                            should_cancel = True
                    if should_cancel:
                        try:
                            bot_state.account_workers[key].cancel()
                        except Exception:
                            pass
                        bot_state.account_workers.pop(key, None)
                await auto_delete_completed_account(card_key)
                return
    except Exception as e:
        print_error(f"refresh_account_profile error: {e}")


# ==================== LOGIN — GUEST ====================
async def process_account_uid_pass(uid: str, password: str, register_key: str = None) -> Optional[Dict]:
    cached = cache_get(uid)
    if cached:
        acc_id_check = str(cached.get('account_id', '')).strip()
        if not acc_id_check or acc_id_check == "0" or not acc_id_check.isdigit():
            print_warning(f"[CACHE HIT] UID {uid} invalid game_id → forcing fresh login")
            cache_invalidate(uid)
            cached = None
        else:
            print_success(f"[CACHE HIT] UID {uid} loaded (game_id={acc_id_check}, no login)")

    if cached:
        acc_id = str(cached['account_id'])
        key = register_key or uid
        target = 0
        try:
            for acc in load_accounts():
                if str(acc.get("uid")) == uid:
                    target = int(acc.get("target_level", 0) or 0)
                    break
        except Exception:
            pass
        bot_state.register_account(
            uid=key,
            nickname=cached.get('nickname', f"Player_{acc_id}"),
            region=cached.get('region', 'BD'),
            level=cached.get('level', 1),
            exp=cached.get('exp', 0),
            likes=cached.get('likes', 0),
            target_level=target,
            game_uid=acc_id
        )
        _register_credentials(cached)
        return cached

    print_info(f"[LOGIN] Full login for UID {uid}...")
    try:
        print_info(f"[LOGIN-1/5] Getting version config...")
        verconfig_res = await version_config()
        if verconfig_res is None:
            print_error(f"[LOGIN-1/5] FAILED — version_config returned None")
            return None
        release_version, client_version, server_url = verconfig_res
        print_success(f"[LOGIN-1/5] OK — version: {client_version}, release: {release_version}")

        print_info(f"[LOGIN-2/5] Getting access token...")
        tokengrant_response = await get_access_token(uid, password)
        if tokengrant_response is None:
            print_error(f"[LOGIN-2/5] FAILED — get_access_token returned None")
            return None
        open_id, access_token, platform = tokengrant_response
        print_success(f"[LOGIN-2/5] OK — open_id: {open_id[:12]}..., platform: {platform}")

        print_info(f"[LOGIN-3/5] Building MajorLogin payload...")
        device_info = get_device_for_account(uid)
        login_payload_data = await build_majorlogin_payload(open_id, access_token, platform, client_version, device_info)
        if login_payload_data is None:
            print_error(f"[LOGIN-3/5] FAILED")
            return None
        print_success(f"[LOGIN-3/5] OK")

        print_info(f"[LOGIN-4/5] Sending MajorLogin...")
        majorlogin_response = await send_majorlogin(login_payload_data, release_version, server_url)
        if majorlogin_response is None:
            print_error(f"[LOGIN-4/5] FAILED")
            return None
        print_success(f"[LOGIN-4/5] OK — account_id: {majorlogin_response.account_id}")

        acc_id = str(majorlogin_response.account_id)
        if not acc_id or acc_id == "0" or not acc_id.isdigit():
            print_error(f"[LOGIN] Invalid game_id '{acc_id}' from MajorLogin for {uid}")
            return None

        print_info(f"[LOGIN-5/5] Sending GetLoginData...")
        getlogin_result = await send_getlogin(login_payload_data, majorlogin_response.url,
                                              majorlogin_response.token, release_version)
        if getlogin_result is None:
            print_error(f"[LOGIN-5/5] FAILED")
            return None
        res_proto, dict_res = getlogin_result
        print_success(f"[LOGIN-5/5] OK")

        level = int(get_proto_field(dict_res, 6, 1))
        exp = int(get_proto_field(dict_res, 7, 0))
        likes = int(get_proto_field(dict_res, 8, 0))
        nickname = res_proto.nickname or get_proto_field(dict_res, 4, f"Player_{acc_id}")
        region = majorlogin_response.region or get_proto_field(dict_res, 3, "BD")

        target = 0
        try:
            for acc in load_accounts():
                if str(acc.get("uid")) == uid:
                    target = int(acc.get("target_level", 0) or 0)
                    break
        except Exception:
            pass

        key = register_key or uid
        bot_state.register_account(
            uid=key, nickname=nickname, region=region, level=level, exp=exp,
            likes=likes, target_level=target, game_uid=acc_id
        )
        account_data = {
            'account_id': majorlogin_response.account_id,
            'nickname': nickname, 'region': region, 'level': level, 'exp': exp, 'likes': likes,
            'open_id': open_id, 'access_token': access_token, 'platform': str(platform),
            'token': majorlogin_response.token,
            'server_time': majorlogin_response.server_time,
            'aes_ak': majorlogin_response.aes_ak, 'iv_i': majorlogin_response.iv_i,
            'functional_addrs': res_proto.functional_addrs or get_proto_field(dict_res, 14),
            'informational_addrs': res_proto.informational_addrs or get_proto_field(dict_res, 32),
            'release_version': release_version, 'client_version': client_version,
            'server_url': majorlogin_response.url, 'login_payload_data': login_payload_data,
            'auth_type': 'guest', 'auth_uid': uid, 'auth_password': password
        }
        _register_credentials(account_data)
        cache_set(uid, account_data)
        print_success(f"[LOGIN] ✅ SUCCESS for UID {uid} → {nickname} (Lv{level})")
        return account_data
    except Exception as e:
        print_error(f"process_account_uid_pass error: {e}")
        return None


async def process_account_token(access_token: str, register_key: str = None) -> Optional[Dict]:
    cache_key = f"tok_{access_token[:20]}"
    cached = cache_get(cache_key)
    if cached:
        acc_id_check = str(cached.get('account_id', '')).strip()
        if not acc_id_check or acc_id_check == "0" or not acc_id_check.isdigit():
            cache_invalidate(cache_key)
            cached = None
        else:
            print_success(f"[CACHE HIT] Token {access_token[:10]}... loaded")
    if cached:
        acc_id = str(cached['account_id'])
        key = register_key or cache_key
        target = 0
        try:
            for acc in load_accounts():
                if acc.get("token") == access_token:
                    target = int(acc.get("target_level", 0) or 0)
                    break
        except Exception:
            pass
        bot_state.register_account(
            uid=key, nickname=cached.get('nickname', f"Player_{acc_id}"),
            region=cached.get('region', 'BD'), level=cached.get('level', 1),
            exp=cached.get('exp', 0), likes=cached.get('likes', 0),
            target_level=target, game_uid=acc_id
        )
        _register_credentials(cached)
        return cached
    print_info("[LOGIN] Full login with Access Token...")
    try:
        verconfig_res = await version_config()
        if verconfig_res is None:
            return None
        release_version, client_version, server_url = verconfig_res
        import requests
        url = f"https://100067.connect.garena.com/oauth/token/inspect?token={access_token}"
        hdrs = {
            "Accept-Encoding": "gzip, deflate, br", "Connection": "close",
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": "100067.connect.garena.com",
            "User-Agent": "GarenaMSDK/4.0.19P4(G011A ;Android 9;en;US;)"
        }
        resp = await asyncio.to_thread(requests.get, url, headers=hdrs, timeout=10)
        data = resp.json()
        if 'error' in data:
            return None
        open_id = data.get('open_id')
        platform = data.get('platform', 4)
        if not open_id:
            return None
        device_info = get_device_for_account(open_id)
        login_payload_data = await build_majorlogin_payload(open_id, access_token, str(platform),
                                                            client_version, device_info)
        if not login_payload_data:
            return None
        majorlogin_response = await send_majorlogin(login_payload_data, release_version, server_url)
        if majorlogin_response is None:
            return None
        acc_id = str(majorlogin_response.account_id)
        if not acc_id or acc_id == "0" or not acc_id.isdigit():
            return None
        getlogin_result = await send_getlogin(login_payload_data, majorlogin_response.url,
                                              majorlogin_response.token, release_version)
        if getlogin_result is None:
            return None
        res_proto, dict_res = getlogin_result
        level = int(get_proto_field(dict_res, 6, 1))
        exp = int(get_proto_field(dict_res, 7, 0))
        likes = int(get_proto_field(dict_res, 8, 0))
        nickname = res_proto.nickname or get_proto_field(dict_res, 4, f"Player_{acc_id}")
        region = majorlogin_response.region or get_proto_field(dict_res, 3, "BD")
        target = 0
        try:
            for acc in load_accounts():
                if acc.get("token") == access_token:
                    target = int(acc.get("target_level", 0) or 0)
                    break
        except Exception:
            pass
        key = register_key or cache_key
        bot_state.register_account(
            uid=key, nickname=nickname, region=region, level=level, exp=exp,
            likes=likes, target_level=target, game_uid=acc_id
        )
        account_data = {
            'account_id': majorlogin_response.account_id,
            'nickname': nickname, 'region': region, 'level': level, 'exp': exp, 'likes': likes,
            'open_id': open_id, 'access_token': access_token, 'platform': str(platform),
            'token': majorlogin_response.token,
            'server_time': majorlogin_response.server_time,
            'aes_ak': majorlogin_response.aes_ak, 'iv_i': majorlogin_response.iv_i,
            'functional_addrs': res_proto.functional_addrs or get_proto_field(dict_res, 14),
            'informational_addrs': res_proto.informational_addrs or get_proto_field(dict_res, 32),
            'release_version': release_version, 'client_version': client_version,
            'server_url': majorlogin_response.url, 'login_payload_data': login_payload_data,
            'auth_type': 'token', 'auth_token': access_token
        }
        _register_credentials(account_data)
        cache_set(cache_key, account_data)
        return account_data
    except Exception as e:
        print_error(f"process_account_token error: {e}")
        return None


# ==================== ACCOUNT WORKER ====================
async def run_account_worker(account_data: Dict, label: str, card_key: str = None):
    acc_id = str(account_data['account_id'])
    stat_key = card_key or acc_id
    informational_task = None
    exp_task = None

    if not acc_id or acc_id == "0" or not acc_id.isdigit():
        print_error(f"[WORKER] Refuse — invalid game_id='{acc_id}' for {label}")
        try:
            bot_state.update_status(stat_key, "ERROR")
        except Exception:
            pass
        return

    try:
        reg = account_data.get('region', 'BD')
        tcp_packet_online = await build_tcp_startup_packet(
            account_data['account_id'], account_data['token'], account_data['server_time'],
            account_data['aes_ak'], account_data['iv_i'], region=reg, typ='OnLine'
        )
        tcp_packet_chat = await build_tcp_startup_packet(
            account_data['account_id'], account_data['token'], account_data['server_time'],
            account_data['aes_ak'], account_data['iv_i'], region=reg, typ='ChaT'
        )
        informational_task = asyncio.create_task(
            informational(account_data['informational_addrs'], tcp_packet_chat,
                          account_data['aes_ak'], account_data['iv_i'], region=reg)
        )

        async def exp_refresher():
            while True:
                await asyncio.sleep(EXP_REFRESH_INTERVAL)
                if stat_key not in bot_state.accounts:
                    print_warning(f"[EXP-REFRESHER] {stat_key} removed → stopping")
                    break
                fresh = bot_state.account_credentials.get(stat_key)
                if fresh:
                    try:
                        await refresh_account_profile(fresh)
                    except Exception:
                        pass
                if bot_state.is_target_reached(stat_key):
                    break

        exp_task = asyncio.create_task(exp_refresher())
        functional_task = asyncio.create_task(
            functional_lone_wolf(
                account_data['functional_addrs'], tcp_packet_online,
                account_data['region'], account_data['client_version'],
                account_data['aes_ak'], account_data['iv_i'],
                account_id=acc_id, account_data=account_data, card_key=stat_key
            )
        )
        await functional_task
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print_error(f"run_account_worker error for {label}: {e}")
    finally:
        for t in (informational_task, exp_task):
            if t and not t.done():
                t.cancel()
        for t in (informational_task, exp_task):
            if t:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


async def account_loop_guest(uid: str, password: str, card_key: str = None):
    stat_key = card_key or uid
    while True:
        try:
            if stat_key not in bot_state.accounts:
                print_warning(f"[WORKER] {stat_key} removed → exiting loop")
                return
            if bot_state.account_states.get(stat_key) == "stopped":
                print_warning(f"[WORKER] {stat_key} stopped manually — exiting loop")
                return
            print_info(f"[LOGIN] Starting login for Guest UID: {uid}...")
            try:
                bot_state.update_status(stat_key, "CONNECTING")
            except Exception:
                pass
            account_data = await process_account_uid_pass(uid, password, register_key=stat_key)
            if not account_data:
                print_error(f"Login failed for UID: {uid}. Retrying in 15 seconds...")
                try:
                    bot_state.update_status(stat_key, "ERROR")
                except Exception:
                    pass
                await asyncio.sleep(15)
                continue
            if bot_state.is_target_reached(stat_key):
                bot_state.update_status(stat_key, "COMPLETE")
                bot_state.log(f"{stat_key} already at target — not running", "success", stat_key)
                await auto_delete_completed_account(stat_key)
                return
            await run_account_worker(account_data, uid, card_key=stat_key)
            print_warning(f"Session finished for {uid}. Reconnecting in 3s...")
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            print_warning(f"Worker for {uid} stopped.")
            try:
                if stat_key in bot_state.accounts:
                    bot_state.update_status(stat_key, "STOPPED")
            except Exception:
                pass
            break
        except Exception as e:
            print_error(f"Error for UID {uid}: {e}. Retrying in 10s...")
            await asyncio.sleep(10)


async def account_loop_token(token: str, card_key: str = None):
    stat_key = card_key or f"tok_{token[:20]}"
    token_label = token[:10]
    while True:
        try:
            if stat_key not in bot_state.accounts:
                print_warning(f"[WORKER] token {token_label} removed → exiting loop")
                return
            if bot_state.account_states.get(stat_key) == "stopped":
                print_warning(f"[WORKER] token {token_label} stopped manually — exiting loop")
                return
            print_info("[LOGIN] Starting login with Access Token...")
            account_data = await process_account_token(token, register_key=stat_key)
            if not account_data:
                print_error("Login failed for Token. Retrying in 15 seconds...")
                await asyncio.sleep(15)
                continue
            if bot_state.is_target_reached(stat_key):
                bot_state.update_status(stat_key, "COMPLETE")
                await auto_delete_completed_account(stat_key)
                return
            await run_account_worker(account_data, str(account_data['account_id']), card_key=stat_key)
            print_warning("Token session finished. Reconnecting in 3s...")
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            print_warning(f"Worker for token {token_label} stopped.")
            break
        except Exception as e:
            print_error(f"Token error: {e}. Retrying in 10s...")
            await asyncio.sleep(10)


# ==================== LOAD / SAVE ====================
def load_accounts():
    accounts = []
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8-sig") as f:
                content = f.read().strip()
            if content:
                data = json.loads(content)
                if isinstance(data, list):
                    accounts = data
        except Exception as e:
            print_error(f"Could not load {ACCOUNTS_FILE}: {e}")
    if not accounts and FALLBACK_UID and FALLBACK_PASSWORD:
        accounts.append({"uid": FALLBACK_UID, "password": FALLBACK_PASSWORD})
    return accounts


def _save_accounts_list(accounts: List[Dict]):
    try:
        tmp = ACCOUNTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2)
        os.replace(tmp, ACCOUNTS_FILE)
    except Exception as e:
        print_error(f"Could not save {ACCOUNTS_FILE}: {e}")


def get_dashboard_url() -> str:
    railway_domain = (
        os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        or os.environ.get("RAILWAY_STATIC_URL")
        or os.environ.get("RAILWAY_URL")
    )
    if railway_domain:
        if not railway_domain.startswith("http"):
            railway_domain = f"https://{railway_domain}"
        return railway_domain
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
        finally:
            s.close()
        if lan_ip and lan_ip != "127.0.0.1":
            return f"http://{lan_ip}:{WEB_PORT}"
    except Exception:
        pass
    return ""


def _update_target_in_file(uid, target_level):
    if target_level <= 0:
        return
    try:
        all_accs = load_accounts()
        for a in all_accs:
            if str(a.get("uid")) == str(uid):
                a["target_level"] = int(target_level)
                break
        _save_accounts_list(all_accs)
    except Exception:
        pass


def _bulk_delete_account(uid):
    uid_str = str(uid).strip()
    if not uid_str:
        return
    try:
        all_accs = load_accounts()
        new_list = [a for a in all_accs if str(a.get("uid")).strip() != uid_str]
        _save_accounts_list(new_list)
    except Exception as e:
        print_error(f"[BULK-DELETE] accounts.json error: {e}")
    try:
        if os.path.exists(TOKEN_CACHE_FILE):
            with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
                c = f.read().strip()
            cache_data = json.loads(c) if c else {}
            if uid_str in cache_data:
                del cache_data[uid_str]
            tmp = TOKEN_CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2)
            os.replace(tmp, TOKEN_CACHE_FILE)
            global _token_cache_memo, _token_cache_memo_time
            _token_cache_memo = {}
            _token_cache_memo_time = 0.0
    except Exception as e:
        print_error(f"[BULK-DELETE] token_cache error: {e}")
    try:
        bot_state.accounts.pop(uid_str, None)
        bot_state.account_credentials.pop(uid_str, None)
        bot_state.account_states.pop(uid_str, None)
    except Exception:
        pass


def register_placeholder_accounts(accounts: List[Dict]):
    for acc in accounts:
        if "uid" in acc and acc.get("uid"):
            guest_uid = str(acc["uid"]).strip()
            target_lvl = int(acc.get("target_level", 0) or 0)
            if guest_uid not in bot_state.accounts:
                bot_state.accounts[guest_uid] = {
                    "uid": guest_uid,
                    "game_uid": str(acc.get("account_id", guest_uid)),
                    "nickname": acc.get("nickname") or (f"Player_{guest_uid[-6:]}" if len(guest_uid) > 6 else f"Player_{guest_uid}"),
                    "region": "—", "level": 0,
                    "initial_exp": 0, "current_exp": 0, "gained_exp": 0, "likes": 0,
                    "status": "OFFLINE", "target_level": target_lvl,
                    "matches_played": 0, "active_matches": 0,
                    "last_match_time": None,
                    "last_updated": time.strftime("%H:%M:%S"),
                    "completed_at": None, "mode_override": "AUTO"
                }
        elif "token" in acc and acc.get("token"):
            token_key = f"tok_{acc['token'][:20]}"
            target_lvl = int(acc.get("target_level", 0) or 0)
            if token_key not in bot_state.accounts:
                bot_state.accounts[token_key] = {
                    "uid": token_key,
                    "game_uid": str(acc.get("account_id", token_key)),
                    "nickname": f"Token_{acc['token'][:6]}",
                    "region": "—", "level": 0,
                    "initial_exp": 0, "current_exp": 0, "gained_exp": 0, "likes": 0,
                    "status": "OFFLINE", "target_level": target_lvl,
                    "matches_played": 0, "active_matches": 0,
                    "last_match_time": None,
                    "last_updated": time.strftime("%H:%M:%S"),
                    "completed_at": None, "mode_override": "AUTO"
                }


# ==================== MAIN ====================
async def main():
    print_colored("=" * 60, Colors.CYAN)
    print_colored("    TEAM 84FF - FreeFire Level Up Bot (BR/LW Auto)", Colors.GREEN)
    print_colored("   Bulk Upload + Sequential Login + Auto-Start", Colors.WHITE)
    print_colored("=" * 60, Colors.CYAN)
    print_info(f"Start Match Interval: {START_MATCH_INTERVAL}s")
    print_info(f"Mode: Lvl < {BR_TO_LW_LEVEL} → BR | Lvl >= {BR_TO_LW_LEVEL} → LW")
    print_info(f"BR: sequential | LW: parallel")
    print_info(f"Auto EXP Refresh: {int(EXP_REFRESH_INTERVAL)}s")
    print_info(f"Target Reached: AUTO DELETE from all storage")
    print_colored("=" * 60, Colors.CYAN)

    try:
        await start_web_dashboard(host=WEB_HOST, port=WEB_PORT)
        public_url = get_dashboard_url()
        if public_url:
            print_success(f"🌐 Dashboard URL: {public_url}")
        else:
            print_success(f"📡 Dashboard listening on port {WEB_PORT}")
    except Exception as e:
        print_error(f"Could not start web dashboard: {e}")

    # ============ ON ACCOUNT ADDED ============
    async def on_account_added_handler(data):
        try:
            await asyncio.sleep(0.3)
            if "uid" in data and data.get("uid"):
                guest_uid = str(data["uid"]).strip()
                password = str(data.get("password", "")).strip()
                if not guest_uid or not password:
                    return
                account_data = await process_account_uid_pass(guest_uid, password, register_key=guest_uid)
                if account_data:
                    real_acc_id = str(account_data["account_id"])
                    all_accs = load_accounts()
                    for a in all_accs:
                        if str(a.get("uid")) == guest_uid:
                            a["account_id"] = real_acc_id
                            if account_data.get("nickname"):
                                a["nickname"] = account_data["nickname"]
                            break
                    _save_accounts_list(all_accs)
                    bot_state.account_credentials[guest_uid] = account_data
                    bot_state.account_credentials[real_acc_id] = account_data
                    if guest_uid in bot_state.accounts:
                        bot_state.accounts[guest_uid]["status"] = "OFFLINE"
                    print_success(f"[ADD-INFO] ✅ {account_data.get('nickname')} — "
                                  f"Lv{account_data.get('level')} — {account_data.get('exp')} EXP")
                    try:
                        r = await on_start_account_handler(guest_uid)
                        if r:
                            print_success(f"[AUTO-START] {guest_uid} started")
                    except Exception as e:
                        print_error(f"[AUTO-START] {guest_uid} error: {e}")
            elif "token" in data and data.get("token"):
                token = str(data["token"]).strip()
                if not token:
                    return
                token_key = f"tok_{token[:20]}"
                account_data = await process_account_token(token, register_key=token_key)
                if account_data:
                    real_acc_id = str(account_data["account_id"])
                    all_accs = load_accounts()
                    for a in all_accs:
                        if a.get("token") == token:
                            a["account_id"] = real_acc_id
                            if account_data.get("nickname"):
                                a["nickname"] = account_data["nickname"]
                            break
                    _save_accounts_list(all_accs)
                    bot_state.account_credentials[token_key] = account_data
                    bot_state.account_credentials[real_acc_id] = account_data
                    if token_key in bot_state.accounts:
                        bot_state.accounts[token_key]["status"] = "OFFLINE"
                    print_success(f"[ADD-INFO] ✅ Token: {account_data.get('nickname')} — Lv{account_data.get('level')}")
                    try:
                        r = await on_start_account_handler(token_key)
                        if r:
                            print_success(f"[AUTO-START] {token_key} started")
                    except Exception as e:
                        print_error(f"[AUTO-START] error: {e}")
        except Exception as e:
            print_error(f"[ADD-INFO] handler error: {e}")

    # ============ START ============
    async def on_start_account_handler(uid):
        uid_str = str(uid)
        if uid_str in bot_state.account_workers:
            t = bot_state.account_workers[uid_str]
            if not t.done():
                print_info(f"[START] {uid_str} already running")
                return True
        all_accs = load_accounts()
        match = None
        card_key = None
        for a in all_accs:
            if str(a.get("uid")) == uid_str:
                match = a
                card_key = str(a.get("uid"))
                break
            if a.get("token") and f"tok_{a['token'][:20]}" == uid_str:
                match = a
                card_key = f"tok_{a['token'][:20]}"
                break
            if str(a.get("account_id", "")) == uid_str:
                match = a
                if a.get("uid"):
                    card_key = str(a.get("uid"))
                elif a.get("token"):
                    card_key = f"tok_{a['token'][:20]}"
                break
        if not match:
            print_error(f"[START] UID {uid_str} not found in accounts.json")
            return False
        if match.get("uid"):
            account_data = await process_account_uid_pass(match["uid"], match["password"], register_key=card_key)
        elif match.get("token"):
            account_data = await process_account_token(match["token"], register_key=card_key)
        else:
            return False
        if not account_data:
            print_error(f"[START] Login failed for {uid_str}")
            return False
        real_acc_id = str(account_data["account_id"])
        if real_acc_id != card_key and real_acc_id in bot_state.accounts:
            del bot_state.accounts[real_acc_id]
        for a in all_accs:
            matched = False
            if a.get("uid") and str(a.get("uid")) == match.get("uid"):
                matched = True
            elif a.get("token") and match.get("token") and a.get("token") == match.get("token"):
                matched = True
            if matched:
                a["account_id"] = real_acc_id
                if account_data.get("nickname"):
                    a["nickname"] = account_data["nickname"]
                break
        _save_accounts_list(all_accs)
        bot_state.account_credentials[card_key] = account_data
        bot_state.account_credentials[real_acc_id] = account_data
        bot_state.account_states[card_key] = "running"
        bot_state.update_status(card_key, "ONLINE")
        if match.get("uid"):
            task = asyncio.create_task(account_loop_guest(match["uid"], match["password"], card_key=card_key))
        else:
            task = asyncio.create_task(account_loop_token(match["token"], card_key=card_key))
        bot_state.account_workers[card_key] = task
        return True

    async def on_stop_account_handler(uid):
        uid_str = str(uid)
        bot_state.account_states[uid_str] = "stopped"
        for key in list(bot_state.account_workers.keys()):
            should_cancel = False
            if key == uid_str:
                should_cancel = True
            else:
                cred = bot_state.account_credentials.get(key)
                if cred and (str(cred.get('account_id')) == uid_str or str(cred.get('auth_uid')) == uid_str):
                    should_cancel = True
            if should_cancel:
                try:
                    bot_state.account_workers[key].cancel()
                except Exception:
                    pass
                del bot_state.account_workers[key]
        if uid_str in bot_state.accounts:
            if bot_state.accounts[uid_str].get("status") != "COMPLETE":
                bot_state.accounts[uid_str]["status"] = "STOPPED"
        return True

    async def on_refresh_account_handler(uid):
        await refresh_account_profile(uid)

    # ============ BULK UPLOAD ============
    async def on_bulk_upload_handler(entries, target_level):
        total = len(entries)
        ok_count = 0
        fail_count = 0
        skipped = 0
        started = 0
        print_info(f"[BULK] Sequential login for {total} accounts (target Lv{target_level})")
        for idx, entry in enumerate(entries, 1):
            uid = str(entry["uid"]).strip()
            password = str(entry["password"]).strip()
            cached = cache_get(uid)
            if cached:
                print_success(f"[BULK {idx}/{total}] ✓ {uid} cached → auto-start")
                try:
                    acc = bot_state.accounts.get(uid)
                    if acc and target_level > 0:
                        acc["target_level"] = target_level
                    r = await on_start_account_handler(uid)
                    if r:
                        started += 1
                except Exception as ex:
                    print_error(f"[BULK] auto-start err: {ex}")
                skipped += 1
                continue
            success = False
            last_err = None
            for attempt in range(1, 4):
                print_info(f"[BULK {idx}/{total}] Login {uid} (attempt {attempt}/3)...")
                try:
                    ad = await process_account_uid_pass(uid, password, register_key=uid)
                    if ad:
                        success = True
                        print_success(f"[BULK {idx}/{total}] ✓ {uid} → {ad.get('nickname')} "
                                      f"(Lv{ad.get('level')}, EXP {ad.get('exp')})")
                        break
                    else:
                        last_err = "login returned None"
                except Exception as ex:
                    last_err = str(ex)
                    print_error(f"[BULK {idx}/{total}] {uid} attempt {attempt} error: {ex}")
                await asyncio.sleep(3)
            if success:
                ok_count += 1
                try:
                    _update_target_in_file(uid, target_level)
                    acc = bot_state.accounts.get(uid)
                    if acc and target_level > 0:
                        acc["target_level"] = target_level
                except Exception:
                    pass
                await asyncio.sleep(1)
                try:
                    r = await on_start_account_handler(uid)
                    if r:
                        started += 1
                        print_success(f"[BULK {idx}/{total}] ▶ {uid} auto-started")
                except Exception as ex:
                    print_error(f"[BULK] auto-start err: {ex}")
            else:
                fail_count += 1
                print_error(f"[BULK {idx}/{total}] ✗ {uid} failed 3x ({last_err}) → auto-deleting")
                try:
                    _bulk_delete_account(uid)
                except Exception as e:
                    print_error(f"[BULK] delete error for {uid}: {e}")
        print_success(f"[BULK] DONE. ✓{ok_count} logged | ⏭{skipped} cached | "
                      f"▶{started} started | ✗{fail_count} auto-deleted")
        bot_state.log(f"📁 Bulk login complete: +{ok_count} ok, {skipped} cached, "
                      f"{started} started, {fail_count} deleted", "success")

    bot_state.refresh_callbacks["on_account_added"] = on_account_added_handler
    bot_state.refresh_callbacks["on_start_account"] = on_start_account_handler
    bot_state.refresh_callbacks["on_stop_account"] = on_stop_account_handler
    bot_state.refresh_callbacks["on_refresh_account"] = on_refresh_account_handler
    bot_state.refresh_callbacks["on_bulk_upload"] = on_bulk_upload_handler

    accounts = load_accounts()
    register_placeholder_accounts(accounts)
    if not accounts:
        print_warning(f"No accounts found in {ACCOUNTS_FILE}! Add from Web Dashboard.")
        public_url = get_dashboard_url()
        if public_url:
            print_warning(f"🌐 Dashboard URL: {public_url}")
        else:
            print_warning(f"📡 Dashboard listening on port {WEB_PORT}")
    else:
        print_info(f"Loaded {len(accounts)} accounts (auto-start OFF — click START or upload bulk)")

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print_warning("\n[STOP] Shutting down all accounts...")
        for t in list(bot_state.account_workers.values()):
            t.cancel()
        await asyncio.gather(*bot_state.account_workers.values(), return_exceptions=True)
        print_success("All sessions cleanly closed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print_warning("\nProgram stopped by user.")
