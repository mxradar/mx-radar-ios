"""Ordinary container spawn locations. Unknown contents stay explicitly unknown.
Only exported container class names and decoded actor locations are accepted.
"""
import math
import numpy as np
import sna
from replication_header import exports_prefix
from codec.bitstream import BitReader, BitError
from codec.engine_bunch import read_dfm_packed_int


class ContainerTracker:
    def __init__(self, paths):
        self.paths = paths
        self.channels = {}
        self.active = {}

    def consume(self, x):
        try:
            if x.open and x.close:
                if x.close_reason == 0:
                    _, start = exports_prefix(x)
                    r = BitReader(x.payload, x.pbits); r.position = start
                    actor = read_dfm_packed_int(r)[0]
                    if r.position == x.pbits:
                        self.active.pop(actor, None)
                return
            if x.open:
                self.channels.pop(x.ch, None)
                bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder='little')[:x.pbits]
                info, _ = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=True, frags=x.frags)
                path = self.paths.get(info.get('arch'), '')
                tail = path.rsplit('.', 1)[-1].rsplit('/', 1)[-1]
                if not tail.startswith(('BP_InteractorContainer_', 'BP_LootContainer_')):
                    return
                actor, loc = info.get('actor'), info.get('loc')
                if not actor or not loc or len(loc) != 3 or not all(math.isfinite(v) for v in loc):
                    return
                self.channels[x.ch] = actor
                self.active[actor] = dict(key='container-'+str(actor), eid=actor, ch=x.ch,
                    kind='container', name='搜索容器', world=list(loc), xyz=list(loc), yaw=None,
                    class_path=path, contents=[], contents_known=False, status='内容未知',
                    decode_status='spawn_location_only', updated_seq=x.seq)
            elif x.close:
                actor = self.channels.pop(x.ch, None)
                if x.close_reason in (0, 2, 4):
                    self.active.pop(actor, None)
                elif actor in self.active:
                    self.active[actor]['channel_state'] = 'dormant'
        except (sna.Bad, BitError, ValueError, IndexError):
            return
