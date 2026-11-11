"""Open-bunch object-description parser (zero-scan prefix locator).

The wire stores the sub-object list that precedes the RepLayout property stream
with a 1-bit left shift (``out[i] = (in[i] << 1) | (in[i-1] >> 7)``).  We first
un-rotate, then walk the recursive object-reference structure.

Empirically recovered layout (un-rotated, LSB-first bit reader)::

    num_subobjects : uint32
    main ref       : guid_a + guid_b + flags (3 packed ints)
                     flags & 3 == 2 -> class_path FString + fname_number uint32 + class_name FString
                     flags & 3 == 3 -> class_name FString
    per subobject  : guid_a..guid_e + flags (6 packed ints)
                     flags & 3 == 3 -> component FString
                     flags & 3 == 1 -> nested component = object-ref list (0-terminated)

The object-ref list entries are ``guid_a + guid_b + flags``; an entry is an
inline export only when ``flags >> 2 == 0`` (low two bits 2/3), otherwise it is
a mapped reference with no trailing fields.
"""
from __future__ import annotations

from .bitstream import BitError, BitReader
from .engine_bunch import read_dfm_packed_int, read_serialized_fname


def unrotate_bytes(data: bytes) -> bytes:
    """Bit-stream shift right by one (drop bit 0)."""
    out = bytearray(len(data))
    for i in range(len(data)):
        cur = data[i]
        nxt = data[i + 1] if i + 1 < len(data) else 0
        out[i] = ((cur >> 1) | ((nxt & 1) << 7)) & 0xFF
    return bytes(out)


def _read_packed(reader: BitReader) -> int:
    # GUID parts may carry full 64-bit NetGUIDs: DFM packed ints up to
    # 10 groups (70 bits).  The default 5-group (35-bit) limit is too small
    # and silently aborted the sub-object list for vehicle/equipment components
    # (e.g. GPVehicleData), leaving stream_start non-deterministic.
    value = 0
    for group in range(10):
        encoded = reader.uint8()
        value |= (encoded >> 1) << (group * 7)
        if encoded & 1 == 0:
            return value
    raise BitError("DFM packed int exceeds 70 bits (64-bit GUID)")


def _read_inline(reader: BitReader, kind: int) -> bool:
    """Read inline-export fields for kind 2/3; return False on failure."""
    try:
        if kind == 2:
            reader.fstring()
            reader.uint32()
            reader.fstring()
        elif kind == 3:
            reader.fstring()
    except BitError:
        return False
    return True


def _read_object_ref(reader: BitReader) -> dict | None:
    try:
        guid_a = _read_packed(reader)
        guid_b = _read_packed(reader)
        flags = _read_packed(reader)
    except BitError:
        return None
    kind = flags & 3
    high = flags >> 2
    info = {"guid_a": guid_a, "guid_b": guid_b, "flags": flags, "kind": kind}
    if high == 0 and kind in (2, 3):
        if not _read_inline(reader, kind):
            return None
        info["inline"] = True
    return info


def _read_object_ref_list(reader: BitReader) -> list[dict] | None:
    refs: list[dict] = []
    while True:
        try:
            guid_a = _read_packed(reader)
        except BitError:
            return None
        if guid_a == 0:
            # 8/19 terminator is two zero bytes (guid_a=0 + guid_b=0); 052201
            # uses a single zero guid_a followed directly by the property
            # stream.  Peek guid_b and only consume it when it is also zero.
            pos = reader.position
            try:
                guid_b = _read_packed(reader)
            except BitError:
                reader.position = pos
                break
            if guid_b == 0:
                break
            reader.position = pos
            break
        try:
            guid_b = _read_packed(reader)
            flags = _read_packed(reader)
        except BitError:
            return None
        kind = flags & 3
        high = flags >> 2
        info = {"guid_a": guid_a, "guid_b": guid_b, "flags": flags, "kind": kind}
        if kind == 1:
            # nested reference: recurse once, then the list ends
            inner = _read_object_ref(reader)
            if inner is None:
                return None
            info["outer"] = inner
            refs.append(info)
            break
        if high == 0 and kind in (2, 3):
            if not _read_inline(reader, kind):
                return None
            info["inline"] = True
        refs.append(info)
        if kind == 0 and guid_b == 0:
            # empty mapped reference marks the end of the list (ch41 variant)
            break
    return refs


def _read_main_ref(reader: BitReader) -> dict | None:
    return _read_object_ref(reader)


# Sub-object GUIDs are variable-length packed-int runs that terminate on a
# "type id".  These differ by build: 10883/225/130051 on 052201, 9859/11395/259
# on 8/19.  The flags field follows the type id.
_SUB_TYPE_IDS = {10883, 225, 130051, 9859}


def _is_type_id(v: int) -> bool:
    """A type-id is either a known build-specific id, or any DFM ``07 XX``
    value (two bytes, low 7 bits == 3, so ``v % 128 == 3`` and ``v >= 131``).

    The ``% 128 == 3`` test is what separates a type id from other large guid
    parts (e.g. 3054 = ``dd 2e`` has ``% 128 == 110``).

    Pawn sub-objects add a second form ``05 XX`` (low 7 bits == 2, e.g. 14210
    = ``05 8f``) that the PlayerState samples don't hit; accept it too.
    """
    return v in _SUB_TYPE_IDS or (v >= 131 and v % 128 in (2, 3))


