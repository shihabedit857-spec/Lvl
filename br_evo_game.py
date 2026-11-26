# -*- coding: utf-8 -*-
"""
br_evo_game.py  —  BR-EVO(1) bot.py থেকে নেওয়া "সত্যিকারের ম্যাচে জয়েন" লজিক।

আগের app.py শুধু TCP-তে exit/create/start প্যাকেট পাঠিয়ে 90s ঘুমাত এবং সার্ভারের
পাঠানো MATCH FOUND (0300, type 5) প্যাকেট ফেলে দিত। ফলে ম্যাচ কাউন্টার বাড়ত,
কিন্তু গেম সার্ভারে আসলে ঢোকা হতো না → EXP আসত না।

এই মডিউল:
  1. TCP প্যাকেট ডিকোড করে (decode_tcp_packet)
  2. MATCH FOUND পেলে UDP গেম-সার্ভারে HELLO → 447 (JOIN_PREPARE) → 448 (JOIN_POST)
     পাঠিয়ে ম্যাচে ঢোকে এবং ম্যাচ শেষ (Cmd 103) পর্যন্ত ping/ack দিয়ে থাকে
     (play_br_evo_match)

শুধু Python stdlib লাগে — নতুন কোনো প্যাকেজ install করতে হবে না।
"""
import asyncio
import random
import struct
import time

DEFAULT_STRONGHOLD = "csoversea.stronghold.freefiremobile.com"
FINGERPRINT = "70c7ccdca4c94a26c670c1571b9578c92c49bcfc2898922a73c5008401b94759"

# একটা UDP সেশন সর্বোচ্চ কতক্ষণ খোলা থাকবে (Cmd 103 না এলেও socket যেন আটকে না থাকে)
MAX_MATCH_SECONDS = 15 * 60
PING_INTERVAL = 4.0


# ───────────────────────── TCP packet decode ─────────────────────────
def decode_tcp_packet(data):
    """সার্ভারের TCP ফ্রেম (5-byte header + protobuf) → nested dict {field: value}"""

    def _varint(buf, offset):
        result, shift = 0, 0
        while offset < len(buf):
            b = buf[offset]
            offset += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                return result, offset
            shift += 7
            if shift > 70:
                return None, offset
        return None, offset

    def _printable(raw):
        try:
            s = raw.decode("utf-8")
        except Exception:
            return False
        if not s:
            return False
        return sum(1 for c in s if c.isprintable() or c in "\n\r\t") / len(s) >= 0.9

    def _parse(buf, depth=0):
        if not buf or depth > 20:
            return None, 0
        result = {}
        offset = 0
        n = len(buf)
        while offset < n:
            try:
                key, offset = _varint(buf, offset)
                if key is None or key == 0:
                    return None, 0
                field, wire = key >> 3, key & 7
                if field == 0:
                    return None, 0
                if wire == 0:
                    val, offset = _varint(buf, offset)
                    if val is None:
                        return None, 0
                elif wire == 1:
                    if offset + 8 > n:
                        return None, 0
                    val = struct.unpack("<Q", buf[offset:offset + 8])[0]
                    offset += 8
                elif wire == 2:
                    length, offset = _varint(buf, offset)
                    if length is None or offset + length > n:
                        return None, 0
                    raw = buf[offset:offset + length]
                    offset += length
                    if not raw:
                        val = ""
                    elif _printable(raw):
                        val = raw.decode("utf-8", errors="replace")
                    else:
                        nested, consumed = _parse(raw, depth + 1)
                        val = nested if nested and consumed == len(raw) else raw.hex()
                elif wire == 5:
                    if offset + 4 > n:
                        return None, 0
                    val = struct.unpack("<I", buf[offset:offset + 4])[0]
                    offset += 4
                else:
                    return None, 0
                if field in result:
                    if not isinstance(result[field], list):
                        result[field] = [result[field]]
                    result[field].append(val)
                else:
                    result[field] = val
            except Exception:
                return None, 0
        return (result, offset) if offset == n else (None, 0)

    try:
        data = bytes(data)
        if not data:
            return None
        if len(data) > 5:
            declared = int.from_bytes(data[2:5], "big")
            if declared == len(data) - 5:
                data = data[5:]
        result, _ = _parse(data)
        return result
    except Exception:
        return None


