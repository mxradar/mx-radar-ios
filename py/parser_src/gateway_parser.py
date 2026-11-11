"""V5 local plaintext consumer and ER web server. No capture or key input."""
from __future__ import annotations

import argparse
import base64
import bisect
from collections import Counter, deque
import json
import math
import os
from operator import itemgetter
from pathlib import Path
import queue
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse
import zipfile
from timeline import Timeline

ROOT = (Path(sys._MEIPASS) / "ErCore") if getattr(sys, "frozen", False) else Path(__file__).parent / "ErCore"
os.environ["ER_ROOT"] = str(ROOT)
sys.path[:0] = [str(ROOT / "src/core"), str(ROOT / "src")]
import flowlive
import liveview
import er_radar

MAX_LINE = 2 * 1024 * 1024
LIVE_FLOW_RETENTION_SECONDS = 15 * 60
_SAMPLE_SEQ = itemgetter(0)
ASSETS = ROOT / "web"


class AssetStore:
    """Read release resources from a directory or a ZIP-compatible .pak file."""
    def __init__(self, *, directory=None, archive=None):
        self.directory = Path(directory).resolve() if directory else None
        self.archive = Path(archive).resolve() if archive else None
        self.zip = zipfile.ZipFile(self.archive, "r") if self.archive else None
        self.entries = ({item.filename: item for item in self.zip.infolist() if not item.is_dir()}
                        if self.zip else {})

    @staticmethod
    def _key(value):
        value = unquote(value).replace("\\", "/").strip("/")
        parts = value.split("/")
        if not value or any(part in ("", ".", "..") for part in parts) or "\x00" in value:
            raise ValueError("invalid asset path")
        return "/".join(parts)

    def exists(self, key):
        try:
            key = self._key(key)
        except ValueError:
            return False
        if self.zip:
            return key in self.entries
        return bool(self.directory and (self.directory / key).is_file())

    def read_bytes(self, key):
        key = self._key(key)
        if self.zip:
            return self.zip.read(self.entries[key])
        return (self.directory / key).read_bytes()

    def read_json(self, key):
        return json.loads(self.read_bytes(key).decode("utf-8-sig"))

    def serve(self, handler, path, *, prefix, root_prefix="", cache_s=604800, no_cache=False):
        if not path.startswith(prefix):
            return False
        if not self.zip:
            root = self.directory / root_prefix if root_prefix else self.directory
            return er_radar.m3d_serve(handler, path, prefix=prefix, root=root,
                                      cache_s=cache_s, no_cache=no_cache)
        try:
            rel = self._key(path[len(prefix):] or "index.html")
            key = self._key(root_prefix + "/" + rel) if root_prefix else rel
        except ValueError:
            handler.send_response(403)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True
        requested = self.entries.get(key)
        body = requested
        encoded_gzip = False
        if body is None:
            body = self.entries.get(key + ".gz")
            encoded_gzip = body is not None
        elif "gzip" in (handler.headers.get("Accept-Encoding") or "").lower():
            sidecar = self.entries.get(key + ".gz")
            if sidecar is not None:
                body = sidecar
                encoded_gzip = True
        if body is None:
            handler.send_response(404)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True
        etag = '"%08x-%x%s"' % (body.CRC, body.file_size, "-gz" if encoded_gzip else "")
        if handler.headers.get("If-None-Match") == etag:
            handler.send_response(304)
            handler.send_header("ETag", etag)
            if encoded_gzip:
                handler.send_header("Vary", "Accept-Encoding")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True
        suffix = Path(key).suffix.lower()
        handler.send_response(200)
        handler.send_header("Content-Type", er_radar.M3D_MIME.get(suffix, "application/octet-stream"))
        handler.send_header("Content-Length", str(body.file_size))
        handler.send_header("ETag", etag)
        if "?v=" in (handler.path or ""):
            handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
        elif no_cache:
            handler.send_header("Cache-Control", "no-cache")
        else:
            handler.send_header("Cache-Control", "public, max-age=%d" % cache_s)
        if encoded_gzip:
            handler.send_header("Content-Encoding", "gzip")
            handler.send_header("Vary", "Accept-Encoding")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        if handler.command == "HEAD":
            return True
        try:
            with self.zip.open(body, "r") as source:
                while True:
                    chunk = source.read(262144)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
        except (ConnectionError, BrokenPipeError, OSError):
            handler.close_connection = True
        return True


