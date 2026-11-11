"""Season relay bridge, ported 1:1 from the Doubao soft-router.

The relay intercepts the game client's type-7 handshake toward an advertised
peer, replaces the client final with its own hello (fresh X25519 scalar),
authenticates the server ACK, and then translates server downlinks to the
16-byte mobile key that the game server advertised over the TGCP channel.
"""

from __future__ import annotations

import base64
import hashlib
import re
import ipaddress
import os
import struct
import time
from functools import lru_cache
from .native_codec import lz4 as _native_lz4

from .seasoncrypto import (
    Control7,
    SignedServerConfig,
    build_control7,
    decrypt_downlink,
    encrypt_downlink,
    parse_control7,
    session_key,
)

_MARKER_HASH = "4119834b0729119d0dc98a684b19a76c098413b40ae8df2acb74143e8b4aabfd"
_LIMIT = 4 * 1024 * 1024

# Public configuration verified against release 1.201.37119 and a captured ACK.
CURRENT_SEASON_CONFIG_B64 = (
    "HSoSI+AYuDE2Vvn0g2cheMsH+jMBFzjpnLUmg7Qhwg1wPUwPFwp/Yv85JaoKI351vVNW0FJ4Pnejggi64jEQHji1CXd"
    "MHLMwd4yDJ+mDcNTpUrnSP8A4JtTTctgTLOw0jQDkgQJjGu09OkazGfF1PJXp6O8pNWyw5IdKARnl7Ao="
)
CURRENT_SEASON_ROOT_B64 = "47jOvrq+hd5SJ0Ju4LYsJ/Q5ep0Kxp6P9Q94WlfAjao="


@lru_cache(maxsize=1)
def current_server_config() -> SignedServerConfig:
    return SignedServerConfig.parse(
        base64.b64decode(CURRENT_SEASON_CONFIG_B64, validate=True),
        base64.b64decode(CURRENT_SEASON_ROOT_B64, validate=True),
    )


def crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


def shift_header(header: bytes, delta: int = 1) -> bytes:
    if len(header) != 8:
        raise ValueError("Expected an eight-byte UDP transport header")
    check, seq = struct.unpack("<II", header)
    changed = struct.pack("<I", (seq + delta) & 0xFFFFFFFF)
    return struct.pack("<I", check ^ crc32c(header[4:]) ^ crc32c(changed)) + changed


def control7(parameter: int, body: bytes) -> bytes:
    return build_control7(parameter, body)


