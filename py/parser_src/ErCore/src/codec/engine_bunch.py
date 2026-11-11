"""Engine-exact DFM bunch-header reader recovered from ``ReceivedPacket``.

This module is deliberately separate from :mod:`codec.bunch`.  The latter
contains the historical/provisional layouts used by the running radar.  This
reader mirrors the ARM64 instruction flow in the current DFM ``libUE4`` and is
intended for offline evidence and regression work until the wire chain is
closed.

The input to :func:`decode_engine_bunches` is the *pure bunch region*.  The
connection-level inner termination bit and the 9/17-bit ``PacketHeader`` must
already be removed.  :mod:`codec.engine_packet` performs that framing and
also supplies bases for ``raw``/``ue``/``packet_body`` coordinates.

Recovered header order (LSB-first)::

    control_gate
      if set: open, close, [CloseReason = SerializeInt(15)]
    replication_paused, reliable, pre_channel_flag
    channel = DFM SerializeIntPacked (continuation in bit 0)
    has_exports, has_must_be_mapped_guids, partial
    if reliable: channel_sequence_mod = SerializeInt(1024)
    if partial: partial_initial, partial_final
    if open or reliable: FName
    payload_bits = SerializeInt(MaxPacket * 8)
    payload

The engine unwraps the 10-bit reliable sequence against per-channel live
state.  Offline parsing therefore records the serialized modulo value; a
stateful caller can unwrap it after ordering packets.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .bitstream import BitError, BitReader, slice_bits


DFM_MAX_PACKED_BYTES = 5
HARD_CODED_FNAME_MAX = 0x100000
RELIABLE_SEQUENCE_MAX = 0x400
DEFAULT_MAX_PACKET_BYTES = 1024
DEFAULT_MAX_CHANNEL_INDEX = 32767


@dataclass
class SerializedFName:
    """Wire representation needed to advance the bunch-header cursor."""

    hardcoded: bool
    hardcoded_index: int | None = None
    text: str | None = None
    number: int = 0
    start_bit: int = 0
    end_bit: int = 0

    def as_dict(self) -> dict:
        return {
            "hardcoded": self.hardcoded,
            "hardcoded_index": self.hardcoded_index,
            "text": self.text,
            "number": self.number,
            "interval": [self.start_bit, self.end_bit],
        }


@dataclass
class EngineBunch:
    index: int = 0
    header_start_bit: int = 0
    header_end_bit: int = 0
    payload_start_bit: int = 0
    payload_end_bit: int = 0
    control_gate: int = 0
    open: int = 0
    close: int = 0
    close_reason: int | None = None
    dormant: int = 0
    replication_paused: int = 0
    reliable: int = 0
    pre_channel_flag: int = 0
    channel_index: int = 0
    channel_packed_bytes: list[int] = field(default_factory=list)
    has_package_map_exports: int = 0
    has_must_be_mapped_guids: int = 0
    partial: int = 0
    partial_initial: int = 1
    partial_final: int = 1
    channel_sequence_mod: int | None = None
    reliable_sequence: int | None = None
    channel_name: SerializedFName | None = None
    payload_bit_count: int = 0
    payload: bytes = b""
    error: str | None = None
    error_bit: int | None = None
    coordinate_bases: dict[str, int] = field(default_factory=dict, repr=False)

    def coordinate_intervals(self) -> dict[str, dict[str, list[int]]]:
        """Return half-open intervals in every coordinate space supplied.

        ``header_*`` and ``payload_*`` are local to the pure bunch stream.
        A caller may add bases such as ``packet_body`` or ``raw`` without
        copying or re-slicing the payload.
        """

        bases = {"bunch_stream": 0, **self.coordinate_bases}
        return {
            name: {
                "bunch_interval": [
                    base + self.header_start_bit,
                    base + self.payload_end_bit,
                ],
                "header_interval": [
                    base + self.header_start_bit,
                    base + self.header_end_bit,
                ],
                "payload_interval": [
                    base + self.payload_start_bit,
                    base + self.payload_end_bit,
                ],
            }
            for name, base in bases.items()
        }

    def as_dict(self, include_payload: bool = False) -> dict:
        out = {
            "index": self.index,
            "header_interval": [self.header_start_bit, self.header_end_bit],
            "payload_interval": [self.payload_start_bit, self.payload_end_bit],
            "control_gate": self.control_gate,
            "open": self.open,
            "close": self.close,
            "close_reason": self.close_reason,
            "dormant": self.dormant,
            "replication_paused": self.replication_paused,
            "reliable": self.reliable,
            "pre_channel_flag": self.pre_channel_flag,
            "channel_index": self.channel_index,
            "channel_packed_bytes": self.channel_packed_bytes,
            "has_package_map_exports": self.has_package_map_exports,
            "has_must_be_mapped_guids": self.has_must_be_mapped_guids,
            "partial": self.partial,
            "partial_initial": self.partial_initial,
            "partial_final": self.partial_final,
            "channel_sequence_mod": self.channel_sequence_mod,
            "reliable_sequence": self.reliable_sequence,
            "channel_name": self.channel_name.as_dict() if self.channel_name else None,
            "payload_bits": self.payload_bit_count,
            "coordinates": self.coordinate_intervals(),
            "error": self.error,
            "error_bit": self.error_bit,
        }
        if include_payload:
            out["payload_hex"] = self.payload.hex()
        return out


@dataclass
class EngineBunchParse:
    bunches: list[EngineBunch]
    body_bits: int
    consumed_bits: int
    trailing_start_bit: int
    error: str | None = None
    error_bit: int | None = None

    @property
    def tiled(self) -> bool:
        return self.error is None and self.consumed_bits == self.body_bits

    def as_dict(self, include_payload: bool = False) -> dict:
        return {
            "body_bits": self.body_bits,
            "consumed_bits": self.consumed_bits,
            "trailing_interval": [self.trailing_start_bit, self.body_bits],
            "tiled": self.tiled,
            "error": self.error,
            "error_bit": self.error_bit,
            "bunches": [b.as_dict(include_payload) for b in self.bunches],
        }


def read_dfm_packed_int(reader: BitReader) -> tuple[int, list[int]]:
    """Read the DFM ``SerializeIntPacked`` byte groups.

    Each serialized byte uses bit 0 as the continuation flag and bits 1..7 as
    the next seven value bits.  This is the exact inline path at the recovered
    channel-index read.
    """

    value = 0
    raw: list[int] = []
    for group in range(DFM_MAX_PACKED_BYTES):
        encoded = reader.uint8()
        raw.append(encoded)
        value |= (encoded >> 1) << (group * 7)
        if encoded & 1 == 0:
            return value, raw
    raise BitError("DFM SerializeIntPacked exceeds 32 bits")


def _read_fstring(reader: BitReader, max_units: int) -> str:
    length = reader.int32()
    units = abs(length)
    if units > max_units:
        raise BitError(f"invalid FName FString length {length}")
    if length >= 0:
        raw = bytes(reader.uint8() for _ in range(units))
        if raw.endswith(b"\x00"):
            raw = raw[:-1]
        return raw.decode("utf-8", errors="replace")

    raw = b"".join(reader.uint16().to_bytes(2, "little") for _ in range(units))
    if raw.endswith(b"\x00\x00"):
        raw = raw[:-2]
    return raw.decode("utf-16-le", errors="replace")


def read_serialized_fname(
    reader: BitReader,
    *,
    hardcoded_max: int = HARD_CODED_FNAME_MAX,
    max_string_units: int = 4096,
) -> SerializedFName:
    """Advance over the FName format used by the recovered reader helper."""

    start = reader.position
    hardcoded = bool(reader.bit())
    if hardcoded:
        index, _ = read_dfm_packed_int(reader)
        if index > hardcoded_max:
            raise BitError(
                f"hardcoded FName index {index} exceeds {hardcoded_max}"
            )
        return SerializedFName(
            hardcoded=True,
            hardcoded_index=index,
            start_bit=start,
            end_bit=reader.position,
        )

    text = _read_fstring(reader, max_string_units)
    number = reader.int32()
    return SerializedFName(
        hardcoded=False,
        text=text,
        number=number,
        start_bit=start,
        end_bit=reader.position,
    )


def _read_one_bunch(
    reader: BitReader,
    index: int,
    *,
    max_packet_bytes: int,
    max_channel_index: int | None,
    pre_flag: bool = True,
    partial_first: bool = False,
    read_fname: bool = True,
) -> EngineBunch:
    b = EngineBunch(index=index, header_start_bit=reader.position)
    try:
        b.control_gate = reader.bit()
        if b.control_gate:
            b.open = reader.bit()
            b.close = reader.bit()
            if b.close:
                b.close_reason = reader.serialize_int(15)
                # The engine derives bDormant from EChannelCloseReason == 1.
                b.dormant = int(b.close_reason == 1)

        b.replication_paused = reader.bit()
        b.reliable = reader.bit()
        if pre_flag:
            b.pre_channel_flag = reader.bit()
        else:
            b.pre_channel_flag = 0

        b.channel_index, b.channel_packed_bytes = read_dfm_packed_int(reader)
        if max_channel_index is not None and b.channel_index >= max_channel_index:
            raise BitError(
                f"channel index {b.channel_index} >= limit {max_channel_index}"
            )

        if partial_first:
            b.partial = reader.bit()
            b.has_package_map_exports = reader.bit()
            b.has_must_be_mapped_guids = reader.bit()
        else:
            b.has_package_map_exports = reader.bit()
            b.has_must_be_mapped_guids = reader.bit()
            b.partial = reader.bit()

        if b.reliable:
            b.channel_sequence_mod = reader.serialize_int(RELIABLE_SEQUENCE_MAX)
        # Sequence precedes the partial boundary bits in the captured header.
        if b.partial:
            b.partial_initial = reader.bit()
            b.partial_final = reader.bit()
        if read_fname and (b.open or b.reliable):
            b.channel_name = read_serialized_fname(reader)

        b.payload_bit_count = reader.serialize_int(max_packet_bytes << 3)
        b.header_end_bit = reader.position
        b.payload_start_bit = reader.position
        if b.payload_bit_count > reader.remaining():
            raise BitError(f"declared bunch payload {b.payload_bit_count} exceeds remaining {reader.remaining()}")
        b.payload = reader.take(b.payload_bit_count)
        b.payload_end_bit = reader.position
        return b
    except BitError as exc:
        # Keep every field that was decoded before the failure.  Older code
        # replaced this object with an empty shell, which hid the most useful
        # evidence in malformed/truncated captures.
        b.header_end_bit = max(b.header_end_bit, reader.position)
        if not b.payload_start_bit:
            b.payload_start_bit = reader.position
        b.payload_end_bit = reader.position
        b.error = str(exc)
        b.error_bit = reader.position
        raise _BunchReadFailure(b) from exc


class _BunchReadFailure(BitError):
    """Internal exception carrying the partially decoded bunch."""

    def __init__(self, bunch: EngineBunch):
        super().__init__(bunch.error or "malformed bunch")
        self.bunch = bunch


def decode_engine_bunches(
    packet_body: bytes,
    body_bits: int,
    *,
    max_packet_bytes: int = DEFAULT_MAX_PACKET_BYTES,
    max_channel_index: int | None = DEFAULT_MAX_CHANNEL_INDEX,
    max_bunches: int = 256,
    coordinate_bases: dict[str, int] | None = None,
    pre_flag: bool = True,
    partial_first: bool = False,
    read_fname: bool = True,
) -> EngineBunchParse:
    """Decode sequential bunches using the recovered engine order.

    Parsing stops at the first malformed/truncated header and retains the exact
    error bit plus the unconsumed interval.  This matters for captures where a
    packet-handler component has left data outside the bit range that reached
    ``UNetConnection::ReceivedPacket``.
    """

    if max_packet_bytes <= 0:
        raise ValueError("max_packet_bytes must be positive")
    if body_bits < 0:
        raise ValueError("body_bits must be non-negative")
    if max_channel_index is not None and max_channel_index <= 0:
        raise ValueError("max_channel_index must be positive or None")

    reader = BitReader(packet_body, body_bits)
    out: list[EngineBunch] = []
    error = None
    error_bit = None

    while reader.remaining() > 0 and len(out) < max_bunches:
        start = reader.position
        try:
            b = _read_one_bunch(
                reader,
                len(out),
                max_packet_bytes=max_packet_bytes,
                max_channel_index=max_channel_index,
                pre_flag=pre_flag,
                partial_first=partial_first,
                read_fname=read_fname,
            )
        except _BunchReadFailure as exc:
            error = str(exc)
            failed = exc.bunch
            failed.coordinate_bases = dict(coordinate_bases or {})
            error_bit = failed.error_bit
            out.append(failed)
            reader.position = start
            break
        b.coordinate_bases = dict(coordinate_bases or {})
        out.append(b)

    if reader.remaining() > 0 and len(out) >= max_bunches and error is None:
        error = f"bunch limit {max_bunches} reached"
        error_bit = reader.position

    consumed = reader.position
    return EngineBunchParse(
        bunches=out,
        body_bits=body_bits,
        consumed_bits=consumed,
        trailing_start_bit=consumed,
        error=error,
        error_bit=error_bit,
    )


def body_slice(packet_body: bytes, start: int, end: int) -> bytes:
    """Convenience helper for a reported half-open body-bit interval."""

    if start < 0 or end < start:
        raise ValueError(f"invalid interval [{start}, {end})")
    return slice_bits(packet_body, start, end - start)
