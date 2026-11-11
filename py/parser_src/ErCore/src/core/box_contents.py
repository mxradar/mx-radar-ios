"""Container contents from controller full/diff RPCs, keyed by object and item GID.

This is received state, not evidence that the local player performed a search
or transfer. A complete visible list may still contain unrevealed placeholders.
"""
from bisect import bisect_right
from collections import Counter
from copy import deepcopy
from functools import lru_cache
import json

import asset_crypto
import numpy as np
import repdec4
import repdec6
import sna
from blocks import tile
from death_box_protocol import box_value
from inventory_native import inventory_item, loot_box_data
from replication_header import exports_prefix
from codec.bitstream import BitReader, BitError
from codec.engine_bunch import read_dfm_packed_int

PLACEHOLDER_ID = 99999999999


@lru_cache(maxsize=1)
def schema():
    return json.loads(asset_crypto.read_text(repdec4.ROOT / 'src/core' / 'box_contents_schema_20261106.json'))


class ContentsDecodeError(BitError):
    def __init__(self, message, actor=None):
        super().__init__(message)
        self.actor = actor


def decode_contents(payload, start, end, method, *, inventory_profile='0904'):
    """Read RPC parameter-presence bits and require the declared bit span exactly."""
    actor = None
    try:
        if method not in ('full', 'diff', 'diff_space') or not 0 <= start <= end <= len(payload)*8:
            raise BitError('contents envelope')
        r = BitReader(payload, end); r.position = start
        actor = read_dfm_packed_int(r)[0] if r.bit() else 0
        items = []
        if r.bit():
            size = r.uint16()
            if size > 4096:
                raise BitError('contents item count')
            for _ in range(size):
                item = inventory_item(r, profile=inventory_profile)
                if not item['guid']:
                    raise BitError('zero item GID')
                if method != 'full':
                    item['change'] = r.bits(2)
                    if item['change'] > 2:
                        raise BitError('unknown item change')
                items.append(item)
        spaces = None
        if method in ('full', 'diff_space'):
            spaces = loot_box_data(r) if r.bit() else []
        extra = {'first': False, 'no': 0, 'num': 0}
        if method != 'diff_space' and r.bit():
            extra = {'first': bool(r.bit()), 'no': r.uint8(), 'num': r.uint8()}
        if r.position != end:
            raise BitError('contents trailing bits')
        if method == 'full' and len({i['guid'] for i in items}) != len(items):
            raise BitError('duplicate full item GID')
        return {'actor': actor, 'method': method, 'items': items, 'spaces': spaces, 'extra': extra}
    except (BitError, ValueError, IndexError) as exc:
        raise ContentsDecodeError(str(exc), actor) from exc


