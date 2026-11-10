"""Loopback request bridge from Scrunch to the Live Object Model.

Threading: socket threads only parse and enqueue. Every Live Object Model call
runs on Live's main thread inside update_display() (~10 Hz), a bounded number
of jobs per tick. Replies are handed to a per-connection writer thread so a
slow client cannot block Live.

Deferred readback: host state changed by a write is not assumed observable on
the same tick. After any write, reads wait until SETTLE_TICKS later ticks have
passed. Transport commands reply with state read after that settle period.

Protocol: newline-delimited JSON on 127.0.0.1.
  request  {"v":1,"id":"<unique>","token":"...","op":"...","args":{...}}
  response {"v":1,"id":"...","ok":true,"result":...}
         | {"v":1,"id":"...","ok":false,"error":{"code":"...","message":"..."}}
Error codes: unauthorized, invalid, unsupported, not_found, conflict, unverified, host, shutdown.
  conflict: the target no longer matches what the caller read, or is disabled/automated; nothing written.
  unverified: a write was sent but readback did not confirm it; it is never retried.
Positions and lengths are quarter-note beats; notes are {p,t,d,v}.
"""
import inspect
import json
import os
import queue
import secrets
import socket
import threading
import time
import traceback

from .host_ops import HostOps, MONITORING_VALUE, parse_label

PROTOCOL = 1
# Bump whenever behaviour changes so an attended test can prove Live loaded this code.
BRIDGE_VERSION = "2026-09-27.2"
HOST = "127.0.0.1"
DEFAULT_PORT = 47831
SETTLE_TICKS = 3
MAX_JOBS_PER_TICK = 4
RECENT_REQUESTS = 256
MAX_LINE = 1 << 20
CONFIG_PATH = os.path.expanduser("~/Library/Application Support/Scrunch/remote-script-bridge.json")

WRITE_OPS = {"create_midi_track", "update_track", "create_arrangement_clip", "delete_clip",
             "delete_track", "stop_playback", "stop_session_clips", "play_arrangement",
             "set_arrangement_loop", "move_playhead", "set_device_parameter",
             "set_parameters_by_name", "set_device_parameters", "verify_last_write", "rack_variation", "create_native_device", "delete_device",
             "duplicate_track", "replace_clip_notes", "select_track", "select_clip", "open_plugin_window",
             "start_sweep", "stop_sweep", "load_device", "reload", "select_device", "set_cc_control",
             "record_automation", "stop_automation_take", "undo_automation_take", "sample_automation"}
# While a sweep runs only these writes are accepted; anything else could fight the moving knob.
SWEEP_WRITES = {"stop_sweep", "stop_playback", "play_arrangement", "set_arrangement_loop", "move_playhead"}
MAX_SWEEP_SECONDS = 120.0
# While Scrunch records automation only these writes are accepted.
AUTOMATION_TAKE_WRITES = {"stop_automation_take", "stop_playback"}
TRACK_FIELDS = {"name", "mute", "solo", "arm", "gainDb", "monitoring"}
# Explicitly unavailable in this bridge; clients must not emulate them.
UNSUPPORTED = {
    "read_device_routing": "Device routing, sidechain options, drum pads and rack macro mappings are not exposed.",
    "update_device_structure": "Moving devices and loading plug-ins or presets are not implemented; only native device insertion and deletion are.",
    "write_automation": ("Live 12.3.5 exposes no arrangement envelope: breakpoints cannot be listed, drawn or deleted. "
                         "Use record_automation (recorded on Live's clock), sample_automation and undo_automation_take."),
}


MAX_DEVICE_DEPTH = 8
MISSING = object()
DEVICE_ROLES = {1: "instrument", 2: "audio-effect", 4: "midi-effect"}
PARAM_STATES = {0: "enabled", 1: "irrelevant", 2: "disabled"}
AUTOMATION_STATES = {0: "none", 1: "playing", 2: "overridden"}
AUTOMATION_NAMES = AUTOMATION_STATES


def _get(obj, attr):
    """Feature-detected attribute read. Live raises RuntimeError for some attributes
    on some parameters (e.g. default_value: "There is no default value available for
    this type of parameter", Live 12.3.5, 17 Sep 2026); treat that as unavailable."""
    try:
        return getattr(obj, attr)
    except Exception:
        return MISSING


def _attr(value, default):
    return default if value is MISSING else value


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if _is_number(number) else None


def _bool_or_none(value):
    return None if value is MISSING or value is None else bool(value)


def _enum_name(names, value):
    if value is None:
        return None
    try:
        return names.get(int(value), "unknown:%s" % value)
    except (TypeError, ValueError):
        return "unknown:%s" % value


def _plugin_kind(class_name):
    return {"PluginDevice": "vst", "AuPluginDevice": "au", "MxDeviceAudioEffect": "max",
            "MxDeviceInstrument": "max", "MxDeviceMidiEffect": "max"}.get(class_name)


def _tolerance(record):
    """Readback tolerance: exact steps for discrete parameters, tiny span fraction otherwise."""
    if record.get("quantized"):
        return 1e-6
    span = (record.get("max") or 0) - (record.get("min") or 0)
    return max(abs(span) * 1e-5, 1e-7)


class BridgeError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


class Defer(object):
    """Reply later: run `then` once `ticks` further ticks have elapsed."""
    def __init__(self, ticks, then):
        self.ticks = ticks
        self.then = then


def _note_spec_factory():
    import Live  # only available inside Ableton
    return lambda p, t, d, v: Live.Clip.MidiNoteSpecification(pitch=p, start_time=t, duration=d, velocity=v, mute=False)


class Connection(object):
    def __init__(self, sock):
        self.sock = sock
        self.outbox = queue.Queue()
        self.open = True
        threading.Thread(target=self._writer, name="scrunch-bridge-writer", daemon=True).start()

    def send(self, payload):
        if self.open:
            self.outbox.put(payload)

    def _writer(self):
        while True:
            payload = self.outbox.get()
            if payload is None or not self.open:
                return
            try:
                self.sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            except OSError:
                self.close()
                return

    def close(self):
        if self.open:
            self.open = False
            self.outbox.put(None)
            try:
                self.sock.close()
            except OSError:
                pass