def split_tcp_frames(buf):
    """
    buffer থেকে পূর্ণ ফ্রেমগুলো আলাদা করে।  ফ্রেম = 2B opcode + 3B length + body
    return: (frames:list[bytes], remaining:bytes, desync:bool)
    """
    frames = []
    while len(buf) >= 5:
        length = int.from_bytes(buf[2:5], "big")
        if length > (1 << 20):          # অস্বাভাবিক বড় → stream desync, buffer ফেলে দাও
            return frames, b"", True
        if len(buf) < 5 + length:
            break
        frames.append(buf[:5 + length])
        buf = buf[5 + length:]
    return frames, buf, False


# ───────────────────────── MajorLogin raw field (stronghold) ─────────────────────────
def extract_majorlogin_extras(raw):
    """
    MajorLogin রেসপন্স থেকে field 1 (account_id) ও field 16 (stronghold url) বের করে।
    thunderFF_pb2.MajorLoginRes-এ field 16 নেই, তাই raw bytes থেকে পড়া হয়।
    return: (account_id:str|None, stronghold:str)
    """

    def _rv(b, p):
        v, sh = 0, 0
        while p < len(b):
            c = b[p]
            p += 1
            v |= (c & 0x7F) << sh
            if not (c & 0x80):
                return v, p
            sh += 7
            if sh > 70:
                break
        return None, p

    def _top(b):
        out, p, n = {}, 0, len(b)
        while p < n:
            k, p = _rv(b, p)
            if not k:
                return None
            f, w = k >> 3, k & 7
            if f == 0:
                return None
            if w == 0:
                v, p = _rv(b, p)
                if v is None:
                    return None
            elif w == 1:
                if p + 8 > n:
                    return None
                v = b[p:p + 8]; p += 8
            elif w == 2:
                ln, p = _rv(b, p)
                if ln is None or p + ln > n:
                    return None
                v = b[p:p + ln]; p += ln
            elif w == 5:
                if p + 4 > n:
                    return None
                v = b[p:p + 4]; p += 4
            else:
                return None
            out.setdefault(f, v)
        return out

    raw = bytes(raw)
    for off in [0, 64] + list(range(1, 64)) + list(range(65, min(128, len(raw)))):
        if off >= len(raw):
            break
        d = _top(raw[off:])
        if not d or 8 not in d or 1 not in d:
            continue
        acc = str(d[1]) if isinstance(d[1], int) else None
        sh = d.get(16, b"")
        if isinstance(sh, (bytes, bytearray)):
            sh = bytes(sh).decode("utf-8", errors="ignore").strip()
        else:
            sh = str(sh)
        return acc, sh
    return None, ""


# ───────────────────────── UDP crypto / framing ─────────────────────────
def _tea_enc_block(blk, k):
    y, z = struct.unpack("<2I", blk)
    s = 0
    for _ in range(16):
        s = (s + 0x9E3779B9) & 0xFFFFFFFF
        y = (y + ((((z << 4) & 0xFFFFFFFF) + k[0]) ^ (z + s) ^ ((z >> 5) + k[1]))) & 0xFFFFFFFF
        z = (z + ((((y << 4) & 0xFFFFFFFF) + k[2]) ^ (y + s) ^ ((y >> 5) + k[3]))) & 0xFFFFFFFF
    return struct.pack("<2I", y, z)


def _tea_dec_block(blk, k):
    y, z = struct.unpack("<2I", blk)
    s = (0x9E3779B9 << 4) & 0xFFFFFFFF
    for _ in range(16):
        z = (z - ((((y << 4) & 0xFFFFFFFF) + k[2]) ^ (y + s) ^ ((y >> 5) + k[3]))) & 0xFFFFFFFF
        y = (y - ((((z << 4) & 0xFFFFFFFF) + k[0]) ^ (z + s) ^ ((z >> 5) + k[1]))) & 0xFFFFFFFF
        s = (s - 0x9E3779B9) & 0xFFFFFFFF
    return struct.pack("<2I", y, z)


