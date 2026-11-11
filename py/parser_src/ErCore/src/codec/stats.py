"""Small shared stats helpers."""
from __future__ import annotations

import math


def entropy(data: bytes) -> float:
    """Shannon entropy per byte (0.0-8.0). >=7.8 usually means
    encrypted or compressed payload."""
    if not data:
        return 0.0
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    n = len(data)
    h = 0.0
    for c in counts:
        if c:
            p = c / n
            h -= p * math.log2(p)
    return h
