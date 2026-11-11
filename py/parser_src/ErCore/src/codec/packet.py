"""DFM packet layer: custom 8-byte request framing + UE-style bitstream header.

Request direction (client -> server) frames are wrapped as:
    tag:u32 LE | serial:u32 LE | ue_data | 1 padding byte
Response frames are the raw UE bitstream.

UE bitstream layout (per the previous session's notes in
battle_codec_capture_simplified.rs):

    data_bits      = termination_data_bits(ue)      (last set bit + 1)
    bit 0          = handler bit (0 = data, 1 = control/handshake)
    bits 1..32     = packed u32:
                       history_count (4 bits)  -> +1  => count
                       acked_seq     (14 bits)
                       packet_seq    (14 bits)
    bits 33..      = history_count x 32-bit ack history words
    body           = remaining bits (bunch stream)
"""
from __future__ import annotations
from dataclasses import dataclass, field

from .bitstream import bit, read_bits_at, slice_bits, termination_data_bits

REQUEST = "request"
RESPONSE = "response"

PACKET_KIND_INVALID = "invalid"
PACKET_KIND_EMPTY = "empty"
PACKET_KIND_CONTROL = "control"
PACKET_KIND_NORMAL = "normal"
PACKET_KIND_HEADER_ONLY = "header_only"


@dataclass
class Packet:
    capture_sequence: int
    direction: str
    request_tag: int | None
    request_serial: int | None
    ue: bytes
    packet_kind: str
    handler_bit: int = 0
    acked_sequence: int | None = None
    packet_sequence: int | None = None
    history_words: list = field(default_factory=list)
    body: bytes = b""
    body_bits: int = 0
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "capture_sequence": self.capture_sequence,
            "direction": self.direction,
            "request_tag": self.request_tag,
            "request_serial": self.request_serial,
            "ue_len": len(self.ue),
            "packet_kind": self.packet_kind,
            "handler_bit": self.handler_bit,
            "acked_sequence": self.acked_sequence,
            "packet_sequence": self.packet_sequence,
            "history_words": self.history_words,
            "body_bits": self.body_bits,
            "error": self.error,
        }


def _u32le(b: bytes, off: int = 0) -> int:
    return int.from_bytes(b[off : off + 4], "little")


def decode_packet_parts(
    capture_sequence: int,
    direction: str,
    raw: bytes,
    request_prefix: bool = True,
) -> Packet:
    request = direction == REQUEST

    # Request framing: tag(4) + serial(4) + ue + padding(1)
    request_tag = None
    request_serial = None
    if request and request_prefix and len(raw) >= 9:
        request_tag = _u32le(raw, 0)
        request_serial = _u32le(raw, 4)
        ue = raw[8:-1]
    else:
        ue = raw

    pkt = Packet(
        capture_sequence=capture_sequence,
        direction=direction,
        request_tag=request_tag,
        request_serial=request_serial,
        ue=ue,
        packet_kind=PACKET_KIND_INVALID,
    )

    data_bits = termination_data_bits(ue)
    if data_bits == 0:
        pkt.packet_kind = PACKET_KIND_EMPTY
        pkt.error = "no termination bit / zero payload"
        return pkt

    pkt.handler_bit = bit(ue, 0)
    if pkt.handler_bit == 1:
        pkt.packet_kind = PACKET_KIND_CONTROL
        # control/handshake packets: structure unknown, keep raw for analysis
        return pkt

    if data_bits < 33:
        pkt.packet_kind = PACKET_KIND_INVALID
        pkt.error = f"header shorter than 33 bits ({data_bits})"
        return pkt

    packed = read_bits_at(ue, 1, 32)
    history_count = (packed & 0xF) + 1
    acked = (packed >> 4) & 0x3FFF
    sequence = (packed >> 18) & 0x3FFF

    pkt.acked_sequence = acked
    pkt.packet_sequence = sequence

    header_bits = 33 + history_count * 32
    if header_bits > data_bits:
        pkt.packet_kind = PACKET_KIND_HEADER_ONLY
        pkt.error = f"history words exceed packet ({header_bits}>{data_bits})"
        return pkt

    pkt.history_words = [
        read_bits_at(ue, 33 + i * 32, 32) for i in range(history_count)
    ]

    pkt.body_bits = data_bits - header_bits
    pkt.body = slice_bits(ue, header_bits, pkt.body_bits)
    pkt.packet_kind = PACKET_KIND_NORMAL
    return pkt
