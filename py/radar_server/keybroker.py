"""Binds TCP-extracted 128-byte banks to UDP flows and emits plaintext events."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import tea
from .tgcp import Observation
from .sessionkeys import RuntimeSession, SessionKeyStore


@dataclass
class UdpPacket:
    up: bool
    local: str
    remote: str
    payload: bytes
    timestamp_ms: int
    account: str = ""


@dataclass
class BankCandidate:
    fingerprint: str
    key: bytes
    connection: int
    sequence: int
    created_ms: int
    account: str = ""


@dataclass
class FlowState:
    account: str = ""
    runtime_session: RuntimeSession | None = None
    pending: list[UdpPacket] = field(default_factory=list)
    spool_path: Path | None = None
    spool_count: int = 0
    bank: BankCandidate | None = None
    plaintext: bool = False
    plain_handshake_controls: set[bytes] = field(default_factory=set)
    plain_down_verified: bool = False
    plain_down_sequence: int | None = None
    plain_down_probe: list[UdpPacket] = field(default_factory=list)
    plain_down_unverified: int = 0
    last_seen: int = 0
    epoch: int = 0
    failures: int = 0
    bypass: bool = False
    unbound: int = 0


class KeyBroker:
    def __init__(
        self,
        emit: Callable[[str, dict], None],
        key_dir: str | Path | None = None,
        pending_dir: str | Path | None = None,
        max_flows: int = 1024,
        max_candidates: int = 64,
        capture_raw_udp: bool = False,
    ):
        self._emit = emit
        self._key_dir = Path(key_dir) if key_dir else None
        self._pending_dir = Path(pending_dir) if pending_dir else None
        if self._key_dir:
            self._key_dir.mkdir(parents=True, exist_ok=True)
        self._session_keys = SessionKeyStore(self._key_dir / "sessions" if self._key_dir else None,
                                            self._pending_dir / "sessions" if self._pending_dir else None)
        self._candidates: list[BankCandidate] = []
        self._flows: dict[str, FlowState] = {}
        self._epoch = 0
        self._max_flows = max_flows
        self._max_candidates = max_candidates
        self._capture_raw_udp = capture_raw_udp
        self._observations = 0
        self.packets = 0
        self.plain_packets = 0
        self.invalid_packets = 0
        self.validated_flows = 0
        self.up_packets = 0
        self.down_packets = 0
        self.up_bytes = 0
        self.down_bytes = 0
        self.unverified_down_packets = 0

    # ------------------------------------------------------------------ banks
    def add(self, connection: int, observation: Observation, account: str = "") -> None:
        added = False
        for key in observation.banks:
            fingerprint = hashlib.sha256(key).hexdigest()
            if any(item.fingerprint == fingerprint and item.account == account for item in self._candidates):
                continue
            candidate = BankCandidate(
                fingerprint=fingerprint,
                key=bytes(key),
                connection=connection,
                sequence=observation.sequence,
                created_ms=int(time.time() * 1000),
                account=account,
            )
            self._candidates.append(candidate)
            added = True
            if len(self._candidates) > self._max_candidates:
                self._candidates.pop(0)
            if self._key_dir:
                (self._key_dir / (fingerprint + ".bin")).write_bytes(candidate.key)
            self._emit(
                "bank_candidate",
                {
                    "fingerprint": fingerprint,
                    "connection": connection,
                    "tgcpSequence": observation.sequence,
                    "keyLength": 128,
                    "account": account,
                },
            )
        if not added:
            return
        for flow in list(self._flows.values()):
            if flow.account == account and flow.bank is None and not flow.plaintext and not flow.bypass and flow.pending:
                self._try_bind(flow)

    # ------------------------------------------------------------------- udp
    def _prune_flows(self, now_ms: int) -> None:
        if not self._flows:
            return
        cutoff = now_ms - 120_000
        stale = [key for key, flow in self._flows.items()
                 if flow.last_seen and flow.last_seen < cutoff]
        for key in stale:
            removed = self._flows.pop(key, None)
            if removed and removed.runtime_session:
                self._close_runtime(removed.runtime_session)
        if len(self._flows) >= self._max_flows:
            oldest = sorted(self._flows.items(), key=lambda item: item[1].last_seen)
            for key, _ in oldest[: max(1, len(self._flows) // 4)]:
                removed = self._flows.pop(key, None)
                if removed and removed.runtime_session:
                    self._close_runtime(removed.runtime_session)

    def observe(self, packet: UdpPacket) -> None:
        if self._capture_raw_udp:
            self._emit(
                "udp_raw",
                {
                    "timestampMs": packet.timestamp_ms,
                    "direction": "up" if packet.up else "down",
                    "local": packet.local,
                    "remote": packet.remote,
                    "payloadLength": len(packet.payload),
                    "payloadBase64": base64.b64encode(packet.payload).decode("ascii"),
                    "account": packet.account,
                },
            )
        self.packets += 1
        if packet.up:
            self.up_packets += 1
            self.up_bytes += len(packet.payload)
        else:
            self.down_packets += 1
            self.down_bytes += len(packet.payload)

        self._observations += 1
        if self._observations % 256 == 0 or len(self._flows) >= self._max_flows:
            self._prune_flows(packet.timestamp_ms)

        flow_id = f"{packet.account}\0{packet.local}>{packet.remote}"
        flow = self._flows.get(flow_id)
        if flow is None:
            if len(self._flows) >= self._max_flows:
                return
            flow = FlowState()
            self._flows[flow_id] = flow
        flow.last_seen = packet.timestamp_ms
        if packet.account:
            flow.account = packet.account
        initial = packet.up and self._is_initial(packet.payload)
        runtime = flow.runtime_session
        if initial and runtime and (runtime.bound or runtime.probes or
                runtime.initial_sha256 != hashlib.sha256(packet.payload).hexdigest()):
            self._close_runtime(runtime)
            flow = FlowState(account=packet.account, last_seen=packet.timestamp_ms)
            self._flows[flow_id] = flow
        if initial and flow.runtime_session is None:
            try:
                runtime = self._session_keys.start(packet)
                if runtime is not None:
                    flow = FlowState(account=packet.account, last_seen=packet.timestamp_ms,
                                     runtime_session=runtime)
                    self._flows[flow_id] = flow
                    self._emit("udp_session_key_waiting", {
                        "sessionId": runtime.session_id, "initialSha256": runtime.initial_sha256,
                        "flow": f"{packet.local}>{packet.remote}", "account": packet.account,
                    })
            except Exception as exc:
                self._emit_runtime_failure(packet, type(exc).__name__)
        if flow.runtime_session is not None:
            runtime = flow.runtime_session
            before = runtime.bound
            rejected = runtime.rejected_packets
            try:
                selected = runtime.observe(packet)
                self.invalid_packets += runtime.rejected_packets - rejected
                if not before and runtime.bound:
                    self._epoch += 1
                    flow.epoch = self._epoch
                    self.validated_flows += 1
                    self._emit("udp_session_key_bound", {
                        "flow": f"{packet.local}>{packet.remote}", "sessionId": runtime.session_id,
                        "epoch": flow.epoch, "fingerprint": runtime.fingerprint,
                        "method": "exact_session_five_monotonic_xtea_envelopes", "account": packet.account,
                    })
                for held, plain in selected:
                    self._write_plain(held, flow, plain, runtime.fingerprint,
                                      "xtea-cbc-ue", tea.valid_envelope(plain))
            except Exception as exc:
                self._close_runtime(runtime)
                flow.runtime_session = None
                flow.bypass = True
                self._emit_runtime_failure(packet, type(exc).__name__)
            return
        # A new transport handshake must be considered before cached modes or
        # bypass state. Neither applies to the next session on this endpoint.
        if packet.up and self._is_initial(packet.payload) and (
            flow.bank is not None or flow.pending or flow.plaintext or flow.bypass
        ):
            flow.bank = None
            flow.plaintext = False
            flow.plain_handshake_controls.clear()
            flow.plain_down_verified = False
            flow.plain_down_sequence = None
            flow.plain_down_probe.clear()
            flow.plain_down_unverified = 0
            flow.pending.clear()
            flow.spool_path = None
            flow.spool_count = 0
            flow.failures = 0
            flow.bypass = False
            flow.unbound = 0
        if flow.bypass:
            return

        if flow.plaintext:
            if self._emit_plain(packet, flow):
                flow.failures = 0
            return

        if flow.bank is not None:
            if self._emit_plain(packet, flow):
                flow.failures = 0
                return
            if not packet.up:
                flow.failures += 1
                if flow.failures >= 3:
                    self._emit("udp_binding_reset", {"flow": flow_id, "reason": "consecutive_invalid_payloads", "account": flow.account})
                    flow.bank = None
                    flow.pending.clear()
                    flow.spool_path = None
                    flow.spool_count = 0
                    flow.failures = 0
                else:
                    return
            else:
                return

        flow.unbound += 1
        if flow.unbound > 128:
            flow.bypass = True
            flow.pending.clear()
            flow.spool_path = None
            flow.spool_count = 0
            return
        flow.pending.append(packet)
        if len(flow.pending) > 64:
            self._spill(flow)
        if self._plaintext_handshake(flow.pending):
            flow.plaintext = True
            up_controls = [p.payload[8:] for p in flow.pending if p.up and len(p.payload) == 33][:2]
            down_controls = [p.payload for p in flow.pending if not p.up and len(p.payload) == 25][:2]
            flow.plain_handshake_controls = set(up_controls + down_controls)
            flow.bank = None
            self._epoch += 1
            flow.epoch = self._epoch
            pending = flow.pending
            flow.pending = []
            flow.spool_path = None
            for item in pending:
                self._emit_plain(item, flow)
            return
        self._try_bind(flow)

    @staticmethod
    def _close_runtime(runtime: RuntimeSession) -> None:
        try:
            runtime.close()
        except Exception:
            pass

    def _emit_runtime_failure(self, packet: UdpPacket, error_type: str) -> None:
        try:
            self._emit("udp_session_capture_failed", {
                "flow": f"{packet.local}>{packet.remote}", "errorType": error_type,
                "account": packet.account,
            })
        except Exception:
            pass  # Optional capture can never terminate a transport relay.

    @staticmethod
    def _is_initial(payload: bytes) -> bool:
        return (
            len(payload) == 33
            and payload[8] == 1
            and payload[-1] == 8
            and all(byte == 0 for byte in payload[9:32])
        )

    def _spill(self, flow: FlowState) -> None:
        if self._pending_dir is None:
            flow.pending.pop(0)
            return
        self._pending_dir.mkdir(parents=True, exist_ok=True)
        if flow.spool_path is None:
            flow.spool_path = self._pending_dir / (hashlib.sha256(str(time.time()).encode()).hexdigest()[:32] + ".jsonl")
        oldest = flow.pending.pop(0)
        with flow.spool_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "up": oldest.up,
                        "timestampMs": oldest.timestamp_ms,
                        "local": oldest.local,
                        "remote": oldest.remote,
                        "payload": base64.b64encode(oldest.payload).decode("ascii"),
                    }
                )
                + "\n"
            )
        flow.spool_count += 1

    def _try_bind(self, flow: FlowState) -> None:
        if not flow.pending:
            return
        down = [item for item in flow.pending[-4:] if not item.up and len(item.payload) >= 10]
        matches: list[tuple[BankCandidate, str]] = []
        for candidate in reversed(self._candidates):
            if candidate.account != flow.account:
                continue
            if self._handshake_matches(flow.pending, candidate):
                matches.append((candidate, "two_handshake_responses"))
            elif len(down) >= 3:
                fingerprints = {hashlib.sha256(item.payload).hexdigest() for item in down}
                if len(fingerprints) >= 3 and all(
                    tea.valid_envelope(tea.decrypt(item.payload, candidate.key)) for item in down
                ):
                    matches.append((candidate, "at_least_three_distinct_envelopes"))
        if len(matches) != 1:
            return
        flow.bank = matches[0][0]
        self._epoch += 1
        flow.epoch = self._epoch
        self.validated_flows += 1
        first = flow.pending[0]
        self._emit(
            "udp_bank_bound",
            {
                "flow": f"{first.local}>{first.remote}",
                "epoch": flow.epoch,
                "fingerprint": flow.bank.fingerprint,
                "method": matches[0][1],
                "account": flow.account,
            },
        )
        if flow.spool_path is not None:
            with flow.spool_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    row = json.loads(line)
                    restored = UdpPacket(
                        up=bool(row["up"]),
                        local=row["local"],
                        remote=row["remote"],
                        payload=base64.b64decode(row["payload"]),
                        timestamp_ms=int(row["timestampMs"]),
                    )
                    self._emit_plain(restored, flow)
            flow.spool_path.unlink(missing_ok=True)
            flow.spool_path = None
            flow.spool_count = 0
        pending = flow.pending
        flow.pending = []
        for packet in pending:
            self._emit_plain(packet, flow)

    @staticmethod
    def _handshake_matches(packets: list[UdpPacket], candidate: BankCandidate) -> bool:
        for i in range(len(packets) - 4):
            window = packets[i:i + 5]
            if len(window) != 5:
                continue
            if not (
                window[0].up
                and not window[1].up
                and window[2].up
                and window[3].up
                and not window[4].up
            ):
                continue
            if not (
                KeyBroker._is_initial(window[0].payload)
                and len(window[1].payload) == 25
                and len(window[2].payload) == 33
                and len(window[3].payload) == 25
                and len(window[4].payload) == 35
            ):
                continue
            reply = window[2].payload[8:]
            first = reply[:4] + bytes.fromhex("1a420b63632b733b2b1b7a7b5b4b2b030000000008")
            second = bytearray(25)
            second[0:4] = bytes.fromhex("050000fc")
            second[4] = (reply[4] & 0xF8) | 5
            second[5:] = reply[5:]
            if (
                tea.decrypt(window[1].payload, candidate.key) == first
                and tea.decrypt(window[3].payload, candidate.key) == bytes(second)
            ):
                return True
        return False

    @staticmethod
    def _plaintext_handshake(packets: list[UdpPacket]) -> bool:
        up33 = [p for p in packets if p.up and len(p.payload) == 33]
        dn25 = [p for p in packets if not p.up and len(p.payload) == 25]
        if len(up33) < 2 or len(dn25) < 2:
            return False
        reply = up33[1].payload[8:]
        if len(reply) < 25:
            return False
        tail = bytes.fromhex("1a420b63632b733b2b1b7a7b5b4b2b030000000008")
        first = reply[:4] + tail
        second = bytearray(25)
        second[0:4] = bytes.fromhex("050000fc")
        second[4] = (reply[4] & 0xF8) | 5
        second[5:] = reply[5:]
        return dn25[0].payload == first and dn25[1].payload == bytes(second)

    def _unverified_down(self, packet: UdpPacket, flow: FlowState) -> None:
        flow.plain_down_unverified += 1
        self.unverified_down_packets += 1
        if flow.plain_down_unverified == 1:
            self._emit("udp_downstream_unverified", {
                "flow": f"{packet.local}>{packet.remote}",
                "epoch": flow.epoch,
                "reason": "clear_handshake_does_not_validate_data",
                "account": flow.account,
            })

    def _plain_down_packets(self, packet: UdpPacket, flow: FlowState) -> list[UdpPacket]:
        # Envelope syntax alone is insufficient: encrypted data can satisfy it.
        valid = tea.valid_envelope(packet.payload)
        sequence = ((int.from_bytes(packet.payload[:5], "little") >> 19) & 0x3FFF) if valid else None
        previous = flow.plain_down_sequence
        delta = ((sequence - previous) & 0x3FFF) if sequence is not None and previous is not None else None
        if flow.plain_down_verified:
            if valid and (delta is None or delta <= 256 or delta >= 0x4000 - 64):
                if previous is None or (delta is not None and 0 < delta <= 256):
                    flow.plain_down_sequence = sequence
                return [packet]
            flow.plain_down_verified = False
            flow.plain_down_probe.clear()
            flow.plain_down_sequence = None
            self._emit("udp_plaintext_verification_lost", {
                "flow": f"{packet.local}>{packet.remote}",
                "epoch": flow.epoch,
                "account": flow.account,
            })
            previous = None
            delta = None
        self._unverified_down(packet, flow)
        if not valid:
            flow.plain_down_probe.clear()
            flow.plain_down_sequence = None
            return []
        if previous is not None and delta == 0:
            return []  # duplicates do not constitute independent evidence
        if previous is not None and not (delta is not None and 0 < delta <= 256):
            flow.plain_down_probe.clear()
        flow.plain_down_sequence = sequence
        flow.plain_down_probe.append(packet)
        if len(flow.plain_down_probe) < 5:
            return []
        # Buffer only the five proving packets so Welcome is not discarded.
        pending = flow.plain_down_probe
        flow.plain_down_probe = []
        flow.plain_down_verified = True
        self._emit("udp_plaintext_verified", {
            "flow": f"{packet.local}>{packet.remote}",
            "epoch": flow.epoch,
            "method": "five_distinct_monotonic_envelopes",
            "validationScope": "envelope_sequence_only",
            "account": flow.account,
        })
        return pending

    def _write_plain(self, packet: UdpPacket, flow: FlowState, plain: bytes,
                     fingerprint: str, format_name: str, valid: bool) -> None:
        self.plain_packets += 1
        self._emit("udp_plaintext", {
            "timestampMs": packet.timestamp_ms,
            "direction": "up" if packet.up else "down",
            "local": packet.local,
            "remote": packet.remote,
            "epoch": flow.epoch,
            "keyFingerprint": fingerprint,
            "format": format_name,
            "validEnvelope": valid,
            "payloadLength": len(plain),
            "payloadBase64": base64.b64encode(plain).decode("ascii"),
            "account": flow.account,
        })

    def _emit_plain(self, packet: UdpPacket, flow: FlowState) -> bool:
        if flow.plaintext:
            plain = (packet.payload[8:] if len(packet.payload) >= 8 else b"") if packet.up else packet.payload
            if not plain:
                return False
            valid = tea.valid_envelope(plain)
            control = plain in flow.plain_handshake_controls
            if control:
                self._write_plain(packet, flow, plain, "plaintext", "ue-handshake", valid)
                return True
            if packet.up:
                if not valid:
                    self.invalid_packets += 1
                    return False
                self._write_plain(packet, flow, plain, "plaintext", "plain-ue", True)
                return True
            selected = self._plain_down_packets(packet, flow)
            for item in selected:
                self._write_plain(item, flow, item.payload, "plaintext", "plain-ue", True)
            return bool(selected)
        bank = flow.bank
        if bank is None:
            return False
        if packet.up:
            plain = packet.payload[8:] if len(packet.payload) >= 8 else b""
        else:
            try:
                plain = tea.decrypt(packet.payload, bank.key)
            except Exception:
                self.invalid_packets += 1
                return False
        valid = tea.valid_envelope(plain)
        control = len(plain) == 25 and (plain[0] & 1) != 0
        if not valid and not control:
            self.invalid_packets += 1
            return False
        self.plain_packets += 1
        self._emit(
            "udp_plaintext",
            {
                "timestampMs": packet.timestamp_ms,
                "direction": "up" if packet.up else "down",
                "local": packet.local,
                "remote": packet.remote,
                "epoch": flow.epoch,
                "keyFingerprint": bank.fingerprint,
                "format": "ue-handshake" if control else "ue-envelope",
                "validEnvelope": valid,
                "payloadLength": len(plain),
                "payloadBase64": base64.b64encode(plain).decode("ascii"),
                "account": flow.account,
            },
        )
        return True

    def snapshot(self) -> dict:
        return {
            "packets": self.packets,
            "plainPackets": self.plain_packets,
            "invalidPackets": self.invalid_packets,
            "validatedFlows": self.validated_flows,
            "upPackets": self.up_packets,
            "downPackets": self.down_packets,
            "upBytes": self.up_bytes,
            "downBytes": self.down_bytes,
            "bankCandidates": len(self._candidates),
            "sessionKeyFlows": sum(bool(flow.runtime_session and flow.runtime_session.bound) for flow in self._flows.values()),
            "sessionKeyWaitingFlows": sum(bool(flow.runtime_session and not flow.runtime_session.bound) for flow in self._flows.values()),
            "pendingFlows": sum(1 for flow in self._flows.values() if flow.bank is None and not (flow.runtime_session and flow.runtime_session.bound)),
            "activeFlows": len(self._flows),
            "clearHandshakeFlows": sum(flow.plaintext for flow in self._flows.values()),
            "verifiedPlainDownstreamFlows": sum(flow.plain_down_verified for flow in self._flows.values()),
            "unverifiedDownPackets": self.unverified_down_packets,
        }
