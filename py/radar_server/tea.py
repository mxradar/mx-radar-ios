"""UDP payload cipher used by the game (TEA-like, 128-byte key bank)."""

from __future__ import annotations

MASK = 0xFFFFFFFF
DELTA = 0x9E3779B9
SUM_INIT = 0xC6EF3720


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little")


def _w32(data: bytearray, offset: int, value: int) -> None:
    data[offset:offset + 4] = (value & MASK).to_bytes(4, "little")


def _f(x: int, total: int, key: int) -> int:
    return (((((x << 4) & MASK) ^ (x >> 5)) + x) & MASK) ^ ((total + key) & MASK)


def _g(x: int, total: int, key: int) -> int:
    return (((((x << 4) & MASK) ^ (x >> 5)) + x) & MASK) ^ ((total + key) & MASK)


def decrypt(payload: bytes, bank: bytes) -> bytes:
    if len(bank) != 128:
        raise ValueError("UDP bank 必须为 128 字节")
    result = bytearray(payload)
    for offset in range(0, len(result) - 7, 8):
        a = _u32(result, offset)
        b = _u32(result, offset + 4)
        total = SUM_INIT
        base = ((offset // 8) % 8) * 4
        for _ in range(32):
            b = (b - _f(a, total, _u32(bank, (base + ((total >> 11) & 3)) * 4))) & MASK
            total = (total - DELTA) & MASK
            a = (a - _g(b, total, _u32(bank, (base + (total & 3)) * 4))) & MASK
        _w32(result, offset, a)
        _w32(result, offset + 4, b)
    return bytes(result)


def encrypt(payload: bytes, bank: bytes) -> bytes:
    """Inverse of decrypt(), used by tests and by an optional re-encrypt path."""
    if len(bank) != 128:
        raise ValueError("UDP bank 必须为 128 字节")
    result = bytearray(payload)
    for offset in range(0, len(result) - 7, 8):
        a = _u32(result, offset)
        b = _u32(result, offset + 4)
        total = 0  # decrypt() finishes with sum 0 after 32 rounds
        base = ((offset // 8) % 8) * 4
        for _ in range(32):
            a = (a + _g(b, total, _u32(bank, (base + (total & 3)) * 4))) & MASK
            total = (total + DELTA) & MASK
            b = (b + _f(a, total, _u32(bank, (base + ((total >> 11) & 3)) * 4))) & MASK
        _w32(result, offset, a)
        _w32(result, offset + 4, b)
    return bytes(result)


class _Bits:
    def __init__(self, data: bytes, end: int):
        self.data = data
        self.end = end
        self.position = 1

    def read(self, count: int) -> int | None:
        if count < 0 or count > 32 or count > self.end - self.position:
            return None
        value = 0
        for i in range(count):
            bit = (self.data[(self.position + i) >> 3] >> ((self.position + i) & 7)) & 1
            value |= bit << i
        self.position += count
        return value

    def packed(self) -> int | None:
        value = 0
        for i in range(5):
            raw = self.read(8)
            if raw is None or (i == 4 and (raw & 0xE1) != 0):
                return None
            value |= (raw >> 1) << (i * 7)
            if (raw & 1) == 0:
                return value
        return None


def valid_envelope(data: bytes) -> bool:
    """Port of the Nva UE4 packet envelope sanity check."""
    if len(data) < 10 or len(data) > 2048 or (data[0] & 1) != 0 or data[-1] == 0:
        return False
    bits = data[-1].bit_length()
    end = (len(data) - 1) * 8 + bits - 2
    if end < 0 or ((data[end >> 3] >> (end & 7)) & 1) == 0:
        return False
    reader = _Bits(data, end)
    words = reader.read(4)
    if words is None:
        return False
    reader.position = 33 + (words + 1) * 32
    if reader.position > end:
        return False
    marker = reader.read(1)
    if marker is None or (marker == 1 and reader.read(8) is None) or reader.read(8) is None:
        return False
    bunches = 0
    while reader.position < end:
        if end - reader.position < 20 or bunches >= 128:
            return False
        control = reader.read(1)
        if control is None:
            return False
        opened = False
        if control == 1:
            opened = reader.read(1) == 1
            closed = reader.read(1) == 1
            if closed and reader.read(4) is None:
                return False
        if reader.read(1) is None:
            return False
        reliable = reader.read(1)
        if reliable is None or reader.read(1) is None:
            return False
        channel = reader.packed()
        if channel is None or channel > 32767 or reader.read(1) is None or reader.read(1) is None:
            return False
        partial = reader.read(1)
        if partial is None:
            return False
        if reliable == 1 and reader.read(10) is None:
            return False
        if partial == 1 and (reader.read(1) is None or reader.read(1) is None):
            return False
        if reliable == 1 or opened:
            exported = reader.read(1)
            if exported is None:
                return False
            if exported == 1:
                if reader.packed() is None:
                    return False
            else:
                length = reader.read(32)
                if length is None:
                    return False
                signed = length - (1 << 32) if length & 0x80000000 else length
                count = abs(signed)
                width = 16 if signed < 0 else 8
                if count == 0 or count > 256 or count * width > end - reader.position:
                    return False
                reader.position += count * width
                if reader.read(32) is None:
                    return False
        size = reader.read(13)
        if size is None or size > end - reader.position:
            return False
        reader.position += size
        bunches += 1
    return reader.position == end