def tea_encrypt(data, key):
    SALT_LEN, ZERO_LEN = 2, 7
    k = list(struct.unpack("<4I", key[:16]))
    n = 1 + SALT_LEN + len(data) + ZERO_LEN
    r = n % 8
    n += (8 - r if r else 0)
    pad = n - (1 + SALT_LEN + len(data) + ZERO_LEN)
    plain = bytearray(n)
    plain[0] = pad
    plain[1 + pad + SALT_LEN: 1 + pad + SALT_LEN + len(data)] = data
    out = bytearray(n)
    iv_p = iv_c = bytes(8)
    for off in range(0, n, 8):
        blk = bytes(a ^ b for a, b in zip(plain[off:off + 8], iv_c))
        c = bytes(a ^ b for a, b in zip(_tea_enc_block(blk, k), iv_p))
        out[off:off + 8] = c
        iv_p, iv_c = blk, c
    return bytes(out)


def tea_decrypt(buf, key):
    SALT_LEN, ZERO_LEN = 2, 7
    if len(buf) < 16 or len(buf) % 8:
        return None
    k = list(struct.unpack("<4I", key[:16]))
    pad = _tea_dec_block(buf[0:8], k)[0] & 7
    out_len = len(buf) - pad - 1 - SALT_LEN - ZERO_LEN
    if out_len < 0:
        return None
    plain = bytearray(len(buf))
    iv_p = iv_c = bytes(8)
    for off in range(0, len(buf), 8):
        c = buf[off:off + 8]
        t = bytes(a ^ b for a, b in zip(c, iv_p))
        blk = _tea_dec_block(t, k)
        plain[off:off + 8] = bytes(a ^ b for a, b in zip(blk, iv_c))
        iv_p, iv_c = blk, c
    at = 1 + pad + SALT_LEN
    if bytes(plain[at + out_len:]) != bytes(ZERO_LEN):
        return None
    return bytes(plain[at:at + out_len])


def parse_mask(mask):
    if len(mask) != 5:
        return [0, 1, 2, 3, 4]
    try:
        o = [int(c) for c in mask]
    except ValueError:
        return [0, 1, 2, 3, 4]
    return o if sorted(o) == [0, 1, 2, 3, 4] else [0, 1, 2, 3, 4]


def _crc7_table():
    t = []
    for b in range(256):
        c = 0
        for i in range(8):
            top = ((c >> 6) & 1) ^ ((b >> (7 - i)) & 1)
            c = (c << 1) & 0x7F
            if top:
                c ^= 0x09
        t.append(c)
    return t


_CRC7 = _crc7_table()


def _crc7_byte(crc, b):
    return _CRC7[((crc << 1) ^ b) & 0xFF]


def _crc7_u16(crc, v):
    return _crc7_byte(_crc7_byte(crc, v & 0xFF), (v >> 8) & 0xFF)


def _crc7_buf(crc, data):
    for b in data:
        crc = _CRC7[((crc << 1) ^ b) & 0xFF]
    return crc


def _is_reliable(cmd, so):
    if cmd == 2 or not 1 <= so <= 5:
        return False
    return bool((0x13 >> (so - 1)) & 1)


def _gen_msg_key(cmd, so):
    return random.randrange(90, 100) if _is_reliable(cmd, so) else random.randrange(100, 110)


def encode_udp(cmd, so, oid, flags, body, key, order):
    reliable = _is_reliable(cmd, so)
    payload = tea_encrypt(body, key) if flags & 1 else body
    k = key[0]
    kk = k | (k << 8)
    m = {0: so ^ k, 1: cmd ^ kk, 2: oid ^ kk, 3: flags ^ k, 4: len(payload) ^ kk}
    out = bytearray([_gen_msg_key(cmd, so), 0])
    crc = 0
    for fid in order:
        if fid == 2 and not reliable:
            continue
        v = m[fid]
        if fid in (0, 3):
            crc = _crc7_byte(crc, v)
            out.append(v)
        else:
            crc = _crc7_u16(crc, v)
            out += struct.pack("<H", v)
    out += payload
    out[1] = _crc7_buf(crc, payload)
    return bytes(out)


def decode_udp(pkt, key, order):
    if len(pkt) < 8:
        return None
    k = key[0]
    kk = k | (k << 8)
    mk, crc = pkt[0], pkt[1]
    if not (90 <= mk <= 110):
        return None
    if _crc7_buf(0, pkt[2:]) != crc:
        return None
    reliable = mk < 100
    hdr = 10 if reliable else 8
    if len(pkt) < hdr:
        return None
    off = 2
    f = {2: 0}
    for fid in order:
        if fid == 2 and not reliable:
            continue
        if fid in (0, 3):
            f[fid] = pkt[off] ^ k
            off += 1
        else:
            f[fid] = struct.unpack_from("<H", pkt, off)[0] ^ kk
            off += 2
    payload = pkt[hdr:]
    body = tea_decrypt(payload, key) if f.get(3, 0) & 1 else payload
    return {
        "MsgKey": mk,
        "Cmd": f.get(1, 0),
        "OrderID": f.get(2, 0),
        "Flags": f.get(3, 0),
        "SendOption": f.get(0, 0),
        "reliable": reliable,
        "body": body,
    }


