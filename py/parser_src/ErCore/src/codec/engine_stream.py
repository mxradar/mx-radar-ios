"""Stateful reliable-sequence tracking and bit-tight partial reassembly."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Hashable

from .bitstream import BitWriter
from .engine_bunch import EngineBunch, RELIABLE_SEQUENCE_MAX


class ReliableSequenceUnwrapper:
    """Unwrap per-channel 10-bit reliable sequences into nearby integers."""

    def __init__(self, modulus: int = RELIABLE_SEQUENCE_MAX):
        if modulus <= 1:
            raise ValueError("modulus must be greater than one")
        self.modulus = modulus
        self._last: dict[tuple[Hashable | None, int], int] = {}

    def unwrap(
        self,
        channel_index: int,
        sequence_mod: int,
        *,
        stream_key: Hashable | None = None,
    ) -> int:
        if not 0 <= sequence_mod < self.modulus:
            raise ValueError(
                f"sequence {sequence_mod} outside [0, {self.modulus})"
            )
        key = (stream_key, channel_index)
        last = self._last.get(key)
        if last is None:
            absolute = sequence_mod
        else:
            base = last - (last % self.modulus)
            absolute = base + sequence_mod
            half = self.modulus // 2
            if absolute - last > half:
                absolute -= self.modulus
            elif last - absolute >= half:
                absolute += self.modulus
        if last is None or absolute > last:
            self._last[key] = absolute
        return absolute

    def apply(
        self,
        bunch: EngineBunch,
        *,
        stream_key: Hashable | None = None,
    ) -> int | None:
        if not bunch.reliable or bunch.channel_sequence_mod is None:
            bunch.reliable_sequence = None
            return None
        bunch.reliable_sequence = self.unwrap(
            bunch.channel_index,
            bunch.channel_sequence_mod,
            stream_key=stream_key,
        )
        return bunch.reliable_sequence


@dataclass
class ReassembledBunch:
    channel_index: int
    payload: bytes
    payload_bit_count: int
    reliable: bool
    was_partial: bool
    fragments: list[dict] = field(default_factory=list)
    first_reliable_sequence: int | None = None
    last_reliable_sequence: int | None = None
    open: int = 0
    close: int = 0
    close_reason: int | None = None
    dormant: int = 0

    def as_dict(self, include_payload: bool = False) -> dict:
        out = {
            "channel_index": self.channel_index,
            "payload_bits": self.payload_bit_count,
            "reliable": self.reliable,
            "was_partial": self.was_partial,
            "first_reliable_sequence": self.first_reliable_sequence,
            "last_reliable_sequence": self.last_reliable_sequence,
            "open": self.open,
            "close": self.close,
            "close_reason": self.close_reason,
            "dormant": self.dormant,
            "fragments": self.fragments,
        }
        if include_payload:
            out["payload_hex"] = self.payload.hex()
        return out

    def map_payload_interval(self, start: int, end: int) -> list[dict]:
        """Map a combined payload interval back to all source fragments."""

        if start < 0 or end < start or end > self.payload_bit_count:
            raise ValueError(f"invalid combined interval [{start}, {end})")
        mapped = []
        for fragment in self.fragments:
            fstart, fend = fragment["combined_payload_interval"]
            left, right = max(start, fstart), min(end, fend)
            if left >= right:
                continue
            item = dict(fragment)
            item["overlap_combined_interval"] = [left, right]
            item["fragment_local_interval"] = [left - fstart, right - fstart]
            mapped.append(item)
        return mapped


@dataclass
class _PartialAssembly:
    channel_index: int
    reliable: bool
    writer: BitWriter = field(default_factory=BitWriter)
    fragments: list[dict] = field(default_factory=list)
    first_reliable_sequence: int | None = None
    last_reliable_sequence: int | None = None
    last_sequence_mod: int | None = None
    open: int = 0
    close: int = 0
    close_reason: int | None = None
    dormant: int = 0


class PartialBunchReassembler:
    """Reassemble UE partial bunch chains without byte-aligning fragments."""

    def __init__(self, sequence_modulus: int = RELIABLE_SEQUENCE_MAX):
        self.sequence_modulus = sequence_modulus
        self._pending: dict[tuple[Hashable | None, int], _PartialAssembly] = {}
        self.diagnostics: list[dict] = []

    @staticmethod
    def _source_dict(bunch: EngineBunch, source: dict | None) -> dict:
        item = dict(source or {})
        item.update(
            {
                "bunch_index": bunch.index,
                "channel_index": bunch.channel_index,
                "payload_bits": bunch.payload_bit_count,
                "channel_sequence_mod": bunch.channel_sequence_mod,
                "reliable_sequence": bunch.reliable_sequence,
                "has_package_map_exports": bunch.has_package_map_exports,
                "has_must_be_mapped_guids": bunch.has_must_be_mapped_guids,
                "coordinates": bunch.coordinate_intervals(),
            }
        )
        return item

    def _append(
        self,
        assembly: _PartialAssembly,
        bunch: EngineBunch,
        source: dict | None,
    ) -> None:
        start = assembly.writer.bit_count
        assembly.writer.append(bunch.payload, bunch.payload_bit_count)
        end = assembly.writer.bit_count
        item = self._source_dict(bunch, source)
        item["combined_payload_interval"] = [start, end]
        assembly.fragments.append(item)
        if assembly.first_reliable_sequence is None:
            assembly.first_reliable_sequence = bunch.reliable_sequence
        assembly.last_reliable_sequence = bunch.reliable_sequence
        assembly.last_sequence_mod = bunch.channel_sequence_mod
        assembly.close = bunch.close
        assembly.close_reason = bunch.close_reason
        assembly.dormant = bunch.dormant

    @staticmethod
    def _finish(assembly: _PartialAssembly, *, was_partial: bool) -> ReassembledBunch:
        return ReassembledBunch(
            channel_index=assembly.channel_index,
            payload=assembly.writer.bytes(),
            payload_bit_count=assembly.writer.bit_count,
            reliable=assembly.reliable,
            was_partial=was_partial,
            fragments=assembly.fragments,
            first_reliable_sequence=assembly.first_reliable_sequence,
            last_reliable_sequence=assembly.last_reliable_sequence,
            open=assembly.open,
            close=assembly.close,
            close_reason=assembly.close_reason,
            dormant=assembly.dormant,
        )

    def _drop(self, key: tuple[Hashable | None, int], reason: str) -> None:
        old = self._pending.pop(key, None)
        self.diagnostics.append(
            {
                "kind": "partial_drop",
                "channel_index": key[1],
                "reason": reason,
                "fragments": len(old.fragments) if old else 0,
                "payload_bits": old.writer.bit_count if old else 0,
            }
        )

    def feed(
        self,
        bunch: EngineBunch,
        *,
        source: dict | None = None,
        stream_key: Hashable | None = None,
    ) -> list[ReassembledBunch]:
        """Consume one bunch and return zero or one completed logical bunch."""

        key = (stream_key, bunch.channel_index)
        if not bunch.partial:
            if key in self._pending:
                self._drop(key, "complete bunch interrupted pending partial chain")
            assembly = _PartialAssembly(
                channel_index=bunch.channel_index,
                reliable=bool(bunch.reliable),
                open=bunch.open,
            )
            self._append(assembly, bunch, source)
            return [self._finish(assembly, was_partial=False)]

        if bunch.partial_initial:
            if key in self._pending:
                self._drop(key, "new initial fragment replaced pending chain")
            assembly = _PartialAssembly(
                channel_index=bunch.channel_index,
                reliable=bool(bunch.reliable),
                open=bunch.open,
            )
            self._pending[key] = assembly
        else:
            assembly = self._pending.get(key)
            if assembly is None:
                self.diagnostics.append(
                    {
                        "kind": "orphan_partial",
                        "channel_index": bunch.channel_index,
                        "partial_final": bunch.partial_final,
                        "channel_sequence_mod": bunch.channel_sequence_mod,
                    }
                )
                return []

        if bool(bunch.reliable) != assembly.reliable:
            self._drop(key, "reliability changed within partial chain")
            return []
        if assembly.reliable and assembly.fragments:
            if (
                bunch.reliable_sequence is not None
                and assembly.last_reliable_sequence is not None
            ):
                expected = assembly.last_reliable_sequence + 1
                if bunch.reliable_sequence != expected:
                    self._drop(
                        key,
                        f"reliable sequence gap: expected {expected}, got "
                        f"{bunch.reliable_sequence}",
                    )
                    return []
            elif (
                bunch.channel_sequence_mod is not None
                and assembly.last_sequence_mod is not None
            ):
                expected_mod = (assembly.last_sequence_mod + 1) % self.sequence_modulus
                if bunch.channel_sequence_mod != expected_mod:
                    self._drop(
                        key,
                        f"reliable sequence gap: expected mod {expected_mod}, got "
                        f"{bunch.channel_sequence_mod}",
                    )
                    return []

        self._append(assembly, bunch, source)
        if bunch.partial_final:
            del self._pending[key]
            return [self._finish(assembly, was_partial=True)]
        return []

    def pending(self) -> list[dict]:
        return [
            {
                "stream_key": repr(key[0]),
                "channel_index": key[1],
                "fragments": len(value.fragments),
                "payload_bits": value.writer.bit_count,
                "last_reliable_sequence": value.last_reliable_sequence,
            }
            for key, value in self._pending.items()
        ]
