"""Small catalog-driven UE RepLayout receive-stream walker.

The implementation mirrors the recovered ``ReceiveProperties_r`` behavior
needed by the PlayerState evidence path:

* property handles are absolute DFM ``SerializeIntPacked`` values;
* a dynamic array reads ``ArrayNum`` as an unaligned little-endian ``uint16``;
* array handles are flattened across elements and terminated by handle ``0``;
* after that terminator the outer handle stream resumes.

Only property command kinds whose wire primitive is proven are decoded.  An
unknown command stops with its exact bit cursor instead of guessing a width.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .bitstream import BitError, BitReader
from .engine_bunch import read_dfm_packed_int, read_serialized_fname


CMD_DYNAMIC_ARRAY = 0x00
CMD_RETURN = 0x01
CMD_UINT16_TEXT = 0x02
CMD_BOOL = {0x03, 0x15}
CMD_FLOAT = 0x04
CMD_INT = 0x05
CMD_BYTE = 0x06
CMD_NAME = 0x07
CMD_OBJECT = 0x08
CMD_UINT32 = 0x09
CMD_VECTOR = 0x0A          # FVector: 3x float32
CMD_ROTATOR = 0x0B         # 24-bit compressed Rotator (3x 8-bit ByteComponents)
CMD_VECTOR_NETQUANTIZE = 0x0D  # UE4 packed vector (Vector_NetQuantize100/10)
CMD_NETID = 0x0E           # UniqueNetIdRepl: DFM packed count + bytes
CMD_REPMOVEMENT = 0x0F     # FRepMovement NetSerialize
CMD_STRING = 0x13
CMD_UINT64 = 0x14


class UnsupportedRepCommand(BitError):
    pass


@dataclass(frozen=True)
class RepParent:
    index: int
    name: str
    property_type: str
    rep_index: int | None
    condition: int | None


@dataclass(frozen=True)
class RepCommand:
    index: int
    descriptor: int
    relative_handle: int
    element_size: int
    object_offset: int
    handle: int
    parent_index: int
    type_flags: int

    @property
    def command_type(self) -> int:
        return self.type_flags & 0xFF


@dataclass
class RepFieldEvent:
    scope: str
    handle: int
    handle_interval: list[int]
    value_interval: list[int]
    command_index: int
    command_type: int
    parent_index: int
    parent_name: str
    property_type: str
    field_name: str
    value: object
    element_index: int | None = None
    relative_handle: int | None = None
    raw_handle_groups: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scope": self.scope,
            "handle": self.handle,
            "raw_handle_groups": self.raw_handle_groups,
            "handle_interval": self.handle_interval,
            "value_interval": self.value_interval,
            "command_index": self.command_index,
            "command_type": self.command_type,
            "parent_index": self.parent_index,
            "parent_name": self.parent_name,
            "property_type": self.property_type,
            "field_name": self.field_name,
            "element_index": self.element_index,
            "relative_handle": self.relative_handle,
            "value": self.value,
        }


@dataclass
class RepLayoutDecode:
    start_bit: int
    end_bit: int
    events: list[RepFieldEvent]
    stopped_after_handle: int | None = None
    terminator_interval: list[int] | None = None
    error: str | None = None
    error_bit: int | None = None

    def as_dict(self) -> dict:
        return {
            "interval": [self.start_bit, self.end_bit],
            "stopped_after_handle": self.stopped_after_handle,
            "terminator_interval": self.terminator_interval,
            "error": self.error,
            "error_bit": self.error_bit,
            "events": [event.as_dict() for event in self.events],
        }


class RepLayoutCatalog:
    def __init__(self, document: dict):
        self.document = document
        self.parents = {
            int(item["index"]): RepParent(
                index=int(item["index"]),
                name=str(item.get("name") or f"Parent_{item['index']}"),
                property_type=str(item.get("type") or "UnknownProperty"),
                rep_index=(
                    int(item["rep_index"])
                    if item.get("rep_index") is not None
                    else None
                ),
                condition=(
                    int(item["condition"])
                    if item.get("condition") is not None
                    else None
                ),
            )
            for item in document.get("parents", [])
        }
        self.commands = {
            int(item["index"]): RepCommand(
                index=int(item["index"]),
                descriptor=int(item.get("descriptor", 0)),
                relative_handle=int(item.get("relative_handle", 0)),
                element_size=int(item.get("element_size", 0)),
                object_offset=int(item.get("object_offset", 0)),
                handle=int(item.get("handle", 0)),
                parent_index=int(item.get("parent_index", 0)),
                type_flags=int(item.get("type_flags", 0)),
            )
            for item in document.get("commands", document.get("cmds", []))
        }
        self.inner_fields: dict[str, dict[int, str]] = {
            parent: {int(offset, 0): name for offset, name in fields.items()}
            for parent, fields in document.get("inner_fields", {}).items()
        }

        nested_indices: set[int] = set()
        for command in self.commands.values():
            if (
                command.command_type == CMD_DYNAMIC_ARRAY
                and command.relative_handle > command.index
            ):
                nested_indices.update(
                    range(command.index + 1, command.relative_handle)
                )
        self.top_commands = {
            command.handle: command
            for command in self.commands.values()
            if command.index not in nested_indices
            and command.handle
            and command.command_type != CMD_RETURN
        }

    def parent_for(self, command: RepCommand) -> RepParent:
        return self.parents.get(
            command.parent_index,
            RepParent(
                command.parent_index,
                f"Parent_{command.parent_index}",
                "UnknownProperty",
                None,
                None,
            ),
        )

    def inner_commands(self, array_command: RepCommand) -> list[RepCommand]:
        return [
            self.commands[index]
            for index in range(array_command.index + 1, array_command.relative_handle)
            if index in self.commands
            and self.commands[index].command_type != CMD_RETURN
            and self.commands[index].handle
        ]

    def inner_field_name(self, parent_name: str, command: RepCommand) -> str:
        return self.inner_fields.get(parent_name, {}).get(
            command.object_offset,
            f"{parent_name}.InnerHandle{command.handle}",
        )


class RepLayoutStreamDecoder:
    def __init__(self, catalog: RepLayoutCatalog):
        self.catalog = catalog

    @staticmethod
    def _read_packed_vector(reader: BitReader, *, scale: float, maxbits: int,
                            variant: str) -> dict:
        """UE4/DFM packed vector (FVector_NetQuantize family).

        Header hb = ceil_log2(maxbits) bits; per-component width w and bias
        differ by variant (DFM: w=h+1 bias=2^h; UE4: w=h+2 bias=2^(h+1)).
        """
        hb = (maxbits - 1).bit_length()
        h = reader.bits(hb)
        if variant == "dfm":
            w = h + 1
            bias = 1 << h
        else:
            w = h + 2
            bias = 1 << (h + 1)
        raw = [reader.bits(w) for _ in range(3)]
        ints = [r - bias for r in raw]
        return {"h": h, "w": w, "raw": raw, "ints": ints,
                "dec": [v / scale for v in ints]}

    @staticmethod
    def _read_frep_movement(reader: BitReader) -> dict:
        """FRepMovement NetSerialize (DFM variant, omits AngularVelocity)."""
        flags = reader.bits(2)
        location = RepLayoutStreamDecoder._read_packed_vector(
            reader, scale=1.0, maxbits=24, variant="dfm")
        rot_bytes = [reader.uint8() for _ in range(3)]
        rotation = [b * 360.0 / 256.0 for b in rot_bytes]
        velocity = RepLayoutStreamDecoder._read_packed_vector(
            reader, scale=1.0, maxbits=24, variant="dfm")
        angular = None
        if flags & 2:
            angular = RepLayoutStreamDecoder._read_packed_vector(
                reader, scale=1.0, maxbits=24, variant="dfm")
        return {
            "flags": flags,
            "location": location["dec"],
            "rotation": rotation,
            "linear_velocity": velocity["dec"],
            "angular_velocity": angular["dec"] if angular else None,
        }

    @staticmethod
    def _read_simple(reader: BitReader, command: RepCommand) -> object:
        kind = command.command_type
        if kind in CMD_BOOL:
            return bool(reader.bit())
        if kind == CMD_FLOAT:
            return reader.float32()
        if kind == CMD_INT:
            return reader.int32()
        if kind == CMD_BYTE:
            return reader.uint8()
        if kind == CMD_NAME:
            return read_serialized_fname(reader).as_dict()
        if kind == CMD_OBJECT:
            value, groups = read_dfm_packed_int(reader)
            return {"network_guid": value, "raw_groups": groups}
        if kind == CMD_UINT32:
            return reader.uint32()
        if kind == CMD_STRING:
            return reader.fstring()
        if kind == CMD_UINT64:
            return reader.uint64()
        if kind == CMD_UINT16_TEXT:
            return reader.uint16()
        if kind == CMD_VECTOR:
            return [reader.float32() for _ in range(3)]
        if kind == CMD_ROTATOR:
            return [b * 360.0 / 256.0 for b in (reader.uint8() for _ in range(3))]
        if kind == CMD_VECTOR_NETQUANTIZE:
            # Vector_NetQuantize100: scale=100, maxbits=30, UE4 variant
            return RepLayoutStreamDecoder._read_packed_vector(
                reader, scale=100.0, maxbits=30, variant="ue4")
        if kind == CMD_NETID:
            count, _ = read_dfm_packed_int(reader)
            raw = [reader.uint8() for _ in range(count)] if count <= 64 else None
            return {"count": count, "bytes": bytes(raw) if raw else None}
        if kind == CMD_REPMOVEMENT:
            return RepLayoutStreamDecoder._read_frep_movement(reader)
        raise UnsupportedRepCommand(
            f"unsupported RepLayout command type {kind:#x} at cmd {command.index}"
        )

    def _event(
        self,
        *,
        scope: str,
        handle: int,
        handle_interval: list[int],
        raw_groups: list[int],
        value_interval: list[int],
        command: RepCommand,
        value: object,
        field_name: str | None = None,
        element_index: int | None = None,
        relative_handle: int | None = None,
    ) -> RepFieldEvent:
        parent = self.catalog.parent_for(command)
        return RepFieldEvent(
            scope=scope,
            handle=handle,
            handle_interval=handle_interval,
            value_interval=value_interval,
            command_index=command.index,
            command_type=command.command_type,
            parent_index=parent.index,
            parent_name=parent.name,
            property_type=parent.property_type,
            field_name=field_name or parent.name,
            value=value,
            element_index=element_index,
            relative_handle=relative_handle,
            raw_handle_groups=raw_groups,
        )

    def _decode_array(
        self,
        reader: BitReader,
        command: RepCommand,
        *,
        outer_handle: int,
        outer_handle_interval: list[int],
        outer_raw_groups: list[int],
    ) -> list[RepFieldEvent]:
        parent = self.catalog.parent_for(command)
        value_start = reader.position
        array_num = reader.uint16()
        inner = self.catalog.inner_commands(command)
        if not inner:
            raise UnsupportedRepCommand(
                f"dynamic array cmd {command.index} has no inner command schema"
            )
        by_relative = {item.handle: item for item in inner}
        handle_span = max(by_relative)
        inner_events: list[RepFieldEvent] = []
        terminator_interval = None

        while True:
            handle_start = reader.position
            flat_handle, raw_groups = read_dfm_packed_int(reader)
            handle_interval = [handle_start, reader.position]
            if flat_handle == 0:
                terminator_interval = handle_interval
                break
            element_index = (flat_handle - 1) // handle_span
            relative_handle = ((flat_handle - 1) % handle_span) + 1
            if element_index >= array_num:
                raise BitError(
                    f"array handle {flat_handle} maps to element {element_index} "
                    f"outside ArrayNum {array_num}"
                )
            inner_command = by_relative.get(relative_handle)
            if inner_command is None:
                raise UnsupportedRepCommand(
                    f"array handle {flat_handle} maps to missing relative handle "
                    f"{relative_handle}"
                )
            if inner_command.command_type == CMD_DYNAMIC_ARRAY:
                raise UnsupportedRepCommand("nested dynamic arrays are not yet decoded")
            inner_value_start = reader.position
            value = self._read_simple(reader, inner_command)
            inner_events.append(
                self._event(
                    scope=f"{parent.name}[{element_index}]",
                    handle=flat_handle,
                    handle_interval=handle_interval,
                    raw_groups=raw_groups,
                    value_interval=[inner_value_start, reader.position],
                    command=inner_command,
                    value=value,
                    field_name=self.catalog.inner_field_name(parent.name, inner_command),
                    element_index=element_index,
                    relative_handle=relative_handle,
                )
            )

        value = {
            "array_num": array_num,
            "inner_handle_span": handle_span,
            "changed_fields": len(inner_events),
            "inner_terminator_interval": terminator_interval,
        }
        array_event = self._event(
            scope="top",
            handle=outer_handle,
            handle_interval=outer_handle_interval,
            raw_groups=outer_raw_groups,
            value_interval=[value_start, reader.position],
            command=command,
            value=value,
        )
        return [array_event, *inner_events]

    def decode(
        self,
        payload: bytes,
        payload_bits: int,
        start_bit: int,
        *,
        stop_after_handles: Iterable[int] = (),
    ) -> RepLayoutDecode:
        reader = BitReader(payload, payload_bits)
        reader.position = start_bit
        events: list[RepFieldEvent] = []
        stop_set = set(stop_after_handles)
        stopped = None
        terminator = None
        error = None
        error_bit = None
        try:
            while reader.remaining() > 0:
                handle_start = reader.position
                handle, raw_groups = read_dfm_packed_int(reader)
                handle_interval = [handle_start, reader.position]
                if handle == 0:
                    terminator = handle_interval
                    break
                command = self.catalog.top_commands.get(handle)
                if command is None:
                    raise UnsupportedRepCommand(
                        f"top-level handle {handle} is absent from compact catalog"
                    )
                if command.command_type == CMD_DYNAMIC_ARRAY:
                    decoded = self._decode_array(
                        reader,
                        command,
                        outer_handle=handle,
                        outer_handle_interval=handle_interval,
                        outer_raw_groups=raw_groups,
                    )
                    events.extend(decoded)
                else:
                    value_start = reader.position
                    value = self._read_simple(reader, command)
                    events.append(
                        self._event(
                            scope="top",
                            handle=handle,
                            handle_interval=handle_interval,
                            raw_groups=raw_groups,
                            value_interval=[value_start, reader.position],
                            command=command,
                            value=value,
                        )
                    )
                if handle in stop_set:
                    stopped = handle
                    break
        except BitError as exc:
            error = str(exc)
            error_bit = reader.position
        return RepLayoutDecode(
            start_bit=start_bit,
            end_bit=reader.position,
            events=events,
            stopped_after_handle=stopped,
            terminator_interval=terminator,
            error=error,
            error_bit=error_bit,
        )


def find_playerstate_stream_start(
    payload: bytes,
    payload_bits: int,
) -> int | None:
    """Zero-scan property-stream start via the open-bunch object description.

    Returns the bit offset (raw payload space) of the first RepLayout handle,
    or ``None`` when the prefix cannot be parsed.  This replaces the historical
    handle-27 bit-scan with the game's own sub-object-list walk.
    """
    from .object_desc import parse_object_description

    desc = parse_object_description(payload, payload_bits)
    if desc is None:
        return None
    return int(desc["stream_start_bit"])


def find_playerstate_stream_anchor(
    payload: bytes,
    payload_bits: int,
) -> list[dict]:
    """Find initial PlayerState streams with present or omitted handle21."""

    anchors = []
    for name_handle_start in range(max(payload_bits - 240, 0)):
        reader = BitReader(payload, payload_bits)
        reader.position = name_handle_start
        try:
            handle, _, name_handle_interval = _read_handle(reader)
            if handle != 27:
                continue
            name_start = reader.position
            name = reader.fstring()
            name_interval = [name_start, reader.position]
            handle28, _, _ = _read_handle(reader)
            if handle28 != 28:
                continue
            uin = reader.uint64()
            handle30, _, _ = _read_handle(reader)
            if handle30 != 30:
                continue
            inner_team_index = reader.int32()
            handle41, _, _ = _read_handle(reader)
            if handle41 != 41 or not name:
                continue
        except (BitError, ValueError, UnicodeError):
            continue

        stream_start = None
        bot_value = None
        bot_present = False
        start_time = None
        prefix_kind = None
        start_floor = max(0, name_handle_start - 256)
        for candidate in range(start_floor, name_handle_start):
            prefix = BitReader(payload, payload_bits)
            prefix.position = candidate
            try:
                h21, _, _ = _read_handle(prefix)
                if h21 != 21:
                    continue
                candidate_bot = bool(prefix.bit())
                h25, _, _ = _read_handle(prefix)
                if h25 != 25:
                    continue
                start_time = prefix.int32()
                h27, _, _ = _read_handle(prefix)
                if h27 == 27 and prefix.position == name_interval[0]:
                    stream_start = candidate
                    bot_value = candidate_bot
                    bot_present = True
                    prefix_kind = "handle21_25_27"
                    break
            except BitError:
                continue
        if stream_start is None:
            for candidate in range(start_floor, name_handle_start):
                prefix = BitReader(payload, payload_bits)
                prefix.position = candidate
                try:
                    h25, _, _ = _read_handle(prefix)
                    if h25 != 25:
                        continue
                    candidate_start_time = prefix.int32()
                    h27, _, _ = _read_handle(prefix)
                    if h27 == 27 and prefix.position == name_interval[0]:
                        stream_start = candidate
                        start_time = candidate_start_time
                        prefix_kind = "handle25_27"
                        break
                except BitError:
                    continue
        if stream_start is None:
            stream_start = name_handle_start
            prefix_kind = "handle27_suffix"
        anchors.append(
            {
                "stream_start_bit": stream_start,
                "player_name_handle_interval": name_handle_interval,
                "player_name_interval": name_interval,
                "player_name": name,
                "uin": uin,
                "inner_team_index": inner_team_index,
                "b_is_a_bot": bot_value,
                "b_is_a_bot_present": bot_present,
                "prefix_kind": prefix_kind,
                "member_info_handle_end_bit": reader.position,
                "start_time": start_time,
            }
        )
    return anchors


def _read_handle(reader: BitReader) -> tuple[int, list[int], list[int]]:
    start = reader.position
    value, raw = read_dfm_packed_int(reader)
    return value, raw, [start, reader.position]
