"""Bounded stateless native codecs; Python implementations remain the oracle."""
import ctypes
import os
import threading

_local = threading.local()
_error = None


def _configure(lib):
    lib.radar_lz4_decode.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    lib.radar_lz4_decode.restype = ctypes.c_int
    lib.radar_tile.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t,
                              ctypes.c_uint, ctypes.c_uint, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
    lib.radar_tile.restype = ctypes.c_int
    return lib


try:
    paths = []
    if os.environ.get('RADAR_CODEC_LIBRARY'):
        paths.append(os.environ['RADAR_CODEC_LIBRARY'])
    try:
        from java import jclass
        paths.extend(str(p) for p in jclass('cn.radar.client.Logx').INSTANCE.nativeCryptoPaths())
    except ImportError:
        pass
    for path in paths:
        try:
            _library = _configure(ctypes.CDLL(path))
            break
        except (OSError, AttributeError):
            continue
    else:
        raise OSError('Native codec library unavailable')
except Exception as exc:
    _library = None
    _error = type(exc).__name__


def status():
    return dict(backend='native' if _library is not None else 'python', error=_error)


def lz4(data):
    if _library is None:
        return NotImplemented
    buffer = getattr(_local, 'lz4', None)
    if buffer is None:
        buffer = _local.lz4 = ctypes.create_string_buffer(4*1024*1024)
    size = _library.radar_lz4_decode(data, len(data), buffer, len(buffer))
    if size < 0:
        raise ValueError('Invalid LZ4 block')
    return ctypes.string_at(buffer, size)


def tile(payload, pbits, start=0, min_blocks=1, max_blocks=4096, partial_ok=False):
    if (_library is None or type(payload) is not bytes or
            any(type(v) is not int for v in (pbits, start, min_blocks, max_blocks)) or
            not 0 <= start <= pbits <= len(payload)*8 or not 0 <= min_blocks <= 4096 or
            not 0 <= max_blocks <= 4096):
        return NotImplemented
    buffer = getattr(_local, 'tiles', None)
    if buffer is None:
        buffer = _local.tiles = (ctypes.c_int64*(4097*10))()
    count = _library.radar_tile(payload, len(payload), pbits, start, min_blocks,
                                max_blocks, bool(partial_ok), buffer, 4097)
    if count < 0:
        return None
    result = []
    for n in range(count):
        at = n*10
        result.append(dict(start=buffer[at], has_rep=buffer[at+1], is_actor=buffer[at+2],
                           sub=None if buffer[at+3] < 0 else buffer[at+3],
                           stably=None if buffer[at+4] < 0 else buffer[at+4],
                           cls=None if buffer[at+5] < 0 else buffer[at+5], deleted=bool(buffer[at+6]),
                           num_bits=buffer[at+7], payload_start=buffer[at+8], payload_end=buffer[at+9]))
    return result
