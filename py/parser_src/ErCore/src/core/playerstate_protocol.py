"""Named, complete PlayerState RepLayout decoding for two captured profiles.

Class exports select a verified catalog, never a numeric NetGUID or an assumed
property handle. The current SOL catalog moved CurrentCharacterLiveStatus from
legacy h127 to h130; current h129 is an int32 PlayerGamePlayState, not bIsDeadBox.
Callers remove exports/NewActor framing and pass the actor property's bit span.
"""
from functools import lru_cache
import json

import asset_crypto
import repdec4
import repdec6


_CLASSES = {"legacy": "BP_DFMPlayerState_C", "current": "BP_SOLPlayerState_C"}
_PACKAGES = {
    "legacy": "/Game/BluePrints/GameModes/TestGame/BP_DFMPlayerState",
    "current": "/Game/BluePrints/GameModes/TestGame/BP_SOLPlayerState",
}
FIELDS = frozenset({
    "Owner", "PlayerId", "bIsABot", "PlayerNamePrivate", "Uin", "InnerTeamIndex",
    "TeamID", "Camp", "HeroId", "ExpertId", "AccountType", "PlatformID",
    "ForcesType", "TeamIdentity", "bDead", "DeadTickNum", "DeathWaitRescueTime",
    "DeathWaitRescueStartTime", "DeathWaitRescueEndTime", "bFinishGame",
    "bDeathWaitingRescueEnd", "CurrentDeathStartTime", "CurrentCharacterLiveStatus",
    "PlayerGamePlayState", "bIsDeadBox",
    "ExitState",
})
# 身份字段白名单 —— 只认「纯标量、且位于 PS 记录最前面」的那批。
#
# 为什么单独列：PS 的开局记录很长，`current` schema 对**本地玩家自己**那条
# （实测 170: 21870 bit）会在中段布局不匹配 -> status=error/leftover，整块身份
# 被丢掉，于是自机 PS 无名、`_derive` 的真人候选把它排除 -> 自机名字退化成同局
# 另一个有名字的人（实测显示成队友「秋元薫」）。但 Owner/PlayerId/PlayerName
# Private/Uin/团队 这些字段在报错点**之前**就已完整解出（实测 error 停在 bit
# 11893，名字在更早的事件里）。
# 所以非 exact 块也导出这批字段，但**只用于身份/命名**：生死/生命周期
# （bDead/ExitState/PlayerGamePlayState/PlayerMatchInfo.*）仍严格只在 exact 时
# 暴露——截断的块可能给出假生死，那正是原严格门要防的。
IDENTITY_FIELDS = frozenset({
    "Owner", "PlayerId", "bIsABot", "PlayerNamePrivate", "Uin", "InnerTeamIndex",
    "TeamID", "Camp", "HeroId", "ExpertId",
})

_MATCH_MEMBERS = {
    0: "bMatchOver", 1: "MatchOverReason", 4: "MatchTime",
    8: "MatchTimeFromBegin", 12: "bCanEnterScav", 13: "bLogin",
    16: "AvgPing", 20: "SDPing", 24: "Over200msCounts",
}


def select_profile(export_path):
    """Return legacy/current only for exact observed class/CDO/package names."""
    if not isinstance(export_path, str):
        return None
    for profile, name in _CLASSES.items():
        package = _PACKAGES[profile]
        if export_path in (name, "Default__" + name, package,
                           package + "." + name,
                           package + ".Default__" + name):
            return profile
    return None


@lru_cache(maxsize=2)
def _schema(profile):
    if profile == "legacy":
        catalog = repdec4.load_catalogs()[_CLASSES[profile]]
        entries = repdec4.load_propmeta().get(_CLASSES[profile], [])
    elif profile == "current":
        path = repdec4.ROOT / "src/core" / "playerstate_schema_0904.json"
        data = json.loads(asset_crypto.read_text(path))
        catalog = data["classes"][_CLASSES[profile]]
        entries = data["meta"][_CLASSES[profile]]
    else:
        raise ValueError("unknown PlayerState profile")
    parents = {parent["index"]: parent for parent in catalog["parents"]}
    meta = {entry["index"]: dict(entry) for entry in entries}
    # Older metadata lists only generic Property commands. Parent names retain
    # the verified ByteProperty enum identities needed for role/gender widths.
    for command in catalog["cmds"]:
        record = meta.setdefault(command["index"], {})
        record.setdefault("name", parents[command["parent_index"]]["name"])
        parent = parents[command["parent_index"]]
        if profile == "current" and parent["name"] == "PlayerMatchInfo":
            member = _MATCH_MEMBERS.get(command["object_offset"] - parent["object_offset"])
            if member:
                record["departure_name"] = "PlayerMatchInfo." + member
            if member == "MatchOverReason":
                record.update(departure_bits=4, departure_values=tuple(range(9)))
        elif profile == "current" and parent["name"] == "ExitState":
            record.update(departure_bits=3, departure_values=(1, 2, 3, 4, 5))
        if command["type"] == 6 and record["name"] in ("Role", "RemoteRole"):
            record["enum_max"] = 4
        elif command["type"] == 6 and record["name"] == "PlayerGender":
            record["enum_max"] = 3 if profile == "legacy" else 5
    return catalog, parents, meta


