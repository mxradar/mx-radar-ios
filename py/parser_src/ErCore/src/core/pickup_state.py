"""Ground item properties and actor lifetime, including dormant destruction.

A destruction-only open/close bunch addresses an actor by NetGUID on a NEW
channel. Channel closure alone does not establish an inventory transaction.
"""
from collections import Counter
from copy import deepcopy
import json
import math
import asset_crypto
import numpy as np
import sna
import repdec4
import repdec6
from blocks import tile
from inventory_native import inventory_item
from replication_header import exports_prefix
from codec.bitstream import BitReader, BitError
from codec.engine_bunch import read_dfm_packed_int
from weapon_state import fast_array


def _scan_pickup_item_id(bits, lo=40, hi=640):
    """Scan a pickup actor block for the decimal item-ID FName written on the wire.

    The current build no longer labels the FName with the 0904 handle layout, so
    the block is scanned for the FName shape: u32 length followed by decimal
    digits and a NUL byte.  Callers validate the result against the item table.
    """
    n = len(bits)
    end = min(n, hi)
    for off in range(lo, max(lo, end - 48)):
        ln = 0
        for i in range(32):
            ln |= int(bits[off + i]) << i
        if not (8 <= ln <= 20):
            continue
        base = off + 32
        if base + ln * 8 > n:
            continue
        chars = []
        ok = True
        for k in range(ln):
            by = 0
            for j in range(8):
                by |= int(bits[base + k * 8 + j]) << j
            if k < ln - 1:
                if not (48 <= by <= 57):
                    ok = False
                    break
                chars.append(chr(by))
            elif by != 0:
                ok = False
                break
        if ok and len(chars) >= 7:
            return "".join(chars)
    return None


def pickup_value(r, t, meta=None):
    if meta:
        if meta.get('name') == 'SourceType':
            value = r.bits(3)  # FEnumProperty EPickupSourceType::MAX=4
            if value >= 4: raise BitError('pickup source enum')
            return value
        if meta.get('enum_max'):
            return r.serialize_int(meta['enum_max'])
        if meta.get('struct') == 'InventoryItemInfo':
            return inventory_item(r)
    return repdec4.read_val(r, t, meta)