# ───────────────────────── UDP message bodies ─────────────────────────
def _wr_varint(o, v):
    v = int(v) & 0xFFFFFFFFFFFFFFFF
    while v >= 0x80:
        o.append((v & 0x7F) | 0x80)
        v >>= 7
    o.append(v)


def _rd_varint(b, p):
    v, sh = 0, 0
    while p < len(b):
        c = b[p]
        p += 1
        v |= (c & 0x7F) << sh
        if not (c & 0x80):
            return v, p
        sh += 7
        if sh > 70:
            break
    return v, p


def _wr_zig(o, v):
    _wr_varint(o, (int(v) << 1) ^ (int(v) >> 63))


def _wr_str(o, s):
    b = s.encode("utf-8") if isinstance(s, str) else bytes(s)
    _wr_zig(o, len(b))
    o.extend(b)


def _wr_byte(o, v):
    o.append(int(v) & 0xFF)


def split_jwt_token(jwt):
    raw = int(len(jwt) * 0.85)
    p1 = raw - (raw % 40)
    while 40 + 100 + p1 > 1300 and p1 > 40:
        p1 -= 40
    return jwt[:p1], jwt[p1:]


def build_join_prepare_body(tok1):
    o = bytearray()
    _wr_str(o, tok1)
    return bytes(o)


def build_udp_hello(account_id, key, order, prefix="0"):
    raw = f"{int(account_id)}_{prefix}".encode()
    body = struct.pack("<i", len(raw)) + raw + bytes(9)
    return encode_udp(1, 1, 0, 1, body, key, order)


def build_448_named_payload(account_id, f5, region, game_version, stronghold_str, part2):
    return {
        "account_id": int(account_id), "match_id": int(f5.get(1)), "const_1": 1,
        "game_mode": int(f5.get(7)), "match_id_again": int(f5.get(1)), "map_id": int(f5.get(6)),
        "zero_7": 0, "zero_8": 0, "zero_9": 0, "zero_10": 0, "is_emulator": 0, "zero_12": 0,
        "one_13": 1, "two_14": 1, "three_15": 3, "antihack_tick": 1, "one_17": 1,
        "country": str(region), "zero_19": 0, "one_20": 1, "two_21": 3, "zero_22": 0,
        "one_23": 1, "zero_24": 0, "zero_25": 0, "four_26": 4,
        "client_version": str(game_version), "client_version_code": "2019121229",
        "client_url": stronghold_str, "zero_30": 0, "zero_31": 0, "friend_count": 0,
        "zero_33": 0, "drift_owner_id": 0, "drift_owner_name": "", "one_36": 0,
        "empty_37": "", "empty_38": "", "zero_39": 0, "zero_40": 0, "zero_41": 0,
        "zero_42": 0, "zero_43": 0, "zero_44": 0, "zero_45": 0, "empty_46": "",
        "zero_47": 0, "zero_48": 0, "one_49": 1, "zero_50": 0, "zero_51": 0,
        "zero_52": 0, "zero_53": 0, "zero_54": 0, "one_55": 1, "empty_56": "",
        "zero_57": 0, "zero_58": 0, "zero_59": 0, "zero_60": 3,
        "match_mode": int(f5.get(8)), "zero_62": 0,
        "session_tag": int(time.time() * 1_000_000) & 0xFFFFFFFFFFFFFFFF,
        "two_64": 2, "zero_65": 0, "client_ip": f5.get(2).split(":")[0],
        "client_fingerprint": FINGERPRINT, "token_part2": part2,
    }


