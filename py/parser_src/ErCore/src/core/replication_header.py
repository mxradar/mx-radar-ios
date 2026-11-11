"""Package-map prefix only; ordinary delta bunches have no NewActor header."""
import numpy as np
import sna


def exports_prefix(x):
    if not (x.hx or x.hm):
        return [], 0
    frags = getattr(x, "frags", None)
    key=(x.payload,x.pbits,x.hx,x.hm,tuple(tuple(f) for f in (frags or [])))
    cached=getattr(x,'_exports_prefix_cache',None)
    if cached is not None and cached[0]==key:
        return [entry.copy() for entry in cached[1]],cached[2]
    bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8),
                         bitorder="little")[:x.pbits]
    exports = []
    pos = 0
    if x.hx:
        if frags:
            for start, hx, _ in frags:
                if hx:
                    _, pos = sna.read_exports2(bits, start, exports)
        else:
            _, pos = sna.read_exports2(bits, pos, exports)
    if x.hm:
        start = next((s for s, _, hm in (frags or []) if hm), pos)
        n = sna.rbits(bits, start, 16)
        if n > 256:
            raise sna.Bad("too many must-map GUIDs")
        pos = start + 16
        for _ in range(n):
            _, pos = sna.packed(bits, pos)
    # Several independent property decoders inspect the same immutable bunch.
    # Its prefix is decoded once; the cache dies with this room's bunch.
    try:x._exports_prefix_cache=(key,tuple(entry.copy() for entry in exports),pos)
    except AttributeError:pass
    return exports, pos
