"""PropertyReplication health batches, confirmed with the 0904 runtime.

Container: dense bit, packed count, presence set, length-delimited items.
Item handles: 1..6 float health fields, 7 Object GUID, 0 end. No checksum bit
inside an item. Object resolves through AttributeCenter's exported Outer;
the container index is NOT a movement slot (self 91 vs MoveHandle 27 in 109677).
"""
from collections import Counter
import math
import numpy as np

import sna
from blocks import tile
from replication_header import exports_prefix
from codec.bitstream import BitReader, BitError
from codec.engine_bunch import read_dfm_packed_int

FIELDS = ("Health", "ShieldHealth", "ArmorShieldHealth", "ArmorHealth",
          "HelmetArmorHealth", "IsAlive", "Object")


def _packed(r):
    return read_dfm_packed_int(r)[0]


def decode_health_block(payload, block):
    """Return complete container deltas or raise; never commit a partial block."""
    r = BitReader(payload, block["payload_end"])
    r.position = block["payload_start"]
    # Runtime/captures use unchecked replication. Checked property serialization
    # includes additional per-property markers and has not been verified here.
    if r.bit():
        raise BitError("checked health block unsupported")
    out = []
    previous = 0
    while True:
        h = _packed(r)
        if h == 0:
            if r.remaining():
                raise BitError("health block has leftover bits")
            return out
        if not previous < h <= 4:
            raise BitError("health component handle order/range")
        previous = h
        if h in (1, 2):
            r.bit()  # bReplicates / bIsActive
            continue
        dense, count = r.bit(), _packed(r)
        if count > 65535:
            raise BitError("health container count exceeds u16 index")
        indices = []
        if dense:
            r.ensure(count)
            indices = [i for i in range(count) if r.bit()]
        else:
            while True:
                index = r.uint16()
                if index == 0:
                    break
                if index > count or (indices and index - 1 <= indices[-1]):
                    raise BitError("health presence index order/range")
                indices.append(index - 1)
        items = []
        for index in indices:
            size = _packed(r)
            r.ensure(size)
            end = r.position + size
            inner = BitReader(payload, end)
            inner.position = r.position
            values = {}
            last = 0
            while True:
                field = _packed(inner)
                if field == 0:
                    break
                if not last < field <= len(FIELDS):
                    raise BitError("health item handle order/range")
                last = field
                value = _packed(inner) if field == 7 else inner.float32()
                if field != 7 and (not math.isfinite(value) or value < 0):
                    raise BitError("invalid health value")
                values[FIELDS[field - 1]] = value
            if inner.position != end:
                raise BitError("health item has leftover bits")
            items.append((index, values))
            r.position = end
        out.append({"handle": h, "count": count, "items": items})


