"""Connection-level framing for the recovered DFM/UE4 bunch stream.

There are two nested UE termination bits in a normal captured datagram:

``PacketHandler outer frame``
    ``handler`` + ``FNetPacketNotify`` + history + ``ReceivedPacket body`` +
    outer termination.

``UNetConnection::ReceivedPacket body``
    9/17-bit legacy ``PacketHeader`` + bunches + inner termination.

The historical :mod:`codec.packet` reader intentionally stops after the
first layer.  This module removes the inner termination, reads PacketHeader,
and invokes :func:`codec.engine_bunch.decode_engine_bunches` on precisely the
remaining interval.
"""
from __future__ import annotations

from dataclasses import dataclass

from .bitstream import BitError, BitReader, bit, slice_bits, termination_data_bits
from .engine_bunch import (
    DEFAULT_MAX_CHANNEL_INDEX,
    DEFAULT_MAX_PACKET_BYTES,
    EngineBunchParse,
    decode_engine_bunches,
)
from .packet import (
    PACKET_KIND_NORMAL,
    REQUEST,
    RESPONSE,
    Packet,
    decode_packet_parts,
)


@dataclass
class LegacyPacketHeader:
    """The connection-level header immediately before the first bunch."""

    direction: str
    frame_time_flag: int = 0
    server_frame_time_byte: int | None = None
    remote_kbytes_per_second_byte: int | None = None
    start_bit: int = 0
    end_bit: int = 0
    error: str | None = None
    error_bit: int | None = None

    @property
    def bit_count(self) -> int:
        return self.end_bit - self.start_bit

    def as_dict(self) -> dict:
        return {
            "direction": self.direction,
            "frame_time_flag": self.frame_time_flag,
            "server_frame_time_byte": self.server_frame_time_byte,
            "remote_kbytes_per_second_byte": self.remote_kbytes_per_second_byte,
            "interval": [self.start_bit, self.end_bit],
            "bits": self.bit_count,
            "error": self.error,
            "error_bit": self.error_bit,
        }


def decode_legacy_packet_header(
    packet_body: bytes,
    data_bits: int,
    direction: str,
    *,
    response_frame_time_capable: bool = True,
) -> LegacyPacketHeader:
    """Read the 9/17-bit PacketHeader recovered at ``ReceivedPacket``.

    The leading bit requests/reports server-frame-time information.  A
    server-to-client packet carries an additional byte when that bit is set
    and the connection supports it; client-to-server packets do not.  Both
    directions always carry the remote bandwidth byte.
    """

    out = LegacyPacketHeader(direction=direction)
    reader = BitReader(packet_body, data_bits)
    try:
        out.frame_time_flag = reader.bit()
        if direction == RESPONSE and out.frame_time_flag and response_frame_time_capable:
            out.server_frame_time_byte = reader.uint8()
        out.remote_kbytes_per_second_byte = reader.uint8()
        out.end_bit = reader.position
    except BitError as exc:
        out.end_bit = reader.position
        out.error = str(exc)
        out.error_bit = reader.position
    return out


