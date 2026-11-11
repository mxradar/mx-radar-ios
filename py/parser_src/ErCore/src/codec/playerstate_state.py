"""Generation-local PlayerState identity and ``bIsABot`` provenance.

The wire property ``bIsABot`` is ``COND_InitialOnly``.  Its absence has two
different meanings and therefore must not be treated as a global false value:

* on a decoded initial/open PlayerState snapshot, absence retains the
  APlayerState ClassDefaultObject value (false for the verified build);
* on a later delta, absence retains a value already established in the same
  flow/channel generation;
* without either fact, the value remains unresolved.

State is keyed by ``(flow_identity, channel_index)`` and every observed channel
open starts a new monotonically increasing generation.  Close removes the
active state immediately.  Reliable event identifiers make retransmitted open,
close, and identity bunches idempotent.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Hashable


IS_AI_WIRE_VALUE = "wire_value"
IS_AI_INITIAL_DEFAULT_CDO = "initial_default_cdo"
IS_AI_GENERATION_CACHE = "generation_cache"
IS_AI_UNRESOLVED = "unresolved"


@dataclass
class _ChannelState:
    generation: int
    opened_explicitly: bool
    player_name: str | None = None
    team_id: int | None = None
    is_ai: bool | None = None
    is_ai_origin: str | None = None
    identity_observations: int = 0


class PlayerStateGenerationTracker:
    """Track PlayerState identity only within one flow/channel generation."""

    def __init__(self, *, initial_default_is_ai: bool = False):
        self.initial_default_is_ai = bool(initial_default_is_ai)
        self._active: dict[tuple[str, int], _ChannelState] = {}
        self._generation_counter: dict[tuple[str, int], int] = {}
        self._lifecycle_results: dict[tuple[str, int, str, Hashable], dict] = {}
        self._observation_results: dict[tuple[str, int, Hashable], dict] = {}
        self.diagnostics: list[dict] = []

    @staticmethod
    def _key(flow_identity: str, channel_index: int) -> tuple[str, int]:
        flow = str(flow_identity)
        channel = int(channel_index)
        if not flow:
            raise ValueError("flow_identity must be non-empty")
        if channel < 0:
            raise ValueError("channel_index must be non-negative")
        return flow, channel

    def _new_state(
        self,
        key: tuple[str, int],
        *,
        opened_explicitly: bool,
    ) -> _ChannelState:
        generation = self._generation_counter.get(key, 0) + 1
        self._generation_counter[key] = generation
        state = _ChannelState(
            generation=generation,
            opened_explicitly=opened_explicitly,
        )
        self._active[key] = state
        return state

    def open_channel(
        self,
        flow_identity: str,
        channel_index: int,
        *,
        event_id: Hashable | None = None,
    ) -> dict:
        key = self._key(flow_identity, channel_index)
        cache_key = None
        if event_id is not None:
            cache_key = (key[0], key[1], "open", event_id)
            cached = self._lifecycle_results.get(cache_key)
            if cached is not None:
                result = deepcopy(cached)
                result["duplicate"] = True
                return result

        replaced = self._active.get(key)
        state = self._new_state(key, opened_explicitly=True)
        result = {
            "kind": "open",
            "flow_identity": key[0],
            "channel_index": key[1],
            "generation": state.generation,
            "duplicate": False,
            "replaced_active_generation": (
                replaced.generation if replaced is not None else None
            ),
            "cleared_identity": replaced is not None,
        }
        if cache_key is not None:
            self._lifecycle_results[cache_key] = deepcopy(result)
        return result

    def close_channel(
        self,
        flow_identity: str,
        channel_index: int,
        *,
        event_id: Hashable | None = None,
    ) -> dict:
        key = self._key(flow_identity, channel_index)
        cache_key = None
        if event_id is not None:
            cache_key = (key[0], key[1], "close", event_id)
            cached = self._lifecycle_results.get(cache_key)
            if cached is not None:
                result = deepcopy(cached)
                result["duplicate"] = True
                return result

        old = self._active.pop(key, None)
        result = {
            "kind": "close",
            "flow_identity": key[0],
            "channel_index": key[1],
            "generation": (
                old.generation
                if old is not None
                else self._generation_counter.get(key)
            ),
            "duplicate": False,
            "had_active_generation": old is not None,
            "evicted_identity": bool(
                old is not None and old.player_name is not None
            ),
        }
        if cache_key is not None:
            self._lifecycle_results[cache_key] = deepcopy(result)
        return result

    def bind_identity(
        self,
        flow_identity: str,
        channel_index: int,
        *,
        player_name: str,
        team_id: int,
        b_is_a_bot_present: bool,
        b_is_a_bot_value: bool | None,
        initial_snapshot: bool,
        member_team_matches_team_id: bool,
        event_id: Hashable | None = None,
    ) -> dict:
        """Bind a complete PlayerState suffix and resolve generation-local AI."""

        key = self._key(flow_identity, channel_index)
        if not player_name:
            raise ValueError("player_name must be non-empty")
        if not member_team_matches_team_id:
            raise ValueError("MemberInfoList.TeamId does not match TeamID")
        team = int(team_id)
        if b_is_a_bot_present and b_is_a_bot_value is None:
            raise ValueError("present bIsABot requires a wire value")

        observation_key = None
        if event_id is not None:
            observation_key = (key[0], key[1], event_id)
            cached = self._observation_results.get(observation_key)
            if cached is not None:
                result = deepcopy(cached)
                result["duplicate"] = True
                return result

        state = self._active.get(key)
        implicit_generation = state is None
        if state is None:
            state = self._new_state(
                key,
                opened_explicitly=bool(initial_snapshot),
            )

        cached_before = state.is_ai
        cached_origin_before = state.is_ai_origin
        if b_is_a_bot_present:
            is_ai = bool(b_is_a_bot_value)
            source = IS_AI_WIRE_VALUE
            state.is_ai = is_ai
            state.is_ai_origin = IS_AI_WIRE_VALUE
        elif initial_snapshot:
            is_ai = self.initial_default_is_ai
            source = IS_AI_INITIAL_DEFAULT_CDO
            state.is_ai = is_ai
            state.is_ai_origin = IS_AI_INITIAL_DEFAULT_CDO
        elif state.is_ai is not None:
            is_ai = state.is_ai
            source = IS_AI_GENERATION_CACHE
        else:
            is_ai = None
            source = IS_AI_UNRESOLVED

        state.player_name = player_name
        state.team_id = team
        state.identity_observations += 1
        result = {
            "flow_identity": key[0],
            "channel_index": key[1],
            "generation": state.generation,
            "state_key": [key[0], key[1], state.generation],
            "player_name": player_name,
            "team_id": team,
            "is_ai": is_ai,
            "is_ai_source": source,
            "is_ai_cached_origin": state.is_ai_origin,
            "bIsABot_wire_present": bool(b_is_a_bot_present),
            "bIsABot_wire_value": (
                bool(b_is_a_bot_value) if b_is_a_bot_present else None
            ),
            "initial_snapshot": bool(initial_snapshot),
            "opened_explicitly": state.opened_explicitly,
            "implicit_generation": implicit_generation,
            "cached_is_ai_before": cached_before,
            "cached_origin_before": cached_origin_before,
            "identity_observation_index": state.identity_observations,
            "duplicate": False,
            "active": True,
        }
        if observation_key is not None:
            self._observation_results[observation_key] = deepcopy(result)
        return result

    def note_partial_drop(
        self,
        flow_identity: str,
        channel_index: int,
        *,
        reason: str,
    ) -> dict:
        """Record a decoder loss without changing the channel generation."""

        key = self._key(flow_identity, channel_index)
        state = self._active.get(key)
        item = {
            "kind": "partial_drop",
            "flow_identity": key[0],
            "channel_index": key[1],
            "generation": state.generation if state is not None else None,
            "active_generation_preserved": state is not None,
            "reason": str(reason),
        }
        self.diagnostics.append(item)
        return deepcopy(item)

    def active_state(
        self,
        flow_identity: str,
        channel_index: int,
    ) -> dict | None:
        key = self._key(flow_identity, channel_index)
        state = self._active.get(key)
        if state is None:
            return None
        return {
            "flow_identity": key[0],
            "channel_index": key[1],
            "generation": state.generation,
            "player_name": state.player_name,
            "team_id": state.team_id,
            "is_ai": state.is_ai,
            "is_ai_origin": state.is_ai_origin,
            "opened_explicitly": state.opened_explicitly,
            "identity_observations": state.identity_observations,
        }

    def active_states(self) -> list[dict]:
        return [
            self.active_state(flow, channel)
            for flow, channel in sorted(self._active)
        ]

