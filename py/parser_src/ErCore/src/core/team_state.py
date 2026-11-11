"""Actor-scoped team deltas from the replicated character TeamComponent."""
from collections import Counter

from codec.bitstream import BitError
from replication_header import exports_prefix
import repdec4
import repdec6


class TeamTracker:
    def __init__(self):
        self.catalog = repdec4.load_catalogs()['GPTeamComponent']
        self.paths = {}
        self.channels = {}
        self.actors = {}
        self.stats = Counter()
        self.local_spawn = None

    def consume(self, x):
        import numpy as np
        import sna
        from blocks import tile
        try:
            exports, start = exports_prefix(x)
            for entry in exports:
                if entry.get('path'):
                    self.paths[entry['guid']] = entry['path']
            if x.open:
                self.channels.pop(x.ch, None)
                if x.close:
                    return
                bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8),
                                     bitorder='little')[:x.pbits]
                info, start = sna.parse_prefix(bits, x.hx, x.hm,
                                              quantize_bit=True, frags=x.frags)
                # Match the exact TeamComponent block below, including local
                # controller / PlayerState components without a Character slot.
                start += 8
                self.channels[x.ch] = info['actor']
                if x.ch == 3:
                    self.local_spawn = x
            actor = self.channels.get(x.ch)
            if actor is None:
                return
            for block in tile(x.payload, x.pbits, start) or []:
                guid = block.get('sub') if block.get('stably') else block.get('cls')
                path = self.paths.get(guid, '').rsplit('/', 1)[-1]
                if path not in ('TeamComponent', 'GPTeamComponent'):
                    continue
                if not block.get('has_rep') or block.get('deleted'):
                    continue
                self.apply_block(actor, x.seq, x.payload, block)
        except (BitError, sna.Bad):
            self.stats['framing_error'] += 1
        finally:
            if x.close:
                self.channels.pop(x.ch, None)
                if x.ch == 3:
                    self.local_spawn = None

    def apply_block(self, actor, seq, payload, block):
        events, _, status = repdec6.decode_block(payload, block, self.catalog, {})
        self.stats[status] += 1
        if status != 'exact':
            return False
        state = self.actors.setdefault(actor, {'values': {}, 'seq': {}})
        for event in events:
            name = {2: 'TeamID', 3: 'CampId'}.get(event['cmd_index'])
            if name and seq >= state['seq'].get(name, -1):
                state['values'][name] = event['value']
                state['seq'][name] = seq
        return True

    def current(self, actor):
        return self.actors.get(actor, {}).get('values', {})
