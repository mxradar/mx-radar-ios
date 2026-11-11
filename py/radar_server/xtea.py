"""Native-verified little-endian XTEA-CBC for current Android UDP data."""
import struct

DELTA = 2654435769

HIST_INIT = DELTA * 32 & 4294967295

HIST_INIT2 = DELTA * 31 & 4294967295

NEG_DELTA = 4294967296 - DELTA & 4294967295

def xtea_cbc_decrypt(data: bytes, key16: bytes) -> bytes:
    if len(key16) != 16:
        raise ValueError('key must be exactly 16 bytes')
    k = list(struct.unpack('<4I', key16))
    n = len(data) // 8
    out = bytearray()
    prev_lo = prev_hi = 0
    for i in range(n):
        c0, c1 = struct.unpack_from('<II', data, i * 8)
        v0, v1 = (c0, c1)
        sum1 = HIST_INIT
        sum2 = HIST_INIT2
        for _ in range(32):
            idx1 = sum1 >> 11 & 3
            mx1 = k[idx1] + sum1 & 4294967295
            idx2 = sum2 & 3
            mx2 = k[idx2] + sum2 & 4294967295
            sum1 = sum1 + NEG_DELTA & 4294967295
            sum2 = sum2 + NEG_DELTA & 4294967295
            t = v0 << 4 & 4294967295 ^ v0 >> 5
            v1 = v1 - (mx1 ^ t + v0 & 4294967295) & 4294967295
            t = v1 << 4 & 4294967295 ^ v1 >> 5
            v0 = v0 - (mx2 ^ t + v1 & 4294967295) & 4294967295
        v0 ^= prev_lo
        v1 ^= prev_hi
        out += struct.pack('<II', v0, v1)
        prev_lo, prev_hi = (c0, c1)
    out += data[n * 8:]
    return bytes(out)

def decrypt_packet(pkt: bytes, key16: bytes, skip: int=1) -> bytes:
    if skip < 0 or skip > len(pkt):
        raise ValueError('skip must be between zero and packet length')
    return pkt[:skip] + xtea_cbc_decrypt(pkt[skip:], key16)