ASSET_STORE = AssetStore(directory=ASSETS)
MODEL_STORE = AssetStore(directory=ASSETS / "m3d")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def save(path, value):
    save_encoded(path, encoded(value))


def save_encoded(path, serialized):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(serialized, encoding="utf-8")
    temp.replace(path)


class ReplayClock:
    current = 0.0
    def time(self):
        return self.current


class PlaintextLoader(flowlive.IncrementalFlowLoader):
    def __init__(self, identity):
        super().__init__(identity, s2c_table=None, auto_s2c=False, c2s_self=True, request_prefix=False)
        self.enable_c2s_self(1)
        self.pending_packets = deque()

    def poll(self, max_frames=None):
        added = 0
        for _ in range(min(len(self.pending_packets), max_frames or 256)):
            seq, direction, payload, timestamp = self.pending_packets.popleft()
            added += self.consume_packet(seq, direction, payload, timestamp)
        return added

    def exhausted(self):
        return not self.pending_packets


class Flow:
    def __init__(self, output, number, identity, account=""):
        self.number, self.identity, self.account = number, identity, account
        self.loader = PlaintextLoader(output / "flows" / str(number))
        self.view = liveview.LiveView(output, self.loader.flow_dir, loader=self.loader)
        self.packets = Counter()
        self.last_received = time.monotonic()
        self.last_timestamp = 0.0
        self.latest_sequence = 0

    def summary(self):
        return {"flow": str(self.number), "account": self.account,
                "local": self.identity[1], "remote": self.identity[2],
                "epoch": self.identity[3], "packets": dict(self.packets),
                "logicalBunches": self.loader.n_lb, "packetErrors": self.loader.packet_errors,
                "selfPoseSamples": self.loader.pose_samples, "playerRecords": len(self.view.players),
                "movementSlots": len(self.view.slots), "mraChannel": self.view.mra_ch,
                "movementSamples": sum(len(track) for track in self.view.slots.values()),
                "handshakeOk": self.loader.hs.ok, "handshakeFull": self.loader.hs.full,
                "welcome": self.loader.hs.welcome, "semanticErrors": self.view.decode_error_count()}