class BridgeCore(HostOps):
    """Host-independent server and scheduler. `song_provider` returns Live's Song."""

    def __init__(self, song_provider, port=DEFAULT_PORT, config_path=CONFIG_PATH, token=None,
                 note_spec=None, log=None, settle_ticks=SETTLE_TICKS, app_provider=None, clock=time.monotonic):
        self._song = song_provider
        self._app = app_provider
        self._port = port
        self._config_path = config_path
        self.token = token or secrets.token_hex(24)
        self._note_spec = note_spec
        self._log = log or (lambda message: None)
        self.settle_ticks = settle_ticks
        self._clock = clock
        self._sweep = None
        self._user_loop = None       # John's loop (on, start, length) before Scrunch playback changed it
        self._scrunch_loop = None    # (the loop Scrunch playback set, heard playing yet)
        self._pending_write = None
        self._lock = threading.Lock()
        self._jobs = []
        self._deferred = []
        self._tick = 0
        self._quiet_until = 0
        self._recent = {}
        self._recent_order = []
        self._server = None
        self._connections = set()
        self._stopping = False
        self.port = None

    # ---- lifecycle ----
    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((HOST, self._port))
        srv.listen(4)
        self._server = srv
        self.port = srv.getsockname()[1]
        self._write_config()
        threading.Thread(target=self._accept, name="scrunch-bridge-accept", daemon=True).start()
        self._log("Scrunch bridge listening on %s:%d" % (HOST, self.port))

    def stop(self):
        self._stopping = True
        try:
            if self._server:
                self._server.close()
        except OSError:
            pass
        with self._lock:
            connections = list(self._connections)
            self._jobs = []
            self._deferred = []
        for conn in connections:
            conn.close()
        try:
            with open(self._config_path) as fh:
                if json.load(fh).get("token") == self.token:
                    os.remove(self._config_path)
        except (OSError, ValueError):
            pass

    def _write_config(self):
        folder = os.path.dirname(self._config_path)
        os.makedirs(folder, exist_ok=True)
        temp = self._config_path + ".tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"protocol": PROTOCOL, "host": HOST, "port": self.port, "token": self.token,
                       "pid": os.getpid()}, fh)
        os.replace(temp, self._config_path)

    # ---- socket threads: parse and enqueue only ----
    def _accept(self):
        while not self._stopping:
            try:
                sock, _ = self._server.accept()
            except OSError:
                return
            conn = Connection(sock)
            with self._lock:
                self._connections.add(conn)
            threading.Thread(target=self._read, args=(conn,), name="scrunch-bridge-reader", daemon=True).start()

    def _read(self, conn):
        buf = b""
        try:
            while conn.open:
                chunk = conn.sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_LINE and b"\n" not in buf:
                    conn.send({"v": PROTOCOL, "id": None, "ok": False,
                               "error": {"code": "invalid", "message": "Request too large."}})
                    break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._accept_line(conn, line)
        except OSError:
            pass
        finally:
            conn.close()
            with self._lock:
                self._connections.discard(conn)

    def _accept_line(self, conn, line):
        try:
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError()
        except ValueError:
            conn.send({"v": PROTOCOL, "id": None, "ok": False, "error": {"code": "invalid", "message": "Malformed JSON request."}})
            return
        rid = request.get("id")
        if request.get("v") != PROTOCOL or not isinstance(rid, str) or not rid or len(rid) > 128:
            conn.send({"v": PROTOCOL, "id": rid if isinstance(rid, str) else None, "ok": False,
                       "error": {"code": "invalid", "message": "Unsupported protocol version or request id."}})
            return
        if not isinstance(request.get("token"), str) or not secrets.compare_digest(request["token"], self.token):
            conn.send({"v": PROTOCOL, "id": rid, "ok": False, "error": {"code": "unauthorized", "message": "Bridge token rejected."}})
            return
        with self._lock:
            if rid in self._recent:
                cached = self._recent[rid]
                if cached is not None:
                    conn.send(cached)
                    return
                # Still running: never execute the same request twice.
                conn.send({"v": PROTOCOL, "id": rid, "ok": False,
                           "error": {"code": "invalid", "message": "Duplicate request id is still executing."}})
                return
            self._remember(rid, None)
            self._jobs.append((conn, request))

    def _remember(self, rid, response):
        if rid not in self._recent:
            self._recent_order.append(rid)
            while len(self._recent_order) > RECENT_REQUESTS:
                self._recent.pop(self._recent_order.pop(0), None)
        self._recent[rid] = response

    # ---- main thread ----
    def tick(self):
        """Call from update_display(). Executes bounded work on Live's thread."""
        self._tick += 1
        with self._lock:
            ready = [d for d in self._deferred if d[0] <= self._tick]
            self._deferred = [d for d in self._deferred if d[0] > self._tick]
        for _, conn, request, then in ready:
            self._finish(conn, request, then)
        self._advance_sweep()
        self._advance_automation_take()
        self._watch_loop()
        executed = 0
        while executed < MAX_JOBS_PER_TICK:
            with self._lock:
                if not self._jobs:
                    return
                conn, request = self._jobs[0]
                write = request.get("op") in WRITE_OPS
                if not conn.open:
                    # Client gave up before execution: drop it, including writes.
                    self._jobs.pop(0)
                    self._recent.pop(request["id"], None)
                    continue
                if not write and self._tick < self._quiet_until:
                    return  # let the previous write settle before reading
                self._jobs.pop(0)
            executed += 1
            self._finish(conn, request, lambda r=request: self._execute(r))
            if write:
                self._quiet_until = self._tick + self.settle_ticks
                return  # one write per tick

    def _finish(self, conn, request, work):
        try:
            result = work()
            if isinstance(result, Defer):
                with self._lock:
                    self._deferred.append((self._tick + max(1, result.ticks), conn, request, result.then))
                return
            response = {"v": PROTOCOL, "id": request["id"], "ok": True, "result": result}
        except BridgeError as exc:
            response = {"v": PROTOCOL, "id": request["id"], "ok": False, "error": {"code": exc.code, "message": str(exc)}}
        except Exception as exc:
            self._log("Scrunch bridge op failed: " + traceback.format_exc())
            response = {"v": PROTOCOL, "id": request["id"], "ok": False,
                        "error": {"code": "host", "message": "%s: %s" % (type(exc).__name__, exc)}}
        with self._lock:
            self._remember(request["id"], response)
        conn.send(response)

    def _execute(self, request):
        op = request.get("op")
        args = request.get("args") or {}
        if not isinstance(op, str) or not isinstance(args, dict):
            raise BridgeError("invalid", "Operation and arguments are required.")
        if op in UNSUPPORTED:
            raise BridgeError("unsupported", UNSUPPORTED[op])
        if self._sweep is not None and op in WRITE_OPS and op not in SWEEP_WRITES:
            raise BridgeError("conflict", "A knob sweep is running on %r; stop it before other changes. Nothing was changed." % self._sweep["name"])
        if getattr(self, "_auto_take", None) is not None and op in WRITE_OPS and op not in AUTOMATION_TAKE_WRITES:
            raise BridgeError("conflict", "Scrunch is recording automation; wait for it or stop it first. Nothing was changed.")
        handler = getattr(self, "op_" + op, None)
        if handler is None:
            raise BridgeError("unsupported", "Unknown operation: %s" % op)
        try:
            inspect.signature(handler).bind(**args)
        except TypeError as exc:
            raise BridgeError("invalid", "Invalid arguments for %s: %s" % (op, exc))
        return handler(**args)

    @staticmethod
    def _error(code, message):
        return BridgeError(code, message)

    def _defer(self, then):
        return Defer(self.settle_ticks, then)

    @staticmethod
    def _default_note_spec():
        return _note_spec_factory()

    # ---- object identity ----
    @staticmethod
    def _ptr(obj):
        ptr = getattr(obj, "_live_ptr", None)
        if not isinstance(ptr, int):
            raise BridgeError("unsupported", "This Live build does not expose stable object references.")
        return ptr

    def _track_id(self, track):
        return "track:%d" % self._ptr(track)

    def _clip_id(self, clip):
        return "clip:%d" % self._ptr(clip)

    def _find_track(self, ident):
        for index, track in enumerate(self._song().tracks):
            if isinstance(ident, str) and self._track_id(track) == ident:
                return index, track
        raise BridgeError("not_found", "Track no longer exists.")

    def _find_clip(self, ident):
        for track in self._song().tracks:
            for clip in self._arrangement_clips(track):
                if isinstance(ident, str) and self._clip_id(clip) == ident:
                    return track, clip
        raise BridgeError("not_found", "Arrangement clip no longer exists.")

    # ---- reads ----
    def op_hello(self):
        return {"protocol": PROTOCOL, "bridge": "scrunch-remote-script", "version": BRIDGE_VERSION, "settleTicks": self.settle_ticks,
                "unsupported": sorted(UNSUPPORTED)}

    def op_connect(self):
        song = self._song()
        # The Set's own file path, so clients need not read Live's window (which is unreadable
        # while Live sits on another Space or behind other apps). Empty for an unsaved Set.
        path = getattr(song, "file_path", None)
        return {"tempo": float(song.tempo),
                "timeSignature": "%d/%d" % (song.signature_numerator, song.signature_denominator),
                "filePath": str(path) if isinstance(path, str) else ""}

    @staticmethod
    def _arrangement_clips(track):
        # Live raises on this property for group tracks, rather than returning ().
        if getattr(track, "is_foldable", False):
            return ()
        return getattr(track, "arrangement_clips", ()) or ()

    def _summary(self, index, track):
        return {"id": self._track_id(track), "trackIndex": index, "name": track.name,
                "color": int(getattr(track, "color", 0) or 0),
                "kind": "midi" if getattr(track, "has_midi_input", False) else "audio",
                "muted": bool(track.mute), "soloed": bool(track.solo),
                "mutedViaSolo": bool(getattr(track, "muted_via_solo", False)),
                "arrangementClipCount": len(self._arrangement_clips(track)),
                "sessionClipCount": sum(1 for slot in getattr(track, "clip_slots", ()) if slot.has_clip)}

    def op_session(self):
        song = self._song()
        return {"tempo": float(song.tempo), "tracks": [self._summary(i, t) for i, t in enumerate(song.tracks)]}

    def op_track(self, id, clips=False, devices=False, mixer=False, routing=False):
        index, track = self._find_track(id)
        out = self._summary(index, track)
        instruments = [d for d in track.devices if getattr(d, "type", None) == 1]
        out["instrument"] = instruments[0].class_display_name if instruments else None
        if clips:
            out["clips"] = [{"id": self._clip_id(c), "name": c.name, "start": float(c.start_time),
                             "span": float(c.end_time - c.start_time)} for c in self._arrangement_clips(track)]
        if devices:
            # Same role/deviceName split as Producer Pal's "<type>: <class>" (racks carry no class name).
            out["devices"] = []
            for d in track.devices:
                role, shown = self._role_and_class(d)
                out["devices"].append({"id": "device:%d" % self._ptr(d), "name": d.name, "role": role, "deviceName": shown})
        if mixer:
            gain = self._gain_db(track)
            if gain is not None:
                out["gainDb"] = gain
        if routing:
            route = getattr(track, "output_routing_type", None)
            out["mainOutput"] = bool(route is not None and route.display_name == "Main")
        return out

    def op_clip(self, id, notes=True):
        track, clip = self._find_clip(id)
        out = {"id": self._clip_id(clip), "name": clip.name, "kind": "midi" if clip.is_midi_clip else "audio",
               "view": "arrangement", "start": float(clip.start_time), "length": float(clip.length),
               "span": float(clip.end_time - clip.start_time), "offset": float(clip.start_marker),
               "looping": bool(clip.looping),
               "loopStart": float(getattr(clip, "loop_start", 0)),
               "loopEnd": float(getattr(clip, "loop_end", clip.length)),
               "timeSignature": "%d/%d" % (clip.signature_numerator, clip.signature_denominator), "notes": None}
        if not clip.is_midi_clip:
            # An audio clip's source file as Live reports it (the mix preflight's evidence of where the audio came
            # from; added 26 Sep 2026). Feature-detected: a missing or failing attribute is left out, never guessed.
            for key, attr, kind in (("filePath", "file_path", str), ("sampleRate", "sample_rate", float),
                                    ("sampleLength", "sample_length", int), ("warping", "warping", bool)):
                value = _get(clip, attr)
                if value is not MISSING and value is not None and (kind is str and isinstance(value, str) or
                                                                   kind is bool and isinstance(value, bool) or
                                                                   kind in (int, float) and _is_number(value)):
                    out[key] = kind(value)
            # Warp markers (27 Sep 2026, audio-to-MIDI placement): clip beat -> seconds in the file. Any unreadable
            # marker leaves the whole list out, so a partial map is never mistaken for the real one.
            markers = _get(clip, "warp_markers")
            if markers is not MISSING and markers is not None:
                rows = []
                for m in markers:
                    beat, seconds = _get(m, "beat_time"), _get(m, "sample_time")
                    if not (_is_number(beat) and _is_number(seconds)):
                        rows = None
                        break
                    rows.append({"beat": float(beat), "seconds": float(seconds)})
                if rows is not None:
                    out["warpMarkers"] = rows
        if notes and clip.is_midi_clip:
            rows = clip.get_notes_extended(0, 128, 0.0, max(float(clip.length) + float(clip.start_marker), out['loopEnd']))
            if any(getattr(n, "mute", False) for n in rows):
                raise BridgeError("unsupported", "Clip contains muted notes, which Scrunch notes cannot represent.")
            out["notes"] = [{"p": int(n.pitch), "t": float(n.start_time), "d": float(n.duration), "v": int(round(n.velocity))}
                            for n in rows]
            out["expression"] = self._phrase_expression(track, clip, rows)
            out["expressionComplete"] = not out["expression"]["differs"] and not out["expression"]["unreadable"]
        return out

    def _phrase_expression(self, track, clip, rows):
        """What else shapes this clip's playback at the instrument beyond pitch/start/duration/velocity (27 Sep 2026).
        `differs` lists what is present but not represented in the notes; `unreadable` what Live would not report.
        The phrase is complete only when both are empty. MPE per-note expression has no Live API readback: it is
        named in `unobservable`, never assumed absent."""
        differs, unreadable = [], []
        for label, attr, default in (("probability", "probability", 1.0), ("velocity deviation", "velocity_deviation", 0.0),
                                     ("release velocity", "release_velocity", 64.0)):
            values = [_get(n, attr) for n in rows]
            if any(v is MISSING or not _is_number(v) for v in values):
                unreadable.append("note " + label)
            elif any(abs(float(v) - default) > 1e-6 for v in values):
                differs.append("%d notes with %s" % (sum(abs(float(v) - default) > 1e-6 for v in values), label))
        envelopes = _get(clip, "has_envelopes")
        if envelopes is MISSING: unreadable.append("clip envelopes")
        elif envelopes: differs.append("clip envelopes")
        groove = _get(clip, "groove")
        if groove is MISSING: unreadable.append("clip groove")
        elif groove is not None:
            # A groove moves notes only through the Set's global groove amount and its own non-zero amounts.
            name = _attr(_get(groove, "name"), "?")
            song_amount = _get(self._song(), "groove_amount")
            amounts = [_get(groove, a) for a in ("quantization_amount", "timing_amount", "random_amount", "velocity_amount")]
            if not _is_number(song_amount) or any(not _is_number(a) for a in amounts):
                unreadable.append("groove %s amounts" % name)
            elif song_amount > 0 and any(a > 0 for a in amounts):
                differs.append("groove %s (global amount %.0f%%)" % (name, 100 * song_amount))
        muted = _get(clip, "muted")
        if muted is MISSING: unreadable.append("clip activation")
        elif muted: differs.append("clip deactivated")
        # MIDI effects change the notes before the instrument; automation on the instrument or a MIDI effect changes
        # its sound during the phrase. Effects after the instrument and the mixer are outside the instrument's capture.
        for path, device in self._walk_devices(getattr(track, "devices", ()), "track"):
            kind = _get(device, "type")
            if kind is MISSING: unreadable.append("device type of %s" % _attr(_get(device, "name"), path)); continue
            if kind == 4: differs.append("MIDI effect %s" % _attr(_get(device, "name"), path))
            if kind not in (1, 4): continue
            for p in _attr(_get(device, "parameters"), ()) or ():
                state = _get(p, "automation_state")
                if state is MISSING: unreadable.append("automation state on %s" % _attr(_get(device, "name"), path)); break
                if state != 0:
                    differs.append("automation on %s / %s" % (_attr(_get(device, "name"), path), _attr(_get(p, "name"), "?")))
        return {"differs": differs, "unreadable": unreadable, "unobservable": ["MPE per-note expression"]}

    # ---- devices and parameters ----
    # Live Object Model attributes are feature-detected on every object: Live
    # versions, native devices and plug-ins differ, and a missing attribute is
    # reported as unavailable instead of guessed. Parameter values are Live's
    # internal values within each parameter's own min/max (not assumed 0-1);
    # display text comes only from Live and is never converted here.
    def _device_id(self, device):
        return "device:%d" % self._ptr(device)

    def _param_id(self, param):
        return "param:%d" % self._ptr(param)

    def _walk_devices(self, devices, path, depth=0):
        """Yield (path, device) for devices and, recursively, rack chain contents."""
        if depth > MAX_DEVICE_DEPTH:
            return
        for index, device in enumerate(_attr(devices, None) or ()):
            here = "%s.devices[%d]" % (path, index)
            yield here, device
            for kind, chains in self._chain_lists(device):
                for c_index, chain in enumerate(chains):
                    for item in self._walk_devices(getattr(chain, "devices", ()), "%s.%s[%d]" % (here, kind, c_index), depth + 1):
                        yield item

    def _chain_lists(self, device):
        out = []
        if _attr(_get(device, "can_have_chains"), False) is True:
            for kind, attr in (("chains", "chains"), ("returnChains", "return_chains")):
                chains = _get(device, attr)
                if chains is not MISSING:
                    out.append((kind, list(chains or ())))
        return out

    def _all_devices(self):
        song = self._song()
        roots = [(t, "tracks[%d]" % i) for i, t in enumerate(song.tracks)]
        roots += [(t, "returnTracks[%d]" % i) for i, t in enumerate(getattr(song, "return_tracks", ()) or ())]
        if getattr(song, "master_track", None) is not None:
            roots.append((song.master_track, "masterTrack"))
        for track, prefix in roots:
            for item in self._walk_devices(getattr(track, "devices", ()), prefix):
                yield track, item[0], item[1]

    def _find_device(self, ident):
        if not isinstance(ident, str) or not ident.startswith("device:"):
            raise BridgeError("invalid", "Device id must look like device:<n>.")
        for track, path, device in self._all_devices():
            if self._device_id(device) == ident:
                return track, path, device
        raise BridgeError("not_found", "Device no longer exists.")

    def _find_param(self, device, ident):
        if not isinstance(ident, str) or not ident.startswith("param:"):
            raise BridgeError("invalid", "Parameter id must look like param:<n>.")
        for index, param in enumerate(getattr(device, "parameters", ()) or ()):
            if self._param_id(param) == ident:
                return index, param
        raise BridgeError("not_found", "Parameter no longer exists on this device.")

    def _device_record(self, track, path, device, parameters=False, depth=0):
        class_name = _attr(_get(device, "class_name"), None)
        out = {"id": self._device_id(device), "trackId": self._track_id(track), "path": path,
               "name": _attr(_get(device, "name"), None),
               "className": class_name,
               "deviceName": _attr(_get(device, "class_display_name"), None),
               "role": DEVICE_ROLES.get(_attr(_get(device, "type"), None), "unknown"),
               "plugin": _plugin_kind(class_name),
               "active": _bool_or_none(_get(device, "is_active")),
               "parameterCount": len(getattr(device, "parameters", ()) or ())}
        unavailable = [k for k, a in (("name", "name"), ("className", "class_name"), ("deviceName", "class_display_name"),
                                      ("active", "is_active")) if _get(device, a) is MISSING]
        if depth < MAX_DEVICE_DEPTH:
            for kind, chains in self._chain_lists(device):
                out[kind] = []
                for i, chain in enumerate(chains):
                    chain_path = "%s.%s[%d]" % (path, kind, i)
                    out[kind].append({"id": "chain:%d" % self._ptr(chain), "name": _attr(_get(chain, "name"), None),
                                      "path": chain_path,
                                      "devices": [self._device_record(track, "%s.devices[%d]" % (chain_path, j), d, parameters, depth + 1)
                                                  for j, d in enumerate(getattr(chain, "devices", ()) or ())]})
        elif self._chain_lists(device):
            out["truncated"] = True
        if parameters:
            out["parameters"] = [self._param_record(i, p) for i, p in enumerate(getattr(device, "parameters", ()) or ())]
        if unavailable:
            out["unavailable"] = unavailable
        return out

    def _param_record(self, index, param):
        unavailable = []
        def read(attr, default=None):
            value = _get(param, attr)
            if value is MISSING:
                unavailable.append(attr)
                return default
            return value
        value, lo, hi = read("value"), read("min"), read("max")
        quantized = read("is_quantized")
        items = read("value_items")
        out = {"id": self._param_id(param), "index": index, "name": read("name"),
               "originalName": read("original_name"),
               "value": _finite(value), "min": _finite(lo), "max": _finite(hi),
               "defaultValue": _finite(read("default_value")),
               "quantized": _bool_or_none(quantized if quantized is not None else MISSING),
               "options": [str(x) for x in items] if items else None,
               "enabled": _bool_or_none(read("is_enabled", MISSING)),
               "state": _enum_name(PARAM_STATES, read("state")),
               "automation": _enum_name(AUTOMATION_STATES, read("automation_state")),
               "display": None}
        str_for_value = _get(param, "str_for_value")
        if callable(str_for_value) and out["value"] is not None:
            try:
                out["display"] = str(str_for_value(value))
            except Exception:
                unavailable.append("str_for_value")
        else:
            unavailable.append("str_for_value")
        if unavailable:
            out["unavailable"] = unavailable
        return out

    def op_devices(self, track, parameters=False):
        index, found = self._find_track(track)
        return {"trackId": track,
                "devices": [self._device_record(found, "tracks[%d].devices[%d]" % (index, i), d, bool(parameters))
                            for i, d in enumerate(getattr(found, "devices", ()) or ())]}

    def op_device(self, id, parameters=True):
        track, path, device = self._find_device(id)
        return self._device_record(track, path, device, bool(parameters))

    def op_device_parameter(self, device, id):
        _, _, found = self._find_device(device)
        index, param = self._find_param(found, id)
        return dict(self._param_record(index, param), deviceId=device)

    def _writable(self, device, id, name, expect_value):
        """Find a parameter and refuse unless it is the named, enabled, unautomated control the caller read."""
        if not isinstance(name, str) or not name:
            raise BridgeError("invalid", "The expected parameter name is required.")
        _, _, found = self._find_device(device)
        index, param = self._find_param(found, id)
        record = self._param_record(index, param)
        missing = [a for a in ("value", "min", "max", "is_quantized", "is_enabled", "automation_state")
                   if a in record.get("unavailable", ())]
        if missing:
            raise BridgeError("unsupported", "This parameter does not expose %s; it cannot be written with verification." % ", ".join(missing))
        if record["name"] != name:
            raise BridgeError("conflict", "Parameter is now named %r, not %r. Nothing was changed; read the device again." % (record["name"], name))
        if None in (record["value"], record["min"], record["max"]) or record["min"] > record["max"]:
            raise BridgeError("unsupported", "Parameter %r reports no usable range." % name)
        if expect_value is not None:
            if not _is_number(expect_value) or abs(record["value"] - expect_value) > _tolerance(record):
                raise BridgeError("conflict", "Parameter %r is %r, not the expected %r. Nothing was changed." % (name, record["value"], expect_value))
        return param, record

    def op_set_device_parameter(self, device, id, name, value=None, option=None, expect_value=None):
        """Set one parameter's internal value, then verify it after settling.

        Target validation: the caller names the parameter it read, so a reused or
        shifted reference is refused rather than writing a different control.
        """
        if (value is None) == (option is None):
            raise BridgeError("invalid", "Give exactly one of value or option.")
        param, record = self._writable(device, id, name, expect_value)
        if option is not None:
            if not record["quantized"] or not record["options"]:
                raise BridgeError("invalid", "Parameter %r has no named options." % name)
            if option not in record["options"]:
                raise BridgeError("invalid", "%r is not an option of %r: %s." % (option, name, record["options"]))
            target = record["options"].index(option) + record["min"]
            # Only map option -> step when every step has exactly one named option.
            if len(record["options"]) != int(record["max"] - record["min"]) + 1 or float(record["min"]) != int(record["min"]):
                raise BridgeError("unsupported", "Options of %r do not line up with its value range." % name)
        else:
            if not _is_number(value):
                raise BridgeError("invalid", "Value must be a finite number in the parameter's own units.")
            if not record["min"] <= value <= record["max"]:
                raise BridgeError("invalid", "%r is outside %r's range %r..%r." % (value, name, record["min"], record["max"]))
            if record["quantized"] and float(value) != int(value):
                raise BridgeError("invalid", "Parameter %r is discrete; give a whole step or an option." % name)
            target = value
        before = record["value"]
        param.value = float(target)

        def verify():
            try:
                _, _, again = self._find_device(device)
                v_index, v_param = self._find_param(again, id)
            except BridgeError as exc:
                raise BridgeError("unverified", "Readback failed: %s The change may have been made." % exc)
            after = self._param_record(v_index, v_param)
            if after["name"] != name or after["value"] is None or abs(after["value"] - target) > _tolerance(after):
                raise BridgeError("unverified", "Readback of %r shows %r (display %r), not %r; previous value %r. Not retried." % (
                    name, after["value"], after["display"], target, before))
            return dict(after, deviceId=device, previousValue=before)
        return Defer(self.settle_ticks, verify)

    def op_verify_last_write(self):
        """Confirm the most recent grouped write that was sent unconfirmed.

        `set_device_parameters` with confirm=false returns as soon as the values are set, so the
        sound changes on the next tick instead of after the settle wait; the caller confirms here.
        """
        pending = self._pending_write
        if pending is None:
            raise BridgeError("conflict", "No unconfirmed write to verify.")
        self._pending_write = None
        return pending()

    def op_set_device_parameters(self, device, writes, confirm=True):
        """Set several parameters of one device in a single tick, then verify them all after settling.

        Every write is validated (name, enabled, no automation, expected value, range) BEFORE any
        value is set, so a plan that cannot be applied whole changes nothing. This is the fast path
        for a coordinated sound-design move: one tick instead of one tick per knob.
        """
        if not isinstance(writes, list) or not 1 <= len(writes) <= 64:
            raise BridgeError("invalid", "Give between 1 and 64 parameter writes.")
        plan = []
        seen = set()
        for w in writes:
            if not isinstance(w, dict):
                raise BridgeError("invalid", "Each write is an object with id, name and value.")
            ident, name = w.get("id"), w.get("name")
            if ident in seen:
                raise BridgeError("invalid", "Parameter %r appears twice in one grouped write." % name)
            seen.add(ident)
            param, record = self._writable(device, ident, name, w.get("expect_value"))
            value = w.get("value")
            if not _is_number(value):
                raise BridgeError("invalid", "Value for %r must be a finite number in its own units." % name)
            if not record["min"] <= value <= record["max"]:
                raise BridgeError("invalid", "%r is outside %r's range %r..%r." % (value, name, record["min"], record["max"]))
            if record["quantized"] and float(value) != int(value):
                raise BridgeError("invalid", "Parameter %r is discrete; give a whole step." % name)
            plan.append({"id": ident, "name": name, "param": param, "target": float(value), "before": record["value"]})
        for step in plan:
            step["param"].value = step["target"]

        def verify():
            _, _, again = self._find_device(device)
            out, wrong = [], []
            for step in plan:
                index, param = self._find_param(again, step["id"])
                after = self._param_record(index, param)
                out.append(dict(after, deviceId=device, previousValue=step["before"], requested=step["target"]))
                if after["name"] != step["name"] or after["value"] is None or abs(after["value"] - step["target"]) > _tolerance(after):
                    wrong.append("%s reads %r (display %r), not %r" % (step["name"], after["value"], after["display"], step["target"]))
            if wrong:
                raise BridgeError("unverified", "Grouped write applied but %d of %d did not confirm: %s. Not retried." % (
                    len(wrong), len(plan), "; ".join(wrong[:4])))
            return {"deviceId": device, "written": out}
        if confirm:
            return Defer(self.settle_ticks, verify)
        self._pending_write = verify
        return {"deviceId": device, "applied": len(plan), "confirmed": False,
                "requested": [{"id": s["id"], "name": s["name"], "value": s["target"], "previousValue": s["before"]} for s in plan]}

    # ---- knob sweeps (sound coach) ----
    def op_start_sweep(self, device, id, name, target, seconds, expect_value=None):
        """Move one continuous parameter smoothly to `target` over `seconds`, one value per tick on Live's thread.

        Replies once started; the knob keeps moving (or holds at the end) until stop_sweep,
        which freezes it and returns the verified position. Other device writes are refused meanwhile.
        """
        if self._sweep is not None:
            raise BridgeError("conflict", "A sweep is already running; stop it first.")
        if not _is_number(seconds) or not 0 < seconds <= MAX_SWEEP_SECONDS:
            raise BridgeError("invalid", "Sweep seconds must be above 0 and at most %d." % MAX_SWEEP_SECONDS)
        param, record = self._writable(device, id, name, expect_value)
        if record["quantized"]:
            raise BridgeError("invalid", "Parameter %r is discrete; sweeps move continuous knobs only." % name)
        if not _is_number(target) or not record["min"] <= target <= record["max"]:
            raise BridgeError("invalid", "Sweep target must be within %r's range %r..%r." % (name, record["min"], record["max"]))
        now = self._clock()
        self._sweep = {"device": device, "id": id, "name": name, "param": param, "from": record["value"], "to": float(target),
                       "started": now, "seconds": float(seconds), "last": record["value"], "error": None}
        return {"sweeping": True, "deviceId": device, "id": id, "name": name, "from": record["value"], "to": float(target),
                "seconds": float(seconds), "display": record["display"]}

    def _advance_sweep(self):
        s = self._sweep
        if s is None or s["error"] or s["last"] == s["to"]:
            return
        fraction = min(1.0, max(0.0, (self._clock() - s["started"]) / s["seconds"]))
        value = s["to"] if fraction >= 1.0 else s["from"] + (s["to"] - s["from"]) * fraction
        try:
            s["param"].value = float(value)
            s["last"] = value
        except Exception as exc:
            s["error"] = str(exc)

    def op_sweep_status(self):
        s = self._sweep
        if s is None:
            return {"sweeping": False}
        return {"sweeping": True, "deviceId": s["device"], "id": s["id"], "name": s["name"], "from": s["from"], "to": s["to"],
                "sent": s["last"], "finished": s["last"] == s["to"], "error": s["error"]}

    def op_stop_sweep(self):
        """Freeze the sweeping knob where it is and return its verified position (value, display, sent value)."""
        s = self._sweep
        if s is None:
            raise BridgeError("conflict", "No sweep is running.")
        self._sweep = None
        sent = s["last"]

        def verify():
            try:
                _, _, again = self._find_device(s["device"])
                v_index, v_param = self._find_param(again, s["id"])
            except BridgeError as exc:
                raise BridgeError("unverified", "Readback after sweep failed: %s" % exc)
            after = self._param_record(v_index, v_param)
            if s["error"]:
                raise BridgeError("unverified", "Sweep of %r halted: %s Knob reads %r (display %r)." % (s["name"], s["error"], after["value"], after["display"]))
            if after["name"] != s["name"] or after["value"] is None or abs(after["value"] - sent) > _tolerance(after):
                raise BridgeError("unverified", "Knob %r reads %r (display %r) after the sweep, not the last value sent %r." % (
                    s["name"], after["value"], after["display"], sent))
            return dict(after, deviceId=s["device"], sweptFrom=s["from"], sweptTo=s["to"], stoppedAt=sent)
        return Defer(self.settle_ticks, verify)

    def op_meter(self, track, ticks=12):
        """Sample a track's output meter over `ticks` display ticks (about 0.1 s each) while it plays.

        Live's meter is a level reading, not a loudness measurement: it is peak-ish, per channel,
        and only meaningful while audio is playing. Enough to compare two versions of the same
        sound at the same playback position; not a substitute for the audio tap.
        """
        if not isinstance(ticks, int) or not 1 <= ticks <= 60:
            raise BridgeError("invalid", "Sample between 1 and 60 ticks.")
        _, found = self._find_track(track)
        samples = []

        def gather():
            left = _get(found, "output_meter_left")
            right = _get(found, "output_meter_right")
            level = _get(found, "output_meter_level")
            if MISSING in (left, right) and level is MISSING:
                raise BridgeError("unsupported", "This Live build does not expose track meters.")
            value = level if level is not MISSING else max(x for x in (left, right) if x is not MISSING)
            samples.append(float(value))
            if len(samples) < ticks:
                return Defer(1, gather)
            playing = bool(getattr(self._song(), "is_playing", False))
            peak = max(samples)
            return {"trackId": track, "samples": len(samples), "peak": peak, "mean": sum(samples) / len(samples),
                    "playing": playing, "basis": "Live output meter, %d ticks" % ticks}
        return gather()

    def op_selected_track(self):
        track = self._song().view.selected_track
        return None if track is None else self._track_id(track)

    def _transport(self):
        song = self._song()
        start = float(song.loop_start)
        return {"playing": bool(song.is_playing),
                "loop": {"start": start, "end": start + float(song.loop_length)} if song.loop else None}

    def op_transport(self):
        return self._transport()

    # ---- writes ----
    def op_create_midi_track(self, name, index=None, mute=True, arm=False):
        song = self._song()
        if not isinstance(name, str) or not name:
            raise BridgeError("invalid", "Track name is required.")
        count = len(song.tracks)
        at = -1 if index is None else int(index)
        if at != -1 and not 0 <= at <= count:
            raise BridgeError("invalid", "Track index is outside the Set.")
        song.create_midi_track(at)
        position = count if at == -1 else at
        track = song.tracks[position]
        track.name = name
        track.mute = bool(mute)
        if getattr(track, "can_be_armed", False):
            track.arm = bool(arm)
        return {"id": self._track_id(track), "trackIndex": position}

    def op_update_track(self, id, **values):
        for key in values:
            if "update_track." + key in UNSUPPORTED:
                raise BridgeError("unsupported", UNSUPPORTED["update_track." + key])
            if key not in TRACK_FIELDS:
                raise BridgeError("invalid", "Unknown track field: %s" % key)
        _, track = self._find_track(id)
        plan = []
        for key, value in values.items():
            if key == "arm" and not getattr(track, "can_be_armed", False):
                if value:
                    raise BridgeError("invalid", "This track cannot be armed.")
                continue
            if key == "gainDb":
                plan.append(("gainDb", self._volume_target(track, value)))
            elif key == "monitoring":
                if value not in MONITORING_VALUE:
                    raise BridgeError("invalid", "Monitoring must be in, auto or off.")
                if not getattr(track, "can_be_armed", False):
                    raise BridgeError("invalid", "Monitoring is only available on armable tracks.")
                plan.append(("current_monitoring_state", MONITORING_VALUE[value]))
            else:
                plan.append((key, value if key == "name" else bool(value)))
        for key, value in plan:
            if key == "gainDb":
                value[0].value = value[1]
            else:
                setattr(track, key, value)
        return {"id": id}

    def _volume_target(self, track, db):
        """Internal fader value whose Live display equals `db` (searched on str_for_value; no guessed curve)."""
        if not _is_number(db) or not -70 <= db <= 6:
            raise BridgeError("invalid", "Track gain must be a number of dB from -70 to 6.")
        volume = self._need(self._need(track, "mixer_device", "the track mixer"), "volume", "the track volume")
        if not self._g(volume, "is_enabled", True):
            raise BridgeError("conflict", "Track volume is disabled in Ableton.")
        lo, hi = float(volume.min), float(volume.max)
        def shown(v):
            value, unit, _ = parse_label(self._call_label(volume, v))
            if unit != "dB" or not _is_number(value):
                raise BridgeError("unsupported", "Track volume display is not in dB: %r." % self._call_label(volume, v))
            return value
        if not shown(lo) <= db <= shown(hi):
            raise BridgeError("invalid", "%r dB is outside this fader's range." % db)
        a, b = lo, hi
        for _ in range(48):
            mid = (a + b) / 2.0
            if shown(mid) < db:
                a = mid
            else:
                b = mid
        return volume, b

    def op_create_arrangement_clip(self, track_index, length, notes, start=0, name="Scrunch clip", looping=False):
        song = self._song()
        if not isinstance(track_index, int) or not 0 <= track_index < len(song.tracks):
            raise BridgeError("invalid", "Track index is outside the Set.")
        if not all(isinstance(x, (int, float)) for x in (length, start)) or length <= 0 or start < 0:
            raise BridgeError("invalid", "Clip start and length must be non-negative beats.")
        rows = []
        for n in notes if isinstance(notes, list) else [None]:
            if not isinstance(n, dict) or set(n) != {"p", "t", "d", "v"}:
                raise BridgeError("invalid", "Notes must be {p,t,d,v} objects.")
            rows.append((int(n["p"]), float(n["t"]), float(n["d"]), int(n["v"])))
        track = song.tracks[track_index]
        if not getattr(track, "has_midi_input", False):
            raise BridgeError("invalid", "Arrangement MIDI needs a MIDI track.")
        spec = self._note_spec or _note_spec_factory()
        clip = track.create_midi_clip(float(start), float(length))
        try:
            # Live 12.3.5 creates Arrangement MIDI clips with looping enabled (attended
            # lab run, 17 Sep 2026). Set the requested state explicitly; clip readback
            # still verifies the result.
            clip.looping = bool(looping)
            clip.name = name
            if rows:
                clip.add_new_notes(tuple(spec(p, t, d, v) for p, t, d, v in rows))
            ident = self._clip_id(clip)
        except Exception:
            # Never leave a half-configured clip this request created.
            try:
                track.delete_clip(clip)
            except Exception:
                self._log("Scrunch bridge could not remove a partially created clip: " + traceback.format_exc())
            raise
        return {"id": ident}

    def op_delete_clip(self, id):
        track, clip = self._find_clip(id)
        track.delete_clip(clip)
        return {"id": id}

    def op_delete_track(self, id):
        index, _ = self._find_track(id)
        self._song().delete_track(index)
        return {"id": id}

    def _settled_transport(self):
        return Defer(self.settle_ticks, self._transport)

    # Scrunch's own playback (an audition loop, or loop off) must not leave John's loop brace moved.
    @staticmethod
    def _loop_of(song):
        return (bool(song.loop), float(song.loop_start), float(song.loop_length))

    @staticmethod
    def _set_loop_brace(song, start, length):
        # A long existing brace can make moving its start illegal even when the
        # requested final brace fits. Shrink first, move, then extend if needed.
        song.loop_length = min(float(song.loop_length), float(length))
        song.loop_start = float(start)
        song.loop_length = float(length)

    def _give_back_loop(self):
        """Put John's loop back unless he has moved it since Scrunch set it; his own change always stands."""
        if self._user_loop is None:
            return
        song = self._song()
        user, scrunch = self._user_loop, self._scrunch_loop
        same = lambda a, b: a[0] == b[0] and abs(a[1] - b[1]) < 1e-6 and abs(a[2] - b[2]) < 1e-6
        if scrunch is not None and same(self._loop_of(song), scrunch[0]) and not same(user, scrunch[0]):
            self._set_loop_brace(song, user[1], user[2])
            song.loop = user[0]
        self._user_loop = self._scrunch_loop = None

    def _watch_loop(self):
        # Covers playback stopped from Live itself (spacebar): once Scrunch's playback has been
        # seen playing and then stops, the loop goes back.
        if self._scrunch_loop is None:
            return
        if bool(getattr(self._song(), "is_playing", False)):
            self._scrunch_loop = (self._scrunch_loop[0], True)
        elif self._scrunch_loop[1]:
            self._give_back_loop()

    def op_stop_playback(self):
        self._song().stop_playing()
        self._give_back_loop()
        return self._settled_transport()

    def op_stop_session_clips(self):
        self._song().stop_all_clips()
        return {}

    def op_play_arrangement(self, start, loop=None):
        song = self._song()
        if not isinstance(start, (int, float)) or start < 0:
            raise BridgeError("invalid", "Start must be non-negative beats.")
        if loop is not None:
            if (not isinstance(loop, list) or len(loop) != 2 or not all(isinstance(x, (int, float)) for x in loop)
                    or not 0 <= loop[0] < loop[1]):
                raise BridgeError("invalid", "Loop must be [start, end] beats.")
        if self._user_loop is None:
            self._user_loop = self._loop_of(song)
        asked = (True, float(loop[0]), float(loop[1] - loop[0])) if loop is not None else (False,) + self._loop_of(song)[1:]
        if loop is not None:
            self._set_loop_brace(song, loop[0], loop[1] - loop[0])
        song.loop = loop is not None
        self._scrunch_loop = (asked, False)      # what was asked for: a same-tick read can still show the old loop
        # Session clips played or stopped on a track grey out its arrangement clips until
        # "Back to Arrangement"; arrangement playback is what was asked for, so hand tracks back.
        if getattr(song, "back_to_arranger", False):
            song.back_to_arranger = False
        song.current_song_time = float(start)
        song.start_playing()
        return self._settled_transport()

    def op_set_arrangement_loop(self, enabled, start, end):
        song = self._song()
        if not isinstance(enabled, bool) or not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or not 0 <= start < end:
            raise BridgeError("invalid", "Loop needs enabled plus start < end beats.")
        self._set_loop_brace(song, start, end - start)
        song.loop = enabled
        self._user_loop = self._scrunch_loop = None     # an asked-for loop is John's own: nothing to give back
        return self._settled_transport()

    def op_move_playhead(self, start):
        if not isinstance(start, (int, float)) or start < 0:
            raise BridgeError("invalid", "Start must be non-negative beats.")
        self._song().current_song_time = float(start)
        return self._settled_transport()