def build_join_post_body(n):
    b = bytearray()
    V, B, S = _wr_varint, _wr_byte, _wr_str
    V(b, n["account_id"]); V(b, n["match_id"]); B(b, n["const_1"]); B(b, n["game_mode"])
    V(b, n["match_id_again"]); B(b, n["map_id"]); B(b, n["zero_7"]); V(b, n["zero_8"])
    V(b, n["zero_9"]); B(b, n["zero_10"]); B(b, n["is_emulator"]); B(b, n["zero_12"])
    B(b, n["one_13"]); B(b, n["two_14"]); B(b, n["three_15"]); B(b, n["antihack_tick"])
    B(b, n["one_17"]); S(b, n["country"]); V(b, n["zero_19"]); B(b, n["one_20"])
    B(b, n["two_21"]); B(b, n["zero_22"]); B(b, n["one_23"]); B(b, n["zero_24"])
    B(b, n["zero_25"]); B(b, n["four_26"]); S(b, n["client_version"])
    S(b, n["client_version_code"]); S(b, n["client_url"]); B(b, n["zero_30"])
    B(b, n["zero_31"]); V(b, n["friend_count"]); B(b, n["zero_33"]); V(b, n["drift_owner_id"])
    S(b, n["drift_owner_name"]); B(b, n["one_36"]); S(b, n["empty_37"]); S(b, n["empty_38"])
    B(b, n["zero_39"]); V(b, n["zero_40"]); V(b, n["zero_41"]); B(b, n["zero_42"])
    B(b, n["zero_43"]); B(b, n["zero_44"]); V(b, n["zero_45"]); S(b, n["empty_46"])
    V(b, n["zero_47"]); B(b, n["zero_48"]); B(b, n["one_49"]); B(b, n["zero_50"])
    B(b, n["zero_51"]); V(b, n["zero_52"]); V(b, n["zero_53"]); B(b, n["zero_54"])
    B(b, n["one_55"]); S(b, n["empty_56"]); V(b, n["zero_57"]); B(b, n["zero_58"])
    B(b, n["zero_59"]); B(b, n["zero_60"]); B(b, n["match_mode"]); B(b, n["zero_62"])
    V(b, n["session_tag"]); B(b, n["two_64"]); B(b, n["zero_65"]); S(b, n["client_ip"])
    S(b, n["client_fingerprint"]); S(b, n["token_part2"])
    return bytes(b)


# ───────────────────────── UDP session ─────────────────────────
class _UdpQueueProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.q = asyncio.Queue(maxsize=2048)
        self.error = None

    def datagram_received(self, data, addr):
        try:
            self.q.put_nowait(data)
        except asyncio.QueueFull:
            pass

    def error_received(self, exc):
        self.error = exc


def is_match_found(decoded):
    """0300 প্যাকেট কি MATCH FOUND (type 5) ও জয়েন করার মতো সম্পূর্ণ?"""
    if not isinstance(decoded, dict) or decoded.get(4) != 5:
        return False
    f5 = decoded.get(5)
    return isinstance(f5, dict) and bool(f5.get(2)) and bool(f5.get(3)) and bool(f5.get(4))


