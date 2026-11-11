"""Bounded, asynchronous diagnostics shared with Android's exportable Logx.

Never enqueue wire payloads, credentials, tokens or key material here.
No disk or Java calls run on the packet forwarding thread.
"""
from __future__ import annotations

import queue
import threading
import time

_queue: queue.Queue = queue.Queue(maxsize=256)
_lock = threading.Lock()
_last: dict[str, float] = {}
_thread: threading.Thread | None = None
_dropped = 0


def _run() -> None:
    try:
        from java import jclass
        logger = jclass("cn.radar.client.Logx").INSTANCE
    except Exception:
        logger = None
    while True:
        tag, message, warning = _queue.get()
        try:
            if logger is None:
                print(f"[diag:{tag}] {message}", flush=True)
            elif warning:
                logger.w(tag, message)
            else:
                logger.d(tag, message)
        except Exception:
            pass
        finally:
            _queue.task_done()


def log(tag: str, message: str, *, warning: bool = False,
        key: str | None = None, interval: float = 10.0) -> None:
    global _thread, _dropped
    now = time.monotonic()
    with _lock:
        if key is not None:
            if now - _last.get(key, float("-inf")) < interval:
                return
            if len(_last) >= 512:
                _last.clear()
            _last[key] = now
        if _thread is None:
            _thread = threading.Thread(target=_run, name="diagnostic-log", daemon=True)
            _thread.start()
    try:
        _queue.put_nowait((tag, str(message).replace("\n", " ")[:1800], warning))
    except queue.Full:
        _dropped += 1


def error(tag: str, context: str, exc: BaseException) -> None:
    # Exception text can contain plaintext/keys. Record type and source frames
    # without arguments, local variables or exception messages.
    frames = []
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        frames.append(f"{code.co_filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{tb.tb_lineno}:{code.co_name}")
        tb = tb.tb_next
    log(tag, f"{context} error={type(exc).__name__} at={' > '.join(frames[-5:])}",
        warning=True, key=f"error:{tag}:{context}:{type(exc).__name__}")


def status() -> dict:
    return {"queue": _queue.qsize(), "dropped": _dropped}
