"""InventoryItemInfo native wire body (runtime 0x123BCD40).

Optional class-specific bodies stop explicitly until their serializer is known.
"""
from codec.engine_bunch import read_serialized_fname
from weapon_native import _count, weapon_dynamic


def loot_box_data(r):
    """FLootObjRepData 0x116C598C / FItemSpaceRepData 0x116C5C64.

    Only grid geometry is transmitted; the remaining FGridSize runtime fields
    are rebuilt locally by the receiver, not serialized as 0x60 raw bytes.
    """
    from codec.bitstream import BitError
    def count(limit):
        value = r.int32()
        if not 0 <= value <= limit:
            raise BitError('loot space count')
        return value
    def signed_byte():
        value = r.uint8()
        return value - 256 if value >= 128 else value
    spaces = []
    for _ in range(count(128)):
        space = {'space_type': r.int32(), 'update_count': r.int32(), 'grids': []}
        for _ in range(count(1024)):
            grid = dict(zip(('id', 'x', 'y', 'width', 'height'), (r.int32() for _ in range(5))))
            grid.update(half_x=signed_byte(), half_y=signed_byte())
            space['grids'].append(grid)
        spaces.append(space)
    return spaces


def _unsupported(name):
    from repdec4 import Opaque
    raise Opaque('InventoryItemInfo ' + name)


def inventory_gun_part(r):
    # 0x113388C8. These fixed fields precede the two owner/quantity arrays.
    widths = ((0x18,64),(0x20,16),(0x28,64),(0x30,64),(0x38,16),
              (0x3c,32),(0x40,32),(0x44,16),(0x46,8),(0x48,32),
              (0x4c,32),(0x50,8),(0x58,64),(0x60,32))
    d = {'field_%x' % offset: r.bits(bits) for offset, bits in widths}
    d['owners'] = [r.uint64() for _ in range(_count(r))]
    d['counts'] = [r.uint32() for _ in range(_count(r))]
    return d


def inventory_gun(r):
    # 0x113381B8: unlike the weapon component's part list, count is u8.
    d = {'parts': [inventory_gun_part(r) for _ in range(r.uint8())],
         'dynamic': weapon_dynamic(r)}
    for offset in (0x60,0x68,0x70,0x88,0x80,0xa0,0xb0,0xb8,0x108):
        d['field_%x' % offset] = r.uint64()
    d['flag_148'] = bool(r.uint32())
    d['field_d8'], d['field_f0'] = r.uint64(), r.uint64()
    d['custom_name'] = r.fstring()
    return d


def inventory_archive(r):
    """Current FInventoryItemInfo archive branch, native 0x11efce14.

    FArchive's name and text operators keep their normal network encodings;
    runtime object padding and shared-pointer bookkeeping are not on the wire.
    """
    from death_box_protocol import text_value
    d = {'name_2e8': r.fstring(),
         'name_2f8': read_serialized_fname(r).as_dict(),
         'field_300': r.uint32(), 'field_308': r.uint32(),
         'field_548': r.uint8(), 'text_550': text_value(r)}
    for offset in (0x568, 0x578, 0x570, 0x580):
        d['field_%x' % offset] = r.uint64()
    d['field_588'] = r.uint32()
    d['field_304'] = r.uint8()
    d['field_590'] = r.uint64()
    d['flag_598'] = bool(r.uint32())
    d['field_5a0'] = r.uint64()
    d['field_5a8'] = r.uint32()
    d['field_5b0'] = r.uint64()
    d['flag_5b8'] = bool(r.uint32())
    return d


def inventory_item(r, *, profile='0904'):
    d = {'item_id': r.uint32() * 10000 + r.uint32(), 'guid': r.uint64()}
    d['attach_position'] = r.uint32()
    d['space_location'] = [r.uint8() for _ in range(4)]
    d['flag_78'] = bool(r.uint32())
    d['field_79'] = r.uint8()
    for offset, bits in ((0x608,32),(0x60c,8),(0x620,64),(0x63c,32),
                         (0x640,64),(0x628,8),(0x630,64),(0x638,32),(0x5f0,32)):
        d['field_%x' % offset] = r.bits(bits)
    for key in ('ids_5e0', 'ids_5f8'):
        d[key] = [r.uint64() for _ in range(_count(r))]
    if r.bit():
        d['count'], d['max_count'] = r.int32(), r.int32()
        d['stack_guids'] = [r.uint64() for _ in range(_count(r))]
        d['stack_counts'] = [r.int32() for _ in range(_count(r))]
    if r.bit():
        d['durability'], d['max_durability'] = r.float32(), r.float32()
    if r.bit():
        d['armor'], d['max_armor'] = r.float32(), r.float32()
    if r.bit(): d['gun'] = inventory_gun(r)
    if r.bit():
        if profile != '20261106': _unsupported('archive')
        d['archive'] = inventory_archive(r)
    if r.bit():
        d['keychain_spaces'] = [
            {'shape': [r.uint8() for _ in range(3)],
             'occupied': [r.uint8() for _ in range(_count(r))],
             'reserved': [r.uint8() for _ in range(_count(r))]}
            for _ in range(_count(r))]
    if r.bit():
        d['name_538'], d['id_548'] = r.fstring(), r.uint64()
    d['id_648'] = r.uint64()
    if r.bit():
        d['field_6d8'] = r.uint32()
        d['field_6d0'], d['field_6e8'] = r.uint64(), r.uint64()
    if r.bit():
        d['ids_570'] = [r.uint64() for _ in range(_count(r))]
        d['map_580'] = [(r.uint64(), r.uint64()) for _ in range(_count(r))]
        d['kill_index'], d['evacuate_count'] = r.int32(), r.uint64()
    if r.bit():
        d['named_ids'] = [(r.uint64(), r.fstring()) for _ in range(_count(r))]
    d['set_750'] = [r.uint32() for _ in range(_count(r))]
    if r.bit():
        d['flags_7a0'] = [bool(r.uint32()), bool(r.uint32())]
        d['names_7a8'] = [r.fstring(), r.fstring()]
        d['ids_7c8'] = [r.uint64(), r.uint64()]
    d['field_7d8'] = r.int32()
    d['flag_6a0'] = bool(r.bit())
    if r.bit(): d['fields_6a4'] = [r.int32(), r.int32()]
    if r.bit(): d['fields_6ac'] = [r.int32(), r.int32(), bool(r.bit())]
    if _count(r): _unsupported('polymorphic extensions')
    if r.bit(): d['fields_7dc'] = [r.int32(), r.uint8()]
    if r.bit(): d['name_7e8'] = r.fstring()
    if r.bit(): d['field_7f8'] = r.int32()
    if r.bit(): d['field_7fc'] = r.int32()
    if profile == '20261106':
        # Current CppStructOps NetSerialize ends with a presence bit followed
        # by an optional uint64 at runtime offset 0x6d8 (0x11f002xx).
        d['tail_6d8_present'] = bool(r.bit())
        if d['tail_6d8_present']: d['tail_6d8'] = r.uint64()
    return d