def _read_value(reader, kind, meta):
    if meta and "departure_bits" in meta:
        from codec.bitstream import BitError
        value = reader.bits(meta["departure_bits"])
        if value not in meta["departure_values"]:
            raise BitError("invalid PlayerState departure enum")
        return value
    if kind == 6 and meta and meta.get("enum_max"):
        return reader.serialize_int(meta["enum_max"])
    from death_box_protocol import box_value
    return box_value(reader, kind, meta or {})


def decode_playerstate(payload, start, end, profile):
    """Return ``{fields, identity, status, position, profile}``.

    ``fields`` only exists for exact blocks: an opaque/truncated/leftover block is
    not a complete valid snapshot, so no partially decoded lifecycle value escapes.
    ``identity`` is the separate, narrower door (see ``IDENTITY_FIELDS``): the
    pure-scalar identity fields that sit at the very front of the record are
    exported even for non-exact blocks, because naming/team can be trusted there
    while death/lifecycle cannot. Arrays and multi-member structures are walked
    for framing but never flattened. Unknown profiles produce an explicit empty
    result.
    """
    result = {"fields": {}, "identity": {}, "status": "unknown_profile",
              "position": int(start), "profile": profile}
    if profile not in _CLASSES:
        return result
    if not 0 <= start <= end <= len(payload) * 8:
        result["status"] = "error"
        return result
    catalog, parents, meta = _schema(profile)
    events, position, status = repdec6.decode_block(
        payload, {"payload_start": start, "payload_end": end},
        catalog, meta, value_reader=_read_value)
    result.update(status=status, position=position)
    identity = {}
    for event in events:
        if event["path"]:
            continue
        parent = parents[event["parent_index"]]
        name = parent["name"]
        if name in IDENTITY_FIELDS and parent["cmd_end"] - parent["cmd_start"] == 1:
            identity[name] = event["value"]
    result["identity"] = identity
    if status != "exact":
        return result
    fields = {}
    for event in events:
        if event["path"]:
            continue
        parent = parents[event["parent_index"]]
        name = parent["name"]
        departure_name = meta[event["cmd_index"]].get("departure_name")
        if departure_name:
            fields[departure_name] = event["value"]
        if name in FIELDS and parent["cmd_end"] - parent["cmd_start"] == 1:
            fields[name] = event["value"]
    result["fields"] = fields
    return result


class PlayerStateTracker:
    """Keep named wire deltas for replay without borrowing later state."""
    def __init__(self):
        from collections import Counter
        self.paths, self.channels, self.profiles, self.records = {}, {}, {}, {}
        self.events = []
        self.stats = Counter()

    def consume(self, bunch):
        import numpy as np
        import sna
        from blocks import tile
        from replication_header import exports_prefix
        from codec.bitstream import BitError
        try:
            exports, start = exports_prefix(bunch)
            for item in exports:
                if item.get('path'):
                    self.paths[item['guid']] = item['path']
            if bunch.open:
                self.channels.pop(bunch.ch, None)
                if bunch.close:
                    return
                bits = np.unpackbits(np.frombuffer(bunch.payload, dtype=np.uint8), bitorder='little')[:bunch.pbits]
                info, start = sna.parse_prefix(bits, bunch.hx, bunch.hm, quantize_bit=False, frags=bunch.frags)
                profile = select_profile(self.paths.get(info.get('arch')))
                if profile is None:
                    return
                actor = info['actor']
                self.channels[bunch.ch] = actor
                self.profiles[actor] = profile
            actor = self.channels.get(bunch.ch)
            if actor is None:
                return
            for block in tile(bunch.payload, bunch.pbits, start) or []:
                if block.get('has_rep') and block.get('is_actor'):
                    result = decode_playerstate(bunch.payload, block['payload_start'], block['payload_end'], self.profiles[actor])
                    self.stats[result['status']] += 1
                    if result['status'] == 'exact':
                        fields = result['fields']
                        self.records.setdefault(actor, {}).update(fields)
                        self.events.append((bunch.seq, actor, dict(fields)))
                    else:
                        # 非 exact：身份字段仍可信（见 IDENTITY_FIELDS）。只并命名要用的
                        # 身份，不写 events —— events 喂生命周期，必须保持 exact-only。
                        identity = result.get('identity') or {}
                        if identity:
                            self.records.setdefault(actor, {}).update(identity)
        except (BitError, sna.Bad, IndexError):
            self.stats['framing_error'] += 1
        finally:
            if bunch.close:
                self.channels.pop(bunch.ch, None)
