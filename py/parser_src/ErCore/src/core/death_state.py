"""Sequence-aware character life state from verified replicated fields.

``GPAttributeSetHealth.IsAlive`` and the corresponding PropertyReplication
float carry ECharacterLiveStatus: 1 alive, 2 dead, 3 impending death (downed).
They are not booleans. Neither unknown enum values nor actor-channel closure,
zero HP, countdowns, movement silence or unbound FSM GUID words prove death.

Feed health ``drain()`` after *each* logical bunch, or import the broadcaster's
complete ``resolved_history()``. Draining once after a large batch discards
intermediate death/revival transitions before this tracker can observe them.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
import math


LIFE_STATES = {1: "alive", 2: "dead", 3: "downed"}


def _stamp(event):
    return event["seq"]


def _priority(event):
    # A detailed downed enum must survive a same-bunch generic bDead=False.
    detailed = event["field"] in ("IsAlive", "CurrentCharacterLiveStatus")
    source = {"property_replication": 3, "attribute_set_current": 2,
              "attribute_set": 2, "playerstate": 1}.get(event["source"], 0)
    return (int(detailed), source, {1: 1, 3: 2, 2: 3}[event["status"]])


class DeathStateTracker:
    """Track explicit semantic life events within Character spawn boundaries.

    ``observe`` rejects events older than the newest accepted semantic event.
    Unknown enum observations are retained separately for diagnostics and do
    not resurrect a dead character. Enable ``keep_history`` for ``at`` queries.
    ``spawn`` may also be called after late actor discovery; observations at
    or after its timestamp remain applicable, earlier ones are isolated.
    """

    def __init__(self, keep_history=False):
        self.keep_history = bool(keep_history)
        self.stats = Counter()
        self._events = {}
        self._raw = {}
        self._spawns = {}
        self._history = {}
        self._raw_history = {}
        self._spawn_history = {}

    def spawn(self, actor, seq):
        """Establish a new Character generation; duplicate/old opens do nothing."""
        actor, seq = int(actor), int(seq)
        previous = self._spawns.get(actor)
        if previous is not None and seq <= previous["seq"]:
            return False
        record = {"seq": seq, "generation":
                  previous["generation"] + 1 if previous else 1}
        self._spawns[actor] = record
        if self.keep_history:
            self._spawn_history.setdefault(actor, []).append(dict(record))
        for mapping in (self._events, self._raw):
            if actor in mapping and mapping[actor]["seq"] < seq:
                del mapping[actor]
        self.stats["spawns"] += 1
        return True

    def observe(self, actor, status, seq, *, source="property_replication",
                field="IsAlive"):
        """Accept one verified numeric life enum; return whether state was accepted.

        Source and field are retained verbatim. This API only accepts numeric
        enum values; use ``observe_playerstate`` for decoded boolean fields.
        Unknown/nonintegral values never become death or revival evidence.
        """
        actor, seq = int(actor), int(seq)
        if isinstance(status, bool) or not isinstance(status, (int, float)) \
                or not math.isfinite(status):
            self.stats["invalid"] += 1
            return False
        boundary = self._spawns.get(actor)
        if boundary is not None and seq < boundary["seq"]:
            self.stats["before_spawn"] += 1
            return False
        previous = self._events.get(actor)
        raw_previous = self._raw.get(actor)
        newest_seq = max(previous["seq"] if previous else -1,
                         raw_previous["seq"] if raw_previous else -1)
        if seq < newest_seq:
            self.stats["stale"] += 1
            return False
        raw = {"seq": seq, "status": status,
               "source": str(source), "field": str(field)}
        if raw_previous is None or seq >= raw_previous["seq"]:
            self._raw[actor] = raw
            if self.keep_history:
                history = self._raw_history.setdefault(actor, [])
                if history and history[-1]["seq"] == seq:
                    history[-1] = dict(raw)
                else:
                    history.append(dict(raw))
        if status not in LIFE_STATES:
            self.stats["unknown"] += 1
            return False
        event = dict(raw, status=int(status))
        if previous is not None and seq == previous["seq"]:
            if event == previous:
                self.stats["duplicate"] += 1
                return False
            if event["status"] != previous["status"]:
                self.stats["same_seq_conflict"] += 1
            if _priority(event) <= _priority(previous):
                return False
        self._events[actor] = event
        if self.keep_history:
            history = self._history.setdefault(actor, [])
            if history and history[-1]["seq"] == seq:
                history[-1] = dict(event)
            else:
                history.append(dict(event))
        self.stats["accepted"] += 1
        return True

    def observe_health_events(self, events, *, source="property_replication"):
        """Consume ``(actor, field, value, seq)`` from a per-bunch health drain."""
        accepted = 0
        for actor, field, value, seq in sorted(events, key=lambda e: e[3]):
            if field == "IsAlive":
                accepted += self.observe(actor, value, seq, source=source,
                                         field=field)
        return accepted

    def observe_health_history(self, history, *, source="property_replication"):
        """Import complete ``HealthBatchTracker.resolved_history()`` in wire order.

        Re-importing the same history cannot replace newer state. Import into
        a fresh tracker when reconstructing history older than live events.
        """
        events = [(actor, "IsAlive", value, seq)
                  for actor, fields in history.items()
                  for seq, value in fields.get("IsAlive", [])]
        return self.observe_health_events(events, source=source)

    def observe_playerstate(self, actor, values, field_seq, *, verified=False):
        """Accept externally decoded PS life fields only after schema verification.

        This module does not assume numeric property handles or select a build.
        ``bIsDeadBox=False`` and countdowns never imply revival. A true box flag
        is auxiliary death evidence; box existence/ownership is handled outside
        this tracker. Every field uses its own wire sequence.
        """
        if not verified:
            self.stats["unverified_playerstate"] += 1
            return 0
        events = []
        for field in ("CurrentCharacterLiveStatus", "bDead", "bIsDeadBox"):
            if field not in values or field not in field_seq:
                continue
            value = values[field]
            if field == "bDead":
                if type(value) is not bool:
                    continue
                value = 2 if value else 1
            elif field == "bIsDeadBox":
                if value is not True:
                    continue
                value = 2
            events.append((field_seq[field], field, value))
        return sum(self.observe(actor, value, seq, source="playerstate", field=field)
                   for seq, field, value in sorted(events))

    @staticmethod
    def _result(event, raw, spawn):
        if spawn is not None:
            if event is not None and event["seq"] < spawn["seq"]:
                event = None
            if raw is not None and raw["seq"] < spawn["seq"]:
                raw = None
        status = event["status"] if event else None
        return {
            "state": LIFE_STATES.get(status, "unknown"),
            "status": status, "seq": event["seq"] if event else None,
            "source": event["source"] if event else None,
            "field": event["field"] if event else None,
            "spawn_seq": spawn["seq"] if spawn else None,
            "generation": spawn["generation"] if spawn else 0,
            "confirmed": event is not None,
            "dead": status == 2, "downed": status == 3,
            "raw_status": raw["status"] if raw else None,
            "raw_seq": raw["seq"] if raw else None,
            "raw_source": raw["source"] if raw else None,
            "raw_field": raw["field"] if raw else None,
        }

    def current(self, actor):
        """Return current semantic state, provenance and latest raw observation."""
        actor = int(actor)
        return self._result(self._events.get(actor), self._raw.get(actor),
                            self._spawns.get(actor))

    @staticmethod
    def _before(history, seq):
        index = bisect_right(history, seq, key=_stamp) - 1
        return history[index] if index >= 0 else None

    def at(self, actor, seq):
        """Return state at the replay cursor without borrowing future information."""
        if not self.keep_history:
            raise ValueError("historical life queries require keep_history=True")
        actor, seq = int(actor), int(seq)
        spawns = self._spawn_history.get(actor, [])
        if spawns and seq < spawns[0]["seq"]:
            return self._result(None, None, None)
        return self._result(
            self._before(self._history.get(actor, []), seq),
            self._before(self._raw_history.get(actor, []), seq),
            self._before(spawns, seq))

    def death_seq(self, actor, seq=None):
        """Return the current death evidence sequence, or None for any other state."""
        state = self.current(actor) if seq is None else self.at(actor, seq)
        return state["seq"] if state["dead"] else None
