"""Heuristic property decoders for character spawn / movement payloads.

These are UE4-default layouts, config-driven, and intentionally
experimental: the analyzer reports every attempt so the real layout can
be pinned once captured traffic is available.
"""
from __future__ import annotations
import math
import struct

from .bitstream import BitReader, BitError


def _float(v: int) -> float:
    return struct.unpack("<f", (v & 0xFFFFFFFF).to_bytes(4, "little"))[0]


def in_bounds(pos: dict, bounds: dict | None) -> bool:
    if not bounds:
        return all(math.isfinite(v) for v in pos.values())
    x, y, z = pos["x"], pos["y"], pos["z"]
    return (
        bounds["x"][0] <= x <= bounds["x"][1]
        and bounds["y"][0] <= y <= bounds["y"][1]
        and bounds["z"][0] <= z <= bounds["z"][1]
        and all(math.isfinite(v) for v in (x, y, z))
    )


def plausible_pos(pos: dict, bounds: dict | None) -> bool:
    """Bounds + sanity: above ground, non-degenerate coordinates.

    Rejects degenerate readings like a quantized int32 payload misread
    as floats (~1e-40 components)."""
    return (
        in_bounds(pos, bounds)
        and pos["z"] >= 0.1
        and (abs(pos["x"]) > 0.5 or abs(pos["y"]) > 0.5 or pos["z"] > 10.0)
    )


def _read_rot_floats(r: BitReader) -> list[float]:
    return [r.float32(), r.float32(), r.float32()]


def try_parse_spawn_multi(
    payload: bytes,
    payload_bit_count: int,
    bounds: dict | None,
    packed_variant: str = "dfm",
    layouts: list | None = None,
) -> list[dict]:
    """Try several spawn layouts (UE4 standard + quantized variants):

    - "floats":         Location(3 f32) + Rotation(3 f32)  [UE4 default]
    - "int32_cm":       Location(3 int32 * 0.01) + rot f32
    - "int32_cm_packed":packed int32 * 0.01 + rot f32
    - "int32_dm":       int32 * 0.1 + rot f32
    - "int32_dm_packed":packed int32 * 0.1 + rot f32

    Returns all bounds-valid candidates, each tagged with "layout".
    """
    layouts = layouts or [
        "floats", "int32_cm", "int32_cm_packed", "int32_dm", "int32_dm_packed",
    ]
    out: list[dict] = []
    for lay in layouts:
        r = BitReader(payload, payload_bit_count)
        try:
            if lay == "floats":
                loc = [r.float32(), r.float32(), r.float32()]
            elif lay == "int32_cm":
                loc = [r.int32() * 0.01 for _ in range(3)]
            elif lay == "int32_cm_packed":
                loc = [r.packed_int(packed_variant) * 0.01 for _ in range(3)]
            elif lay == "int32_dm":
                loc = [r.int32() * 0.1 for _ in range(3)]
            elif lay == "int32_dm_packed":
                loc = [r.packed_int(packed_variant) * 0.1 for _ in range(3)]
            else:
                continue
            rot = _read_rot_floats(r)
        except BitError:
            continue
        if not all(math.isfinite(v) for v in loc + rot):
            continue
        pos = {
            "layout": lay,
            "x": loc[0], "y": loc[1], "z": loc[2],
            "pitch": rot[0], "yaw": rot[1], "roll": rot[2],
        }
        if plausible_pos(pos, bounds):
            out.append(pos)
    return out


def try_parse_spawn(
    payload: bytes,
    payload_bit_count: int,
    bounds: dict | None,
    packed_variant: str = "dfm",
) -> dict | None:
    """Back-compat: first candidate of the multi-layout attempt."""
    cands = try_parse_spawn_multi(
        payload, payload_bit_count, bounds, packed_variant, ["floats"]
    )
    return cands[0] if cands else None


def _read_quantized_vector(r: BitReader, bits: int, scale: float) -> list[float]:
    return [r.bits(bits) * scale for _ in range(3)]


def try_parse_rep_movement(
    payload: bytes,
    payload_bit_count: int,
    bounds: dict | None,
    cfg: dict,
) -> dict | None:
    """UE FRepMovement-style update: quantized Location/Rotation/Velocity."""
    loc_bits = int(cfg.get("loc_bits", 20))
    loc_scale = float(cfg.get("loc_scale", 0.01))
    rot_bits = int(cfg.get("rot_bits", 8))
    rot_scale = float(cfg.get("rot_scale", 360.0 / 256.0))
    vel_bits = int(cfg.get("vel_bits", 18))
    vel_scale = float(cfg.get("vel_scale", 1.0))

    needed = (loc_bits + rot_bits + vel_bits) * 3
    if payload_bit_count < needed:
        return None

    r = BitReader(payload, payload_bit_count)
    try:
        loc = _read_quantized_vector(r, loc_bits, loc_scale)
        rot = _read_quantized_vector(r, rot_bits, rot_scale)
        vel = _read_quantized_vector(r, vel_bits, vel_scale)
        flags_bits = payload_bit_count - r.position
        flags = r.bits(min(flags_bits, 64)) if flags_bits else 0
    except BitError:
        return None

    pos = {
        "x": loc[0], "y": loc[1], "z": loc[2],
        "pitch": rot[0], "yaw": rot[1], "roll": rot[2],
        "vx": vel[0], "vy": vel[1], "vz": vel[2],
        "flags": flags,
    }
    if not in_bounds(pos, bounds):
        return None
    return pos


