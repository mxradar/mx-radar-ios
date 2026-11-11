"""Season (type-7) handshake crypto, ported 1:1 from the Doubao soft-router.

Wire format: 69-byte control packet = 5 status bits | 544-bit body | terminator
bit 549, little-endian bit stream.  Body = 4-byte parameter + 64-byte payload.

The relay generates its own X25519 scalar, builds the client hello with two
MD5 confirmation digests and derives the 16-byte UDP session key with
SHA-512(shared || client_public || server_public)[:16].
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
import struct
import zlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

MASK32 = 0xFFFFFFFF
DELTA = 0x9E3779B9
SUM_INIT = DELTA * 32 & MASK32

_xtea_native = None
_xtea_error = None
try:
    # Load from Android's nativeLibraryDir (also works inside uncompressed APK
    # native library mappings). Assets are data and may be mounted no-exec.
    try:
        from java import jclass
        _xtea_paths = [str(path) for path in jclass("cn.radar.client.Logx").INSTANCE.nativeCryptoPaths()]
    except ImportError:
        _xtea_paths = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "libxtea.so")]
    for _xtea_path in _xtea_paths:
        try:
            _xtea_lib = ctypes.CDLL(_xtea_path)
            break
        except OSError:
            continue
    else:
        raise OSError("XTEA native library unavailable")
    _xtea_lib.xtea_cbc_encrypt.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_void_p]
    _xtea_lib.xtea_cbc_encrypt.restype = None
    _xtea_lib.xtea_cbc_decrypt.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_void_p]
    _xtea_lib.xtea_cbc_decrypt.restype = None
    _xtea_native = _xtea_lib
except Exception as exc:
    _xtea_native = None
    _xtea_error = type(exc).__name__


def crypto_status() -> dict:
    return {"backend": "native-xtea" if _xtea_native is not None else "python-xtea",
            "error": _xtea_error}


def _length(value: bytes, expected: int, name: str) -> None:
    if len(value) != expected:
        raise ValueError(f"{name} must contain {expected} bytes")


@dataclass(frozen=True)
class Control7:
    parameter: int
    data: bytes

    @property
    def client_public(self) -> bytes:
        return self.data[:32]

    @property
    def first_digest(self) -> bytes:
        return self.data[32:48]

    @property
    def second_digest(self) -> bytes:
        return self.data[48:64]

    def client_digest_chain_matches(self) -> bool:
        return hmac.compare_digest(
            hashlib.md5(self.client_public + self.first_digest).digest(),
            self.second_digest,
        )

    def shared_secret_matches(self, shared: bytes) -> bool:
        _length(shared, 32, "shared secret")
        if not self.client_digest_chain_matches():
            return False
        return hmac.compare_digest(
            hashlib.md5(self.client_public + shared).digest(),
            self.first_digest,
        )


@dataclass(frozen=True)
class SignedServerConfig:
    """Authenticated public configuration; contains no session secret."""

    exchange_public: bytes
    signing_public: bytes
    root_signature: bytes

    @classmethod
    def parse(cls, data: bytes, root_public: bytes) -> "SignedServerConfig":
        if not verify_server_config(data, root_public):
            raise ValueError("Server configuration root signature did not verify")
        return cls(data[:32], data[32:64], data[64:])

    @property
    def identifier(self) -> int:
        return zlib.crc32(self.exchange_public)

    def matches_transcript(self, client: Control7, ack: Control7) -> bool:
        return (
            client.parameter == self.identifier
            and client.client_digest_chain_matches()
            and ack.parameter == 0
            and verify_ack(ack, client.client_public, self.signing_public)
        )

    def key_from_client_private(self, client: Control7, ack: Control7, private_bytes: bytes) -> bytes:
        _length(private_bytes, 32, "client private")
        if not self.matches_transcript(client, ack):
            raise ValueError("Configuration does not authenticate the captured handshake")
        private = X25519PrivateKey.from_private_bytes(private_bytes)
        public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        if not hmac.compare_digest(public, client.client_public):
            raise ValueError("Private material belongs to a different client key epoch")
        shared = private.exchange(X25519PublicKey.from_public_bytes(self.exchange_public))
        if not client.shared_secret_matches(shared):
            raise ValueError("Private material does not match the captured shared-secret commitment")
        return session_key(shared, public, self.exchange_public)


def parse_control7(payload: bytes) -> Control7:
    """Parse a UE payload; remove the uplink's 8-byte prefix before calling."""
    if len(payload) != 69 or (payload[0] & 31) != 15:
        raise ValueError("Not an exact type-7 control payload")
    bits = int.from_bytes(payload, "little")
    if bits.bit_length() != 550:
        raise ValueError("Invalid type-7 termination or trailing bits")
    body = (bits >> 5) & ((1 << 544) - 1)
    raw = body.to_bytes(68, "little")
    return Control7(int.from_bytes(raw[:4], "little"), raw[4:])