class Engine:
    def __init__(self, output, replay=False, idle_seconds=15, cache_only=False, preserve_event_context=False):
        self.output, self.replay, self.idle_seconds = Path(output), replay, idle_seconds
        self.cache_only = cache_only
        self.preserve_event_context = preserve_event_context
        self._gap_flows = set()
        self.timeline = None
        self.transport = {}
        self.journal_read_bytes = 0
        self.last_disk_write = 0.0
        self.rate_events = deque()
        self.last_pose_keys = {}
        self.last_pose_seen = {}
        self.last_identity = None
        self.last_identity_write = 0.0
        self.output.mkdir(parents=True, exist_ok=True)
        self.clock = ReplayClock() if replay else None
        flowlive.time = self.clock or time
        liveview.time = self.clock or time
        self.flows = {}
        self.retired = deque(maxlen=64) if cache_only else []
        self.epochs = {}
        self.session = 0
        self.flow_counter = 0
        self.sequence = None
        self.selected = {}   # account -> flow key
        self.pinned = {}     # account -> flow key
        self.states_by_account = {}
        self.resync_accounts = set()
        self.ended = False
        self.fatal_error = None
        self.stats = Counter()
        self.dirty = True
        self.last_state = self.empty("waiting")
        self.last_nonempty = None
        self._empty_bodies = {name: encoded(self.empty(name)).encode('utf-8') for name in ('waiting','stopped')}
        self._publish_states()
        self.lock = threading.RLock()
        self.state_sequence = 0
        self.states = None if cache_only else (self.output / "states.jsonl").open("x", encoding="utf-8", buffering=1)
        self.errors = (self.output / "parser-errors.jsonl").open("w" if cache_only else "x", encoding="utf-8", buffering=1)
        if cache_only:
            for name in ("snapshot-final.json", "parser-result.json"):
                (self.output / name).unlink(missing_ok=True)
            save(self.output / "state-latest.json", self.last_state)
            self.cache_identity(force=True)

    def cache_identity(self, force=False):
        if not self.cache_only or (not force and time.monotonic() - self.last_identity_write < 1):
            return
        self.last_identity_write = time.monotonic()
        player_fields = ("name", "player_id", "bot", "actor_guid", "owner_guid", "channel", "uin",
                         "team", "camp", "inner_team", "hero_id", "expert_id", "spawn_seq")
        link_fields = ("owner", "ps_guid", "spawn_seq", "cls", "ai_key", "hero", "move_handle", "move_handle_seq")
        flows = []
        for f in self.flows.values():
            flows.append({"identity": f.identity,
                          "players": {str(k): {field: v[field] for field in player_fields if field in v}
                                      for k, v in f.view.players.items()},
                          "characters": {str(k): {field: v[field] for field in link_fields if field in v}
                                         for k, v in f.view.links.items()},
                          "playerChannels": dict(f.view.ps_ch_actor),
                          "characterChannels": dict(f.view.ch_actor),
                          "slotClaims": dict(f.view.claim)})
        identity = {"schema": "gateway-identity-cache/v5", "session": self.session,
                    "restoreSupported": False, "flows": flows}
        serialized = encoded(identity)
        if serialized != self.last_identity:
            save_encoded(self.output / "identity-cache.json", serialized)
            self.last_identity = serialized

    def empty(self, status):
        return {"schema": "gateway-state/v5", "self": None, "entities": [], "names": [],
                "live_active": False, "status": status, "cursor": {"seq": 0, "live": not self.replay}}

    def retire(self, key):
        self._gap_flows.discard(key)
        flow = self.flows.pop(key)
        while not flow.loader.exhausted():
            flow.view.poll(max_frames=256)
        self.retired.append(flow.summary())
        account = flow.account
        for pose_key in list(self.last_pose_keys):
            if pose_key[:2] == (account, flow.identity):
                self.last_pose_keys.pop(pose_key, None)
                self.last_pose_seen.pop(pose_key, None)
        if self.selected.get(account) == key:
            self.selected[account] = None
        if self.pinned.get(account) == key:
            self.pinned[account] = None

    def prune_idle_flows(self, now):
        # Live clients continue sending packets even when players stand still.
        # Replays keep their full history and are never expired by wall time.
        if not self.cache_only or self.replay:
            return
        for key, flow in list(self.flows.items()):
            if now - flow.last_received > LIVE_FLOW_RETENTION_SECONDS:
                self.retire(key)
                self.stats["expiredFlows"] += 1

    def reset(self):
        self._gap_flows.clear()
        for key in list(self.flows):
            self.retire(key)
        self.epochs.clear()
        self.selected.clear()
        self.pinned.clear()
        self.states_by_account.clear()
        self.resync_accounts.clear()
        self.last_state = self.empty("waiting")
        self._publish_states()
        self.cache_identity(force=True)

    def reset_account(self, account):
        # An intentional freshness drop is local to one account. Never let it
        # invalidate the other rooms assigned to the same CPU worker.
        for key, flow in list(self.flows.items()):
            if flow.account == account:
                flow.loader.pending_packets.clear()
                self.retire(key)
        for pair in list(self.epochs):
            if pair[0] == account:
                self.epochs.pop(pair, None)
        self.selected.pop(account, None)
        self.pinned.pop(account, None)
        self.resync_accounts.add(account)
        state = self.empty('resync')
        state['error'] = '数据过载已丢弃旧事件；若无法恢复，请重新进入对局'
        self.states_by_account[account] = state
        if self.last_state.get('account') == account:
            self.last_state = state
        self._publish_states()

    def accept(self, event):
        with self.lock:
            self.dirty = True
            kind = event.get("type")
            if kind == "session_status":
                self.transport = event.get("data") or {}
            data = event.get("data") or {}
            account = str(data.get("account") or "").strip()
            if kind == "parser_fault":
                self.fatal_error = str(data.get("error", "Input failed"))
                self.flush(force=True)
                return
            if kind == "parser_input_complete":
                self.flush(force=True)
                self.ended = True
                return
            if kind == "session_started":
                self.reset()
                self.session += 1
                self.sequence = None
                self.ended = False
            sequence = event.get("sequence")
            if sequence is not None:
                if not isinstance(sequence, int) or sequence < 0:
                    raise ValueError("Invalid event sequence")
                if self.sequence is not None and sequence <= self.sequence:
                    self.stats["duplicateOrOutOfOrder"] += 1
                    return
                if self.sequence is not None and sequence != self.sequence + 1:
                    self.stats["eventGaps"] += sequence - self.sequence - 1
                    if self.preserve_event_context:
                        # A transport gap is not a UE4 session restart. Keep
                        # channel and handshake context, but mark affected flows
                        # degraded until a new session/epoch replaces them.
                        self._gap_flows.update(self.flows)
                        self.stats["contextPreservedGaps"] += 1
                    else:
                        self.reset()
                self.sequence = sequence
            self.stats["inputEvents"] += 1
            if kind == "session_stopped":
                self.flush(force=True)
                self.ended = True
                return
            if kind == "udp_binding_reset":
                for key in list(self.flows):
                    flow = self.flows.get(key)
                    if flow is not None and flow.account == account \
                            and flow.identity[1] + ">" + flow.identity[2] == data.get("flow"):
                        self.retire(key)
                return
            if kind != "udp_plaintext":
                return
            if data.get("validEnvelope") is False:
                self.stats["controlFramesSkipped"] += 1
                return
            if sequence is None:
                raise ValueError("Plaintext packet is missing its sequence")
            direction = {"up": "request", "down": "response"}[data["direction"]]
            timestamp = float(data["timestampMs"]) / 1000
            if not math.isfinite(timestamp) or timestamp <= 0:
                raise ValueError("Invalid packet timestamp")
            payload = base64.b64decode(data["payloadBase64"], validate=True)
            if not 1 <= len(payload) <= 65535 or len(payload) != data["payloadLength"]:
                raise ValueError("Invalid plaintext packet length")
            local, remote, epoch = data["local"], data["remote"], data["epoch"]
            if not isinstance(local, str) or not isinstance(remote, str):
                self.stats["invalidFlowIdentityPackets"] += 1
                return
            if not isinstance(epoch, int) or epoch < 1:
                self.stats["invalidEpochPackets"] += 1
                return
            pair = (account, local, remote)
            if epoch < self.epochs.get(pair, 0):
                self.stats["retiredEpochPackets"] += 1
                return
            if epoch > self.epochs.get(pair, 0):
                for key in list(self.flows):
                    item = self.flows.get(key)
                    if item is not None and item.account == account and item.identity[1:3] == (local, remote):
                        self.retire(key)
                self.epochs[pair] = epoch
            if self.clock:
                self.clock.current = timestamp
            identity = (self.session, local, remote, epoch)
            key = (account,) + identity
            if key not in self.flows:
                if len(self.flows) >= 256:
                    victims = [k for k, f in self.flows.items() if f.account == account] or list(self.flows)
                    victim = min(victims, key=lambda k: self.flows[k].last_received)
                    self.retire(victim)
                    self.stats["evictedFlows"] += 1
                self.flow_counter += 1
                self.flows[key] = Flow(self.output, self.flow_counter, identity, account)
            flow = self.flows[key]
            flow.loader.pending_packets.append((sequence, direction, payload, timestamp))
            flow.packets[direction] += 1
            flow.last_received = time.monotonic()
            flow.last_timestamp, flow.latest_sequence = timestamp, sequence
            self.stats["plaintextPackets"] += 1
            self.stats["upPackets" if direction == "request" else "downPackets"] += 1
            self.rate_events.append((time.monotonic(), "up" if direction == "request" else "down", 1, account))
            if len(flow.loader.pending_packets) >= 256:
                flow.view.poll(max_frames=256)

    def _build_state(self, flow, now):
        if self.fatal_error:
            state = self.empty("error")
            state["error"] = self.fatal_error
            return state
        if flow is None:
            return self.empty("stopped" if self.ended else "waiting")
        if not self.replay and (self.ended or now - flow.last_received > self.idle_seconds):
            return self.empty("stopped" if self.ended else "idle")
        state = flow.view.snapshot(include_trails=False)
        # Preserve the gateway schema without building trails only to discard them.
        for entity in state["entities"]:
            entity.pop("trail", None)
        state.update(schema="gateway-state/v5", status="replay" if self.replay else "live",
                     live_active=not self.replay, session=self.session, epoch=flow.identity[3],
                     local=flow.identity[1], remote=flow.identity[2],
                     account=flow.account, timestampMs=int(flow.last_timestamp * 1000))
        if (flow.account,) + flow.identity in self._gap_flows:
            state.update(status='resync', live_active=False, inputContinuity='gap',
                         error='Input sequence gap; session context retained, waiting for a new session')
        pose = flow.loader.self_pose
        pose_age = (self.clock.time() if self.clock else time.time()) - flow.loader.self_pose_t
        if pose and 0 <= pose_age <= 0.25:
            state.update(self_pitch=round(pose["pitch"], 1), self_aim_yaw=round(pose["yaw"], 1),
                         self_aim_age_ms=round(pose_age * 1000), self_aim_source="upstream")
            if pose_age <= 1:
                state.update(self_up_loc=pose["loc"], self_up_ts=pose["ts"])
                # This is the authenticated account's own movement message,
                # rather than a nearest-player estimate or a predicted position.
                state['self'] = list(pose['loc'])
                state['self_position_source'] = 'upstream'
                state['self_position_age_ms'] = round(pose_age * 1000)
                for entity in state['entities']:
                    world = entity.get('world')
                    if world and len(world) >= 3:
                        entity['rel'] = [world[i]-state['self'][i] for i in range(3)]
        state["cursor"]["live"] = not self.replay
        account = flow.account
        for entity in state.get("entities", []):
            if entity.get("kind") == "loot":
                entity["identity_status"] = ("missing_item_id" if not entity.get("item_id") else
                    "name_not_mapped" if entity.get("name") in (None, "", "物资") else "identified")
                entity["metadata_status"] = "available" if entity.get("price") is not None else "missing"
            if entity.get("kind") not in ("player", "mate", "unknown"):
                continue
            slot = entity.get("slot")
            track = flow.view.slots.get(slot) or []
            key = (account, flow.identity, entity.get("key"))
            if track:
                seq = track[-1][0]
                prev = self.last_pose_keys.get(key, -1)
                count = (len(track) - bisect.bisect_right(track, prev, key=_SAMPLE_SEQ)
                         if seq > prev else 0)
                if count:
                    self.last_pose_keys[key] = seq
                    self.last_pose_seen[key] = now
                    self.rate_events.append((now, "characters", count, account))
                    if not entity.get("is_bot"):
                        self.rate_events.append((now, "human", count, account))
                entity["pose_seq"] = seq
                entity["pose_timestamp_ms"] = int(track[-1][1] * 1000)
                entity["pose_age_ms"] = round((now - self.last_pose_seen.get(key, now)) * 1000)
        pose_key = (account, flow.identity, "self")
        if flow.loader.pose_samples != self.last_pose_keys.get(pose_key, 0):
            count = flow.loader.pose_samples - self.last_pose_keys.get(pose_key, 0)
            self.last_pose_keys[pose_key] = flow.loader.pose_samples
            self.last_pose_seen[pose_key] = now
            self.rate_events.append((now, "self", max(0, count), account))
        return state

    def flush(self, force=False, accounts=None):
        with self.lock:
            for flow in self.flows.values():
                while not flow.loader.exhausted():
                    flow.view.poll(max_frames=256)
            now = time.monotonic()
            self.prune_idle_flows(now)
            eligible_by_account = {}
            for key, f in self.flows.items():
                if f.loader.hs.ok:
                    eligible_by_account.setdefault(f.account, []).append(key)

            states = {}
            for account, eligible in eligible_by_account.items():
                pinned = self.pinned.get(account)
                selected = pinned if pinned in eligible else max(
                    eligible, key=lambda k: self.flows[k].latest_sequence)
                self.selected[account] = selected
                if accounts is not None and account not in accounts and not force:
                    states[account]=self.states_by_account.get(account) or self.empty('waiting')
                else:
                    states[account] = self._build_state(self.flows.get(selected), now)
                self.resync_accounts.discard(account)

            known_accounts = set(self.selected) | set(eligible_by_account) | {f.account for f in self.flows.values()}
            for account in known_accounts:
                if account not in states:
                    self.selected[account] = None
                    states[account] = self._build_state(None, now)
                    if account in self.resync_accounts:
                        states[account]['status'] = 'resync'
                        states[account]['error'] = '数据过载已丢弃旧事件；请重新进入对局恢复'
            self.states_by_account.update(states)

            all_eligible = [k for keys in eligible_by_account.values() for k in keys]
            legacy_key = max(all_eligible, key=lambda k: self.flows[k].latest_sequence) if all_eligible else None
            if legacy_key is not None:
                legacy_account = self.flows[legacy_key].account
                state = states.get(legacy_account) or self._build_state(self.flows.get(legacy_key), now)
            else:
                state = self._build_state(None, now)
            if self.timeline:
                replay_state = self.timeline.status()
                for item in list(states.values()) + [state]:
                    item["replay"] = replay_state
                    item["cursor"] = dict(seq=replay_state["position"], lo=0, hi=replay_state["duration"],
                                          t_lo=0, t_hi=replay_state["duration"], live=False)
            if state != self.last_state or force:
                self.last_state = state
                if state.get("self") is not None or state.get("entities"):
                    self.last_nonempty = state
                self.state_sequence += 1
                event = {"schema": "gateway-semantic/v5", "type": "state", "sequence": self.state_sequence,
                         "timestampMs": int(time.time() * 1000), "data": state}
                if self.states is not None:
                    self.states.write(encoded(event) + "\n")
                if force or now - self.last_disk_write >= 0.5:
                    save(self.output / "state-latest.json", state)
                    self.last_disk_write = now
                    save(self.output / "status-latest.json", self.status())
            self._publish_states(reuse_accounts=(set(self.states_by_account)-accounts
                                 if accounts is not None and not force else ()))
            self.cache_identity(force=force)

    def error(self, message):
        with self.lock:
            self.stats["inputErrors"] += 1
            if self.cache_only and self.errors.tell() >= 1024 * 1024:
                self.errors.seek(0)
                self.errors.truncate()
            self.errors.write(encoded({"sequence": self.sequence, "error": str(message)[:8192]}) + "\n")
            self.reset()

    def _publish_states(self,reuse_accounts=()):
        # Writer holds the engine lock. Publish one complete immutable byte
        # snapshot, rather than making every viewer wait behind decoding work.
        bodies = {}
        default = None
        previous_bodies=getattr(self,'_published',({},None))[0]
        previous_states=getattr(self,'_published_states',{})
        for account, state in self.states_by_account.items():
            body = (previous_bodies[account] if account in reuse_accounts and previous_states.get(account) is state and account in previous_bodies
                    else encoded(state).encode('utf-8'))
            bodies[account] = body
            if state is self.last_state:
                default = body
        self._published = (bodies, default if default is not None else
                           encoded(self.last_state).encode('utf-8'))
        self._published_states=dict(self.states_by_account)

    def api_state(self, account=""):
        bodies, default = self._published
        if account:
            return bodies.get(account, self._empty_bodies['stopped' if self.ended else 'waiting'])
        return default

    def api_flows(self, account=""):
        with self.lock:
            flows = [f for f in self.flows.values() if not account or f.account == account]
            top = [{"flow": str(f.number), "n_ps": len(f.view.players)} for f in flows]
            selected = self.flows.get(self.selected.get(account)) if account else None
            if selected is None and not account:
                for key in self.selected.values():
                    if key:
                        selected = self.flows.get(key)
                        break
            return {"runs": [{"run": self.output.name, "top": top}],
                    "current": [self.output.name, str(selected.number)] if selected else None}

    def api_select(self, run, flow, account=""):
        with self.lock:
            for key, item in self.flows.items():
                if item.account != account:
                    continue
                if run == self.output.name and str(item.number) == flow and item.loader.hs.ok:
                    self.pinned[account] = key
                    return {"ok": True}
            return {"ok": False, "error": "Flow is not active"}

    def restart_replay(self):
        with self.lock:
            self.flows.clear()
            self.retired.clear()
            self.epochs.clear()
            self.sequence = None
            self.selected.clear()
            self.pinned.clear()
            self.states_by_account.clear()
            self.session = self.flow_counter = 0
            self.stats.clear()
            self.rate_events.clear()
            self.last_pose_keys.clear()
            self.last_pose_seen.clear()
            self.ended = False
            self.fatal_error = None
            self.last_nonempty = None
            self.last_state = self.empty("seeking")
            self._publish_states()

    def api_ctrl(self, query):
        if self.timeline:
            try: return self.timeline.control(query)
            except (ValueError, TypeError) as exc: return {"ok": False, "error": str(exc)}
        return {"ok": False, "live": not self.replay, "error": "This view follows the input stream"}

    def status(self, account=""):
        with self.lock:
            now = time.monotonic()
            while self.rate_events and self.rate_events[0][0] < now - 2:
                self.rate_events.popleft()
            rates = Counter()
            for entry in self.rate_events:
                entry_account = entry[3] if len(entry) > 3 else ""
                if account and entry_account != account:
                    continue
                rates[entry[1]] += entry[2] / 2
            state = (self.states_by_account.get(account) if account else self.last_state) or self.empty("waiting")
            entities = state.get("entities", [])
            counts = Counter(e.get("kind", "unknown") for e in entities)
            counts["human"] = sum(e.get("kind") in ("player", "mate") and not e.get("is_bot") for e in entities)
            counts["bot"] = sum(e.get("kind") == "ai" or bool(e.get("is_bot")) for e in entities)
            counts["characters"] = sum(e.get("kind") in ("player", "mate", "unknown") for e in entities)
            counts["charactersIncludingSelf"] = counts["characters"] + int(state.get("self") is not None)
            counts["confirmedBots"] = sum(e.get("kind") in ("player", "mate", "unknown") and bool(e.get("is_bot")) for e in entities)
            # Compatibility alias for older local consumers. is_bot is already multi-field confirmed.
            counts["suspectedBots"] = counts["confirmedBots"]
            missing = sum(e.get("kind") == "loot" and not e.get("item_id") for e in entities)
            retired = [r for r in self.retired if not account or r.get("account") == account]
            flows = [f.summary() for f in self.flows.values() if not account or f.account == account]
            return {"schema": "gateway-parser-status/v5", "stats": dict(self.stats),
                    "counts": dict(counts), "unknownLoot": missing, "rates": dict(rates),
                    "transport": self.transport, "journalReadBytes": self.journal_read_bytes,
                    "replay": self.timeline.status() if self.timeline else None,
                    "account": account or None,
                    "flows": retired + flows,
                    "states": self.state_sequence, "ended": self.ended, "error": self.fatal_error}

    def close(self):
        self.flush(force=True)
        save(self.output / "snapshot-final.json", self.last_nonempty or self.last_state)
        save(self.output / "parser-result.json", self.status())
        if self.states is not None:
            self.states.close()
        self.errors.close()