class HealthBatchTracker:
    def __init__(self, keep_history=False):
        self.exports = {}
        self.channels = {}
        self.containers = {}
        self.objects = {}
        self._object_order = {}
        self._dirty = set()
        self.delivered = {}
        self.stats = Counter()
        self.history = [] if keep_history else None

    def observe_exports(self, exports):
        for entry in exports:
            if entry.get("path"):
                guid = entry["guid"]
                previous = self.exports.get(guid)
                self.exports[guid] = entry
                if guid in self.objects and previous != entry:
                    self._dirty.add(guid)  # resolve pending values on late exports

    def _is_health_actor(self, guid):
        return "BP_PropertyReplicationCharacterHealth_C" in \
            self.exports.get(guid, {}).get("path", "")

    def _is_health_component(self, guid, actor):
        entry = self.exports.get(guid, {})
        name = entry.get("path", "").rsplit(".", 1)[-1]
        return name in ("PropertyReplication", "PropertyReplicationComponent") \
            and entry.get("outer") == actor and self._is_health_actor(actor)

    def apply(self, component, deltas, seq):
        for delta in deltas:
            key = (component, delta["handle"])
            state = self.containers.setdefault(key, {"seq": -1, "items": {}})
            if seq < state["seq"]:
                continue
            state["seq"] = seq
            state["count"] = delta["count"]
            slots = state["items"]
            for i in list(slots):
                if i >= delta["count"]:
                    del slots[i]
            for index, values in delta["items"]:
                item = slots.setdefault(index, {})
                if "Object" in values and values["Object"] != item.get("Object"):
                    item.clear()  # slot reuse must not transfer the old health
                item.update(values)
                obj = item.get("Object")
                if not obj:
                    continue
                if obj not in self.objects:
                    self.objects[obj] = {}
                    self._object_order[obj] = len(self._object_order)
                rec = self.objects[obj]
                for name, value in values.items():
                    if name == "Object":
                        continue
                    if seq >= rec.get(name, (-1, None))[0]:
                        rec[name] = (seq, value)
                        self._dirty.add(obj)
                        if self.history is not None:
                            self.history.append((obj, name, value, seq))

    def consume(self, x):
        try:
            exports, start = exports_prefix(x)
            self.observe_exports(exports)
        except (sna.Bad, IndexError):
            self.stats["prefix_error"] += 1
            return
        if x.open:
            self.channels.pop(x.ch, None)
            try:
                bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8),
                                     bitorder="little")[:x.pbits]
                # Health broadcasters are stably named actors; do not parse
                # dynamic Character transforms using this branch.
                actor, _ = sna.load_object(bits, start, False)
                if not self._is_health_actor(actor):
                    return
                info, start = sna.parse_prefix(bits, x.hx, x.hm,
                                              frags=getattr(x, "frags", None))
                self.channels[x.ch] = info["actor"]
            except (sna.Bad, IndexError):
                self.stats["prefix_error"] += 1
                return
        actor = self.channels.get(x.ch)
        if actor is None:
            return
        # Strict block tiling: never search bit offsets for health-like floats.
        for block in tile(x.payload, x.pbits, start) or []:
            if block.get("deleted") or block["is_actor"] or not block["has_rep"]:
                continue
            if not self._is_health_component(block["sub"], actor):
                continue
            try:
                deltas = decode_health_block(x.payload, block)
            except BitError:
                self.stats["invalid"] += 1
                continue
            self.stats["exact"] += 1
            self.stats["items"] += sum(len(d["items"]) for d in deltas)
            self.apply(block["sub"], deltas, x.seq)
        if x.close:
            self.channels.pop(x.ch, None)

    def drain(self):
        """Resolve late exports too, returning each object/field version once."""
        events = []
        # Preserve object/field insertion order, but never walk unchanged
        # history after every bunch. Unresolved objects are retried on export.
        dirty = sorted(self._dirty, key=self._object_order.__getitem__)
        self._dirty.clear()
        for obj in dirty:
            fields = self.objects[obj]
            actor = self.actor_of(obj)
            if not actor:
                continue
            for name, (seq, value) in fields.items():
                key = (obj, name)
                if seq > self.delivered.get(key, -1):
                    events.append((actor, name, value, seq))
                    self.delivered[key] = seq
        return events

    def actor_of(self, obj):
        entry = self.exports.get(obj, {})
        if entry.get("path", "").rsplit(".", 1)[-1] in \
                ("AttributeCenter", "GPAttributeSetHealth"):
            return entry.get("outer")
        return None

    def resolved_history(self):
        history = {}
        for obj, name, value, seq in self.history or []:
            actor = self.actor_of(obj)
            if actor is not None:
                history.setdefault(actor, {}).setdefault(name, []).append((seq, value))
        for fields in history.values():
            for points in fields.values():
                points.sort()
        return history


def operator_health(hp, is_operator=True):
    """Use the configured 100 HP operator cap without inventing current HP.

    Keep replicated maxima and values above the default intact. AI units do
    not inherit the operator rule.
    """
    if not is_operator or not hp or not hp.get('total'):
        return hp
    current, maximum = hp['total']
    if maximum is None and isinstance(current, (int, float)) and 0 <= current <= 100:
        return {**hp, 'total': [current, 100.0], 'max_source': 'operator_default'}
    return hp


def health_at_seq(history, actor, seq):
    """Replay only what was observed at the cursor, including real zero HP."""
    from bisect import bisect_right
    fields = history.get(actor, {})
    points = fields.get("Health", [])
    index = bisect_right(points, seq, key=lambda point: point[0]) - 1
    if index < 0:
        return None
    stamp, value = points[index]
    return {"total": [round(value, 2), None], "seq": stamp,
            "source": "property_replication"}