def build_control7(parameter: int, body: bytes) -> bytes:
    if len(body) != 64:
        raise ValueError("Expected a 64-byte type-7 body")
    value = int.from_bytes(struct.pack("<I", parameter) + body, "little")
    return (15 | (value << 5) | (1 << 549)).to_bytes(69, "little")


def session_key(shared: bytes, client_public: bytes, server_public: bytes) -> bytes:
    _length(shared, 32, "shared secret")
    _length(client_public, 32, "client public")
    _length(server_public, 32, "server public")
    return hashlib.sha512(shared + client_public + server_public).digest()[:16]


def derive_session_key(client_private: bytes, server_public: bytes) -> bytes:
    _length(client_private, 32, "client private")
    _length(server_public, 32, "server public")
    private = X25519PrivateKey.from_private_bytes(client_private)
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    shared = private.exchange(X25519PublicKey.from_public_bytes(server_public))
    return session_key(shared, public, server_public)


def verify_server_config(config: bytes, root_public: bytes) -> bool:
    _length(config, 128, "signed server config")
    _length(root_public, 32, "root signing public")
    try:
        Ed25519PublicKey.from_public_bytes(root_public).verify(config[64:], config[:64])
    except InvalidSignature:
        return False
    return True


def verify_ack(ack: Control7, client_public: bytes, signing_public: bytes) -> bool:
    _length(client_public, 32, "client public")
    _length(signing_public, 32, "server signing public")
    try:
        Ed25519PublicKey.from_public_bytes(signing_public).verify(
            ack.data, client_public + struct.pack("<I", ack.parameter)
        )
    except InvalidSignature:
        return False
    return True


def xtea_cbc_decrypt(data: bytes, key: bytes) -> bytes:
    """Native-verified little-endian XTEA-CBC for current Android UDP data."""
    if len(key) != 16:
        raise ValueError("key must be exactly 16 bytes")
    if _xtea_native is not None and data:
        out = ctypes.create_string_buffer(len(data))
        _xtea_native.xtea_cbc_decrypt(data, len(data), key, out)
        return out.raw
    words = list(struct.unpack("<4I", key))
    blocks = len(data) // 8
    out = bytearray()
    previous_a = previous_b = 0
    for index in range(blocks):
        cipher_a, cipher_b = struct.unpack_from("<II", data, index * 8)
        a, b = cipher_a, cipher_b
        total = SUM_INIT
        total2 = (DELTA * 31) & MASK32
        for _ in range(32):
            mix_a = (words[total >> 11 & 3] + total) & MASK32
            mix_b = (words[total2 & 3] + total2) & MASK32
            total = (total - DELTA) & MASK32
            total2 = (total2 - DELTA) & MASK32
            b = (b - (mix_a ^ (((a << 4 & MASK32) ^ (a >> 5)) + a) & MASK32)) & MASK32
            a = (a - (mix_b ^ (((b << 4 & MASK32) ^ (b >> 5)) + b) & MASK32)) & MASK32
        out += struct.pack("<II", a ^ previous_a, b ^ previous_b)
        previous_a, previous_b = cipher_a, cipher_b
    out += data[blocks * 8:]
    return bytes(out)


def xtea_cbc_encrypt(data: bytes, key: bytes) -> bytes:
    """Inverse of xtea_cbc_decrypt, used to translate downlinks to mobile key."""
    if len(key) != 16:
        raise ValueError("key must be exactly 16 bytes")
    if _xtea_native is not None and data:
        out = ctypes.create_string_buffer(len(data))
        _xtea_native.xtea_cbc_encrypt(data, len(data), key, out)
        return out.raw
    words = list(struct.unpack("<4I", key))
    blocks = len(data) // 8
    out = bytearray()
    previous_a = previous_b = 0
    for index in range(blocks):
        a, b = struct.unpack_from("<II", data, index * 8)
        a ^= previous_a
        b ^= previous_b
        total = 0
        for _ in range(32):
            a = (a + ((words[total & 3] + total) & MASK32 ^ (((b << 4 & MASK32) ^ (b >> 5)) + b) & MASK32)) & MASK32
            total = (total + DELTA) & MASK32
            b = (b + ((words[total >> 11 & 3] + total) & MASK32 ^ (((a << 4 & MASK32) ^ (a >> 5)) + a) & MASK32)) & MASK32
        out += struct.pack("<II", a, b)
        previous_a, previous_b = a, b
    out += data[blocks * 8:]
    return bytes(out)


def decrypt_downlink(payload: bytes, key: bytes) -> bytes:
    """First byte and controls stay clear; data blocks are XTEA-CBC from byte 1."""
    _length(key, 16, "session key")
    if not payload:
        raise ValueError("Empty UDP payload")
    if payload[0] & 1:
        return payload
    return payload[:1] + xtea_cbc_decrypt(payload[1:], key)


def encrypt_downlink(plain: bytes, key: bytes) -> bytes:
    _length(key, 16, "session key")
    if not plain:
        raise ValueError("Expected a session key and nonempty datagram")
    if plain[0] & 1:
        return plain
    return plain[:1] + xtea_cbc_encrypt(plain[1:], key)
