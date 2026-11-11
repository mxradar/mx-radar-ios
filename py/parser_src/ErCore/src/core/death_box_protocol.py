"""Death-body RepLayout readers, validated against 440 recorded actor blocks."""
from functools import lru_cache
import json

import asset_crypto
import repdec4
import repdec6
from pickup_state import pickup_value
from codec.bitstream import BitError
from codec.engine_bunch import read_serialized_fname


def text_value(reader):
    flags, history = reader.uint32(), reader.uint8()
    out = {'flags': flags, 'history': history}
    if history == 11:
        out.update(table=read_serialized_fname(reader).as_dict(), key=reader.fstring())
    elif history == 0:
        out.update(namespace=reader.fstring(), key=reader.fstring(), source=reader.fstring())
    elif history == 255:
        present = reader.uint32()
        if present not in (0, 1):
            raise BitError('FText invariant presence')
        out['source'] = reader.fstring() if present else ''
    else:
        raise repdec4.Opaque('FText history %d' % history)
    return out


def box_value(reader, kind, meta):
    meta = meta or {}
    if meta.get('struct') == 'LootBoxRepData':
        from inventory_native import loot_box_data
        return loot_box_data(reader)
    if meta.get('prop_class') == 'EnumProperty' and meta.get('enum_max') is not None:
        return reader.bits(int(meta['enum_max']).bit_length())
    if meta.get('prop_class') == 'TextProperty':
        return text_value(reader)
    if meta.get('struct') == 'GameplayTagContainer':
        if reader.bit():
            return []
        return [read_serialized_fname(reader).as_dict() for _ in range(reader.bits(6))]
    if kind == 15:
        # Both captured class defaults use whole-number location/velocity and
        # byte rotations. Preserve the optional physics angular velocity.
        flags = reader.bits(2)
        result = {'location': repdec4.pv(reader, 1., 24),
                  'rotation': repdec4.rot_c(reader), 'velocity': repdec4.pv(reader, 1., 24),
                  'physics_sleep': bool(flags & 1), 'rep_physics': bool(flags & 2)}
        if flags & 2:
            result['angular_velocity'] = repdec4.pv(reader, 1., 24)
        return result
    return pickup_value(reader, kind, meta)


@lru_cache(maxsize=2)
def schemas(profile):
    name = 'death_box_schema_0904.json' if profile == 'current' else 'death_box_schema_legacy.json'
    return json.loads(asset_crypto.read_text(repdec4.ROOT / 'src/core' / name))['classes']


ALIASES = {'AttachmentReplication.AttachParent': 'AttachParent',
           'AttachmentReplication.AttachComponent': 'AttachComponent',
           'AttachmentReplication.LocationOffset': 'LocationOffset',
           'AttachmentReplication.RotationOffset': 'RotationOffset',
           'DeadBodyBoxInfo.DeadBodyCharacterOwner': 'DeadBodyCharacterOwner',
           'DeadBodyBoxInfo.CharacterUin': 'CharacterUin',
           'DeadBodyBoxInfo.CarryBoxCharacterUin': 'CarryBoxCharacterUin',
           'DeadBodyBoxInfo.DeadBodyBoxStatus': 'DeadBodyBoxStatus'}


class DeathBoxDecoder:
    """A connection owns its profile; shared caches contain only immutable catalogs."""
    def __init__(self):
        self.profile = None

    def observe_exports(self, exports):
        for entry in exports:
            path = entry.get('path', '')
            if path in ('Default__BP_SOLPlayerState_C', 'BP_SOLPlayerState_C'):
                self.profile = 'current'
            elif path in ('Default__BP_DFMPlayerState_C', 'BP_DFMPlayerState_C'):
                self.profile = 'legacy'

    def _decode(self, payload, block, name, profile):
        cat = schemas(profile).get(name)
        if cat is None:
            return {'fields': {}, 'status': 'unknown_class', 'profile': profile}
        meta = {m['index']: m for m in cat['meta']}
        events, position, status = repdec6.decode_block(payload, block, cat, meta,
                                                      value_reader=box_value, include_arrays=True)
        out = {'fields': {}, 'arrays': [], 'status': status, 'profile': profile, 'position': position}
        if status not in ('exact', 'leftover', 'short', 'opaque', 'error'):
            return out
        fields, arrays = {}, []
        include_arrays = status == 'exact'
        for event in events:
            record = meta[event['cmd_index']]
            name = '.'.join(record['field_path'])
            if event.get('kind') or event['path']:
                if include_arrays:
                    arrays.append(dict(event, name=name))
            else:
                fields[name] = event['value']
                if name in ALIASES:
                    fields[ALIASES[name]] = event['value']
        out.update(fields=fields, arrays=arrays)
        return out

    def __call__(self, payload, block, class_name):
        name = class_name.rsplit('.', 1)[-1].removeprefix('Default__')
        if self.profile:
            return self._decode(payload, block, name, self.profile)
        candidates = [self._decode(payload, block, name, p) for p in ('legacy', 'current')]
        exact = [c for c in candidates if c['status'] == 'exact']
        if len(exact) == 1:
            self.profile = exact[0]['profile']
            return exact[0]
        if len(exact) == 2 and exact[0]['fields'] == exact[1]['fields'] and not exact[0]['arrays'] and not exact[1]['arrays']:
            return dict(exact[0], profile='common')
        return {'fields': {}, 'arrays': [], 'status': 'ambiguous_profile' if exact else 'unresolved_profile'}
