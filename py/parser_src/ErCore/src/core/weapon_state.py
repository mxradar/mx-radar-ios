"""Current weapon state: RepLayout arrays plus framed FastArray fields 5/20."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from codec.bitstream import BitReader, BitError
from codec.engine_bunch import read_dfm_packed_int
import repdec4
import repdec6
from weapon_native import weapon_extra


def weapon_value(r, t, meta=None):
    # FEnumProperty EWeaponSwitchType has max=9, so SerializeBits(4).
    if meta and meta.get('weapon_switch'):
        return r.bits(4)
    return repdec4.read_val(r, t, meta)


def _info(r):
    return {'reserve': bool(r.bit()), 'switch': r.bits(4),
            'identity': r.uint32(), 'class_path': repdec4.st_fastweapbp(r),
            'frame': r.uint32(), 'slot': r.uint8()}


def fast_array(data, start, end, item_reader):
    r = BitReader(data, end)
    r.position = start
    mode = r.bit()
    key, base, nd, nc = (r.int32() for _ in range(4))
    if not (0 <= nd <= 4096 and 0 <= nc <= 4096):
        raise BitError('FastArray counts')
    deleted = [r.int32() for _ in range(nd)]
    items = []
    for _ in range(nc):
        rid = r.int32()
        items.append((rid, item_reader(r)))
    if r.position != end:
        raise BitError('FastArray trailing bits')
    return {'key': key, 'base': base, 'mode': mode,
            'deleted': deleted, 'items': items}


class WeaponTracker:
    def __init__(self, keep_history=False):
        schema = json.loads(Path(__file__).with_name('weapon_schema_20261106.json').read_text(encoding='utf-8'))
        self.catalog = schema['class']
        self.meta = {int(k): v for k, v in schema['meta'].items()}
        for index in (26, 34):
            self.meta[index] = dict(self.meta.get(index, {}), weapon_switch=True)
        self.actors = {}
        self.stats = Counter()
        self.keep_history = keep_history
        self.history = {}
        self.paths = {}
        self.channels = {}

    def consume(self, x):
        import numpy as np
        import sna
        from blocks import tile
        from replication_header import exports_prefix
        try:
            exports, start = exports_prefix(x)
            for e in exports:
                if e.get('path'):
                    self.paths[e['guid']] = e['path']
            if x.open:
                self.channels.pop(x.ch, None)
                if x.close:
                    return
                bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8),
                                     bitorder='little')[:x.pbits]
                info, start = sna.parse_prefix(bits, x.hx, x.hm,
                                              quantize_bit=True, frags=x.frags)
                name = self.paths.get(info.get('arch'), '')
                if 'Character' not in name or 'Weapon' in name:
                    return
                start += 8  # Character custom spawn byte after NewActor
                self.channels[x.ch] = info['actor']
            actor = self.channels.get(x.ch)
            if actor is None:
                return
            for block in tile(x.payload, x.pbits, start) or []:
                if block.get('deleted') or not block.get('has_rep'):
                    continue
                guid = block.get('sub') if block.get('stably') else block.get('cls')
                if 'WeaponReplicate' in self.paths.get(guid, ''):
                    self.apply_block(actor, x.seq, x.payload, block)
        except (BitError, sna.Bad):
            self.stats['framing_error'] += 1
        finally:
            if x.close:
                self.channels.pop(x.ch, None)

    def apply_block(self, actor, seq, payload, block):
        ev, pos, status = repdec6.decode_block(
            payload, block, self.catalog, self.meta,
            include_arrays=True, value_reader=weapon_value)
        self.stats['layout_' + status] += 1
        state = self.actors.setdefault(actor, {'current': [], 'extra': {}, 'info': {}})
        if seq < state.get('seq', -1):
            self.stats['stale_block'] += 1
            return False
        # A complete array scope may precede an unsupported, unrelated native
        # property. Never apply an incomplete array or a truncated resize.
        fields = {25: 'reserve', 26: 'switch', 27: 'identity',
                  28: 'class_path', 29: 'frame', 30: 'slot'}
        pending = None
        changed = False
        for e in ev:
            if e['cmd_index'] == 24 and not e['path']:
                if e.get('kind') == 'array_begin':
                    pending = deepcopy(state['current'][:e['value']])
                    while len(pending) < e['value']:
                        pending.append({'identity': 0, 'slot': 0, 'frame': 0})
                elif e.get('kind') == 'array_end' and pending is not None:
                    state.update(current=pending, current_seq=seq)
                    changed = True
                    pending = None
            elif pending is not None and e['cmd_index'] in fields:
                ei = e['path'][0][1]
                pending[ei][fields[e['cmd_index']]] = e['value']
        if status in ('short', 'exact'):
            r = BitReader(payload, block['payload_end'])
            r.position = pos
            while r.position < r.bit_count:
                try:
                    # Runtime FClassNetCache: 24 fields, WeapInfoArray=5,
                    # WeaponReplicatedExtraDataList=20 (current captured schema). Length delimits each.
                    fi = r.serialize_int(24)
                    n = read_dfm_packed_int(r)[0]
                    end = r.position + n
                    if end > r.bit_count:
                        raise BitError('weapon field length')
                    if fi in (5, 20):
                        batch = fast_array(payload, r.position, end,
                                           _info if fi == 5 else weapon_extra)
                        target = state['info' if fi == 5 else 'extra']
                        # Initial snapshots have base=0; later changes are
                        # keyed by ReplicationID, which is distinct from identity.
                        if batch['base'] == 0:
                            target.clear()
                        for rid in batch['deleted']:
                            target.pop(rid, None)
                        for rid, item in batch['items']:
                            target[rid] = dict(item, seq=seq)
                        self.stats['fast_array_' + str(fi)] += 1
                        changed = True
                    r.position = end
                except BitError:
                    self.stats['fast_array_error'] += 1
                    break
        if changed:
            state['seq'] = seq
            if self.keep_history:
                self.history.setdefault(actor, []).append((seq, self.current(actor)))
        return changed

    def current(self, actor, at_seq=None):
        if at_seq is not None:
            import bisect
            rows = self.history.get(actor, [])
            i = bisect.bisect_right(rows, at_seq, key=lambda row: row[0]) - 1
            return dict(rows[i][1]) if i >= 0 else {'known': False}
        state = self.actors.get(actor, {})
        if 'current_seq' not in state:
            return {'known': False}
        # Slot 0 is the held weapon. Offhand/ability slots remain active too;
        # comparing their frame numbers selects the wrong hand on ability use.
        entries = [e for e in state['current'] if e.get('slot', 0) == 0]
        if not entries:
            return {'known': True, 'status': 'empty', 'weapon_id': None,
                    'seq': state['current_seq']}
        e = entries[-1]
        identity = e.get('identity', 0)
        # Class-path IDs identify reusable weapon classes, even for type 2/3.
        # Recorded examples: RPG class 18080000001 -> AI item 18080000007;
        # bow class 18130000001 -> item 18130000002. They cannot replace an
        # instance mapping after its FastArray entry is deleted or before it
        # arrives. Keep identities scoped to this actor and reject conflicts.
        mapped = {x['weapon_id'] for x in state['extra'].values()
                  if identity and x['identity'] == identity
                  and isinstance(x.get('weapon_id'), int)
                  and not isinstance(x['weapon_id'], bool) and x['weapon_id'] > 0}
        wid = next(iter(mapped)) if len(mapped) == 1 else None
        return {'known': True, 'status': 'empty' if identity == 0 else
                'resolved' if wid else 'unresolved', 'weapon_id': wid,
                'identity': identity, 'slot': 0, 'frame': e.get('frame'),
                'seq': state['current_seq'],
                'resolved_seq': state.get('seq'),
                'source': 'weapon_extra' if wid else
                          'conflicting_extra' if len(mapped) > 1 else 'missing_extra'}


def weapon_fields(tracker, actor, names, at_seq=None):
    d = tracker.current(actor, at_seq)
    name = None
    if d.get('known'):
        wid = d.get('weapon_id')
        name = ('空手' if d.get('status') == 'empty' else
                names.get(wid) or ('武器 %s' % wid if wid else '未知武器'))
    return {'curr_weapon': name, 'curr_weapon_known': d.get('known', False),
            'curr_weapon_id': d.get('weapon_id'),
            'curr_weapon_status': d.get('status'), 'curr_weapon_seq': d.get('seq')}
