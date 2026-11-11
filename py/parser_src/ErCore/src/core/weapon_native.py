"""Native weapon serializers recovered from the 0904 runtime CppStructOps.

All counts are checked before allocation; no scan or bit-position recovery.
"""
from codec.bitstream import BitError
from codec.engine_bunch import read_dfm_packed_int, read_serialized_fname


def _count(r, limit=512):
    n = r.uint32()
    if n > limit:
        raise BitError('weapon array count %d' % n)
    return n


def weapon_skin(r):
    # 0x1196B0A0: bool32 default; only a non-default skin has a body.
    if r.uint32():
        return {'default': True}
    names = ('skin_gid', 'skin_id', 'appearance_id', 'origin_avatar_id',
             'mystical_id', 'color_id', 'wear', 'material_seed',
             'unique_number', 'kill_count')
    d = {key: r.uint64() for key in names}
    d['custom_name'] = r.fstring()
    d['pendant_id'] = r.uint64()
    d['pendant_guid'] = r.uint64()
    for key in ('suit_active', 'lock_appearance', 'enemy_visible', 'teammate_visible'):
        d[key] = bool(r.uint32())
    d['pendant_appearances'] = [
        {'mystical_id': r.uint64(), 'material_seed': r.uint64()}
        for _ in range(_count(r))]
    return d


def weapon_part(r):
    # 0x10B54FD4 -> 0x10B55154 (parent socket information).
    d = {'item_id': r.uint64(), 'guid': r.uint64(), 'node_type': r.uint8()}
    parent = {'id': r.uint32()}
    name_omitted, has_guid = r.uint32(), r.uint32()
    if not name_omitted:
        parent['name'] = read_serialized_fname(r).as_dict()
    if has_guid:
        parent['guid'] = [r.uint64(), r.uint64()]
    parent['index'] = r.uint32()
    d['parent'] = parent
    for key in ('exclude_3p', 'exclude_1p', 'exclude_ui'):
        d[key] = bool(r.bit())
    return d


def weapon_extra(r):
    # 0x10B54978; the FastArraySerializerItem header is not serialized here.
    d = {'identity': r.uint32(), 'weapon_id': r.uint64()}
    d['guid'] = d['weapon_id'] if r.bit() else r.uint64()
    d['equip_position'] = read_dfm_packed_int(r)[0]
    d['skill_id'] = read_dfm_packed_int(r)[0]
    d['slot'] = r.uint8()
    d['parts'] = [weapon_part(r) for _ in range(_count(r))]
    d['poor_item_id'] = 0 if r.bit() else r.uint64()
    d['skin'] = weapon_skin(r)
    d['ammo_logic'] = r.uint8()
    return d


def part_node_attributes(r):
    # 0x1133665C, runtime name-compression setting is 1 in this protocol.
    values = [r.float32() for _ in range(_count(r))]
    indices = [r.uint32() for _ in range(_count(r))]
    names = [read_serialized_fname(r).as_dict() if i == 0 else
             {'registry_index': i} for i in indices]
    return {'values': values, 'names': names}


def weapon_dynamic_base(r):
    # 0x113361A4, called through CppStructOps and the struct's vtable.
    return {'part_guids': [r.uint64() for _ in range(_count(r))],
            'node_attributes': [part_node_attributes(r) for _ in range(_count(r))]}


def weapon_dynamic(r):
    # 0x11335BB4: base, optional owner, ammo context, three overheat floats.
    d = weapon_dynamic_base(r)
    d['owner'] = 0 if r.uint32() else r.uint64()
    ammo = []
    for _ in range(_count(r)):
        ammo.append({'item_id': r.uint64(), 'guid': r.uint64(),
                     'count': r.int32(), 'owner': r.uint64(),
                     'owners': [r.uint64() for _ in range(_count(r))],
                     'flag': bool(r.uint32())})
    d['ammo'], d['overheat'] = ammo, [r.float32() for _ in range(3)]
    return d
