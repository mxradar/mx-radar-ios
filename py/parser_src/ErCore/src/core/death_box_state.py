"""Death-box actor lifetime and verified replication properties.

The default decoder reads only the established AActor prefix.  Specialized
properties require an injected, class-specific decoder; unknown handles are
reported instead of assigning speculative meanings.  Dormancy is not removal.
"""
from bisect import bisect_right
from collections import Counter
from copy import deepcopy
import math

import numpy as np
import repdec4
import sna
from blocks import tile
from replication_header import exports_prefix
from codec.bitstream import BitReader, BitError
from codec.engine_bunch import read_dfm_packed_int, read_serialized_fname
from death_box_protocol import DeathBoxDecoder


def actor_prefix(payload, block, class_name=None, *, movement_reader=None):
    """Read known fields up to the first unknown field, never scan for handles.

    A movement reader must have the class's verified quantization and physics
    flags. The generic repdec4 movement reader is deliberately not a default.
    """
    r = BitReader(payload, block['payload_end'])
    r.position = block['payload_start']
    fields = {}
    try:
        checksum = r.bit()

        def handle():
            h = read_dfm_packed_int(r)[0]
            if checksum:
                r.uint32()
            return h

        h = handle()
        previous = 0
        while h:
            if h <= previous:
                raise BitError('nonmonotonic actor handle')
            previous = h
            if 1 <= h <= 5:
                fields[{1:'bHidden', 2:'bNetActive', 3:'bReplicateMovement',
                        4:'bTearOff', 5:'bCanBeDamaged'}[h]] = bool(r.bit())
            elif h in (6, 15):
                fields['RemoteRole' if h == 6 else 'Role'] = r.serialize_int(4)
            elif h == 7:
                if movement_reader is None:
                    return {'fields': fields, 'status': 'movement_unknown',
                            'position': r.position, 'next_handle': h}
                fields['ReplicatedMovement'] = movement_reader(r)
            elif h in (8, 13, 14, 16):
                name = {8:'AttachParent', 13:'AttachComponent',
                        14:'Owner', 16:'Instigator'}[h]
                fields[name] = read_dfm_packed_int(r)[0]
            elif h in (9, 10):
                fields['LocationOffset' if h == 9 else 'RelativeScale3D'] = repdec4.pv(r, 100., 30)
            elif h == 11:
                fields['RotationOffset'] = repdec4.rot_c(r)
            elif h == 12:
                fields['AttachSocket'] = read_serialized_fname(r).as_dict()
            else:
                return {'fields': fields, 'status': 'prefix',
                        'position': r.position, 'next_handle': h}
            h = handle()
        return {'fields': fields,
                'status': 'exact' if r.position == r.bit_count else 'tail',
                'position': r.position, 'next_handle': 0}
    except BitError:
        # A malformed prefix cannot partially overwrite a previously valid bind.
        return {'fields': {}, 'status': 'error', 'position': r.position}


def _guid(value):
    if isinstance(value, dict):
        value = value.get('guid')
    return int(value) if value is not None else None


def _point(value):
    return (isinstance(value, (list, tuple)) and len(value) == 3
            and all(isinstance(v, (int, float)) and math.isfinite(v) for v in value))