async def play_br_evo_match(decoded, region, game_version, stronghold="", log=None, state=None):
    """
    MATCH FOUND (0300, type 5) পেলে UDP গেম-সার্ভারে ঢুকে ম্যাচ শেষ পর্যন্ত থাকে।
    state: optional dict — JOIN_POST (448) পাঠানোর পর state["joined"]=True বসে
           (task cancel হলেও caller জানতে পারে ম্যাচে ঢোকা হয়েছিল কিনা)
    return: True যদি JOIN_POST (448) পর্যন্ত সফলভাবে পৌঁছানো গেছে (= সত্যিকারের ম্যাচে ঢুকেছে)
    """
    def _log(msg, level="info"):
        if log:
            try:
                log(msg, level)
            except Exception:
                pass

    f5 = decoded.get(5)
    account_id = decoded.get(1)
    transport = None
    joined = False
    try:
        ip, port = str(f5.get(2)).split(":")
        port = int(port)
        udp_key = bytes.fromhex(f5.get(3))
        order = parse_mask(str(f5.get(42)) if f5.get(42) else "01234")
        part1, part2 = split_jwt_token(f5.get(4))
        stronghold_str = stronghold or DEFAULT_STRONGHOLD

        loop = asyncio.get_running_loop()
        transport, proto = await loop.create_datagram_endpoint(
            _UdpQueueProtocol, remote_addr=(ip, port)
        )

        async def recv(timeout):
            try:
                return await asyncio.wait_for(proto.q.get(), timeout=timeout)
            except asyncio.TimeoutError:
                return None

        def send(pkt):
            transport.sendto(pkt)

        def ack_for(dec):
            req = (dec["OrderID"] + 1) & 0xFFFF
            return encode_udp(2, 0, 0, 0, struct.pack("<H", req), udp_key, order)

        # ── 1) HELLO handshake ──
        hello = build_udp_hello(account_id, udp_key, order)
        send(hello)
        got_hello = got_ack = False
        acked = set()
        deadline = loop.time() + 15.0
        last_send = loop.time()
        while loop.time() < deadline and not (got_hello and got_ack):
            data = await recv(0.5)
            if data is None:
                if loop.time() - last_send >= 1.0:
                    send(hello)
                    last_send = loop.time()
                continue
            dec = decode_udp(data, udp_key, order)
            if dec is None:
                continue
            if dec["Cmd"] == 1 and not got_hello:
                got_hello = True
                if dec["OrderID"] not in acked:
                    acked.add(dec["OrderID"])
                    send(ack_for(dec))
            if dec["Cmd"] == 2 and not got_ack:
                got_ack = True
        if not (got_hello and got_ack):
            _log(f"[UDP] HELLO handshake অসম্পূর্ণ (server_hello={got_hello}, ack={got_ack}) | UID {account_id}", "warning")
            return False

        # ── 2) 447 JOIN_PREPARE ──
        pkt_447 = encode_udp(447, 2, 0, 1, build_join_prepare_body(part1), udp_key, order)
        send(pkt_447)
        got_447 = False
        acked = set()
        deadline = loop.time() + 10.0
        last_send = loop.time()
        while loop.time() < deadline and not got_447:
            data = await recv(0.3)
            if data is None:
                if loop.time() - last_send >= 1.0:
                    send(pkt_447)
                    last_send = loop.time()
                continue
            dec = decode_udp(data, udp_key, order)
            if dec is None:
                continue
            if dec["Cmd"] == 2:
                got_447 = True
                break
            if dec["reliable"] and dec["OrderID"] not in acked:
                acked.add(dec["OrderID"])
                send(ack_for(dec))
        if not got_447:
            _log(f"[UDP] 447 ACK আসেনি | UID {account_id}", "warning")
            return False

        # ── 3) 448 JOIN_POST ──
        named = build_448_named_payload(account_id, f5, region, game_version, stronghold_str, part2)
        send(encode_udp(448, 2, 1, 1, build_join_post_body(named), udp_key, order))
        joined = True
        if state is not None:
            state["joined"] = True
        _log(f"[UDP] ম্যাচে জয়েন সম্পন্ন | UID {account_id} | {ip}:{port}", "success")

        # ── 4) ম্যাচ চলাকালীন: ack + ping + sync-time, Cmd 103 এ শেষ ──
        main_acked = set()
        t_end = loop.time() + MAX_MATCH_SECONDS
        next_ping = loop.time() + PING_INTERVAL
        while loop.time() < t_end:
            now = loop.time()
            if now >= next_ping:
                tick = int(time.time() * 1000) & 0xFFFFFFFF
                pb = bytearray()
                _wr_zig(pb, tick); _wr_zig(pb, 0); _wr_zig(pb, 0); _wr_zig(pb, 0)
                send(encode_udp(3, 0, 0, 0, bytes(pb), udp_key, order))
                next_ping = now + PING_INTERVAL
            data = await recv(max(0.05, min(1.0, next_ping - loop.time())))
            if data is None:
                continue
            dec = decode_udp(data, udp_key, order)
            if dec is None:
                continue
            if dec["reliable"] and dec["OrderID"] not in main_acked:
                main_acked.add(dec["OrderID"])
                send(ack_for(dec))
            if dec["Cmd"] == 2000 and dec["body"]:
                try:
                    st, _ = _rd_varint(dec["body"], 0)
                    ab = bytearray()
                    _wr_varint(ab, int(st) & 0xFFFFFFFF)
                    send(encode_udp(2000, 0, 0, 1, bytes(ab), udp_key, order))
                except Exception:
                    pass
            if dec["Cmd"] == 103:
                _log(f"[UDP] ম্যাচ শেষ (Cmd 103) | UID {account_id}", "info")
                break
        return True

    except asyncio.CancelledError:
        raise
    except Exception as e:
        _log(f"[UDP] error | UID {account_id}: {repr(e)[:150]}", "warning")
        return joined
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