class BoxContentsTracker:
    def __init__(self, keep_history=False):
        data = schema()
        self.catalog = data['controller_catalog']
        self.meta = {m['index']: m for m in data['controller_meta']}
        self.fields = {int(k): v for k, v in data['methods'].items()}
        self.field_count = data['field_count']
        self.inventory_profile = data['inventory_profile']
        self.paths, self.channels, self.states, self.removed = {}, {}, {}, {}
        self.profile = None
        self.stats = Counter()
        self.keep_history, self.history = keep_history, {}

    def _state(self, actor):
        return self.states.setdefault(actor, {'seq': -1, 'has_full': False, 'valid': False,
                                             'items': {}, 'spaces': [], 'pending': set(), 'expected': 0})

    def _publish(self, actor):
        if not self.keep_history:
            return
        state = self.states[actor]
        rows = self.history.setdefault(actor, [])
        entry = (state['seq'], deepcopy(state))
        if rows and rows[-1][0] == entry[0]:
            rows[-1] = entry
        else:
            rows.append(entry)

    def forget(self, actor, seq):
        if seq < self.removed.get(actor, -1):
            return
        self.removed[actor] = seq
        if actor in self.states and seq >= self.states[actor]['seq']:
            self.states[actor].update(seq=seq, valid=False, has_full=False, items={}, deleted=True)
            self._publish(actor)

    def apply(self, message, seq):
        actor = message['actor']
        if not actor or actor in self.removed:
            self.stats['null_or_removed_target'] += 1
            return False
        state = self._state(actor)
        if seq < state['seq']:
            self.stats['stale'] += 1
            return False
        proposed = deepcopy(state)
        if message['method'] == 'full':
            proposed.update(items={i['guid']: deepcopy(i) for i in message['items']},
                            has_full=True, valid=True, pending=set(), expected=0)
        else:
            for item in message['items']:
                gid, change = item['guid'], item['change']
                if change == 0:
                    proposed['items'].pop(gid, None)
                elif change == 1:
                    proposed['items'][gid] = deepcopy(item)
                elif gid in proposed['items']:
                    proposed['items'][gid] = deepcopy(item)
                else:
                    # Receiver Modified only updates an existing GID. Without
                    # its baseline we cannot invent a complete container list.
                    proposed['valid'] = False
                    self.stats['missing_modified_gid'] += 1
        if message['spaces'] is not None:
            proposed['spaces'] = deepcopy(message['spaces'])
        extra = message['extra']
        if extra['num']:
            if proposed['expected'] != extra['num']:
                proposed.update(expected=extra['num'], pending=set())
            if message['method'] != 'full':
                proposed['pending'].add(extra['no'])
            count = proposed['expected']
            # Preserve an incomplete label across a gap, even when the last
            # part arrives. Both numbering origins can be recognized exactly.
            if set(range(count)) <= proposed['pending'] or set(range(1,count+1)) <= proposed['pending']:
                proposed.update(expected=0, pending=set())
        proposed.update(seq=seq, extra=extra, method=message['method'], deleted=False)
        self.states[actor] = proposed
        self.stats[message['method']] += 1
        self.stats['items'] += len(message['items'])
        self._publish(actor)
        return True

    def consume(self, bunch):
        try:
            exports, start = exports_prefix(bunch)
            for entry in exports:
                path = entry.get('path')
                if path:
                    self.paths[entry['guid']] = path
                    tail = path.rsplit('.',1)[-1].removeprefix('Default__')
                    if tail == 'BP_SOLPlayerState_C': self.profile = 'current'
                    elif tail == 'BP_DFMPlayerState_C' and self.profile is None: self.profile = 'legacy'
            if bunch.open:
                self.channels.pop(bunch.ch, None)
                r = BitReader(bunch.payload,bunch.pbits); r.position = start
                guid = read_dfm_packed_int(r)[0]
                if bunch.close:
                    if bunch.close_reason == 0 and r.position == r.bit_count:
                        self.forget(guid,bunch.seq)
                    return
                if guid in self.removed and bunch.seq > self.removed[guid]:
                    self.removed.pop(guid)
                bits = np.unpackbits(np.frombuffer(bunch.payload,dtype=np.uint8),bitorder='little')[:bunch.pbits]
                info,start = sna.parse_prefix(bits,bunch.hx,bunch.hm,quantize_bit=True,frags=bunch.frags)
                path = self.paths.get(info.get('arch'),'').rsplit('.',1)[-1].removeprefix('Default__')
                if path == 'BP_DFMPlayerController_C':
                    self.channels[bunch.ch] = info['actor']
            if self.profile != 'current' or bunch.ch not in self.channels:
                return
            for block in tile(bunch.payload,bunch.pbits,start) or []:
                if not block.get('is_actor'):
                    continue
                position = block['payload_start']
                if block.get('has_rep'):
                    _,position,status = repdec6.decode_block(bunch.payload,block,self.catalog,self.meta,value_reader=box_value)
                    if status not in ('exact','short'):
                        continue
                r = BitReader(bunch.payload,block['payload_end']); r.position = position
                while r.position < r.bit_count:
                    field = r.serialize_int(self.field_count)
                    size = read_dfm_packed_int(r)[0]; end = r.position+size
                    if end > r.bit_count:
                        raise BitError('controller field length')
                    if field in self.fields:
                        try:
                            message = decode_contents(bunch.payload,r.position,end,self.fields[field],
                                                      inventory_profile=self.inventory_profile)
                            self.apply(message,bunch.seq)
                        except ContentsDecodeError as exc:
                            self.stats['decode_error'] += 1
                            if exc.actor and exc.actor in self.states and bunch.seq >= self.states[exc.actor]['seq']:
                                self.states[exc.actor].update(seq=bunch.seq,valid=False)
                                self._publish(exc.actor)
                    r.position = end
        except (BitError,sna.Bad,IndexError):
            self.stats['framing_error'] += 1
        finally:
            if bunch.close:
                self.channels.pop(bunch.ch,None)

    def describe(self, actor, names=None, seq=None):
        state = self.states.get(actor)
        if seq is not None:
            rows = self.history.get(actor,[])
            index = bisect_right(rows,seq,key=lambda row:row[0])-1
            state = rows[index][1] if index >= 0 else None
        if state is None:
            return {'contents_known':False,'contents':[],'contents_seq':None,'contents_revealed':False}
        items = []
        for gid,item in state['items'].items():
            iid = item['item_id']; revealed = iid not in (0,PLACEHOLDER_ID)
            quantity = item.get('count',1)
            items.append({'gid':str(gid),'item_id':str(iid) if revealed else None,
                          'name':(names or {}).get(str(iid),'未识别物品') if revealed else '未搜索物品',
                          'count':quantity if quantity >= 0 else None,'revealed':revealed,
                          'attach_position':item['attach_position']})
        items.sort(key=lambda item:(item['attach_position'],item['gid']))
        known = bool(state['has_full'] and state['valid'] and not state['expected'] and not state.get('deleted'))
        return {'contents_known':known,'contents':items,'contents_seq':state['seq'],
                'contents_revealed':known and all(i['revealed'] for i in items),
                'contents_pending':bool(state['expected'])}
