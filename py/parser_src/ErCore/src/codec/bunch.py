"""Bunch layer: UE replication channel messages inside a packet body.

Two candidate layouts are implemented and scoreable against real traffic:

- "dfm": serialized `control` bit first, then flags (per the previous
  session's protocol notes in battle_codec_capture_simplified.rs).
- "ue4": standard UE4 FInBunch::ReadHeader order (control derived from
  channel index >= 8; flags only present when open or close).

Both are driven by the same BitReader, so the analyzer can score them
on the same frame and pick the winner.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from .bitstream import BitReader, BitError

BUNCH_LAYOUTS = ("dfm2", "dfm", "ue4")

CHANNELS_MAX = 10_240          # UE CHANNELS
PAYLOAD_BITS_MAX = 131_074     # MAX_BUNCH_SIZE_BITS + slack

# ---- verified against real DFM battle capture (run_20260816_014832) -------
# Packet body = [bunch]* [fixed trailer block]? [ping trailer 10 bits]
# Bunch header (24 bits, LSB-first):
#   b0..b3   flags (semantics provisional; observed values vary per packet)
#   b4..b15  channel index (12-bit ReadInt)
#   b16      NOT a payload-presence gate: movement bunches carry b16=0
#            with count+payload following. Semantics TBD.
#   b17..b23 extra flags (TBD; 0 in movement traffic)
# Then payload bit count (14-bit ReadInt over [0,16384)) then payload bits.
# The count+payload are read unconditionally (verified against live
# ch2048/2049 movement traffic).
# Verified: request seq +1 for 3985/3985 single-bunch packets, channel
# distribution sane (client 0/1/2 + 2048..2079, server 2048/3072).
DFM2_HDR_BITS = 24
DFM2_COUNT_MAX = 1 << 14


@dataclass
class Bunch:
    control: int = 0
    open: int = 0
    close: int = 0
    dormant: int = 0
    paused: int = 0
    reliable: int = 0
    channel_index: int = 0
    has_package_map_exports: int = 0
    has_must_be_mapped_guids: int = 0
    partial: int = 0
    partial_initial: int = 1
    partial_final: int = 1
    channel_sequence: int | None = None
    name_number: int | None = None
    name_index: int | None = None
    channel_name_string: str | None = None  # resolved via exports later
    payload: bytes = b""
    payload_bit_count: int = 0
    error: str | None = None
    extra: int = 0  # dfm2: b17..b23 of the header

    def as_dict(self) -> dict:
        return {
            "control": self.control,
            "open": self.open,
            "close": self.close,
            "dormant": self.dormant,
            "paused": self.paused,
            "reliable": self.reliable,
            "channel_index": self.channel_index,
            "has_package_map_exports": self.has_package_map_exports,
            "has_must_be_mapped_guids": self.has_must_be_mapped_guids,
            "partial": self.partial,
            "partial_initial": self.partial_initial,
            "partial_final": self.partial_final,
            "channel_sequence": self.channel_sequence,
            "name_number": self.name_number,
            "name_index": self.name_index,
            "channel_name_string": self.channel_name_string,
            "payload_bits": self.payload_bit_count,
            "error": self.error,
        }


def _read_fname(r: BitReader, packed_variant: str) -> tuple[int | None, int | None]:
    """UE FName net serialization: packed int32 number, packed int32 index."""
    return r.packed_int(packed_variant), r.packed_int(packed_variant)


def _read_rest(reader: BitReader, b: Bunch, packed_variant: str) -> None:
    """Read the post-flags part of a bunch header + payload.
    Sets b.error instead of raising, leaving reader position undefined on error."""
    try:
        b.reliable = reader.bit()
        b.channel_index = reader.serialize_int(CHANNELS_MAX)
        b.has_package_map_exports = reader.bit()
        b.has_must_be_mapped_guids = reader.bit()
        b.partial = reader.bit()
        if b.reliable:
            b.channel_sequence = reader.packed_int(packed_variant)
        if b.partial:
            b.partial_initial = reader.bit()
            b.partial_final = reader.bit()
        if b.reliable or b.open:
            b.name_number, b.name_index = _read_fname(reader, packed_variant)
        b.payload_bit_count = reader.serialize_int(PAYLOAD_BITS_MAX)
        b.payload = reader.take(b.payload_bit_count)
    except BitError as exc:
        b.error = str(exc)


def _read_rest_ue4(reader: BitReader, b: Bunch, packed_variant: str) -> None:
    """UE4-ordered rest (used for open/close bunches and update fallback)."""
    try:
        b.reliable = reader.bit()
        b.partial = reader.bit()
        if b.reliable:
            b.channel_sequence = reader.packed_int(packed_variant)
        if b.partial:
            b.partial_initial = reader.bit()
            b.partial_final = reader.bit()
        if b.reliable:
            b.has_package_map_exports = reader.bit()
            b.has_must_be_mapped_guids = reader.bit()
        ch = reader.serialize_int(CHANNELS_MAX)
        b.control = 1 if ch >= 8 else 0
        if b.control:
            ch -= 8
        b.channel_index = ch
        if b.reliable or b.open:
            b.name_number, b.name_index = _read_fname(reader, packed_variant)
        b.payload_bit_count = reader.serialize_int(PAYLOAD_BITS_MAX)
        b.payload = reader.take(b.payload_bit_count)
    except BitError as exc:
        b.error = str(exc)


def _decode_dfm2(reader: BitReader) -> list[Bunch]:
    """Verified DFM layout: [hdr24][count14][payload]* + trailers."""
    bunches: list[Bunch] = []
    while reader.remaining() > 0:
        if reader.remaining() <= DFM2_HDR_BITS:
            break  # ping/fixed trailer — not a bunch
        b = Bunch()
        try:
            hdr = reader.bits(DFM2_HDR_BITS)
        except BitError as exc:
            b.error = str(exc)
            break
        b.control = hdr & 1
        b.open = (hdr >> 1) & 1
        b.close = (hdr >> 2) & 1
        b.reliable = (hdr >> 3) & 1
        b.channel_index = (hdr >> 4) & 0xFFF
        b.has_payload = (hdr >> 16) & 1
        b.extra = (hdr >> 17) & 0x7F
        # b16 is NOT a payload-presence gate: real traffic carries count+payload
        # with b16=0 as well (b16 semantics TBD — partial/reliable?).
        if reader.remaining() < 14:
            b.error = "count truncated"
            bunches.append(b)
            break
        b.payload_bit_count = reader.serialize_int(DFM2_COUNT_MAX)
        if b.payload_bit_count > reader.remaining():
            # Partial bunch: declared logical length exceeds this packet's
            # remaining bits.  Keep the fragment (all remaining bits) so
            # downstream reassembly can stitch across packets.
            frag_bits = reader.remaining()
            b.partial = 1
            b.error = f"partial_fragment declared={b.payload_bit_count} have={frag_bits}"
            b.payload = reader.take(frag_bits) if frag_bits else b""
            b.payload_bit_count = frag_bits
            bunches.append(b)
            break
        b.payload = reader.take(b.payload_bit_count)
        bunches.append(b)
    return bunches


def _decode_dfm(reader: BitReader, packed_variant: str) -> list[Bunch]:
    bunches: list[Bunch] = []
    while reader.remaining() > 0:
        b = Bunch()
        b.control = reader.bit()
        if not b.control:
            b.open = reader.bit()
            b.close = reader.bit()
            if b.open or b.close:
                b.dormant = reader.bit()
                b.paused = reader.bit()
            else:
                # update bunch (open=0 close=0) or keepalive: try full parse,
                # fall back to keepalive on error
                mark = reader.position
                _read_rest(reader, b, packed_variant)
                if b.error:
                    reader.position = mark
                    bunches.append(Bunch(control=0))
                else:
                    bunches.append(b)
                continue
        _read_rest(reader, b, packed_variant)
        bunches.append(b)
        if b.error:
            break
    return bunches


def _decode_ue4(reader: BitReader, packed_variant: str) -> list[Bunch]:
    bunches: list[Bunch] = []
    while reader.remaining() > 0:
        b = Bunch()
        b.open = reader.bit()
        b.close = reader.bit()
        if not (b.open or b.close):
            # update bunch or keepalive: try full parse, fall back on error
            mark = reader.position
            _read_rest_ue4(reader, b, packed_variant)
            if b.error:
                reader.position = mark
                bunches.append(Bunch())
            else:
                bunches.append(b)
            continue
        b.dormant = reader.bit()
        b.paused = reader.bit()
        _read_rest_ue4(reader, b, packed_variant)
        bunches.append(b)
        if b.error:
            break
    return bunches


def decode_bunches(
    packet_body: bytes,
    body_bits: int,
    layout: str = "dfm2",
    packed_variant: str = "dfm",
) -> list[Bunch]:
    if layout not in BUNCH_LAYOUTS:
        raise ValueError(f"unknown bunch layout {layout!r}")
    reader = BitReader(packet_body, body_bits)
    if layout == "dfm2":
        return _decode_dfm2(reader)
    if layout == "dfm":
        return _decode_dfm(reader, packed_variant)
    return _decode_ue4(reader, packed_variant)


def score_bunches(bunches: list[Bunch]) -> float:
    """Score a bunch parse: how plausible is this layout for this frame?"""
    if not bunches:
        return -100.0
    score = 0.0
    n = 0
    for b in bunches:
        if b.error:
            score -= 5.0
            continue
        n += 1
        if b.channel_index < 64:
            score += 1.0
        elif b.channel_index < 1024:
            score += 0.5
        elif b.channel_index < 4096:
            score += 0.5
        else:
            score -= 1.0
        if b.reliable or b.open:
            # name should look sane: small-ish packed indices
            if b.name_number is not None and b.name_number < 1000:
                score += 0.25
            if b.name_index is not None and b.name_index < 2_000_000:
                score += 0.25
            else:
                score -= 1.0
        if b.payload_bit_count == 0 and not (b.open or b.close):
            score -= 0.5
        if b.partial and not (b.partial_initial or b.partial_final):
            score -= 1.0
    score += min(n, 8) * 0.5  # prefer frames with several clean bunches
    return score