class GatewayHTTPServer(er_radar.ThreadingHTTPServer):
    # Persistent state readers should not compete for a five-connection backlog.
    request_queue_size = 128
    daemon_threads = True


class Handler(er_radar.Handler):
    protocol_version = "HTTP/1.1"
    timeout = 15
    disable_nagle_algorithm = True

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            # Normal viewer disconnects are not parser failures.
            pass

    def end_headers(self):
        # Some inherited error routes have no response length. Close those
        # connections explicitly so HTTP/1.1 clients never wait for a body forever.
        if not any(line.lower().startswith(b"content-length:")
                   for line in self._headers_buffer):
            self.send_header("Connection", "close")
            self.close_connection = True
        super().end_headers()

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/resources/"):
            ASSET_STORE.serve(self, path, prefix="/resources/")
            return
        if path.startswith("/ui/"):
            er_radar.m3d_serve(self, path, prefix="/ui/", root=ROOT / "web/ui")
            return
        if path.startswith("/m3d/") and (path.endswith(".glb") or path.endswith("manifest.json")):
            MODEL_STORE.serve(self, path, prefix="/m3d/")
        elif path == "/api/route":
            query = parse_qs(urlparse(self.path).query)
            route=getattr(self.app,'api_route',None)
            self._json(route(query.get('account',[''])[0]) if route else dict(port=None))
        elif path == "/api/status":
            query = parse_qs(urlparse(self.path).query)
            self._json(self.app.status(query.get("account", [""])[0]))
        elif path.startswith("/vendor/"):
            er_radar.m3d_serve(self, path, prefix="/vendor/", root=ROOT / "web/vendor")
        else:
            super().do_GET()