class PickupTracker:
    def __init__(self, archs=None, names=None, item_meta=None, keep_history=False):
        self.catalog = json.loads(asset_crypto.read_text(repdec4.ROOT / 'src/core' / 'pickup_schema_0904.json'))
        self.meta = {m['index']: m for m in self.catalog['meta']}
        self.parents = {p['index']: p['name'] for p in self.catalog['parents']}
        self.inventory_catalog = self.catalog['inventory_catalog']
        self.inventory_meta = {m['index']: m for m in self.catalog['inventory_meta']}
        self.archs = archs if archs is not None else set()
        self.names, self.item_meta = names or {}, item_meta or {}
        self.paths, self.channels, self.records = {}, {}, {}
        self.inventory_owners, self.inventory_arrays = {}, {}
        self.inventory_channels = set()
        self.inventory_profile = None
        self.active, self.removed = {}, {}
        self.stats = Counter()
        self.keep_history, self.history = keep_history, {}

    def _publish(self, actor):
        record = self.records[actor]
        if record.get('gone_seq') is not None:
            self.active.pop(actor, None)
            self.removed[actor] = record
        elif record.get('loc') is not None:
            self.removed.pop(actor, None)
            self.active[actor] = record
        if self.keep_history:
            self.history.setdefault(actor, []).append((record['update_seq'], deepcopy(record)))

    def at(self, actor, seq):
        import bisect
        rows = self.history.get(actor, [])
        i = bisect.bisect_right(rows, seq, key=lambda row: row[0]) - 1
        return rows[i][1] if i >= 0 else None

    def remove(self, actor, seq, reason, evidence):
        rec = self.records.get(actor)
        if rec is None or seq < rec.get('lifecycle_seq', rec.get('seq', -1)):
            return
        rec.update(gone_seq=seq, gone_reason=reason, update_seq=max(seq, rec['update_seq']), lifecycle_seq=seq,
                   pickup_confidence='removed', removal_evidence=evidence)
        if reason in (0, 'property') and rec.get('inventory_transfer'):
            rec['pickup_confidence'] = 'confirmed'
        self.stats[evidence] += 1
        self._publish(actor)

    def close_channel(self, channel, seq, reason):
        actor = self.channels.pop(channel, None)
        if actor is None:
            return
        if reason in (0, 2, 4):
            self.remove(actor, seq, reason, {0:'destroyed', 2:'level_unloaded', 4:'tear_off'}[reason])
        else:
            rec = self.records.get(actor)
            if rec and seq >= rec['update_seq']:
                rec.update(channel_state={1:'dormant', 3:'irrelevant'}.get(reason, 'closed'), update_seq=seq)
                self._publish(actor)

    def consume(self, x):
        try:
            exports, start = exports_prefix(x)
            for e in exports:
                if e.get('path'):
                    self.paths[e['guid']] = e['path']
                    if e['path'] == 'InventoryManager' and e.get('outer'):
                        self.inventory_owners[e['guid']] = e['outer']
                    if 'BP_SOLPlayerState' in e['path']:
                        self.inventory_profile = '0904'
                    if 'BP_InventoryPickup' in e['path']:
                        self.archs.add(e['guid'])
            if x.open:
                self.channels.pop(x.ch, None)
                self.inventory_channels.discard(x.ch)
                if x.close:
                    if x.close_reason == 0:
                        r = BitReader(x.payload, x.pbits); r.position = start
                        actor = read_dfm_packed_int(r)[0]
                        if r.position == x.pbits:
                            self.remove(actor, x.seq, 0, 'destruction_info')
                    return
                bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder='little')[:x.pbits]
                info, start = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=True, frags=x.frags)
                if info.get('arch') not in self.archs:
                    name = self.paths.get(info.get('arch'), '')
                    if 'Character' not in name or 'Weapon' in name:
                        return
                    start += 8
                    self.inventory_channels.add(x.ch)
                else:
                    actor = info.get('actor')
                    if not actor: return
                    self.channels[x.ch] = actor
                    rec = self.records.setdefault(actor, {'seq': x.seq, 'update_seq': x.seq,
                        'gone_seq': None, 'gone_reason': None, 'pickup_confidence': 'visible'})
                    if x.seq < rec['update_seq']: return
                    if rec.get('gone_seq') is not None:
                        rec.clear()
                        rec['seq'] = x.seq
                    rec.update(update_seq=x.seq, lifecycle_seq=x.seq, gone_seq=None, gone_reason=None,
                               pickup_confidence='visible', channel_state='open')
                    loc = info.get('loc')
                    if loc and all(math.isfinite(v) for v in loc):
                        rec['loc'] = list(loc)
                    self._publish(actor)
            actor = self.channels.get(x.ch)
            if actor is None and (self.inventory_profile != '0904' or x.ch not in self.inventory_channels):
                return
            for block in tile(x.payload, x.pbits, start) or []:
                if block.get('deleted'): continue
                guid = block.get('sub') if block.get('stably') else block.get('cls')
                if self.paths.get(guid) == 'InventoryManager' and self.inventory_profile == '0904':
                    self.apply_inventory(guid, x.seq, x.payload, block)
                elif actor is not None and block.get('has_rep') and block.get('is_actor'):
                    self.apply_block(actor, x.seq, x.payload, block)
        except (BitError, sna.Bad):
            self.stats['framing_error'] += 1
        finally:
            if x.close:
                self.close_channel(x.ch, x.seq, x.close_reason)
                self.inventory_channels.discard(x.ch)

    def apply_inventory(self, component, seq, payload, block):
        if not block.get('has_rep'):
            return
        _, pos, status = repdec6.decode_block(payload, block, self.inventory_catalog,
                                              self.inventory_meta)
        if status not in ('exact', 'short'):
            self.stats['inventory_layout_' + status] += 1
            return
        r = BitReader(payload, block['payload_end']); r.position = pos
        try:
            while r.position < r.bit_count:
                field = r.serialize_int(52)
                size = read_dfm_packed_int(r)[0]
                end = r.position + size
                if end > r.bit_count: raise BitError('inventory field length')
                if field in (7, 8):
                    batch = fast_array(payload, r.position, end, inventory_item)
                    state = self.inventory_arrays.setdefault((component, field), {'seq': -1, 'items': {}})
                    if seq >= state['seq']:
                        if batch['base'] == 0: state['items'].clear()
                        for rid in batch['deleted']: state['items'].pop(rid, None)
                        for rid, item in batch['items']:
                            state['items'][rid] = item
                            self.inventory_transfer(component, seq, item)
                        state['seq'] = seq
                    self.stats['inventory_fast_array'] += 1
                r.position = end
        except repdec4.Opaque:
            self.stats['inventory_native_opaque'] += 1
        except BitError:
            self.stats['inventory_error'] += 1

    def inventory_transfer(self, component, seq, item):
        owner = self.inventory_owners.get(component)
        if not owner or item.get('attach_position') in (None, 0, 301):
            return
        for actor, rec in self.records.items():
            if (rec.get('inventory_gid') != item['guid'] or rec.get('seq', seq) > seq
                    or rec.get('item_id') != str(item['item_id'])):
                continue
            previous = rec.get('inventory_transfer', {})
            if seq < previous.get('seq', -1): continue
            rec['inventory_transfer'] = {'actor': owner, 'seq': seq,
                                         'count': item.get('count', 1),
                                         'attach_position': item['attach_position']}
            if rec.get('gone_seq') is not None and rec.get('gone_reason') in (0, 'property'):
                rec['pickup_confidence'] = 'confirmed'
            rec['update_seq'] = max(seq, rec['update_seq'])
            self.stats['inventory_gid_match'] += 1
            self._publish(actor)

    def apply_block(self, actor, seq, payload, block):
        ev, pos, status = repdec6.decode_block(payload, block, self.catalog, self.meta,
                                              value_reader=pickup_value)
        self.stats['layout_' + status] += 1
        rec = self.records[actor]
        if seq < rec['update_seq']: return
        # Early, independent scalar properties remain useful with an unknown
        # version of the native inventory body. Never trust fields after it
        # unless the complete RepLayout terminates at the block boundary.
        complete = status == 'exact'
        usable = status in ('exact', 'leftover', 'short')
        props = {self.parents[e['parent_index']]: e['value'] for e in ev
                 if not e['path'] and (usable or e['handle'] < 67)}
        old_gid = rec.get('inventory_gid')
        if old_gid and props.get('InventoryGID', old_gid) != old_gid:
            loc = rec.get('loc')
            rec.clear()
            rec.update(seq=seq, loc=loc, gone_seq=None, gone_reason=None,
                       pickup_confidence='visible')
        mapping = {'InventoryGID':'inventory_gid','SourceType':'source_type',
                   'StackCount':'stack_count','Durability':'durability',
                   'ItemState':'item_state','bIsHiddenItem':'hidden',
                   'WhoDropThisPlayerId':'dropper_id','LastOwnedPlayerId':'last_owner_id'}
        for name, key in mapping.items():
            if name in props: rec[key] = props[name]
        iid = (props.get('InventoryIdName') or {}).get('text')
        if iid and iid.isdecimal(): rec['item_id'] = iid
        native = props.get('PickupItemInfo')
        if native:
            rec['inventory_info'] = native
            rec['item_id'] = str(native['item_id'])
            rec['inventory_gid'] = native['guid']
        if not rec.get('item_id'):
            p0 = block['payload_start']
            p1 = min(block['payload_end'], p0 + 1024)
            bits = ''.join('1' if (payload[p >> 3] >> (p & 7)) & 1 else '0'
                           for p in range(p0, p1))
            scanned = _scan_pickup_item_id(bits)
            if scanned and (scanned in self.names or scanned in self.item_meta):
                rec['item_id'] = scanned
                rec['item_id_source'] = 'bit_scan'
        meta = self.item_meta.get(rec.get('item_id'), {})
        rec.update(update_seq=seq, lifecycle_seq=seq, decode_status=status, name=self.names.get(rec.get('item_id')),
                   grade=meta.get('grade'), price=meta.get('price'))
        if complete and (rec.get('hidden') or rec.get('item_state') == 5 or rec.get('stack_count') == 0):
            self.remove(actor, seq, 'property', 'item_unavailable')
        else:
            if complete and (props.get('ItemState') == 1 or props.get('bIsHiddenItem') is False):
                rec.update(gone_seq=None, gone_reason=None, pickup_confidence='visible')
            self._publish(actor)