def _read_component_name(reader: BitReader) -> str | None:
    """Read a sub-object component name (printable-ASCII FString).

    Returns None when the next field is not a readable FString.
    """
    pos = reader.position
    try:
        length = reader.int32()
        if 0 < abs(length) < 4096:
            raw = bytes(reader.uint8() for _ in range(abs(length)))
            if all(b == 0 or 0x20 <= b < 0x7F for b in raw):
                return raw.rstrip(b"\x00").decode("ascii", "replace")
    except BitError:
        pass
    reader.position = pos
    return None


def _read_sub_ref(reader: BitReader) -> dict | None:
    guid_parts: list[int] = []
    type_found = False
    while len(guid_parts) < 16:
        try:
            v = _read_packed(reader)
        except BitError:
            return None
        guid_parts.append(v)
        # peek the next value
        pos = reader.position
        try:
            nxt = _read_packed(reader)
        except BitError:
            reader.position = pos
            break
        reader.position = pos
        # A type id is the last ``07 XX`` value before a *small* flags value.
        # Guid parts that are also ``07 XX`` (e.g. 10883 inside the
        # Dam_Iris_Level1 guid) are followed by another large value, so the
        # ``nxt < 128`` bound rejects them.
        if _is_type_id(v) and nxt < 128 and (nxt & 3) in (1, 2, 3):
            type_found = True
            break
        # "level" form: four consecutive zero guid parts, then a printable
        # name FString with no type id / flags (a mapped reference).
        if len(guid_parts) >= 4 and guid_parts[-4:] == [0, 0, 0, 0]:
            npos = reader.position
            name = _read_component_name(reader)
            if name is not None:
                return {
                    "guid_parts": guid_parts,
                    "flags": 0,
                    "kind": 0,
                    "level_name": name,
                }
            reader.position = npos
    if not type_found:
        return None
    try:
        flags = _read_packed(reader)
    except BitError:
        return None
    kind = flags & 3
    info = {
        "guid_parts": guid_parts,
        "flags": flags,
        "kind": kind,
    }
    if kind == 3:
        name = _read_component_name(reader)
        if name is None:
            return None
        if name.startswith("/"):
            # full export: class_path FString + fname_number + class_name FString
            try:
                fname_num = reader.uint32()
            except BitError:
                return None
            class_name = _read_component_name(reader)
            if class_name is None:
                return None
            info["class_path"] = name
            info["fname_number"] = fname_num
            info["class_name"] = class_name
        else:
            info["component"] = name
    elif kind == 2:
        # full export (same as main-ref kind 2): class_path FString +
        # fname_number uint32 + class_name FString
        try:
            class_path = reader.fstring()
            fname_num = reader.uint32()
            class_name = reader.fstring()
        except BitError:
            return None
        info["class_path"] = class_path
        info["fname_number"] = fname_num
        info["class_name"] = class_name
    elif kind == 1:
        refs = _read_object_ref_list(reader)
        if refs is None:
            return None
        info["component_refs"] = refs
        # the nested component list is followed by a fixed 3-bit tail padding
        # before the RepLayout property stream (observed across ch8/ch17).
        if reader.position + 3 <= reader.bit_count:
            reader.position += 3
        info["tail_padding_bits"] = 3
    # kind 0/2 sub-objects: no inline fields in the samples observed
    return info


def parse_object_description(payload: bytes, payload_bits: int) -> dict | None:
    """Parse the open-bunch prefix; return the property-stream start bit.

    ``stream_start_bit`` is in the original payload bit-space.
    """
    if payload_bits <= 8:
        return None
    nbytes = (payload_bits + 7) // 8
    # numExp (DFM packed) + main ref (3 DFM packed ints) are byte-aligned in the
    # RAW stream; the sub-object list (guid parts / type-id / class FString) is
    # 1-bit-left-shifted on the wire, i.e. byte-aligned only after unrotate.
    # So parse the prefix from RAW, then the sub-objects from the unrotated
    # stream at the same byte position.
    raw = BitReader(payload, payload_bits)
    try:
        num_sub = raw.packed_int("dfm")
    except BitError:
        return None
    main = _read_main_ref(raw)
    if main is None:
        return None
    raw_pos = raw.position
    unr = unrotate_bytes(payload[:nbytes])
    reader = BitReader(unr, payload_bits - 1)
    reader.position = raw_pos
    sub_objects: list[dict] = []
    for _ in range(num_sub):
        try:
            sub = _read_sub_ref(reader)
        except BitError:
            return None
        if sub is None:
            return None
        sub_objects.append(sub)
    return {
        "num_subobjects": num_sub,
        "main": main,
        "sub_objects": sub_objects,
        "stream_start_bit": reader.position + 1,
        "unrotated_end_bit": reader.position,
    }
