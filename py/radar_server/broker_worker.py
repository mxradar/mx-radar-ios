"""One ordered owner for optional legacy decoding; transport never waits on it."""
import queue
import threading
import time
from .keybroker import UdpPacket
from .tgcp import Observation
from . import diagnostics as diag


class BrokerWorker:
    def __init__(self, broker, max_events=4096, max_bytes=32*1024*1024):
        self.broker = broker
        self.queue = queue.Queue(maxsize=max_events)
        self.max_bytes = max_bytes
        self.pending_bytes = 0
        self.dropped = 0
        self.failures = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.last_error = 0.0
        self.reported_drops = 0
        self.thread = threading.Thread(target=self._run, name='legacy-broker', daemon=True)
        self.thread.start()

    def _submit(self, method, args, size):
        with self.lock:
            if self.stop.is_set() or self.pending_bytes+size>self.max_bytes:
                self.dropped += 1
                return False
            self.pending_bytes += size
            try:
                self.queue.put_nowait((method,args,size))
            except queue.Full:
                self.pending_bytes -= size
                self.dropped += 1
                return False
        return True

    def add(self, connection, observation, account=''):
        if not observation.banks:
            return True
        # KeyBroker.add only needs sequence/banks. Do not retain entire TCP
        # application messages while an expensive candidate search is running.
        banks=[bytes(key) for key in observation.banks]
        item=Observation(observation.up,observation.command,observation.sequence,b'',banks)
        return self._submit('add',(connection,item,account),sum(map(len,banks))+256)

    def observe(self, packet):
        item=UdpPacket(packet.up,packet.local,packet.remote,bytes(packet.payload),packet.timestamp_ms,packet.account)
        return self._submit('observe',(item,),len(item.payload)+256)

    def _run(self):
        while not self.stop.is_set():
            try:
                method,args,size=self.queue.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                getattr(self.broker,method)(*args)
            except Exception as exc:
                self.failures += 1
                diag.error("BrokerError", f"optional decoder method={method}", exc)
                now=time.monotonic()
                if now-self.last_error>=10:
                    print('[legacy-broker] optional decoder error: '+type(exc).__name__,flush=True)
                    self.last_error=now
            finally:
                with self.lock:self.pending_bytes-=size
                self.queue.task_done()
                if self.dropped>self.reported_drops and time.monotonic()-self.last_error>=10:
                    print('[legacy-broker] optional decode backlog overflow; skipped='+str(self.dropped),flush=True)
                    self.reported_drops=self.dropped;self.last_error=time.monotonic()

    def close(self, timeout=2):
        self.stop.set()
        self.thread.join(timeout=timeout)