def _varint(data: bytes, at: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if at >= len(data):
            raise ValueError("Short protobuf varint")
        octet = data[at]
        at += 1
        if shift == 63 and octet > 1:
            raise ValueError("Protobuf varint overflow")
        value |= (octet & 0x7F) << shift
        if octet < 128:
            return value, at
    raise ValueError("Long protobuf varint")


def _fields(data: bytes):
    if len(data) > _LIMIT:
        raise ValueError("Oversized protobuf")
    at = 0
    count = 0
    while at < len(data):
        tag, at = _varint(data, at)
        field = tag >> 3
        wire = tag & 7
        if not 0 < field < 0x20000000:
            raise ValueError("Invalid protobuf field")
        if wire == 0:
            value, at = _varint(data, at)
        else:
            if wire == 2:
                size, at = _varint(data, at)
            elif wire in (1, 5):
                size = 8 if wire == 1 else 4
            else:
                raise ValueError("Unsupported protobuf wire type")
            if size > len(data) - at:
                raise ValueError("Short protobuf value")
            value = data[at:at + size]
            at += size
        count += 1
        if count > 20000:
            raise ValueError("Too many protobuf fields")
        yield field, wire, value


def _pick(data: bytes, wanted: dict) -> dict:
    result = {}
    for field, wire, value in _fields(data):
        if wanted.get(field) == wire:
            if field in result:
                raise ValueError("Duplicate credential field")
            result[field] = value
    return result


def _lz4_python(data: bytes) -> bytes:
    output = bytearray()
    at = 0

    def length(value: int) -> int:
        nonlocal at
        if value == 15:
            while True:
                if at >= len(data):
                    raise ValueError("Short LZ4 length")
                extra = data[at]
                at += 1
                value += extra
                if value > _LIMIT:
                    raise ValueError("Oversized LZ4")
                if extra != 255:
                    break
        return value

    while at < len(data):
        token = data[at]
        at += 1
        size = length(token >> 4)
        if size > len(data) - at or len(output) + size > _LIMIT:
            raise ValueError("Invalid LZ4 literals")
        output.extend(data[at:at + size])
        at += size
        if at == len(data):
            return bytes(output)
        if at + 2 > len(data):
            raise ValueError("Short LZ4 offset")
        offset = int.from_bytes(data[at:at + 2], "little")
        at += 2
        size = length(token & 15) + 4
        if not (0 < offset <= len(output)) or len(output) + size > _LIMIT:
            raise ValueError("Invalid LZ4 match")
        for _ in range(size):
            output.append(output[-offset])
    raise ValueError("Missing LZ4 final literals")


def _lz4(data: bytes) -> bytes:
    result = _native_lz4(data)
    return _lz4_python(data) if result is NotImplemented else result


def _credential_variants(plain):
    # Raw protobuf credentials must not wait for 65 speculative decompresses.
    yield plain
    for offset in range(min(65, len(plain))):
        try:
            yield _lz4(plain[offset:])
        except ValueError:
            continue


def match_credentials(command: int, plain: bytes, *, _variants=None):
    """Return (mobile key, explicitly advertised peers), never publish them."""
    if command != 16403 or len(plain) < 150:
        return None
    variants = _credential_variants(plain) if _variants is None else _variants
    for data in variants:
        try:
            envelope = _pick(data, {1: 2, 2: 2})
            marker = _pick(envelope[1], {7: 2})[7]
            marker_ok = hashlib.sha256(marker).hexdigest() == _MARKER_HASH
            message = envelope[2]
            fields = _pick(message, {3: 0, 8: 2, 19: 2})
            cookie = _pick(fields[8], {24: 2})[24]
            bank = base64.b64decode(fields[19], validate=True)
            if len(bank) != 128 or len(cookie) != 20 or base64.b64encode(bank) != fields[19]:
                continue
            if not 0 < fields[3] <= 65535:
                continue
            peers = set()
            for number, wire, group in _fields(message):
                if (number, wire) != (12, 2):
                    continue
                for subfield, subtype, endpoint in _fields(group):
                    if (subfield, subtype) != (2, 2):
                        continue
                    record = _pick(endpoint, {1: 2, 2: 0})
                    ip = ipaddress.IPv4Address(record[1].decode("ascii"))
                    port = record.get(2, fields[3])
                    if ip.is_global and 0 <= port <= 65535:
                        peers.add(f"{ip}:{port}")
            if 0 < len(peers) <= 32:
                return bank[:16], frozenset(peers)
        except (KeyError, ValueError, UnicodeError):
            continue
    return None


def _walk_fields(data: bytes, depth: int = 3):
    """Yield protobuf field lists recursively (bounded)."""
    try:
        values = list(_fields(data))
    except Exception:
        return
    stack = [(values, depth)]
    while stack:
        vals, remaining = stack.pop()
        yield vals
        if remaining <= 0:
            continue
        for _field, wire, value in vals:
            if wire == 2 and isinstance(value, bytes):
                try:
                    sub = list(_fields(value))
                except Exception:
                    continue
                stack.append((sub, remaining - 1))


def _decode_bank(value: bytes):
    if not 150 <= len(value) <= 180:
        return None
    for pad in (0, 1, 2):
        try:
            raw = base64.b64decode(value + b"=" * pad, validate=True)
        except Exception:
            continue
        if len(raw) == 128 and base64.b64encode(raw).rstrip(b"=") == value.rstrip(b"="):
            return raw
    try:
        raw = base64.urlsafe_b64decode(value + b"=" * ((-len(value)) % 4))
    except Exception:
        return None
    return raw if len(raw) == 128 else None


def _scan_endpoints(lists):
    peers = set()
    for vals in lists:
        for _field, wire, value in vals:
            if wire != 2 or not isinstance(value, bytes):
                continue
            try:
                record = list(_fields(value))[:10]
            except Exception:
                continue
            ip = None
            port = None
            for rf, rw, rv in record:
                if rf == 1 and rw == 2:
                    try:
                        ip = rv.decode("ascii")
                    except Exception:
                        ip = None
                elif rf == 2 and rw == 0 and isinstance(rv, int):
                    port = rv
            if ip and port and 0 < port <= 65535:
                try:
                    address = ipaddress.IPv4Address(ip)
                except Exception:
                    continue
                if address.is_global:
                    peers.add(f"{address}:{port}")
    return peers


def match_credentials_flexible(command: int, plain: bytes, *, _variants=None):
    if command != 16403 or len(plain) < 150:
        return None
    """Structure-first credential match: locate a 128-byte base64 bank anywhere."""
    if command != 16403:
        return None
    variants = _credential_variants(plain) if _variants is None else _variants
    for data in variants:
        lists = list(_walk_fields(data))
        if not lists:
            continue
        bank = None
        for vals in lists:
            for _field, wire, value in vals:
                if wire == 2 and isinstance(value, bytes):
                    decoded = _decode_bank(value)
                    if decoded is not None:
                        bank = decoded
                        break
            if bank is not None:
                break
        if bank is None:
            continue
        return bank[:16], frozenset(_scan_endpoints(lists))
    return None


def match_credentials_combined(command, plain):
    if command != 16403 or len(plain) < 150:
        return None
    variants = []
    def collect():
        for variant in _credential_variants(plain):
            variants.append(variant)
            yield variant
    result = match_credentials(command, plain, _variants=collect())
    if result is not None:
        return result
    return match_credentials_flexible(command, plain, _variants=variants)


def match_credentials_raw(command, plain):
    return match_credentials(command, plain, _variants=(plain,))


def credential_report(plain: bytes) -> dict:
    """Structural report for a 0x4013 payload. No key material is returned."""
    report = {"rawLength": len(plain), "head": plain[:8].hex(), "variants": 0, "attempts": []}
    variants = [(plain, -1)]
    for offset in range(min(65, len(plain))):
        try:
            variants.append((_lz4(plain[offset:]), offset))
        except ValueError:
            continue
    report["variants"] = len(variants) - 1
    for data, offset in variants[:10]:
        item = {"offset": offset, "length": len(data), "head": data[:8].hex()}
        try:
            token = re.search(rb"[A-Za-z0-9+/]{171}=", data)
            item["bankTokenOffset"] = token.start() if token else None
        except Exception:
            pass
        try:
            fields = list(_fields(data))
            item["top"] = [{"f": f, "w": w, "n": (len(v) if isinstance(v, bytes) else None)}
                           for f, w, v in fields[:12]]
        except Exception as exc:
            item["structError"] = type(exc).__name__
            report["attempts"].append(item)
            continue
        lists = list(_walk_fields(data))
        banks = []
        for vals in lists:
            for f, w, v in vals:
                if w == 2 and isinstance(v, bytes):
                    decoded = _decode_bank(v)
                    if decoded is not None:
                        banks.append({"field": f, "length": len(v), "decoded": len(decoded)})
        if banks:
            item["banks"] = banks[:4]
        endpoints = _scan_endpoints(lists)
        if endpoints:
            item["endpoints"] = sorted(endpoints)[:8]
        try:
            envelope = _pick(data, {1: 2, 2: 2})
            item["envelope"] = sorted(envelope)
            if 1 in envelope:
                marker_fields = list(_fields(envelope[1]))[:6]
                item["markerTop"] = [{"f": f, "w": w, "n": (len(v) if isinstance(v, bytes) else None)}
                                     for f, w, v in marker_fields]
                item["markerHashes"] = [hashlib.sha256(v).hexdigest()[:16]
                                        for f, w, v in marker_fields if w == 2]
            if 2 in envelope:
                msg = envelope[2]
                item["messageLength"] = len(msg)
                try:
                    item["messageFields"] = sorted({f for f, w, v in _fields(msg)})
                except Exception as exc:
                    item["messageError"] = type(exc).__name__
                try:
                    picked = _pick(msg, {3: 0, 8: 2, 19: 2})
                    item["picked"] = sorted(picked)
                    if 19 in picked:
                        try:
                            item["bankLength"] = len(base64.b64decode(picked[19], validate=True))
                        except Exception:
                            item["bankError"] = "b64"
                    if 8 in picked:
                        item["cookieLength"] = len(picked[8])
                except Exception as exc:
                    item["pickError"] = type(exc).__name__
        except Exception as exc:
            item["envelopeError"] = type(exc).__name__
        report["attempts"].append(item)
    return report


def is_initial(payload: bytes) -> bool:
    return (
        len(payload) == 33
        and payload[8] == 1
        and payload[-1] == 8
        and all(byte == 0 for byte in payload[9:32])
    )


class SeasonSession:
    """Wire state machine. Returned actions are (upstream, payload) tuples."""

    def __init__(self, mobile_key: bytes, config: SignedServerConfig | None = None,
                 clock=None, private: bytes | None = None):
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        self.config = config or current_server_config()
        self.clock = clock or time.monotonic
        if len(mobile_key) != 16:
            raise ValueError("Expected a 16-byte mobile key")
        self.mobile_key = bytes(mobile_key)
        self.private = bytes(private) if private is not None else os.urandom(32)
        client = X25519PrivateKey.from_private_bytes(self.private)
        public = client.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        shared = client.exchange(X25519PublicKey.from_public_bytes(self.config.exchange_public))
        first = hashlib.md5(public + shared).digest()
        self.client_wire = control7(
            self.config.identifier,
            public + first + hashlib.md5(public + first).digest(),
        )
        self.client = parse_control7(self.client_wire)
        self.key = session_key(shared, public, self.config.exchange_public)
        self.ack: Control7 | None = None
        self.state = "hello"
        self.reply_header = None
        self.request = None
        self.mobile_final = None
        self.cookie_reply = None
        self.cookie_recovery_pending = False
        self.cookie_retries = 0
        self.last_cookie_retry = float("-inf")
        self.server_final = None
        self.offer_started = None
        self.initial_header = None
        self.initial_seen = None
        self.last_offer = 0
        self.retries = 0
        self.translated = 0
        self.account = ""
        self.local = ""
        self.remote = ""

    def _retry(self, up: bool, payload: bytes):
        if self.state == "confirm" and self.clock() - self.offer_started >= 5:
            self.state = "bypass"
            self.private = None
            self.key = None
            actions = [(False, self.server_final)]
            if up:
                actions.append((True, payload))
            return actions
        if self.state == "confirm" and self.clock() - self.last_offer >= 1 and self.retries < 3:
            self.last_offer = self.clock()
            self.retries += 1
            return [(True, self.request)]
        return []

    def _retry_cookie(self):
        now = self.clock()
        if (self.state != "confirm" or self.cookie_reply is None
                or now - self.offer_started >= 5 or self.cookie_retries >= 3
                or now - self.last_cookie_retry < 0.25):
            return []
        self.cookie_retries += 1
        self.last_cookie_retry = now
        self.cookie_recovery_pending = True
        return [(True, self.cookie_reply)]

    def from_game(self, payload: bytes):
        if self.state == "bypass":
            return [(True, payload)]
        if self.state == "ready":
            if len(payload) < 9:
                return []
            return [(True, shift_header(payload[:8]) + payload[8:])]
        if is_initial(payload):
            if self.initial_header is None:
                self.initial_header = payload[:8]
                self.initial_seen = self.clock()
            return [(True, payload)]
        if (self.state == "cookie" and len(payload) == 33
                and payload[8] & 3 == 1 and payload[-1] & 0xF8 == 8):
            self.reply_header = payload[:8]
            self.cookie_reply = payload
            self.state = "final"
            return [(True, payload)]
        if self.state == "final" and payload[:8] == self.reply_header:
            return [(True, payload)]
        if self.state == "confirm" and payload == self.cookie_reply:
            actions = self._retry_cookie()
            if actions:
                return actions
        return self._retry(True, payload)

    def from_server(self, payload: bytes):
        if self.state == "bypass":
            return [(False, payload)]
        if self.state == "ready":
            if payload and payload[0] & 1:
                return []
            plain = decrypt_downlink(payload, self.key)
            self.translated += 1
            return [(False, encrypt_downlink(plain, self.mobile_key))]
        if len(payload) == 25 and payload[0] & 3 == 1 and payload[-1] & 0xF8 == 8:
            bits = (int.from_bytes(payload[:5], "little") >> 3) & 0xFFFFFFFF
            value = struct.unpack("<f", bits.to_bytes(4, "little"))[0]
            if value > 0 and self.state in ("hello", "cookie", "final"):
                if self.state == "hello":
                    self.state = "cookie"
                return [(False, payload)]
            if value == -1.0 and self.state == "final":
                self.server_final = payload
                self.mobile_final = payload[:24] + bytes([payload[24] & 7]) + bytes(19) + b"\x08"
                self.request = shift_header(self.reply_header) + self.client_wire
                self.last_offer = self.clock()
                self.offer_started = self.last_offer
                self.state = "confirm"
                return [(True, self.request)]
            if (value == -1.0 and self.state == "confirm"
                    and self.cookie_recovery_pending
                    and self.clock() - self.offer_started < 5):
                self.cookie_recovery_pending = False
                self.server_final = payload
                self.last_offer = self.clock()
                return [(True, self.request)]
        if self.state == "confirm" and payload == b"\x0f":
            actions = self._retry_cookie()
            if actions:
                return actions
        try:
            ack = parse_control7(payload)
        except ValueError:
            return self._retry(False, payload)
        if self.config.matches_transcript(self.client, ack):
            self.ack = ack
            self.state = "ready"
            return [(False, self.mobile_final)]
        return self._retry(False, payload)


class SeasonBridge:
    """Connect local offers to the real broker via authenticated wire events."""

    def __init__(self, observer, installer, config: SignedServerConfig | None = None):
        self.observer = observer
        self.installer = installer
        self.config = config or current_server_config()
        self.peers: dict[tuple[str, str], bytes] = {}
        self.flows: dict[str, SeasonSession] = {}
        self.bypass_peers: dict[str, set[str]] = {}
        self.fallback_key: bytes | None = None
        self.offers = 0

    @staticmethod
    def flow_key(account: str, local: str, remote: str) -> str:
        return f"{account}|{local}>{remote}"

    def observe_notice(self, observation, account: str = "") -> None:
        """Require a current-generation notice for new exchanges.

        Existing sessions keep translating packets so an upload reconnect
        does not invalidate the key already negotiated with the game server.
        """
        if observation.up:
            return None
        credential = match_credentials(observation.command, observation.plain)
        if credential is None:
            flexible = match_credentials_flexible(observation.command, observation.plain)
            if flexible is not None and flexible[1]:
                credential = flexible
        self.apply_notice(credential,account)
        return None

    def apply_notice(self,credential,account=''):
        if credential is not None:
            key, peers = credential
            bypass = self.bypass_peers.setdefault(account, set())
            for peer in peers:
                slot = (account, peer)
                if self.peers.get(slot) != key:
                    bypass.discard(peer)
                self.peers[slot] = key
            while len(self.peers) > 1024:
                self.peers.pop(next(iter(self.peers)))
        return None

    def reset_notices(self, account: str | None = None) -> None:
        if account is None:
            self.peers.clear()
            self.bypass_peers.clear()
        else:
            for slot in [s for s in self.peers if s[0] == account]:
                self.peers.pop(slot, None)
            self.bypass_peers.pop(account, None)
        return None

    def should_relay(self, up: bool, remote: str, payload: bytes, account: str = "") -> bool:
        return up and (account, remote) in self.peers and is_initial(payload)

    def poll(self, local: str, remote: str, account: str = ""):
        session = self.flows.get(self.flow_key(account, local, remote))
        if session is None or session.state != "confirm":
            return []
        actions = session._retry(False, b"")
        if session.state == "bypass":
            self.bypass_peers.setdefault(account, set()).add(remote)
            for up, wire in actions:
                self.observer(up, local, remote, wire, account)
        return actions

    def poll_all(self):
        result = []
        for session in list(self.flows.values()):
            if session.state != "confirm":
                continue
            account, local, remote = session.account, session.local, session.remote
            actions = self.poll(local, remote, account)
            if actions:
                result.append((account, local, remote, actions))
        return result

    def transform(self, up: bool, local: str, remote: str, payload: bytes, account: str = ""):
        flow = self.flow_key(account, local, remote)
        session = self.flows.get(flow)
        remote_key = self.peers.get((account, remote))
        bypass = self.bypass_peers.get(account) or set()
        if (up and is_initial(payload) and remote_key is not None
                and remote not in bypass):
            if (session is not None and session.state == "ready"
                    and payload[:8] == session.initial_header):
                fresh_generation = (session.translated > 20 or
                                    (session.initial_seen is not None
                                     and session.clock() - session.initial_seen >= 5))
                if not fresh_generation:
                    return []
                session = None
            if session is None or (session.initial_header is not None
                                   and payload[:8] != session.initial_header):
                if session is None and len(self.flows) >= 256:
                    raise RuntimeError("Too many active season relay flows")
                session = SeasonSession(remote_key, self.config)
                session.account = account
                session.local, session.remote = local, remote
                self.flows[flow] = session
        if session is None:
            self.observer(up, local, remote, payload, account)
            return [(up, payload)]
        before = session.state
        actions = session.from_game(payload) if up else session.from_server(payload)
        if before == "ready":
            if up:
                for direction, wire in actions:
                    self.observer(direction, local, remote, wire, account)
            else:
                self.observer(False, local, remote, payload, account)
        elif before != "confirm" and session.state == "confirm":
            self.offers += 1
            self.observer(True, local, remote, session.request, account)
        elif before == "confirm" and session.state == "ready":
            self.observer(False, local, remote, payload, account)
            self.installer(local, remote, session.private, account)
        elif before == "confirm" and session.state == "bypass":
            self.bypass_peers.setdefault(account, set()).add(remote)
            for direction, wire in actions:
                self.observer(direction, local, remote, wire, account)
        elif before == "bypass":
            for direction, wire in actions:
                self.observer(direction, local, remote, wire, account)
        elif before != "confirm":
            for direction, wire in actions:
                self.observer(direction, local, remote, wire, account)
        return actions

    def snapshot(self) -> dict:
        return {
            "offers": self.offers,
            "flows": len(self.flows),
            "peers": len(self.peers),
            "bypassPeers": sum(len(v) for v in self.bypass_peers.values()),
            "states": {key: session.state for key, session in self.flows.items()},
        }
