"""Additive season relay: type-7 handshake replacement + downlink translation.

Runs beside the legacy bank broker without touching it.  The SOCKS5 UDP relay
asks handle() whether a flow is owned by a season session and forwards the
returned actions instead of the original datagram.
"""

from __future__ import annotations

import base64
import hashlib
import os
import queue
import threading
import time

from . import tea
from . import diagnostics as diag
from .seasonbridge import SeasonBridge, credential_report, is_initial
from .seasoncrypto import decrypt_downlink, parse_control7

STALE_SECONDS = 600
MAX_SESSIONS = int(os.environ.get("RADAR_SEASON_MAX_SESSIONS", "128"))
_DIAG = os.environ.get("RADAR_SEASON_DIAG", "0") == "1"


class SeasonRelay:
    def __init__(self, emit):
        self._emit = emit
        self.bridge = SeasonBridge(self._observer, self._installer)
        self._obs_queue: queue.Queue = queue.Queue(maxsize=512)
        self._obs_thread = threading.Thread(
            target=self._obs_loop, name="season-observer", daemon=True
        )
        self._obs_thread.start()
        self._keys: dict[str, bytes] = {}
        self._epochs: dict[str, int] = {}
        self._last: dict[str, float] = {}
        self._seen_controls: set = set()
        self._seen_notices: set = set()
        self._scan = {"messages": 0, "interesting": 0, "decompressed": 0, "lastEmit": 0.0}
        self._diag_states = {}
        self._diag_last = 0.0
        self._diag_counts = dict(empty=0, transformErrors=0, observerDrops=0,
                                 observerErrors=0, noKey=0, decryptErrors=0,
                                 invalidEnvelope=0, plaintext=0)

    def _record_state(self, flow, session, source):
        if session is None:
            return
        previous = self._diag_states.get(flow)
        if previous is None or previous[:2] != (id(session), session.state):
            now = time.monotonic()
            elapsed = now - previous[2] if previous else 0.0
            old = previous[1] if previous else "none"
            self._diag_states[flow] = (id(session), session.state, now)
            diag.log("Handshake", f"flow={flow} {old}->{session.state} source={source} "
                     f"previousAge={elapsed:.1f}s retries={session.retries} "
                     f"cookieRetries={session.cookie_retries} translated={session.translated}")

    def _diagnose(self):
        now = time.monotonic()
        if now - self._diag_last < 10:
            return
        self._diag_last = now
        counts = " ".join(f"{k}={v}" for k, v in self._diag_counts.items())
        diag.log("Season", f"peers={len(self.bridge.peers)} flows={len(self.bridge.flows)} "
                 f"offers={self.bridge.offers} observerQueue={self._obs_queue.qsize()} {counts}")
        # Prefer unfinished handshakes, bounded so a packet storm cannot flood logs.
        sessions = sorted(self.bridge.flows.items(), key=lambda item: item[1].state == "ready")
        for flow, session in sessions[:8]:
            self._record_state(flow, session, "poll")
            age = now - self._diag_states[flow][2]
            diag.log("Handshake", f"flow={flow} state={session.state} age={age:.1f}s "
                     f"retries={session.retries} cookieRetries={session.cookie_retries} "
                     f"translated={session.translated}",
                     warning=session.state not in ("ready", "bypass") and age >= 5)

    # ------------------------------------------------------------- tcp feed
    def observe_notice(self, observation, account: str = "") -> None:
        if _DIAG and not observation.up and observation.command == 16403:
            size = len(observation.plain)
            if 100 <= size <= 6000:
                self._scan["messages"] += 1
                digest = hashlib.sha256(observation.plain).hexdigest()
                if digest not in self._seen_notices:
                    self._seen_notices.add(digest)
                    report = credential_report(observation.plain)
                    if report.get("variants"):
                        self._scan["decompressed"] += 1
                    if self._interesting(report):
                        self._scan["interesting"] += 1
                        report["sha16"] = digest[:16]
                        self._emit("season_credential_detail", {"report": report, "account": account})
        before = {peer for acct, peer in self.bridge.peers if acct == account}
        self.bridge.observe_notice(observation, account)
        after = {peer for acct, peer in self.bridge.peers if acct == account}
        added = sorted(after - before)
        if added:
            self._emit(
                "season_peers",
                {"count": len(after), "added": added[:16], "account": account},
            )

    # ------------------------------------------------------------- udp entry
    def handle(self, up: bool, local: str, remote: str, payload: bytes, account: str = ""):
        flow = self.bridge.flow_key(account, local, remote)
        if up and is_initial(payload):
            diag.log("Handshake", f"initial flow={flow} bytes={len(payload)} "
                     f"credential={int((account, remote) in self.bridge.peers)} "
                     f"bypassPeer={int(remote in self.bridge.bypass_peers.get(account, set()))}",
                     key=f"initial:{flow}")
        self._probe_control(up, local, remote, payload, account)
        if len(self.bridge.flows) >= MAX_SESSIONS:
            self._prune()
        session = self.bridge.flows.get(flow)
        before = session.state if session is not None else None
        try:
            actions = self.bridge.transform(up, local, remote, payload, account)
        except RuntimeError:
            self._prune(force=True, account=account)
            try:
                actions = self.bridge.transform(up, local, remote, payload, account)
            except RuntimeError:
                self._diag_counts["transformErrors"] += 1
                diag.log("Season", f"flow={flow} capacity retry failed; passthrough", warning=True,
                         key="season-capacity")
                return False, []
        except Exception as exc:
            self._diag_counts["transformErrors"] += 1
            diag.error("Season", f"transform flow={flow} up={up} bytes={len(payload)} state={before}", exc)
            raise
        session = self.bridge.flows.get(flow)
        self._record_state(flow, session, "packet")
        after = session.state if session is not None else None
        if after != before:
            print(f"[season] {flow} {before}->{after}", flush=True)
            self._emit(
                "season_state",
                {"flow": f"{local}>{remote}", "from": before, "to": after, "account": account},
            )
        handled = flow in self.bridge.flows
        if handled:
            self._last[flow] = time.monotonic()
            if not actions:
                self._diag_counts["empty"] += 1
                diag.log("Withheld", f"flow={flow} up={up} bytes={len(payload)} state={after} "
                         f"total={self._diag_counts['empty']} (handled without output)",
                         key=f"empty:{flow}:{up}:{after}")
        return handled, actions

    def poll_all(self):
        self._prune()
        now = time.monotonic()
        if now - self._scan["lastEmit"] >= 60 and self._scan["messages"]:
            self._scan["lastEmit"] = now
            self._emit("season_credential_scan", {
                "messages": self._scan["messages"],
                "interesting": self._scan["interesting"],
                "decompressed": self._scan["decompressed"],
            })
            self._scan = {"messages": 0, "interesting": 0, "decompressed": 0,
                          "lastEmit": self._scan["lastEmit"]}
        try:
            events = self.bridge.poll_all()
            for account, local, remote, _ in events:
                flow = self.bridge.flow_key(account, local, remote)
                self._record_state(flow, self.bridge.flows.get(flow), "timer")
            return events
        finally:
            self._diagnose()

    @staticmethod
    def _interesting(report: dict) -> bool:
        marker = "4119834b0729119d0dc98a684b19a76c098413b40ae8df2acb74143e8b4aabfd"[:16]
        for item in report.get("attempts") or []:
            if item.get("bankTokenOffset") is not None or item.get("banks") or item.get("endpoints"):
                return True
            if marker in (item.get("markerHashes") or []):
                return True
            if (item.get("envelope") and 1 in item["envelope"] and 2 in item["envelope"]
                    and (item.get("messageLength") or 0) >= 200):
                return True
        return False

    def _probe_control(self, up: bool, local: str, remote: str, payload: bytes, account: str) -> None:
        raw = payload[8:] if up else payload
        if len(raw) != 69 or not raw or (raw[0] & 31) != 15:
            return
        try:
            control = parse_control7(raw)
        except ValueError:
            return
        key = (local, remote, up, control.parameter)
        if key in self._seen_controls:
            return
        self._seen_controls.add(key)
        self._emit(
            "season_type7",
            {
                "direction": "up" if up else "down",
                "parameter": control.parameter,
                "flow": f"{local}>{remote}",
                "account": account,
            },
        )

    def _prune(self, force: bool = False, account: str = "") -> None:
        now = time.monotonic()
        prefix = f"{account}|" if force and account else ""
        stale = [flow for flow, stamp in self._last.items()
                 if (now - stamp > STALE_SECONDS or (prefix and flow.startswith(prefix)))]
        if force and not account:
            stale = list(self.bridge.flows)
        for flow in stale:
            self.bridge.flows.pop(flow, None)
            self._keys.pop(flow, None)
            self._epochs.pop(flow, None)
            self._last.pop(flow, None)
            self._diag_states.pop(flow, None)

    # ---------------------------------------------------------- bridge hooks
    def _installer(self, local: str, remote: str, private: bytes, account: str = "") -> None:
        flow = self.bridge.flow_key(account, local, remote)
        session = self.bridge.flows.get(flow)
        if session is None or session.ack is None:
            return
        key = self.bridge.config.key_from_client_private(session.client, session.ack, private)
        self._keys[flow] = key
        epoch = self._epochs.get(flow, 0) + 1
        self._epochs[flow] = epoch
        diag.log("Handshake", f"flow={flow} keyInstalled=true epoch={epoch}")
        self._emit(
            "udp_bank_bound",
            {
                "flow": f"{local}>{remote}",
                "epoch": epoch,
                "fingerprint": hashlib.sha256(key).hexdigest(),
                "method": "authenticated_type7_client_private",
                "keyLength": 16,
                "protocol": "season-type7",
                "account": account,
            },
        )

    def _observer(self, up: bool, local: str, remote: str, payload: bytes, account: str = "") -> None:
        """Queue the observation and return at once: decrypting, base64 and the
        bridge emit used to run on the UDP/TCP event loop and blocked it for
        150-500 ms per large packet, which is exactly what the game feels as
        packet loss."""
        try:
            flow = self.bridge.flow_key(account, local, remote)
            session = self.bridge.flows.get(flow)
            if session is None:
                return
            item = (
                up, local, remote, bytes(payload), account,
                self._epochs.get(flow, 1), self._keys.get(flow), session.key,
            )
        except Exception:
            return
        try:
            self._obs_queue.put_nowait(item)
        except queue.Full:
            self._diag_counts["observerDrops"] += 1

    def _obs_loop(self) -> None:
        while True:
            item = self._obs_queue.get()
            try:
                self._observe_one(*item)
            except Exception as exc:
                self._diag_counts["observerErrors"] += 1
                diag.error("Decode", "season observer", exc)

    def _observe_one(self, up: bool, local: str, remote: str, payload: bytes,
                     account: str, epoch: int, key: bytes | None,
                     session_key: bytes | None) -> None:
        raw_control = payload[8:] if up else payload
        if len(raw_control) == 69 and raw_control and (raw_control[0] & 31) == 15:
            self._emit(
                "udp_plaintext",
                {
                    "timestampMs": int(time.time() * 1000),
                    "direction": "up" if up else "down",
                    "local": local,
                    "remote": remote,
                    "epoch": epoch,
                    "keyFingerprint": hashlib.sha256(session_key).hexdigest() if session_key else "",
                    "format": "season-type7",
                    "validEnvelope": False,
                    "payloadLength": len(raw_control),
                    "payloadBase64": base64.b64encode(raw_control).decode("ascii"),
                    "account": account,
                },
            )
            return
        if up:
            plain = payload[8:] if len(payload) >= 8 else b""
        else:
            if key is None:
                self._diag_counts["noKey"] += 1
                return
            try:
                plain = decrypt_downlink(payload, key)
            except Exception as exc:
                self._diag_counts["decryptErrors"] += 1
                diag.error("Decode", f"downlink flow={local}>{remote} bytes={len(payload)}", exc)
                return
        if plain and (plain[0] & 1):
            return
        if not tea.valid_envelope(plain):
            self._diag_counts["invalidEnvelope"] += 1
            return
        self._diag_counts["plaintext"] += 1
        self._emit(
            "udp_plaintext",
            {
                "timestampMs": int(time.time() * 1000),
                "direction": "up" if up else "down",
                "local": local,
                "remote": remote,
                "epoch": epoch,
                "keyFingerprint": hashlib.sha256(key).hexdigest() if key else "",
                "format": "ue-envelope",
                "validEnvelope": True,
                "payloadLength": len(plain),
                "payloadBase64": base64.b64encode(plain).decode("ascii"),
                "account": account,
            },
        )