class ScrunchBridge(object):
    """Live control-surface shell. Startup only serves requests; no self-test."""

    def __init__(self, c_instance, **core_options):
        self._c = c_instance
        self.core = BridgeCore(c_instance.song, log=self._log, app_provider=self._application, **core_options)
        try:
            self.core.start()
        except OSError as exc:
            self._log("Scrunch bridge could not start: %s" % exc)

    @staticmethod
    def _application():
        import Live  # only available inside Ableton
        return Live.Application.get_application()

    def _log(self, message):
        try:
            self._c.log_message(message)
        except Exception:
            pass

    def update_display(self):
        try:
            self.core.tick()
        except Exception:
            self._log("Scrunch bridge tick error: " + traceback.format_exc())

    def disconnect(self):
        self.core.stop()

    def can_lock_to_devices(self):
        return False

    def suggest_map_mode(self, *args):
        return 0

    def build_midi_map(self, *args):
        pass

    def refresh_state(self):
        pass

    def connect_script_instances(self, *args):
        pass

    def set_appointed_device(self, *args):
        pass

    def receive_midi(self, *args):
        pass


# Mix actions (26 Sep 2026) live in their own module so this file's other work is not disturbed.
# Reloaded here so op_reload picks up changes to it; install() copies the mix_* ops onto BridgeCore.
import importlib as _importlib
from . import mix_ops as _mix_ops
_mix_ops = _importlib.reload(_mix_ops)
_mix_ops.install(BridgeCore)
WRITE_OPS.update(_mix_ops.MIX_WRITE_OPS)