class DeathBoxTracker:
    """Shared live/replay state; no geometric naming or distance deduplication."""
    def __init__(self, box_archs=None, carry_archs=None, *, keep_history=False,
                 decode_properties=None):
        self.box_archs = box_archs if box_archs is not None else set()
        self.carry_archs = carry_archs if carry_archs is not None else set()
        self.decode_properties = decode_properties or DeathBoxDecoder()
        self.paths, self.channels, self.channel_seq = {}, {}, {}
        self.records, self.active, self.removed = {}, {}, {}
        self.tombstones = {}
        self.stats = Counter()
        self.keep_history, self.history = keep_history, {}

    def _publish(self, actor):
        rec = self.records[actor]
        if rec.get('gone_seq') is not None:
            self.active.pop(actor, None)
            self.removed[actor] = rec
        elif _point(rec.get('loc')):
            self.removed.pop(actor, None)
            self.active[actor] = rec
        if self.keep_history:
            rows = self.history.setdefault(actor, [])
            value = (rec['update_seq'], deepcopy(rec))
            if rows and rows[-1][0] == value[0]:
                rows[-1] = value
            else:
                rows.append(value)

    def at(self, actor, seq):
        rows = self.history.get(actor, [])
        index = bisect_right(rows, seq, key=lambda row: row[0]) - 1
        return rows[index][1] if index >= 0 else None

    def open_actor(self, actor, channel, seq, kind, loc, class_name=''):
        if seq < self.channel_seq.get(channel, -1):
            return False
        self.channel_seq[channel] = seq
        self.channels.pop(channel, None)
        if seq <= self.tombstones.get(actor, -1):
            return False
        rec = self.records.get(actor)
        if rec and seq < rec['update_seq']:
            return False
        if rec is None or rec.get('gone_seq') is not None:
            generation = rec.get('generation', 0) + 1 if rec else 1
            rec = {'actor': actor, 'spawn_seq': seq, 'spawn_loc': list(loc) if _point(loc) else None,
                   'generation': generation, 'properties': {}, 'arrays': {}, 'contents_known': False}
            self.records[actor] = rec
        self.channels[channel] = actor
        rec.update(seq=seq, update_seq=seq, lifecycle_seq=seq, kind=kind,
                   class_name=class_name, channel_state='open', gone_seq=None,
                   gone_reason=None)
        if _point(loc):
            rec['loc'] = list(loc)
            rec['location_source'] = 'spawn'
        self._publish(actor)
        return True

    def remove(self, actor, seq, reason, evidence):
        if reason == 0:
            self.tombstones[actor] = max(seq, self.tombstones.get(actor, -1))
        rec = self.records.get(actor)
        if rec is None or seq < rec['update_seq']:
            return
        rec.update(gone_seq=seq, gone_reason=reason, update_seq=seq,
                   lifecycle_seq=seq, removal_evidence=evidence)
        for channel, value in list(self.channels.items()):
            if value == actor:
                self.channels.pop(channel)
                self.channel_seq[channel] = max(seq, self.channel_seq.get(channel, -1))
        self.stats[evidence] += 1
        self._publish(actor)

    def close_channel(self, channel, seq, reason):
        if seq < self.channel_seq.get(channel, -1):
            return
        self.channel_seq[channel] = seq
        actor = self.channels.pop(channel, None)
        if actor is None:
            return
        rec = self.records[actor]
        if seq < rec['update_seq']:
            return
        if reason in (0, 2, 4):
            self.remove(actor, seq, reason,
                        {0:'destroyed', 2:'level_unloaded', 4:'tear_off'}[reason])
        else:
            rec.update(update_seq=seq, lifecycle_seq=seq,
                       channel_state={1:'dormant', 3:'irrelevant'}.get(reason, 'closed'))
            self._publish(actor)

    def apply_properties(self, actor, seq, fields, *, status='exact', arrays=()):
        """Accept named properties from a verified decoder, including zero clears.

        Owner is a network-owner reference. A specialized decoder can supply
        DeadBodyCharacterOwner and OwnerPlayerState as explicit identity fields.
        Empty/looted/hidden are retained states, not inferred actor destruction.
        """
        rec = self.records.get(actor)
        if rec is None or rec.get('gone_seq') is not None or seq < rec['update_seq']:
            return
        if status == 'error':
            return
        rec['properties'].update(deepcopy(fields))
        for event in arrays:
            path = tuple(tuple(p) for p in event['path'])
            key = (event['cmd_index'], path)
            if event.get('kind') == 'array_begin':
                length = event['value']
                state = rec['arrays'].setdefault(key, {'name': event['name'], 'handle': event['handle'], 'items': {}})
                state['length'] = length
                state['items'] = {i: v for i, v in state['items'].items() if i < length}
            elif not event.get('kind') and path:
                # Match the closest opened array scope; one actor block is atomic.
                for array_key, state in reversed(list(rec['arrays'].items())):
                    if array_key[1] == path[:-1] and state['handle'] == path[-1][0]:
                        state['items'].setdefault(path[-1][1], {})[event['name']] = deepcopy(event['value'])
                        break
        for state in rec['arrays'].values():
            if state['name'] in ('CurrentSearchingPlayers', 'CurrentLootingPlayers'):
                rec['searchers' if state['name'] == 'CurrentSearchingPlayers' else 'looters'] = [next(iter(v.values())) for _, v in sorted(state['items'].items()) if v]
        refs = {'Owner':'owner_actor', 'AttachParent':'attach_parent',
                'AttachComponent':'attach_component',
                'DeadBodyCharacterOwner':'deceased_actor',
                'OwnerPlayerState':'owner_playerstate', 'RelatedXXBody':'related_body'}
        for name, target in refs.items():
            if name in fields:
                rec[target] = _guid(fields[name])
        scalar = {'CharacterUin':'deceased_uin', 'CarryBoxCharacterUin':'carrier_uin',
                  'PlayerName_Buffer':'player_name', 'DeadBodyBoxStatus':'box_status',
                  'bIsEmpty':'empty', 'bLooted':'looted', 'bHidden':'hidden',
                  'bIsCarrySelfBySomeOne':'carried', 'bIsAI':'is_ai'}
        for name, target in scalar.items():
            if name in fields:
                rec[target] = fields[name]
        if any(k in fields for k in ('LastPlayerLootingTime', 'LastLootingPlayer',
                                     'SearchDonePlayer', 'CurrentSearchingPlayerUin')):
            rec['loot_seen'] = True
        movement = fields.get('ReplicatedMovement')
        if isinstance(movement, dict) and _point(movement.get('location')):
            rec['loc'] = list(movement['location'])
            rec['location_source'] = 'replicated_movement'
        rec.update(update_seq=seq, decode_status=status)
        rec['location_offset'] = rec['properties'].get('LocationOffset', rec['properties'].get('AttachmentReplication.LocationOffset'))
        rec['kill_info'] = {k.split('.', 1)[1]: v for k, v in rec['properties'].items() if k.startswith('KillInfo.')}
        self._publish(actor)

    def world_position(self, actor, character_positions=None, at_seq=None, seen=None):
        """Resolve replicated attachment edges; ownership is never used as a carrier."""
        seen = set() if seen is None else seen
        if actor in seen:
            return None
        seen.add(actor)
        rec = self.at(actor, at_seq) if at_seq is not None else self.active.get(actor)
        if not rec or rec.get('gone_seq') is not None:
            return None
        parent = rec.get('attach_parent')
        position = (character_positions or {}).get(parent)
        if position is None and parent:
            position = self.world_position(parent, character_positions, at_seq, seen)
        offset = rec.get('location_offset')
        if position is not None and (offset is None or all(v == 0 for v in offset)):
            return list(position)
        # A nonzero socket/rotated offset requires its component transform;
        # retain the explicitly replicated world point instead of guessing it.
        return list(rec['loc']) if _point(rec.get('loc')) else None


    def consume(self, x):
        try:
            exports, start = exports_prefix(x)
            if hasattr(self.decode_properties, 'observe_exports'):
                self.decode_properties.observe_exports(exports)
            for value in exports:
                path = value.get('path')
                if not path:
                    continue
                self.paths[value['guid']] = path
                name = path.rsplit('.', 1)[-1].removeprefix('Default__')
                if name == 'BP_Inventory_DeadBody_C':
                    self.box_archs.add(value['guid'])
                elif name == 'BP_Inventory_CarryBody_C':
                    self.carry_archs.add(value['guid'])
            if x.seq < self.channel_seq.get(x.ch, -1):
                return
            if x.open:
                self.channels.pop(x.ch, None)
                self.channel_seq[x.ch] = x.seq
                if x.close:
                    if x.close_reason == 0:
                        r = BitReader(x.payload, x.pbits)
                        r.position = start
                        actor = read_dfm_packed_int(r)[0]
                        if r.position == r.bit_count:
                            self.remove(actor, x.seq, 0, 'destruction_info')
                    return
                bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8),
                                     bitorder='little')[:x.pbits]
                info, start = sna.parse_prefix(bits, x.hx, x.hm,
                                               quantize_bit=True, frags=x.frags)
                arch = info.get('arch')
                if arch not in self.box_archs and arch not in self.carry_archs:
                    return
                if not self.open_actor(info.get('actor'), x.ch, x.seq,
                                       'box' if arch in self.box_archs else 'carry',
                                       info.get('loc'), self.paths.get(arch, '')):
                    return
            actor = self.channels.get(x.ch)
            if actor is None:
                return
            for block in tile(x.payload, x.pbits, start) or []:
                if block.get('has_rep') and block.get('is_actor'):
                    decoded = self.decode_properties(x.payload, block,
                                                     self.records[actor]['class_name'])
                    status = decoded['status']
                    self.stats['layout_' + status] += 1
                    self.apply_properties(actor, x.seq, decoded.get('fields', {}), status=status,
                                          arrays=decoded.get('arrays', ()))
        except (BitError, sna.Bad):
            self.stats['framing_error'] += 1
        finally:
            if x.close:
                self.close_channel(x.ch, x.seq, x.close_reason)