def prepare_page():
    er_radar.PAGE = er_radar.PAGE.replace("https://unpkg.com/leaflet@1.9.4/dist/", "/vendor/leaflet/")
    er_radar.PAGE = er_radar.PAGE.replace("<title>DFM 战术雷达</title>", "<title>雷达页</title>")
    # Temporarily disable the 3D entry until the critical bugs are fixed.
    er_radar.PAGE = er_radar.PAGE.replace("const NO3D=0;", "const NO3D=1;")


def start_http(engine, port):
    prepare_page()
    Handler.app = er_radar.Handler.app = engine
    map_info = er_radar.load_map_info()
    for item in map_info.get('maps', []):
        item['tileUrl'] = '/resources/maps2d/' + item['key'] + '/{z}_{x}_{y}.jpg'
    Handler.map_info = er_radar.Handler.map_info = map_info
    server = GatewayHTTPServer(('127.0.0.1', port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    global ASSETS, ASSET_STORE, MODEL_STORE
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--port", type=int, default=17891)
    ap.add_argument("--source", type=Path)
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--cache-only", action="store_true")
    asset_group = ap.add_mutually_exclusive_group()
    asset_group.add_argument("--assets", type=Path)
    asset_group.add_argument("--asset-pak", type=Path)
    ap.add_argument("--models", type=Path)
    ap.add_argument("--journal", type=Path)
    ap.add_argument("--timeline", type=Path)
    ap.add_argument("--parent-pid", type=int)
    ap.add_argument('--workers', type=int, default=int(os.environ.get('RADAR_PARSER_WORKERS',
                    str(max(1, min(32, (os.cpu_count() or 4) - 2))))))
    args = ap.parse_args()
    if not 1<=args.workers<=64:
        ap.error('--workers must be within 1..64')
    if args.assets:
        ASSETS = args.assets
        ASSET_STORE = AssetStore(directory=args.assets)
    elif args.asset_pak:
        ASSET_STORE = AssetStore(archive=args.asset_pak)
    if args.models:
        MODEL_STORE = AssetStore(directory=args.models)
    if args.workers > 1 and args.cache_only and not any((args.source, args.journal, args.timeline, args.replay)):
        from parser_pool import serve_pool
        return serve_pool(args, start_http)
    engine = Engine(args.output, replay=args.replay or bool(args.timeline), cache_only=args.cache_only)
    stop = threading.Event()
    if args.timeline:
        engine.timeline = Timeline(engine, args.timeline, stop)
    server = start_http(engine, args.port)
    if args.parent_pid and os.name != "nt":
        def watch_parent_posix():
            while os.getppid() == args.parent_pid:
                time.sleep(1)
            os._exit(3)
        threading.Thread(target=watch_parent_posix, daemon=True).start()
    if args.parent_pid and os.name == "nt":
        def watch_parent():
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x00100000, False, args.parent_pid)
            if handle:
                kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
                kernel.CloseHandle(handle)
            os._exit(3)
        threading.Thread(target=watch_parent, daemon=True).start()
    print(encoded({"type": "parser_ready", "port": server.server_port, "schema": "gateway-state/v5"}), flush=True)
    if args.timeline:
        worker = threading.Thread(target=engine.timeline.run, daemon=True)
        worker.start()
        try:
            # stdin EOF is the parent's clean shutdown signal.
            for _ in sys.stdin.buffer:
                pass
        finally:
            stop.set(); worker.join(timeout=30)
            engine.close(); server.shutdown(); server.server_close()
            print(encoded({"type": "parser_completed", "result": engine.status()}), flush=True)
        return
    inbox = queue.Queue(maxsize=4096)
    def read_input():
        source = None
        try:
            if args.journal:
                def parent_eof():
                    for _ in sys.stdin.buffer:
                        pass
                    stop.set()
                threading.Thread(target=parent_eof, daemon=True).start()
                source = args.journal.open("rb")
                pending = b""
                while True:
                    chunk = source.readline(MAX_LINE + 1)
                    if chunk:
                        pending += chunk
                        if len(pending) > MAX_LINE:
                            raise ValueError("Journal line exceeds limit")
                        if pending.endswith(b"\n"):
                            inbox.put(pending)
                            engine.journal_read_bytes = source.tell()
                            pending = b""
                        continue
                    if stop.is_set():
                        if pending:
                            raise ValueError("Incomplete final journal record")
                        break
                    stop.wait(0.005)
                return
            source = args.source.open("rb") if args.source else sys.stdin.buffer
            while True:
                line = source.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    inbox.put(ValueError("JSONL line exceeds limit"))
                    break
                inbox.put(line)
        except Exception as exc:
            inbox.put(exc)
        finally:
            if (args.source or args.journal) and source is not None:
                source.close()
            inbox.put(None)
    threading.Thread(target=read_input, daemon=True).start()
    last_flush = time.monotonic()
    try:
        done = False
        while not done:
            try:
                line = inbox.get(timeout=0.015)
            except queue.Empty:
                line = b""
            for _ in range(256):
                if line is None:
                    done = True
                    break
                if isinstance(line, Exception):
                    raise line
                if line.strip():
                    try:
                        event = json.loads(line.decode("utf-8-sig"))
                        engine.accept(event)
                        if event.get("type") == "parser_input_complete":
                            print(encoded({"type": "parser_input_drained"}), flush=True)
                    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
                        engine.error(exc)
                if _ == 255:
                    break
                try:
                    line = inbox.get_nowait()
                except queue.Empty:
                    break
            _now = time.monotonic()
            _interval = 1 / 20 if engine.dirty else 1.0
            if _now - last_flush >= _interval or done:
                engine.flush()
                engine.dirty = False
                last_flush = _now
        engine.close()
        print(encoded({"type": "parser_completed", "result": engine.status()}), flush=True)
    except Exception as exc:
        engine.fatal_error = str(exc)
        engine.close()
        raise
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