@dataclass
class EnginePacketTrace:
    """A normal packet plus all recovered nested framing coordinates."""

    packet: Packet
    raw_len: int
    raw_ue_start_bit: int
    outer_termination_ue_bit: int | None = None
    packet_body_ue_start_bit: int | None = None
    inner_termination_body_bit: int | None = None
    inner_trailing_zero_bits: int = 0
    packet_header: LegacyPacketHeader | None = None
    bunch_parse: EngineBunchParse | None = None
    error: str | None = None

    def _body_interval(self, start: int, end: int) -> dict[str, list[int]]:
        assert self.packet_body_ue_start_bit is not None
        ue_start = self.packet_body_ue_start_bit + start
        ue_end = self.packet_body_ue_start_bit + end
        return {
            "packet_body": [start, end],
            "ue": [ue_start, ue_end],
            "raw": [self.raw_ue_start_bit + ue_start, self.raw_ue_start_bit + ue_end],
        }

    def coordinate_summary(self) -> dict:
        out: dict[str, object] = {
            "raw_ue_interval": [
                self.raw_ue_start_bit,
                self.raw_ue_start_bit + len(self.packet.ue) * 8,
            ]
        }
        if self.outer_termination_ue_bit is not None:
            outer = self.outer_termination_ue_bit
            out["outer_data"] = {
                "ue": [0, outer],
                "raw": [self.raw_ue_start_bit, self.raw_ue_start_bit + outer],
            }
            out["outer_termination"] = {
                "ue": [outer, outer + 1],
                "raw": [
                    self.raw_ue_start_bit + outer,
                    self.raw_ue_start_bit + outer + 1,
                ],
            }
        if self.packet_body_ue_start_bit is not None:
            out["packet_body_storage"] = self._body_interval(0, self.packet.body_bits)
        if self.inner_termination_body_bit is not None:
            inner = self.inner_termination_body_bit
            out["received_packet_data"] = self._body_interval(0, inner)
            out["inner_termination"] = self._body_interval(inner, inner + 1)
            if self.packet_header is not None:
                out["packet_header"] = self._body_interval(
                    self.packet_header.start_bit, self.packet_header.end_bit
                )
                out["bunch_stream"] = self._body_interval(
                    self.packet_header.end_bit, inner
                )
        return out

    def as_dict(self, include_payload: bool = False) -> dict:
        return {
            "packet": self.packet.as_dict(),
            "raw_len": self.raw_len,
            "raw_ue_start_bit": self.raw_ue_start_bit,
            "outer_termination_ue_bit": self.outer_termination_ue_bit,
            "packet_body_ue_start_bit": self.packet_body_ue_start_bit,
            "inner_termination_body_bit": self.inner_termination_body_bit,
            "inner_trailing_zero_bits": self.inner_trailing_zero_bits,
            "packet_header": self.packet_header.as_dict() if self.packet_header else None,
            "coordinates": self.coordinate_summary(),
            "bunch_parse": (
                self.bunch_parse.as_dict(include_payload) if self.bunch_parse else None
            ),
            "error": self.error,
        }


def decode_engine_packet(
    capture_sequence: int,
    direction: str,
    raw: bytes,
    *,
    request_prefix: bool = True,
    response_frame_time_capable: bool = True,
    max_packet_bytes: int = DEFAULT_MAX_PACKET_BYTES,
    max_channel_index: int | None = DEFAULT_MAX_CHANNEL_INDEX,
    max_bunches: int = 256,
) -> EnginePacketTrace:
    """Decode both termination layers, PacketHeader, and all engine bunches."""

    packet = decode_packet_parts(
        capture_sequence,
        direction,
        raw,
        request_prefix=request_prefix,
    )
    raw_ue_start = (
        64
        if direction == REQUEST and request_prefix and len(raw) >= 9
        else 0
    )
    trace = EnginePacketTrace(
        packet=packet,
        raw_len=len(raw),
        raw_ue_start_bit=raw_ue_start,
    )

    if packet.packet_kind != PACKET_KIND_NORMAL:
        trace.error = packet.error
        return trace

    outer_data_bits = termination_data_bits(packet.ue)
    trace.outer_termination_ue_bit = outer_data_bits
    trace.packet_body_ue_start_bit = outer_data_bits - packet.body_bits
    if trace.packet_body_ue_start_bit < 0:
        trace.error = "packet body starts before UE bit zero"
        return trace

    if not packet.body or not any(packet.body):
        trace.error = "missing inner termination bit"
        return trace
    inner_data_bits = termination_data_bits(packet.body)
    if bit(packet.body, inner_data_bits) != 1:
        trace.error = "missing inner termination bit"
        return trace
    trace.inner_termination_body_bit = inner_data_bits
    trace.inner_trailing_zero_bits = packet.body_bits - inner_data_bits - 1
    if trace.inner_trailing_zero_bits < 0:
        trace.error = "inner termination lies outside Packet.body"
        return trace

    header = decode_legacy_packet_header(
        packet.body,
        inner_data_bits,
        direction,
        response_frame_time_capable=response_frame_time_capable,
    )
    trace.packet_header = header
    if header.error:
        trace.error = f"PacketHeader: {header.error}"
        return trace

    bunch_bits = inner_data_bits - header.end_bit
    if bunch_bits < 0:
        trace.error = "PacketHeader exceeds inner data interval"
        return trace
    bunch_data = slice_bits(packet.body, header.end_bit, bunch_bits)
    body_base = header.end_bit
    ue_base = trace.packet_body_ue_start_bit + body_base
    coordinate_bases = {
        "packet_body": body_base,
        "ue": ue_base,
        "raw": raw_ue_start + ue_base,
    }
    trace.bunch_parse = decode_engine_bunches(
        bunch_data,
        bunch_bits,
        max_packet_bytes=max_packet_bytes,
        max_channel_index=max_channel_index,
        max_bunches=max_bunches,
        coordinate_bases=coordinate_bases,
    )
    return trace