def box_entity(actor, record, position, owner=None, name=None, origin=None, ai_names=None):
    """JSON-safe box information shared by real-time and replay views."""
    from ai_name_table import extract_ref_key
    replicated_name = record.get('player_name') or ''
    ai_key = extract_ref_key(replicated_name.encode())
    is_ai = record.get('is_ai')
    if ai_key is not None:
        is_ai = True
        name = (ai_names or {}).get(ai_key) or '人机死亡盒'
    elif replicated_name and 'LOCTABLE' not in replicated_name and '/Game/' not in replicated_name:
        name = replicated_name
    status = ('box_carried' if record.get('carried') else 'box_searching' if record.get('searchers') or record.get('looters')
              else 'box_empty' if record.get('empty') is True
              else 'box_looted' if record.get('looted') is True or record.get('loot_seen') else None)
    label = {'box_carried':'搬运中', 'box_searching':'搜刮中', 'box_empty':'已搜空', 'box_looted':'已搜刮'}.get(status)
    return {'key':'box-%s' % actor, 'eid':actor, 'ch':actor, 'slot':None,
            'kind':'box', 'name':name or '死亡盒', 'world':list(position), 'xyz':list(position),
            'status':label, 'status_key':status,
            'rel':[a-b for a,b in zip(position,origin)] if origin else None,
            'yaw':None, 'trail':None, 'owner_actor':owner,
            'owner_dead':None, 'owner_binding': 'dead_body_character' if record.get('deceased_actor') else 'actor_owner' if owner else None,
            'box_status':record.get('box_status'), 'empty':record.get('empty'),
            'looted':record.get('looted'), 'carried':record.get('carried'),
            'is_ai':is_ai,
            'searching_count':len(record.get('searchers',[])),
            'looting_count':len(record.get('looters',[])),
            'contents_known':record.get('contents_known',False),
            'inventory_gid':str(record['properties']['InventoryGID']) if record.get('properties',{}).get('InventoryGID') else None,
            'deceased_uin':str(record['deceased_uin']) if record.get('deceased_uin') else None,
            'decode_status':record.get('decode_status'),
            'location_source':record.get('location_source'), 'box_seq':record.get('update_seq')}