def scan_spawn(
    payload: bytes,
    payload_bit_count: int,
    bounds: dict | None,
    packed_variant: str = "dfm",
    max_offset_bits: int = 128,
) -> list[dict]:
    """Scan bit offsets for a plausible spawn (6 float32) when a direct
    parse fails — real payloads may carry leading guid/flags bits.

    Sanity: x/y/z within bounds, z above ground, |pitch|/|roll| sane.
    Returns [{offset_bits, x, y, z, pitch, yaw, roll}, ...].
    """
    cands: list[dict] = []
    need = 6 * 32
    if payload_bit_count < need:
        return cands
    limit = min(max_offset_bits, payload_bit_count - need)
    for off in range(0, limit + 1):
        r = BitReader(payload, payload_bit_count)
        r.position = off
        try:
            loc = [r.float32(), r.float32(), r.float32()]
            rot = [r.float32(), r.float32(), r.float32()]
        except BitError:
            break
        if not all(math.isfinite(v) for v in loc + rot):
            continue
        pos = {
            "offset_bits": off,
            "x": loc[0], "y": loc[1], "z": loc[2],
            "pitch": rot[0], "yaw": rot[1], "roll": rot[2],
        }
        if not plausible_pos(pos, bounds):
            continue
        if abs(pos["pitch"]) > 720.0 or abs(pos["roll"]) > 720.0:
            continue
        cands.append(pos)
    return cands


def scan_rep_movement(
    payload: bytes,
    payload_bit_count: int,
    bounds: dict | None,
    cfg: dict,
    prev_pos: dict | None = None,
    max_jump: float = 500.0,
    max_offset_bits: int = 128,
) -> list[dict]:
    """Scan bit offsets for a plausible quantized FRepMovement.

    Optional continuity check: distance from prev_pos must be < max_jump
    (metres) — the strongest false-positive filter available.
    """
    loc_bits = int(cfg.get("loc_bits", 20))
    loc_scale = float(cfg.get("loc_scale", 0.01))
    rot_bits = int(cfg.get("rot_bits", 8))
    rot_scale = float(cfg.get("rot_scale", 360.0 / 256.0))
    vel_bits = int(cfg.get("vel_bits", 18))
    vel_scale = float(cfg.get("vel_scale", 1.0))
    needed = (loc_bits + rot_bits + vel_bits) * 3
    cands: list[dict] = []
    if payload_bit_count < needed:
        return cands
    limit = min(max_offset_bits, payload_bit_count - needed)
    for off in range(0, limit + 1):
        r = BitReader(payload, payload_bit_count)
        r.position = off
        try:
            loc = _read_quantized_vector(r, loc_bits, loc_scale)
            rot = _read_quantized_vector(r, rot_bits, rot_scale)
            vel = _read_quantized_vector(r, vel_bits, vel_scale)
        except BitError:
            break
        pos = {
            "offset_bits": off,
            "x": loc[0], "y": loc[1], "z": loc[2],
            "pitch": rot[0], "yaw": rot[1], "roll": rot[2],
            "vx": vel[0], "vy": vel[1], "vz": vel[2],
        }
        if not plausible_pos(pos, bounds):
            continue
        if prev_pos:
            dx = pos["x"] - prev_pos.get("x", 0)
            dy = pos["y"] - prev_pos.get("y", 0)
            dz = pos["z"] - prev_pos.get("z", 0)
            if math.sqrt(dx * dx + dy * dy + dz * dz) > max_jump:
                continue
        cands.append(pos)
    return cands


def parse_package_map_exports_with_offset(
    payload: bytes,
    payload_bit_count: int,
    packed_variant: str = "dfm",
):
    """UE package-map export list (present on channel-open bunches).

    Returns (exports, consumed_bits) where consumed_bits is the reader
    position after the export list (rest of payload = spawn/field data).
    """
    exports: list[dict] = []
    r = BitReader(payload, payload_bit_count)
    try:
        count = r.packed_int(packed_variant)
        if count > 4096:
            return exports, 0
        for _ in range(count):
            entry: dict = {}
            entry["has_package"] = r.bit()
            if entry["has_package"]:
                entry["package_index"] = r.packed_int(packed_variant)
            entry["has_outer"] = r.bit()
            if entry["has_outer"]:
                entry["outer_index"] = r.packed_int(packed_variant)
            entry["path"] = r.fstring()
            exports.append(entry)
    except BitError:
        return exports, r.position
    return exports, r.position


def parse_package_map_exports(
    payload: bytes,
    payload_bit_count: int,
    packed_variant: str = "dfm",
) -> list[dict]:
    exports, _ = parse_package_map_exports_with_offset(
        payload, payload_bit_count, packed_variant
    )
    return exports


def find_class_hint(exports: list[dict], hints: list[str]) -> str | None:
    for e in exports:
        path = e.get("path") or ""
        for hint in hints:
            if hint.lower() in path.lower():
                return path
    return None
