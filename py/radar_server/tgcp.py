"""TGCP (tsf4g) TCP control-channel MITM.

The game uses an unauthenticated Diffie-Hellman hello.  A relay that terminates
the TCP stream can substitute both public values and therefore read the
encrypted application frames.  This is a direct port of the Nva C# prototype.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = bytes.fromhex("3366000B000C")
IV = bytes(range(16))
PRIME = int.from_bytes(
    bytes.fromhex(
        "97981e0aade0de72e29cd2789193562547bad2591fb7e59c523923dddecebcca"
        "e49bdcd4b8ec39022b0bd95d4af8fd3cd600919393255c1084c2fd5abb2fede3"
    ),
    "big",
)
MAX_BODY = 16 * 1024 * 1024
MAX_HEADER = 65536
HELLO_CLIENT = 0x1001
HELLO_SERVER = 0x1002

BANK_RE = re.compile(rb"(?<![A-Za-z0-9+/])([A-Za-z0-9+/]{171}=)(?![A-Za-z0-9+/=])")


class TgcpError(ValueError):
    pass


def key_pair() -> tuple[int, int]:
    raw = os.urandom(64)
    raw = bytes([(raw[0] & 0x7F) | 0x40]) + raw[1:]
    value = int.from_bytes(raw, "big")
    return value, pow(2, value, PRIME)


def validate_public(value: int) -> None:
    if value <= 1 or value >= PRIME - 1:
        raise TgcpError("DH 公开值越界")


def derive(peer: int, private_value: int) -> bytes:
    validate_public(peer)
    shared = pow(peer, private_value, PRIME)
    validate_public(shared)
    raw = shared.to_bytes((shared.bit_length() + 7) // 8, "big")
    return hashlib.md5(raw).digest()


def public_bytes(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    if len(raw) > 64:
        raise TgcpError("DH 公开值超过 64 字节")
    return b"\x00" * (64 - len(raw)) + raw


def lengths(data: bytes | bytearray) -> tuple[int, int]:
    if len(data) < 21 or bytes(data[:6]) != MAGIC:
        raise TgcpError("TGCP 帧头错误")
    header = int.from_bytes(data[13:17], "big")
    body = int.from_bytes(data[17:21], "big")
    if header < 21 or header > MAX_HEADER or body > MAX_BODY:
        raise TgcpError("TGCP 帧长度超出范围")
    return header, body


def sniff(prefix: bytes | bytearray) -> int:
    """0 = need more bytes, 1 = not TGCP, 2 = valid client hello."""
    n = min(len(prefix), len(MAGIC))
    if bytes(prefix[:n]) != MAGIC[:n]:
        return 1
    if len(prefix) < 21:
        return 0
    try:
        header, body = lengths(prefix)
        if int.from_bytes(prefix[6:8], "big") != HELLO_CLIENT or header < 88:
            return 1
        if len(prefix) >= 24 and bytes(prefix[21:24]) != b"\x03\x00\x40":
            return 1
        if len(prefix) < header + body:
            return 0
        frame = Frame(bytes(prefix[:header]), bytes(prefix[header:header + body]))
        frame.public(HELLO_CLIENT)
        return 2
    except TgcpError:
        return 1


@dataclass
class Frame:
    header: bytes
    body: bytes

    @property
    def command(self) -> int:
        return int.from_bytes(self.header[6:8], "big")

    @property
    def sequence(self) -> int:
        return int.from_bytes(self.header[9:13], "big")

    def wire(self) -> bytes:
        header = bytearray(self.header)
        header[17:21] = len(self.body).to_bytes(4, "big")
        return bytes(header) + self.body

    def public(self, command: int) -> int:
        if self.command != command or len(self.header) < 88 or self.header[21:24] != b"\x03\x00\x40":
            raise TgcpError("TGCP hello 格式不匹配")
        value = int.from_bytes(self.header[24:88], "big")
        validate_public(value)
        return value

    def replace_public(self, value: int, body: bytes) -> "Frame":
        header = bytearray(self.header)
        header[24:88] = public_bytes(value)
        return Frame(bytes(header), body)


def unpad(padded: bytes) -> bytes:
    if len(padded) < 16:
        raise TgcpError("TGCP 密文过短")
    padding = padded[-1]
    if padding < 6 or padding > 21 or padding > len(padded) or padded[-6:-1] != b"tsf4g":
        raise TgcpError("TGCP 填充标记校验失败")
    length = len(padded) - padding
    expected = (32 if length % 16 >= 11 else 16) - length % 16
    if padding != expected:
        raise TgcpError("TGCP 填充长度错误")
    return padded[:length]


def decrypt_padded(body: bytes, key: bytes) -> bytes:
    if not body or len(body) % 16:
        raise TgcpError("TGCP 密文未对齐")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(IV)).decryptor()
    return decryptor.update(body) + decryptor.finalize()


def encrypt_padded(padded: bytes, key: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(key), modes.CBC(IV)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def encrypt(plain: bytes, key: bytes) -> bytes:
    padding = (32 if len(plain) % 16 >= 11 else 16) - len(plain) % 16
    result = bytearray(plain) + b"\x00" * padding
    result[-6:-1] = b"tsf4g"
    result[-1] = padding
    return encrypt_padded(bytes(result), key)


def banks(plain: bytes):
    for match in BANK_RE.finditer(plain):
        token = match.group(1)
        try:
            bank = base64.b64decode(token, validate=True)
        except Exception:
            continue
        if len(bank) == 128 and base64.b64encode(bank) == token:
            yield bank


@dataclass
class Observation:
    up: bool
    command: int
    sequence: int
    plain: bytes
    banks: list[bytes]


class TgcpSession:
    """State machine for one proxied TCP connection."""

    def __init__(self) -> None:
        self._x, self._public_x = key_pair()
        self._y, self._public_y = key_pair()
        self._client_key: bytes | None = None
        self._server_key: bytes | None = None
        self._buffers = [bytearray(), bytearray()]  # 0 = up, 1 = down
        self._finished = [False, False]
        self._banks: set[str] = set()
        self.state = 0
        self.frames = 0

    def feed(self, up: bool, data: bytes) -> tuple[bytes, bytes, list[Observation]]:
        if self.state < 0 or self._finished[0 if up else 1]:
            raise TgcpError("TGCP 会话已结束")
        to_server = bytearray()
        to_client = bytearray()
        observations: list[Observation] = []
        self._buffers[0 if up else 1].extend(data)

        def drain(direction: bool) -> None:
            nonlocal observations
            buffer = self._buffers[0 if direction else 1]
            while not (direction and self.state == 1):
                frame = self._take(buffer)
                if frame is None:
                    break
                if frame.header[8] != (0 if direction else 1):
                    raise TgcpError("TGCP 方向不匹配")
                if self.state == 0:
                    if not direction or frame.body:
                        raise TgcpError("当前配置要求空体 client hello")
                    self._client_key = derive(frame.public(HELLO_CLIENT), self._x)
                    to_server.extend(frame.replace_public(self._public_y, b"").wire())
                    self.state = 1
                    self.frames += 1
                    continue
                if self.state == 1:
                    if direction:
                        raise TgcpError("服务端 hello 尚未到达")
                    self._server_key = derive(frame.public(HELLO_SERVER), self._y)
                    frame = frame.replace_public(self._public_x, frame.body)
                    self.state = 2
                    self._x = self._y = 0
                elif frame.command in (HELLO_CLIENT, HELLO_SERVER):
                    raise TgcpError("意外的重复 TGCP hello")

                source = self._client_key if direction else self._server_key
                target = self._server_key if direction else self._client_key
                if source is None or target is None:
                    raise TgcpError("TGCP 密钥尚未就绪")
                if frame.body:
                    padded = decrypt_padded(frame.body, source)
                    plain = unpad(padded)
                    candidates: list[bytes] = []
                    for bank in banks(plain):
                        fingerprint = hashlib.sha256(bank).hexdigest()
                        if fingerprint not in self._banks:
                            self._banks.add(fingerprint)
                            candidates.append(bank)
                        if len(self._banks) > 1024:
                            raise TgcpError("会话候选密钥数量超限")
                    observations.append(
                        Observation(direction, frame.command, frame.sequence, plain, candidates)
                    )
                    frame = Frame(frame.header, encrypt_padded(padded, target))
                (to_server if direction else to_client).extend(frame.wire())
                self.frames += 1

        drain(up)
        if self.state == 2:
            drain(not up)
        return bytes(to_server), bytes(to_client), observations

    @staticmethod
    def _take(buffer: bytearray) -> Frame | None:
        if len(buffer) < 21:
            return None
        header, body = lengths(buffer)
        if len(buffer) < header + body:
            return None
        frame = Frame(bytes(buffer[:header]), bytes(buffer[header:header + body]))
        del buffer[:header + body]
        return frame

    def finish(self, up: bool) -> None:
        if self.state != 2 or self._buffers[0 if up else 1]:
            raise TgcpError("TCP 在完整 TGCP 握手/帧前结束")
        self._finished[0 if up else 1] = True

    def dispose(self) -> None:
        self.state = -1
        self._x = self._y = 0
        self._client_key = None
        self._server_key = None
