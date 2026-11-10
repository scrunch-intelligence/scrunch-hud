"""Host-schema reads and Producer Pal-compatible writes for ScrunchBridge.

Scrunch's device, synth and effect layers were written against Producer Pal
2.2.0 records (daw_adapter.HOST_SCHEMA): parameter values in display units
("Off", 10 for "10.0 %", 1800 for "1.80 s"), device types such as
"audio-effect: Reverb", paths such as "t0/d0/c0/d0". Stored journals and
calibration tables contain those values, so this module reproduces the same
conversion rules on Live's own objects instead of inventing new ones. The rules
were ported from Producer Pal 2.2.0's bundled live-api-adapter.js (label parsing,
quantized options, division and pan labels, display-value search). Devices that
Producer Pal models with device-specific extra parameters are refused for
parameter reads here rather than returned incompletely.

Every Live attribute is feature-detected; a missing capability raises
`unsupported` before any write.
"""
import math
import re

# Producer Pal 2.2.0 specialised these class display names with extra synthetic
# parameters or inactive-state rules; generic reads would differ, so refuse them.
SPECIALIZED_DEVICES = frozenset()  # John: no device refusals; all use the generic parameter path
PARAM_STATE = {0: "active", 1: "inactive", 2: "disabled"}
AUTOMATION_STATE = {0: "none", 1: "active", 2: "overridden"}
MONITORING = {0: "in", 1: "auto", 2: "off"}
MONITORING_VALUE = {"in": 0, "auto": 1, "off": 2}
PITCH_CLASS_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
SEARCH_ITERATIONS = 40
MAX_TAKE_BEATS = 512.0   # longest single automation recording
MAX_SAMPLES = 64         # most playhead positions one read-back visits
READ_ASPECTS = frozenset({"mixer", "devices", "arrangement-clips", "session-clips", "timing", "routings", "params",
                          "param-values", "options", "chains", "return-chains", "drum-pads", "tracks", "locators",
                          "sample", "available-routings"})
# Accepted names this bridge refuses explicitly instead of silently omitting.
UNSUPPORTED_ASPECTS = {"sample": "Simpler sample paths are not provided by this bridge.",
                       "available-routings": "Available routing lists are not provided by this bridge.",
                       "session-clips": "Session clip records are not provided by this bridge (Arrangement workflows only)."}

_NUM = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)")
LABEL_PATTERNS = [
    (re.compile(r"^([\d.]+)\s*khz$", re.I), "Hz", 1000.0, None),
    (re.compile(r"^([\d.]+)\s*hz$", re.I), "Hz", 1.0, None),
    (re.compile(r"^([\d.]+)\s*ms$", re.I), "ms", 1.0, None),
    (re.compile(r"^([\d.]+)\s*s$", re.I), "ms", 1000.0, None),
    (re.compile(r"^([\d.-]+)\s*db$", re.I), "dB", 1.0, None),
    (re.compile(r"^(-?inf)\s*db$", re.I), "dB", None, -70),
    (re.compile(r"^([\d.-]+)\s*(?:%|percent)$", re.I), "%", 1.0, None),
    (re.compile(u"^([\\d.-]+)\\s*(?:°|deg|degrees?)$", re.I), "degrees", 1.0, None),
    (re.compile(r"^([+-]?\d+)\s*(?:st|semis?|semitones?)$", re.I), "semitones", 1.0, None),
    (re.compile(r"^([a-g][#b]?-?\d+)$", re.I), "note", "note", None),
    (re.compile(r"^(\d+)([lr])$", re.I), "pan", "pan", None),
    (re.compile(r"^(c)$", re.I), "pan", None, 0),
]
DIVISION = re.compile(r"^1/\d+$")
PAN_LABEL = re.compile(r"^(\d+[LR]|C)$")
NOTE_NAME = re.compile(r"^([A-Ga-g])([#b]?)(-?\d+)$")
NOTE_OFFSETS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def js_float(text):
    """JavaScript parseFloat: leading numeric prefix, else None (NaN)."""
    match = _NUM.match(text.strip()) if isinstance(text, str) else None
    return float(match.group(0)) if match else None


def clean_number(value):
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        return int(value)
    return value


def parse_label(label):
    """Producer Pal parseLabel: (value, unit, direction)."""
    if not isinstance(label, str) or not label:
        return None, None, None
    text = label.strip()
    for regex, unit, multiplier, fixed in LABEL_PATTERNS:
        match = regex.match(text)
        if not match:
            continue
        if fixed is not None:
            return fixed, unit, None
        if multiplier == "note":
            return match.group(1), "note", None
        if multiplier == "pan":
            return int(match.group(1)), "pan", match.group(2)
        number = js_float(match.group(1))
        return (None if number is None else clean_number(number * multiplier)), unit, None
    number = js_float(text)
    return (None if number is None else clean_number(number)), None, None


def note_to_midi(name):
    match = NOTE_NAME.match(name)
    if not match:
        return None
    pc = NOTE_OFFSETS[match.group(1).upper()] + {"#": 1, "b": -1, "": 0}[match.group(2)]
    midi = (int(match.group(3)) + 2) * 12 + pc  # Live names MIDI 60 as C3
    return midi if 0 <= midi <= 127 else None


def normalize_value(raw):
    """Producer Pal normalizeParamValue for a string written by Scrunch."""
    text = str(raw).strip()
    try:
        number = float(text)
        if math.isfinite(number):
            return clean_number(number)
    except ValueError:
        pass
    value, unit, direction = parse_label(text)
    if unit == "pan" and direction is not None:
        return text
    return value if isinstance(value, (int, float)) and unit is not None else text


NUMERIC_LABEL = re.compile(r"^\s*[+-]?(?:\d+\.?\d*|\.\d+)\s*$")


def max_atom(label):
    """Max parses a purely numeric symbol into a 32-bit float atom, so Producer Pal saw a
    number, not text, and used that single-precision value (Serum A Uni Detune 0.01 was
    stored as 0.009999999776482582). Returns None for any other label."""
    if not isinstance(label, str) or not NUMERIC_LABEL.match(label):
        return None
    import struct
    return clean_number(struct.unpack("f", struct.pack("f", float(label)))[0])


def js_string(value):
    if isinstance(value, float):
        value = clean_number(value)
    return str(value)


class HostOps(object):
    """Mixin for BridgeCore. Needs _song, _app, _error, _ptr, *_id helpers, _find_track, _find_clip,
    _all_devices, _note_spec and _default_note_spec."""

    # ---- Live access helpers ----
    def _g(self, obj, attr, default=None):
        try:
            return getattr(obj, attr)
        except Exception:
            return default

    def _need(self, obj, attr, what):
        try:
            return getattr(obj, attr)
        except Exception:
            raise self._error("unsupported", "This Live build does not expose %s." % what)

    def _call_label(self, param, value):
        fn = self._g(param, "str_for_value")
        if not callable(fn):
            raise self._error("unsupported", "Parameter display text (str_for_value) is not exposed.")
        return str(fn(value))

    # ---- parameters (Producer Pal readParameter) ----
    def _param_name(self, param):
        name = self._g(param, "name")
        original = self._g(param, "original_name", name)
        return name if original in (None, name) else "%s (%s)" % (name, original)

    def _state_flags(self, out, param):
        if not (self._g(param, "is_enabled", True)):
            out["enabled"] = False
        state = PARAM_STATE.get(self._g(param, "state", 0))
        if state and state != "active":
            out["state"] = state
        automation = AUTOMATION_STATE.get(self._g(param, "automation_state", 0))
        if automation and automation != "none":
            out["automation"] = automation
        return out

    def _host_param(self, param):
        out = {"id": "param:%d" % self._ptr(param), "name": self._param_name(param)}
        value = self._g(param, "value")
        if self._g(param, "is_quantized"):
            items = [str(x) for x in (self._g(param, "value_items") or ())]
            index = int(value) if isinstance(value, (int, float)) else -1
            out.update(value=items[index] if 0 <= index < len(items) else None, options=items)
            return self._state_flags(out, param)
        lo, hi = self._g(param, "min"), self._g(param, "max")
        label, lo_label, hi_label = (self._call_label(param, v) for v in (value, lo, hi))
        if DIVISION.match(label) or DIVISION.match(lo_label):
            options = [self._call_label(param, i) for i in range(int(math.ceil(min(lo, hi))), int(math.floor(max(lo, hi))) + 1)]
            out.update(value=label, options=options)
            return self._state_flags(out, param)
        # Purely numeric labels arrive in Producer Pal as Max float atoms: parseLabel ignores
        # them and the single-precision value is used (see max_atom).
        parsed, lo_parsed, hi_parsed = ((max_atom(x), None, None) if max_atom(x) is not None else parse_label(x)
                                        for x in (label, lo_label, hi_label))
        unit = parsed[1] or lo_parsed[1] or hi_parsed[1]
        if unit == "pan":
            max_pan = self._max_pan(hi_label) or self._max_pan(lo_label) or 50
            out.update(value=self._normal_pan(label, max_pan), min=-1, max=1, unit="pan")
            return self._state_flags(out, param)
        display = self._g(param, "display_value")
        out.update(value=parsed[0] if parsed[0] is not None else clean_number(display if display is not None else value),
                   min=lo_parsed[0] if lo_parsed[0] is not None else clean_number(lo),
                   max=hi_parsed[0] if hi_parsed[0] is not None else clean_number(hi))
        if unit:
            out["unit"] = unit
        return self._state_flags(out, param)

    @staticmethod
    def _max_pan(label):
        match = re.match(r"^(\d+)[LR]$", label or "")
        return int(match.group(1)) if match else None

    @staticmethod
    def _normal_pan(label, max_pan):
        if label == "C":
            return 0
        match = re.match(r"^(\d+)([LR])$", label or "")
        if not match:
            return 0
        number = int(match.group(1))
        return clean_number(-number / float(max_pan) if match.group(2) == "L" else number / float(max_pan))

    def _find_param_by_name(self, device, name):
        lower = name.lower()
        for param in self._g(device, "parameters", ()) or ():
            plain = self._g(param, "name") or ""
            if plain.lower() == lower or self._param_name(param).lower() == lower:
                return param
        return None

    def _resolve_write(self, param, raw):
        """Producer Pal setParamValue target, computed without writing. Returns internal value."""
        value = normalize_value(raw)
        if self._g(param, "is_quantized"):
            items = [str(x) for x in (self._g(param, "value_items") or ())]
            if js_string(value) not in items:
                raise self._error("invalid", "%r is not an option of %r: %s." % (raw, self._param_name(param), items))
            return float(items.index(js_string(value)))
        lo, hi = float(self._g(param, "min")), float(self._g(param, "max"))
        if isinstance(value, str) and NOTE_NAME.match(value):
            midi = note_to_midi(value)
            if midi is None:
                raise self._error("invalid", "Invalid note name %r." % value)
            target = float(midi)
        else:
            current = self._call_label(param, self._g(param, "value"))
            lo_label = self._call_label(param, lo)
            if PAN_LABEL.match(current):
                if isinstance(value, str):
                    if not PAN_LABEL.match(value):
                        raise self._error("invalid", "%r is not a pan value." % value)
                    max_pan = self._max_pan(self._call_label(param, hi)) or self._max_pan(lo_label) or 50
                    value = self._normal_pan(value, max_pan)
                target = (value + 1) / 2.0 * (hi - lo) + lo
            elif DIVISION.match(current) or DIVISION.match(lo_label):
                wanted = js_string(value)
                target = next((float(i) for i in range(int(math.ceil(min(lo, hi))), int(math.floor(max(lo, hi))) + 1)
                               if self._call_label(param, i) == wanted), None)
                if target is None:
                    raise self._error("invalid", "%r is not a valid division option." % raw)
            elif isinstance(value, (int, float)):
                target = self._raw_for_display(param, float(value), lo, hi, lo_label)
            else:
                raise self._error("invalid", "Could not interpret %r for parameter %r." % (raw, self._param_name(param)))
        if not math.isfinite(target) or not min(lo, hi) - 1e-9 <= target <= max(lo, hi) + 1e-9:
            raise self._error("invalid", "%r is outside %r's range." % (raw, self._param_name(param)))
        return target

    def _raw_for_display(self, param, wanted, lo, hi, lo_label):
        lo_parsed = parse_label(lo_label)[0]
        hi_parsed = parse_label(self._call_label(param, hi))[0]
        if not isinstance(lo_parsed, (int, float)) or not isinstance(hi_parsed, (int, float)):
            return wanted
        tolerance = .01 * abs(hi - lo) if hi != lo else .01
        if abs(lo_parsed - lo) < tolerance and abs(hi_parsed - hi) < tolerance:
            return wanted
        a, b = lo, hi
        for _ in range(SEARCH_ITERATIONS):
            mid = (a + b) / 2.0
            shown = parse_label(self._call_label(param, mid))[0]
            if not isinstance(shown, (int, float)):
                return mid
            if shown < wanted:
                a = mid
            else:
                b = mid
        # Producer Pal returns the final midpoint, which can display one step low
        # (Delay Dry/Wet 15 read back 14 in Live, 17 Sep 2026). Choose the candidate
        # whose Live display is closest to the request; ties prefer the upper bound.
        best = None
        for candidate in (b, (a + b) / 2.0, a):
            shown = parse_label(self._call_label(param, candidate))[0]
            if isinstance(shown, (int, float)):
                score = abs(shown - wanted)
                if best is None or score < best[0]:
                    best = (score, candidate)
        return best[1] if best else (a + b) / 2.0

    def _guard_writable(self, param, device_name):
        # John: automated or macro-controlled knobs are written anyway (Live overrides the envelope).
        return None

    # ---- devices (Producer Pal readDevice) ----
    def _device_type(self, device):
        kind, chains, pads = self._g(device, "type"), self._g(device, "can_have_chains"), self._g(device, "can_have_drum_pads")
        if kind == 1:
            return "drum-rack" if pads else "instrument-rack" if chains else "instrument"
        if kind == 2:
            return "audio-effect-rack" if chains else "audio-effect"
        if kind == 4:
            return "midi-effect-rack" if chains else "midi-effect"
        return "unknown"

    def _role_and_class(self, device):
        """Producer Pal's device type split: role plus class name, None for a rack's own class."""
        kind, class_name = self._device_type(device), self._g(device, "class_display_name")
        canonical = {"instrument-rack": "Instrument Rack", "drum-rack": "Drum Rack", "audio-effect-rack": "Audio Effect Rack",
                     "midi-effect-rack": "MIDI Effect Rack"}.get(kind)
        return kind, (None if canonical == class_name else class_name)

    def _all_tracks(self):
        """(category, index, track, path prefix) for regular, return and master tracks."""
        song = self._song()
        for i, track in enumerate(song.tracks):
            yield "regular", i, track, "t%d" % i
        for i, track in enumerate(self._g(song, "return_tracks", ()) or ()):
            yield "return", i, track, "rt%d" % i
        master = self._g(song, "master_track")
        if master is not None:
            yield "master", None, master, "mt"

    def _device_path(self, device):
        """Find a device's Producer Pal path (t/rt/mt, d/c/rc segments) by walking the Set."""
        for _, _, track, prefix in self._all_tracks():
            found = self._path_in(self._g(track, "devices", ()) or (), prefix, device, 0)
            if found:
                return track, found
        raise self._error("not_found", "Device no longer exists.")

    def _chain_by_id(self, ident):
        """Find a rack chain by id; returns (chain, Producer Pal path)."""
        def search(devices, prefix, depth):
            if depth > 8:
                return None
            for index, device in enumerate(devices):
                here = "%s/d%d" % (prefix, index)
                if self._g(device, "can_have_chains"):
                    for kind, attr in (("c", "chains"), ("rc", "return_chains")):
                        for c_index, chain in enumerate(self._g(device, attr, ()) or ()):
                            chain_path = "%s/%s%d" % (here, kind, c_index)
                            if "chain:%d" % self._ptr(chain) == ident:
                                return chain, chain_path
                            found = search(self._g(chain, "devices", ()) or (), chain_path, depth + 1)
                            if found:
                                return found
            return None
        for _, _, track, prefix in self._all_tracks():
            found = search(self._g(track, "devices", ()) or (), prefix, 0)
            if found:
                return found
        raise self._error("not_found", "Chain %s no longer exists." % ident)

    def _path_in(self, devices, prefix, target, depth):
        if depth > 8:
            return None
        for index, device in enumerate(devices):
            here = "%s/d%d" % (prefix, index)
            if self._ptr(device) == self._ptr(target):
                return here
            if self._g(device, "can_have_chains"):
                for kind, attr in (("c", "chains"), ("rc", "return_chains")):
                    for c_index, chain in enumerate(self._g(device, attr, ()) or ()):
                        found = self._path_in(self._g(chain, "devices", ()) or (), "%s/%s%d" % (here, kind, c_index), target, depth + 1)
                        if found:
                            return found
        return None

    def _resolve_path(self, path):
        parts = str(path or "").split("/")
        roots = {prefix: track for _, _, track, prefix in self._all_tracks()}
        if parts[0] not in roots or len(parts) < 2:
            raise self._error("unsupported" if re.match(r"^(t|rt)\d+$|^mt$", parts[0]) is None else "not_found",
                              "No device path %r (use t0/d0, rt0/d0 or mt/d0)." % path)
        container, obj = roots[parts[0]], None
        for part in parts[1:]:
            seg = re.match(r"^(d|c|rc)(\d+)$", part)
            if not seg:
                raise self._error("unsupported", "Path segment %r (drum pads) is not supported." % part)
            attr = {"d": "devices", "c": "chains", "rc": "return_chains"}[seg.group(1)]
            items = list(self._g(obj if obj is not None else container, attr, ()) or ())
            n = int(seg.group(2))
            if n >= len(items):
                raise self._error("not_found", "Nothing at %r." % path)
            obj = items[n]
        return obj, ("device" if seg.group(1) == "d" else "chain")

    def _host_chain(self, chain, path, depth, max_depth, options):
        out = {"id": "chain:%d" % self._ptr(chain), "path": path, "type": "Chain", "name": self._g(chain, "name")}
        color = self._g(chain, "color")
        if isinstance(color, int) and color:
            out["color"] = "#%06X" % color
        state = self._mix_state(chain)
        if state != "active":
            out["state"] = state
        devices = list(self._g(chain, "devices", ()) or ())
        if depth >= max_depth:
            out["deviceCount"] = len(devices)
        else:
            out["devices"] = [self._host_device_record(d, "%s/d%d" % (path, i), dict(options, params=False, depth=depth + 1))
                              for i, d in enumerate(devices)]
        return out

    def _host_device_record(self, device, path, options):
        kind, shown = self._role_and_class(device)
        class_name = self._g(device, "class_display_name")
        out = {"id": self._device_id(device), "path": path, "type": kind if shown is None else "%s: %s" % (kind, shown)}
        name = self._g(device, "name")
        if name != class_name:
            out["name"] = name
        if not self._g(device, "is_active", True):
            out["deactivated"] = True
        depth, max_depth = options.get("depth", 0), options.get("max_depth", 0)
        if "rack" in kind:
            if kind == "drum-rack" and (options.get("chains") or options.get("drum_pads")):
                raise self._error("unsupported", "Drum Rack pads and chains are not modelled by this bridge.")
            if options.get("chains"):
                out["chains"] = [self._host_chain(c, "%s/c%d" % (path, i), depth, max_depth, options)
                                 for i, c in enumerate(self._g(device, "chains", ()) or ())]
            if options.get("return_chains"):
                chains = list(self._g(device, "return_chains", ()) or ())
                if chains:
                    out["returnChains"] = [self._host_chain(c, "%s/rc%d" % (path, i), depth, max_depth, options)
                                           for i, c in enumerate(chains)]
        if options.get("params"):
            if class_name in SPECIALIZED_DEVICES:
                raise self._error("unsupported", "%s has device-specific parameters that this bridge does not model." % class_name)
            if self._g(device, "can_have_chains"):
                count = self._g(device, "variation_count")
                if count:
                    out["variations"] = {"count": count, "selected": self._g(device, "selected_variation_index")}
                visible = self._g(device, "visible_macro_count")
                if visible:
                    out["macros"] = {"count": visible, "hasMappings": bool(self._g(device, "has_macro_mappings"))}
            params = list(self._g(device, "parameters", ()) or ())
            out["parameters"] = ([self._host_param(p) for p in params] if options.get("values")
                                 else [{"id": "param:%d" % self._ptr(p), "name": self._param_name(p)} for p in params])
        return out

    def _read_options(self, include, max_depth):
        include = self._aspects(include)
        values = "param-values" in include
        return {"params": values or "params" in include, "values": values, "chains": "chains" in include,
                "return_chains": "return-chains" in include, "drum_pads": "drum-pads" in include,
                "max_depth": int(max_depth or 0), "depth": 0}

    def op_host_device(self, id=None, path=None, include=(), max_depth=0):
        if (id is None) == (path is None):
            raise self._error("invalid", "Give exactly one of id or path.")
        options = self._read_options(include, max_depth)
        if id is not None and str(id).startswith("chain:"):
            chain, chain_path = self._chain_by_id(id)
            return self._host_chain(chain, chain_path, -1, options["max_depth"], options)
        if id is not None:
            device = self._device_by_host_id(id)
            _, found_path = self._device_path(device)
        else:
            device, kind = self._resolve_path(path)
            if kind == "chain":
                return self._host_chain(device, path, -1, options["max_depth"], options)
            found_path = path
        return self._host_device_record(device, found_path, options)

    def _device_by_host_id(self, ident):
        for _, _, device in self._all_devices():
            if self._device_id(device) == ident:
                return device
        raise self._error("not_found", "Device %s no longer exists." % ident)

    def _aspects(self, include):
        include = set(include or ())
        unknown = include - READ_ASPECTS
        if unknown:
            raise self._error("invalid", "Unknown read aspects: %s." % ", ".join(sorted(unknown)))
        for aspect in sorted(include & set(UNSUPPORTED_ASPECTS)):
            raise self._error("unsupported", UNSUPPORTED_ASPECTS[aspect])
        return include

    # ---- tracks and Set (Producer Pal readTrackGeneric / readLiveSet) ----
    def _mix_state(self, obj):
        mute, solo, via = bool(self._g(obj, "mute")), bool(self._g(obj, "solo")), bool(self._g(obj, "muted_via_solo"))
        if mute and solo:
            return "muted-and-soloed"
        if solo:
            return "soloed"
        if mute and via:
            return "muted-also-via-solo"
        if via:
            return "muted-via-solo"
        return "muted" if mute else "active"

    def _gain_db(self, track):
        volume = self._g(self._g(track, "mixer_device"), "volume")
        if volume is None:
            return None
        display = self._g(volume, "display_value")
        if isinstance(display, (int, float)):
            return clean_number(float(display))
        value, unit, _ = parse_label(self._call_label(volume, self._g(volume, "value")))
        return value if unit == "dB" else None

    def _host_track(self, track, index, category, include, return_names):
        include = set(include or ())
        devices = list(self._g(track, "devices", ()) or ())
        armable = bool(self._g(track, "can_be_armed"))
        is_group = bool(self._g(track, "is_foldable"))
        out = {"id": self._track_id(track), "name": self._g(track, "name"),
               "type": category if category in ("return", "master") else "midi" if self._g(track, "has_midi_input") else "audio"}
        if armable and self._g(track, "arm"):
            out["isArmed"] = True
        if is_group:
            out["isGroup"] = True
        if self._g(track, "is_grouped"):
            out["isGroupMember"] = True
        group = self._g(track, "group_track")
        if group is not None:
            out["groupId"] = self._track_id(group)
        for device in devices:
            if self._device_type(device) in ("instrument", "instrument-rack", "drum-rack"):
                out["instrument"] = self._g(device, "class_display_name")
                break
        if "mixer" in include:
            self._mixer_fields(out, track, return_names)
        if category == "regular":
            out["trackIndex"] = index
        elif category == "return":
            out["returnTrackIndex"] = index
        if category == "regular":
            slots = list(self._g(track, "clip_slots", ()) or ())
            out["sessionClipCount"] = sum(1 for s in slots if self._g(s, "has_clip"))
        clips = [] if is_group or category != "regular" else list(self._g(track, "arrangement_clips", ()) or ())
        if "arrangement-clips" in include:
            out["arrangementClips"] = [self._clip_ref(c) for c in clips]
        else:
            out["arrangementClipCount"] = len(clips)
        if "devices" in include:
            out["devices"] = [self._host_device_record(d, "%s/d%d" % (self._track_prefix(category, index), i), {})
                              for i, d in enumerate(devices)]
        else:
            out["deviceCount"] = len(devices)
        if category == "regular":
            playing = self._g(track, "playing_slot_index", -1)
            if isinstance(playing, int) and playing >= 0:
                out["playingSlotIndex"] = playing
        state = "active" if category == "master" else self._mix_state(track)
        if state != "active":
            out["state"] = state
        if "routings" in include:
            self._routing_fields(out, track, category, is_group, armable)
        return out

    @staticmethod
    def _track_prefix(category, index):
        return "mt" if category == "master" else ("rt%d" if category == "return" else "t%d") % index

    def _clip_ref(self, clip):
        start, end = float(self._g(clip, "start_time")), float(self._g(clip, "end_time"))
        return {"id": self._clip_id(clip), "name": self._g(clip, "name"),
                "arrangementStartBeats": start, "arrangementLengthBeats": end - start,
                "lengthBeats": float(self._g(clip, "length")), "looping": bool(self._g(clip, "looping")),
                "timeSignature": "%s/%s" % (self._g(clip, "signature_numerator"), self._g(clip, "signature_denominator"))}

    def _mixer_fields(self, out, track, return_names):
        mixer = self._g(track, "mixer_device")
        if mixer is None:
            return
        gain = self._gain_db(track)
        if gain is not None:
            out["gainDb"] = gain
        if self._g(mixer, "panning_mode") == 1:
            out["panningMode"] = "split"
            for key, attr in (("leftPan", "left_split_stereo"), ("rightPan", "right_split_stereo")):
                param = self._g(mixer, attr)
                if param is not None:
                    out[key] = round(float(self._g(param, "value")) * 100) / 100.0
        else:
            panning = self._g(mixer, "panning")
            if panning is not None:
                out["pan"] = clean_number(round(float(self._g(panning, "value")) * 100) / 100.0)
        sends = list(self._g(mixer, "sends", ()) or ())
        if sends:
            out["sends"] = []
            for i, send in enumerate(sends):
                display = self._g(send, "display_value")
                if not isinstance(display, (int, float)):
                    display = parse_label(self._call_label(send, self._g(send, "value")))[0]
                out["sends"].append({"gainDb": clean_number(display) if isinstance(display, float) else display,
                                     "return": return_names[i] if i < len(return_names) else "Return %d" % (i + 1)})

    def _routing_fields(self, out, track, category, is_group, armable):
        def route(attr, key):
            value = self._g(track, attr)
            if value is None:
                return None
            ident = self._g(value, "identifier")
            return {"name": self._g(value, "display_name"), **({key: str(ident)} if ident is not None else {})}
        if category == "master":
            out.update(inputRoutingType=None, inputRoutingChannel=None, outputRoutingType=None, outputRoutingChannel=None)
            return
        if not is_group and category == "regular":
            out["inputRoutingType"] = route("input_routing_type", "inputId")
            out["inputRoutingChannel"] = route("input_routing_channel", "inputId")
        elif category == "return":
            out.update(inputRoutingType=None, inputRoutingChannel=None)
        out["outputRoutingType"] = route("output_routing_type", "outputId")
        out["outputRoutingChannel"] = route("output_routing_channel", "outputId")
        if armable:
            out["monitoringState"] = MONITORING.get(self._g(track, "current_monitoring_state"), "unknown")

    def op_host_track(self, id, include=()):
        include = self._aspects(include)
        names = [self._g(t, "name") for t in self._song().return_tracks]
        for category, index, track, _ in self._all_tracks():
            if self._track_id(track) == id:
                return self._host_track(track, index, category, include, names)
        raise self._error("not_found", "Track no longer exists.")

    def op_live_set(self, include=("tracks",)):
        include = self._aspects(include)
        song = self._song()
        names = [self._g(t, "name") for t in song.return_tracks]
        out = {"tempo": clean_number(float(song.tempo)), "timeSignature": "%d/%d" % (song.signature_numerator, song.signature_denominator),
               "sceneCount": len(self._g(song, "scenes", ()) or ())}
        if self._g(song, "is_playing"):
            out["isPlaying"] = True
        track_include = [x for x in ("routings", "mixer") if x in include]
        if "tracks" in include:
            out["tracks"] = [self._host_track(t, i, "regular", track_include, names) for i, t in enumerate(song.tracks)]
            out["returnTracks"] = [self._host_track(t, i, "return", track_include, names) for i, t in enumerate(song.return_tracks)]
            out["masterTrack"] = self._host_track(song.master_track, None, "master", track_include, names)
        else:
            out.update(regularTrackCount=len(song.tracks), returnTrackCount=len(song.return_tracks))
        if self._g(song, "scale_mode"):
            root = self._g(song, "root_note")
            out["scale"] = "%s %s" % (PITCH_CLASS_NAMES[root], self._g(song, "scale_name"))
            intervals = self._g(song, "scale_intervals") or ()
            out["scalePitches"] = ",".join(PITCH_CLASS_NAMES[(root + i) % 12] for i in intervals)
        if "locators" in include:
            out["locators"] = [{"id": "locator-%d" % i, "name": self._g(c, "name"), "timeBeats": float(self._g(c, "time"))}
                               for i, c in enumerate(self._g(song, "cue_points", ()) or ())]
        return out

    # ---- writes ----
    def op_set_parameters_by_name(self, device, params):
        """Producer Pal update-device params: {name: value text}. All targets are
        resolved and validated before anything is written."""
        if not isinstance(params, dict) or not params:
            raise self._error("invalid", "params must be a non-empty {name: value} object.")
        found = self._device_by_host_id(device)
        class_name = self._g(found, "class_display_name")
        if class_name in SPECIALIZED_DEVICES:
            raise self._error("unsupported", "%s has device-specific parameters that this bridge does not model." % class_name)
        plan = []
        for name, raw in params.items():
            if not isinstance(name, str) or not name.strip() or raw is None or str(raw).strip() == "":
                raise self._error("invalid", "Parameter names and values must be non-empty.")
            param = self._find_param_by_name(found, name.strip())
            if param is None:
                raise self._error("invalid", "Parameter %r was not found on %r. Nothing was changed." % (name, self._g(found, "name")))
            self._guard_writable(param, self._g(found, "name"))
            plan.append((param, self._resolve_write(param, raw)))
        for param, target in plan:
            param.value = target
        return {"id": device, "written": len(plan)}

    def op_rack_variation(self, device, action, index=None):
        found = self._device_by_host_id(device)
        if not self._g(found, "can_have_chains"):
            raise self._error("invalid", "Macro variations are only available on Racks.")
        count = self._need(found, "variation_count", "Rack macro variations")
        if action == "create":
            self._need(found, "store_variation", "Rack macro variations")()
            return {"id": device, "previousCount": count}
        if action == "load":
            if not isinstance(index, int) or not 0 <= index < count:
                raise self._error("invalid", "Variation %r is out of range (%d available)." % (index, count))
            found.selected_variation_index = index
            self._need(found, "recall_selected_variation", "Rack macro variations")()
            return {"id": device, "index": index}
        raise self._error("invalid", "Rack variation action must be create or load.")

    def op_create_native_device(self, track_index, device_name, name, params=None, index=None):
        """Insert a native device, optionally at a position in the chain rather than at the end.

        `index` matters because a tap only hears what is in front of it: the Scope plug-in measures
        at its own device position, so an EQ or Compressor appended after it cannot be verified by
        measurement - a -15 dB cut read as 0.00 dB of change. Pass the index of the listening device
        to land in front of it.

        Live's `Track.insert_device` is a C method with no introspectable signature, so whether it
        accepts a position is discovered by calling it and catching TypeError. If it does not, this
        refuses **without leaving a device behind** rather than silently appending to the wrong place.

        Two ordering rules, both the result of a real leak: `params` is checked against the devices
        this bridge cannot address by name *before* anything is inserted, and anything that goes
        wrong after insertion deletes what was made. The previous version inserted the device, named
        it, then applied `params` through the refused name-based path - so a failed call reported
        "EQ Eight has device-specific parameters that this bridge does not model" and left a fully
        created EQ Eight on the track.
        """
        song = self._song()
        if not isinstance(track_index, int) or not 0 <= track_index < len(song.tracks):
            raise self._error("invalid", "Track index is outside the Set.")
        if not isinstance(device_name, str) or not device_name or not isinstance(name, str) or not name:
            raise self._error("invalid", "Device and display names are required.")
        track = song.tracks[track_index]
        existing = len(track.devices)
        if index is not None:
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= existing:
                raise self._error("invalid", "Insertion index must be a whole number in 0..%d for this track." % existing)
        if params and device_name in SPECIALIZED_DEVICES:
            # Refused before insertion, not after: this is what used to leave an orphan behind.
            raise self._error("unsupported", "%s cannot be given parameters by name; create it without "
                                             "parameters and write them by parameter id." % device_name)
        insert = self._need(track, "insert_device", "native device insertion (Track.insert_device)")
        before = [self._ptr(d) for d in track.devices]
        positioned = False
        if index is None:
            insert(device_name)
        else:
            try:
                insert(device_name, index)
                positioned = True
            except TypeError:
                # Live will not place it; do not append to the wrong position and call it done.
                created = [d for d in track.devices if self._ptr(d) not in before]
                for stray in created:
                    self._delete_device_object(track, stray)
                raise self._error("unsupported", "This Live version's Track.insert_device takes no position, "
                                                 "so a device cannot be placed at index %d. Nothing was added." % index)
        created = [d for d in track.devices if self._ptr(d) not in before]
        if len(created) != 1:
            raise self._error("unverified", "Live did not report exactly one new device after inserting %r; inspect the track." % device_name)
        device = created[0]
        try:
            device.name = name
            if params:
                self.op_set_parameters_by_name(self._device_id(device), params)
        except Exception:
            self._delete_device_object(track, device)     # leave nothing half-made behind
            raise
        at = list(track.devices).index(device)
        if index is not None and at != index:
            self._delete_device_object(track, device)
            raise self._error("unverified", "Live placed %r at %d, not the requested %d. It has been removed." % (
                device_name, at, index))
        return {"id": self._device_id(device), "deviceIndex": at, "positioned": positioned}

    def _delete_device_object(self, track, device):
        """Remove a device we just made, by object identity, without trusting its id to still resolve."""
        try:
            for position, existing in enumerate(track.devices):
                if self._ptr(existing) == self._ptr(device):
                    self._need(track, "delete_device", "device deletion")(position)
                    return True
        except Exception:
            pass
        return False

    BROWSER_SHELVES = ("audio_effects", "instruments", "midi_effects", "plugins",
                       "user_library", "sounds", "drums", "packs", "current_project")

    def _browser(self):
        app = self._app() if self._app else None
        browser = self._g(app, "browser") if app is not None else None
        if browser is None:
            raise self._error("unsupported", "This Live exposes no browser, so devices cannot be loaded by name.")
        return browser

    def _browser_walk(self, item, depth=0, limit=6):
        """Yield every loadable browser item under `item`, depth-first."""
        if depth > limit:
            return
        for child in (self._g(item, "children", ()) or ()):
            if self._g(child, "is_loadable"):
                yield child
            for deeper in self._browser_walk(child, depth + 1, limit):
                yield deeper

    def op_browser_find(self, name, shelves=None, limit=20):
        """Loadable browser items whose name matches - plug-ins and presets included.

        `insert_device` only takes native Live devices, so this is the only way to reach a plug-in
        or a preset by name. Read-only: it loads nothing.
        """
        if not isinstance(name, str) or not name.strip():
            raise self._error("invalid", "Give a device or preset name to look for.")
        wanted = name.strip().lower()
        browser = self._browser()
        names = [s for s in (shelves or self.BROWSER_SHELVES) if s in self.BROWSER_SHELVES]
        exact, partial = [], []
        for shelf in names:
            root = self._g(browser, shelf)
            if root is None:
                continue
            for item in self._browser_walk(root):
                label = str(self._g(item, "name") or "")
                low = label.lower()
                if low == wanted:
                    exact.append({"name": label, "shelf": shelf, "uri": self._g(item, "uri")})
                elif wanted in low:
                    partial.append({"name": label, "shelf": shelf, "uri": self._g(item, "uri")})
                if len(exact) + len(partial) >= int(limit) * 4:
                    break
        found = (exact + partial)[: int(limit)]
        return {"query": name, "shelvesSearched": names, "matches": found, "exactMatches": len(exact)}

    def op_load_device(self, track, name, shelf=None, after_device=None, uri=None):
        """Load a device or preset - including a plug-in - onto a track, by name.

        Why this exists: `Track.insert_device` takes **native Live devices only** (`Scope` and
        `Kickstart 2` both fail with "not found"), and devices cannot be moved once placed. So
        without this, a plug-in can only be put on a track by hand, and a measuring tap can never be
        put behind a plug-in the producer added. Live's browser loads anything its own browser can.

        Live loads into the **selected** track, after the **selected** device, so both selections are
        made here and the result is verified by comparing the chain before and after. Exactly one new
        device must appear, or this reports unverified rather than guessing which one it was.
        """
        _, found_track = self._find_track(track)
        if not isinstance(name, str) or not name.strip():
            raise self._error("invalid", "Give a device or preset name to load.")
        wanted = name.strip().lower()
        browser = self._browser()
        shelves = [shelf] if shelf else list(self.BROWSER_SHELVES)
        target = None
        for shelf_name in shelves:
            root = self._g(browser, shelf_name)
            if root is None:
                continue
            for item in self._browser_walk(root):
                if (str(self._g(item, "name") or "").lower() == wanted
                        and (uri is None or self._g(item, "uri") == uri)):
                    target = item
                    break
            if target is not None:
                break
        if target is None:
            raise self._error("not_found", "No loadable browser item called %r. Use browser_find to look." % name)

        song = self._song()
        view = song.view
        before = [self._ptr(d) for d in found_track.devices]
        self._need(view, "selected_track", "track selection")
        view.selected_track = found_track
        if after_device is not None:
            _, _, anchor_device = self._find_device(after_device)
            if self._ptr(anchor_device) not in before:
                raise self._error("invalid", "That device is not on this track, so it cannot set the load position.")
            # Live 12.3.5 refuses `track.view.selected_device = ...` (read-only); Song.View.select_device
            # is the supported way, with the track's own view as a fallback.
            picked = False
            for owner, method in ((view, "select_device"), (self._g(found_track, "view"), "select_device")):
                call = self._g(owner, method)
                if callable(call):
                    try:
                        call(anchor_device)
                        picked = True
                        break
                    except Exception:
                        continue
            if not picked:
                raise self._error("unsupported", "This Live will not select a device, so a load position cannot be chosen.")
        load = self._need(browser, "load_item", "browser loading (Browser.load_item)")
        load(target)
        created = [d for d in found_track.devices if self._ptr(d) not in before]
        if len(created) != 1:
            raise self._error("unverified", "Live reported %d new devices after loading %r; inspect the track before retrying."
                              % (len(created), name))
        device = created[0]
        # NOTE: Live 12.3.5 exposes no `is_editor_open`, so a plug-in's floating window cannot be
        # closed from here (see op_open_plugin_window, which reports "ui-required"). Loading several
        # plug-ins therefore leaves a stack of windows over the producer's Set. The fix is Live's own
        # "Auto-Open Plug-In Windows" setting, turned off - not something this op can do.
        return {"id": self._device_id(device), "name": str(self._g(device, "name") or ""),
                "deviceIndex": list(found_track.devices).index(device),
                "trackId": self._track_id(found_track), "loaded": name}

    def op_reload(self):
        """Re-import this bridge's own code and rebind it onto the running instance.

        Live imports a Remote Script **once, at startup**, and caches the modules. Re-selecting the
        control surface in Settings makes a fresh *instance* of the *old* code - measured on
        20 September: after a full None/ScrunchBridge toggle, a newly added operation was still
        "Unknown operation". So a code change normally costs a Live restart, and a restart costs the
        producer their session.

        This copies the freshly imported functions onto the live class rather than swapping
        `__bases__`, which CPython refuses for layout-incompatible classes. Handlers are looked up as
        `op_<name>` on the instance, so rebinding the attributes is enough for new operations to
        appear immediately.

        It cannot reload itself into existence - the first install of this still needs one restart -
        and it does not re-run `__init__`, so sockets, threads and settle state are left alone.
        """
        import importlib, os, sys
        reloaded, failed, removed, fresh_ops = [], {}, [], set()
        for name in ("ScrunchBridge.host_ops", "ScrunchBridge.bridge"):
            module = sys.modules.get(name)
            if module is None:
                continue
            try:
                importlib.reload(module)
                reloaded.append(name)
            except Exception as exc:
                failed[name] = "%s: %s" % (type(exc).__name__, exc)
        if not reloaded:
            raise self._error("unsupported", "This bridge's modules are not in sys.modules under the expected names.")
        live = type(self)
        rebound = 0
        for name in reloaded:
            module = sys.modules[name]
            for attr in list(vars(module).values()):
                if not isinstance(attr, type):
                    continue
                if not any(base.__name__ == attr.__name__ for base in live.__mro__):
                    continue
                # Copy from __dict__, never dir()/getattr: getattr unwraps staticmethod and
                # classmethod into plain functions, and installing one of those on the live class
                # turns `self._ptr(obj)` into a two-argument call. That exact mistake broke the
                # bridge on 20 September ("_ptr() takes 1 positional argument but 2 were given").
                for member, value in list(vars(attr).items()):
                    if member.startswith("__"):
                        continue
                    if not isinstance(value, (staticmethod, classmethod, property)) and not callable(value):
                        continue
                    setattr(live, member, value)
                    rebound += 1
                fresh_ops.update(m for m in vars(attr) if m.startswith("op_"))
        # __init__ is not re-run, so state that new code added to __init__ would be missing and the
        # next tick would raise before any job runs, stalling the bridge until the control surface
        # is re-selected (26 Sep 2026: _scrunch_loop). Build an unstarted core from the fresh class
        # (its __init__ only sets attributes) and copy over every attribute this instance lacks.
        filled = []
        fresh_core = self._g(sys.modules.get("ScrunchBridge.bridge"), "BridgeCore")
        if isinstance(fresh_core, type):
            try:
                blank = fresh_core(self._song, token=self.token, config_path=os.devnull)
                for attr, value in vars(blank).items():
                    if attr not in vars(self):
                        setattr(self, attr, value)
                        filled.append(attr)
            except Exception as exc:
                failed["state"] = "%s: %s" % (type(exc).__name__, exc)
        # An operation deleted from the source must stop working too, not linger until Live restarts.
        if not failed and fresh_ops:
            for klass in live.__mro__:
                for member in [m for m in list(vars(klass)) if m.startswith("op_") and m not in fresh_ops]:
                    delattr(klass, member)
                    removed.append(member)
        return {"reloaded": reloaded, "rebound": rebound, "failed": failed, "removed": removed, "stateFilled": filled,
                "note": "New operations are live now; __init__ was not re-run, so the server and its threads are untouched."}

    def op_select_device(self, device):
        """Select a device and scroll Live's device view to it - native devices included.

        `op_open_plugin_window` refuses anything that is not a VST/AU, but the device chain still has
        to be scrolled to a native device before its UI can be read or clicked (the Compressor's
        sidechain panel sits off-screen otherwise).
        """
        found = self._device_by_host_id(device)
        track, _ = self._device_path(found)
        view = self._song().view
        view.selected_track = track
        self._need(view, "select_device", "device selection (select_device)")(found)
        self._show_arrangement()
        app_view = self._g(self._app() if self._app else None, "view")
        if callable(self._g(app_view, "show_view")):
            app_view.show_view("Detail/DeviceChain")
        return {"id": device, "trackName": self._g(track, "name"), "deviceName": self._g(found, "name")}

    def op_delete_device(self, device):
        found = self._device_by_host_id(device)
        for _, _, track, _ in self._all_tracks():
            parent = self._parent_of(track, found, 0)
            if parent is not None:
                container, index = parent
                self._need(container, "delete_device", "device deletion")(index)
                return {"id": device}
        raise self._error("not_found", "Device no longer exists.")

    def _parent_of(self, container, target, depth):
        if depth > 8:
            return None
        for index, device in enumerate(self._g(container, "devices", ()) or ()):
            if self._ptr(device) == self._ptr(target):
                return container, index
            if self._g(device, "can_have_chains"):
                for attr in ("chains", "return_chains"):
                    for chain in self._g(device, attr, ()) or ():
                        found = self._parent_of(chain, target, depth + 1)
                        if found:
                            return found
        return None

    def op_duplicate_track(self, id, name, clips=False, devices=True):
        index, track = self._find_track(id)
        song = self._song()
        self._need(song, "duplicate_track", "track duplication")(index)
        copy = song.tracks[index + 1]
        if self._ptr(copy) == self._ptr(track):
            raise self._error("unverified", "Live did not place the duplicate after the source track; inspect the Set.")
        copy.name = name
        if not devices:
            for i in range(len(copy.devices) - 1, -1, -1):
                copy.delete_device(i)
        if not clips:
            for slot in self._g(copy, "clip_slots", ()) or ():
                if self._g(slot, "has_clip"):
                    slot.delete_clip()
            for clip in list(self._g(copy, "arrangement_clips", ()) or ()):
                copy.delete_clip(clip)
        return {"id": self._track_id(copy), "trackIndex": index + 1}

    def op_replace_clip_notes(self, id, notes, name=None):
        track, clip = self._find_clip(id)
        if not self._g(clip, "is_midi_clip"):
            raise self._error("invalid", "Only MIDI clips have notes.")
        rows = []
        for n in notes if isinstance(notes, list) else [None]:
            if not isinstance(n, dict) or set(n) != {"p", "t", "d", "v"}:
                raise self._error("invalid", "Notes must be {p,t,d,v} objects.")
            rows.append((int(n["p"]), float(n["t"]), float(n["d"]), int(n["v"])))
        remove = self._need(clip, "remove_notes_extended", "note removal (remove_notes_extended)")
        spec = self._note_spec or self._default_note_spec()
        span = float(clip.length) + abs(float(self._g(clip, "start_marker", 0.0))) + 1024.0
        remove(0, 128, 0.0, span)
        if rows:
            clip.add_new_notes(tuple(spec(p, t, d, v) for p, t, d, v in rows))
        if name is not None:
            clip.name = name
        return {"id": id, "noteCount": len(rows)}

    def op_select_track(self, id):
        _, track = self._find_track(id)
        self._song().view.selected_track = track
        self._show_arrangement()
        return {"selectedTrack": self._track_id(track)}

    def op_select_clip(self, id):
        _, clip = self._find_clip(id)
        view = self._song().view
        self._need(view, "detail_clip", "clip selection (detail_clip)")
        self._show_arrangement()
        view.detail_clip = clip

        def verify():
            shown = self._g(self._song().view, "detail_clip")
            return {"selectedClip": self._clip_id(shown) if shown is not None else None}
        return self._defer(verify)

    def op_cc_control_info(self, device):
        found = self._device_by_host_id(device)
        if self._g(found, "class_name") != "MidiCcControl":
            raise self._error("invalid", "Choose a CC Control device.")
        return {"deviceId": device,
                "target": found.custom_float_target_0,
                "targets": list(found.custom_float_target_0_list)}

    def op_preset_clock(self):
        song = self._song()
        return {"playing": bool(song.is_playing), "beat": float(song.current_song_time),
                "barBeats": float(song.signature_numerator) * 4 / song.signature_denominator,
                "tempo": float(song.tempo)}

    def op_set_cc_control(self, device, controller, value, only_while_playing=False):
        if only_while_playing and not self._song().is_playing:
            return {"deviceId": device, "skipped": "playback-stopped"}
        info = self.op_cc_control_info(device)
        if (not isinstance(controller, int) or isinstance(controller, bool)
                or not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 127):
            raise self._error("invalid", "Controller and value must be MIDI integers; value must be 0..127.")
        matches = [i for i, label in enumerate(info["targets"])
                   if str(label).split(":", 1)[0] == str(controller)]
        if len(matches) != 1:
            raise self._error("unsupported", "CC Control does not expose controller %s." % controller)
        found = self._device_by_host_id(device)
        parameters = [p for p in found.parameters if self._g(p, "original_name") == "Custom B"]
        if len(parameters) != 1:
            raise self._error("unsupported", "CC Control's Custom B parameter is unavailable.")
        parameter = parameters[0]
        target = matches[0]
        previous = parameter.value
        found.custom_float_target_0 = target
        parameter.value = value

        def verify():
            if found.custom_float_target_0 != target or parameter.value != value:
                raise self._error("unverified", "CC Control did not retain the requested assignment and value.")
            return {"deviceId": device, "controller": controller, "value": value,
                    "valueChanged": previous != value, "verified": "cc-device-readback"}
        return self._defer(verify)

    def op_plugin_catalog(self, device):
        """Read the loaded instance's host preset and parameter catalogues."""
        found = self._device_by_host_id(device)
        result = {"deviceId": device, "name": self._g(found, "name"),
                  "className": self._g(found, "class_name"), "errors": {}}
        for attr in ("presets", "selected_preset_index"):
            try:
                value = getattr(found, attr)
                result[attr] = list(value) if attr == "presets" else value
            except Exception as exc:
                result["errors"][attr] = str(exc)
        try:
            result["parameterNames"] = list(found.get_parameter_names())
        except Exception as exc:
            result["errors"]["parameterNames"] = str(exc)
        return result

    def op_device_attributes(self, device):
        """Read-only diagnostic: attribute names Live exposes on a device, its view and the app view."""
        found = self._device_by_host_id(device)
        names = lambda obj: sorted(n for n in dir(obj) if not n.startswith("__")) if obj is not None else []
        app = self._app() if self._app else None
        return {"device": names(found), "deviceView": names(self._g(found, "view")),
                "songView": names(self._song().view), "appView": names(self._g(app, "view"))}

    def op_automation_api(self, track=None):
        """Read-only diagnostic: what this Live build exposes for automation (class members and,
        for one track, whether its arrangement clips hand out envelopes). Changes nothing."""
        import Live  # only available inside Ableton
        names = lambda obj: sorted(n for n in dir(obj) if not n.startswith("__")) if obj is not None else []
        out = {}
        for label, path in (("Song", "Song.Song"), ("Track", "Track.Track"), ("Clip", "Clip.Clip"),
                            ("DeviceParameter", "DeviceParameter.DeviceParameter"),
                            ("AutomationEnvelope", "Clip.AutomationEnvelope"), ("MixerDevice", "MixerDevice.MixerDevice"),
                            ("ClipSlot", "ClipSlot.ClipSlot")):
            obj = Live
            for part in path.split("."):
                obj = self._g(obj, part)
            out[label] = names(obj)
        app = self._app() if self._app else None
        out["version"] = [self._g(app, a)() if callable(self._g(app, a)) else None
                          for a in ("get_major_version", "get_minor_version", "get_bugfix_version")]
        out["LiveModules"] = names(Live)
        env_mod = self._g(Live, "Envelope")
        out["EnvelopeModule"] = {n: names(self._g(env_mod, n)) for n in names(env_mod) if isinstance(self._g(env_mod, n), type)}
        out["EnvelopeDocs"] = {n: str(self._g(self._g(self._g(env_mod, "Envelope"), n), "__doc__"))[:600]
                               for n in names(self._g(env_mod, "Envelope"))}
        clip_cls = self._g(self._g(Live, "Clip"), "Clip")
        out["ClipDocs"] = {n: str(self._g(self._g(clip_cls, n), "__doc__"))[:600]
                           for n in ("automation_envelope", "create_automation_envelope", "clear_envelope",
                                     "clear_all_envelopes", "automation_envelopes", "has_envelopes")}
        song_cls = self._g(self._g(Live, "Song"), "Song")
        out["SongDocs"] = {n: str(self._g(self._g(song_cls, n), "__doc__"))[:400]
                           for n in ("session_automation_record", "record_mode", "arrangement_overdub", "re_enable_automation", "start_time", "current_song_time", "continue_playing", "start_playing", "is_counting_in", "count_in_duration", "punch_in",
                                     "begin_undo_step", "end_undo_step", "undo")}
        param_cls = self._g(self._g(Live, "DeviceParameter"), "DeviceParameter")
        out["ParamDocs"] = {n: str(self._g(self._g(param_cls, n), "__doc__"))[:400]
                            for n in ("automation_state", "re_enable_automation", "begin_gesture", "value")}
        if track is not None:
            _, found = self._find_track(track)
            out["trackObject"] = names(found)
            volume = found.mixer_device.volume
            out["volumeParam"] = names(volume)
            for clip in self._arrangement_clips(found)[:1]:
                out["clipObject"] = names(clip)
                env = None
                try:
                    env = clip.automation_envelope(volume)
                    out["clipVolumeEnvelope"] = repr(env)
                except Exception as exc:
                    out["clipVolumeEnvelope"] = "raised %s: %s" % (type(exc).__name__, exc)
                if env is not None:
                    out["envelopeObject"] = names(env)
        return out

    # ---- arrangement automation (26 Sep 2026) ----
    # Live 12.3.5's Python API has no track arrangement envelope: Clip.create_automation_envelope
    # raises "Not a session clip or parameter belongs to another track." on Arrangement clips, and
    # Track/Song/DeviceParameter expose no envelope, breakpoint list or delete (probed live,
    # evidence/automation-envelopes-2026-09-26). What Live does allow: Arrangement recording of
    # parameter changes (record_mode + automation arm), gestures, reading a parameter's value where
    # the playhead is, and Song.undo. So Scrunch writes arrangement automation by recording it on
    # Live's own clock, reads it back by sampling the parameter at song positions, and takes a
    # recording back out with Live's undo. Breakpoints themselves cannot be listed, drawn or deleted.
    # (Limits are module constants: op_reload copies only callables onto the running class.)

    def _automation_param(self, track_id, target):
        """(track, param, id) for {"mixer": "volume"|"pan"} / {"mixer": "send", "send": i} / {"device": id, "parameter": id}."""
        if not isinstance(target, dict):
            raise self._error("invalid", "Automation target must be an object.")
        # Automation applies to return and Main controls too; the Arrangement
        # clip lookup intentionally covers regular tracks only.
        _, _, track = self._mix_track(track_id)
        if "mixer" in target:
            mixer = self._need(track, "mixer_device", "the track mixer")
            control = target["mixer"]
            if control == "volume":
                param = self._need(mixer, "volume", "the track volume")
            elif control == "pan":
                param = self._need(mixer, "panning", "the track pan")
            elif control == "send":
                sends = list(self._need(mixer, "sends", "the track sends") or ())
                index = target.get("send")
                if not isinstance(index, int) or not 0 <= index < len(sends):
                    raise self._error("invalid", "Send index is outside this track's %d sends." % len(sends))
                param = sends[index]
            else:
                raise self._error("invalid", "Mixer automation targets are volume, pan and send.")
        elif "device" in target and "parameter" in target:
            owner, _, device = self._find_device(target["device"])
            if self._ptr(owner) != self._ptr(track):
                raise self._error("conflict", "That device is on another track. Nothing was changed.")
            _, param = self._find_param(device, target["parameter"])
        else:
            raise self._error("invalid", "Give a mixer control or a device and parameter id.")
        return track, param, "param:%d" % self._ptr(param)

    def _automation_record(self, param, ident):
        value = self._g(param, "value")
        out = {"id": ident, "name": self._g(param, "name"), "value": float(value) if value is not None else None,
               "min": float(self._g(param, "min", 0.0)), "max": float(self._g(param, "max", 1.0)),
               "quantized": bool(self._g(param, "is_quantized", False)), "enabled": bool(self._g(param, "is_enabled", True)),
               "automation": {0: "none", 1: "playing", 2: "overridden"}.get(self._g(param, "automation_state", 0), "unknown")}
        try:
            out["display"] = str(param.str_for_value(value))
        except Exception:
            out["display"] = None
        return out

    def _display_target(self, param, text):
        """Internal value whose Live display parses to the number `text` asks for (monotonic search on str_for_value)."""
        want, unit = text
        lo, hi = float(param.min), float(param.max)
        def shown(v):
            value, got, _ = parse_label(self._call_label(param, v))
            if not isinstance(value, (int, float)) or (unit and got != unit):
                raise self._error("unsupported", "%r does not display in %s: %r." % (self._g(param, "name"), unit, self._call_label(param, v)))
            return value
        a, b = shown(lo), shown(hi)
        if not min(a, b) - 1e-9 <= want <= max(a, b) + 1e-9:
            raise self._error("invalid", "%r %s is outside %r's range %r..%r." % (want, unit or "", self._g(param, "name"), a, b))
        rising = b >= a
        x, y = lo, hi
        for _ in range(SEARCH_ITERATIONS + 8):
            mid = (x + y) / 2.0
            if (shown(mid) < want) == rising:
                x = mid
            else:
                y = mid
        return y if rising else x

    def op_automation_target(self, track, target, displays=None):
        """Read one automation target (identity, value, range, automation state); optionally convert displays such as
        [[-12, "dB"]] to internal values. Read-only."""
        _, param, ident = self._automation_param(track, target)
        out = dict(self._automation_record(param, ident), trackId=track)
        if displays is not None:
            if not isinstance(displays, list) or len(displays) > 256:
                raise self._error("invalid", "Displays must be a list of [number, unit] pairs.")
            out["internal"] = [self._display_target(param, (float(d[0]), d[1] if len(d) > 1 else None)) for d in displays]
        return out

    def _validate_lane(self, track, lane):
        if not isinstance(lane, dict):
            raise self._error("invalid", "Each lane is an object with target, name and points.")
        _, param, ident = self._automation_param(track, lane.get("target"))
        record = self._automation_record(param, ident)
        if record["name"] != lane.get("name"):
            raise self._error("conflict", "Target is now named %r, not %r. Nothing was changed; read it again." % (record["name"], lane.get("name")))
        if lane.get("id") not in (None, ident):
            raise self._error("conflict", "Target %r is now %s, not %s. Nothing was changed." % (record["name"], ident, lane.get("id")))
        if not record["enabled"]:
            raise self._error("conflict", "%r is disabled in Live. Nothing was changed." % record["name"])
        points = lane.get("points")
        if not isinstance(points, list) or not 2 <= len(points) <= 256:
            raise self._error("invalid", "Give 2 to 256 [beat, value] points per lane.")
        rows, last = [], -1.0
        for p in points:
            if not isinstance(p, (list, tuple)) or len(p) != 2 or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in p):
                raise self._error("invalid", "Points are [beat, value] pairs of numbers.")
            beat, value = float(p[0]), float(p[1])
            if not (beat == beat and value == value) or beat < 0 or beat <= last:
                raise self._error("invalid", "Point beats must be non-negative and strictly increasing.")
            if not record["min"] <= value <= record["max"]:
                raise self._error("invalid", "%r is outside %r's range %r..%r." % (value, record["name"], record["min"], record["max"]))
            if record["quantized"] and value != int(value):
                raise self._error("invalid", "%r is discrete; give whole steps." % record["name"])
            rows.append((beat, value))
            last = beat
        return {"param": param, "id": ident, "name": record["name"], "points": rows, "before": record,
                "touching": False, "released": False, "sent": []}

    @staticmethod
    def _lane_value(points, beat):
        if beat <= points[0][0]:
            return points[0][1]
        for (b0, v0), (b1, v1) in zip(points, points[1:]):
            if beat <= b1:
                return v0 + (v1 - v0) * (beat - b0) / (b1 - b0)
        return points[-1][1]

    def op_record_automation(self, track, lanes, lead=1.0):
        """Record arrangement automation for one track's parameters on Live's own clock.

        Arrangement record runs from `lead` beats before the first point; each tick the bridge sets
        every lane to its planned value for the current song position inside a gesture, and releases
        it after the last point. Nothing else may be recording: no armed track, record off, stopped.
        Loop, punch, automation arm and playhead are put back afterwards. Replies at once; poll
        automation_take_status. Values are internal parameter values within each parameter's min/max.
        """
        song = self._song()
        if getattr(self, "_auto_take", None) is not None:
            raise self._error("conflict", "An automation recording is already running.")
        if getattr(self, "_sweep", None) is not None:
            raise self._error("conflict", "A knob sweep is running; stop it first. Nothing was changed.")
        if not isinstance(lanes, list) or not 1 <= len(lanes) <= 8:
            raise self._error("invalid", "Give 1 to 8 lanes.")
        if not isinstance(lead, (int, float)) or not 0.5 <= lead <= 16:
            raise self._error("invalid", "Lead-in must be 0.5 to 16 beats: recording has to be running before the first point.")
        plan = [self._validate_lane(track, lane) for lane in lanes]
        if len({p["id"] for p in plan}) != len(plan):
            raise self._error("invalid", "A parameter appears in two lanes.")
        first = min(p["points"][0][0] for p in plan)
        end = max(p["points"][-1][0] for p in plan)
        if end - first > MAX_TAKE_BEATS:
            raise self._error("invalid", "One recording covers at most %d beats." % MAX_TAKE_BEATS)
        if bool(song.is_playing) or bool(self._g(song, "record_mode", False)) or bool(self._g(song, "is_counting_in", False)):
            raise self._error("conflict", "Live is playing or recording. Nothing was changed.")
        armed = [t.name for t in song.tracks if self._g(t, "can_be_armed", False) and self._g(t, "arm", False)]
        if armed:
            raise self._error("conflict", "Armed tracks would record too: %s. Nothing was changed." % ", ".join(armed[:4]))
        saved = {"loop": bool(song.loop), "punch_in": bool(self._g(song, "punch_in", False)),
                 "punch_out": bool(self._g(song, "punch_out", False)),
                 "session_automation_record": bool(self._g(song, "session_automation_record", False)),
                 "arrangement_overdub": bool(self._g(song, "arrangement_overdub", False)),
                 "current_song_time": float(song.current_song_time),
                 "start_time": float(self._need(song, "start_time", "the start marker (start_time)"))}
        start = max(0.0, first - float(lead))
        take_id = "take:%d" % int(self._clock() * 1e6)
        self._auto_take = {"id": take_id, "track": track, "plan": plan, "saved": saved, "start": start, "first": first,
                           "end": end, "state": "starting", "error": None, "startedAt": self._clock(),
                           "tempo": float(song.tempo), "ticks": 0, "undoOpen": False}
        try:
            song.begin_undo_step()
            self._auto_take["undoOpen"] = True
            song.loop = False
            for key in ("punch_in", "punch_out"):
                if saved[key]:
                    setattr(song, key, False)
            song.session_automation_record = True
            if getattr(song, "back_to_arranger", False):
                song.back_to_arranger = False
            # Live 12.3.5, 26 Sep 2026: start_playing starts at the start marker (it began at 2036 with the
            # playhead set to 3) and continue_playing resumes where playback last stopped (105.2 with the
            # playhead located at 0). So the start marker is moved to the lead-in (loop is off, so the loop
            # start cannot override it), playback starts on a later tick once Live reads it back, and the
            # marker is put back afterwards.
            song.start_time = start
            song.current_song_time = start
            self._auto_take["state"] = "locating"
        except Exception as exc:
            self._finish_take("failed", "Could not start recording: %s: %s" % (type(exc).__name__, exc))
            raise self._error("host", "Recording did not start (%s); Live's transport settings were put back." % exc)
        seconds = (end - start) * 60.0 / float(song.tempo)
        return {"take": take_id, "recording": True, "startBeat": start, "firstBeat": first, "endBeat": end,
                "expectedSeconds": round(seconds + 1.0, 2),
                "lanes": [{"id": p["id"], "name": p["name"], "before": p["before"]} for p in plan]}

    def _release(self, lane, value=None):
        if lane["touching"] and not lane["released"]:
            try:
                if value is not None:
                    lane["param"].value = float(value)
            finally:
                lane["param"].end_gesture()
                lane["released"] = True

    def _finish_take(self, state, error=None):
        take = getattr(self, "_auto_take", None)
        if take is None:
            return
        song = self._song()
        problems = []
        for lane in take["plan"]:
            try:
                self._release(lane)
            except Exception as exc:
                problems.append("release %s: %s" % (lane["name"], exc))
        steps = [("record_mode", lambda: setattr(song, "record_mode", False)),
                 ("stop", lambda: song.stop_playing() if song.is_playing else None)]
        saved = take["saved"]
        for key in ("loop", "punch_in", "punch_out", "session_automation_record", "arrangement_overdub"):
            steps.append((key, lambda key=key: setattr(song, key, saved[key]) if bool(self._g(song, key, saved[key])) != saved[key] else None))
        steps.append(("start marker", lambda: setattr(song, "start_time", saved["start_time"])))
        steps.append(("playhead", lambda: setattr(song, "current_song_time", saved["current_song_time"])))
        if take["undoOpen"]:
            steps.append(("end_undo_step", song.end_undo_step))
            take["undoOpen"] = False
        for name, fn in steps:
            try:
                fn()
            except Exception as exc:
                problems.append("%s: %s" % (name, exc))
        take["state"] = state
        take["error"] = "; ".join(x for x in ([error] if error else []) + problems) or None
        take["finishedAt"] = self._clock()
        self._last_take = take
        self._auto_take = None

    def _advance_automation_take(self):
        take = getattr(self, "_auto_take", None)
        if take is None:
            return
        song = self._song()
        take["ticks"] += 1
        playing = bool(song.is_playing)
        now = float(song.current_song_time)
        if take["state"] == "locating":
            if playing:
                self._finish_take("failed", "Live started playing before Scrunch's recording began; nothing was touched.")
            elif abs(float(song.start_time) - take["start"]) < 1e-3 and take["ticks"] > self.settle_ticks:
                song.record_mode = True
                song.start_playing()
                take["state"] = "starting"
            elif take["ticks"] > 30:
                self._finish_take("failed", "The start marker did not reach beat %.3f (it reads %.3f); nothing was touched." % (take["start"], float(song.start_time)))
            return
        if take["state"] == "starting":
            take.setdefault("startLog", []).append([take["ticks"], round(now, 4), playing, bool(self._g(song, "record_mode", False)),
                                                    bool(self._g(song, "is_counting_in", False))])
            if playing and bool(self._g(song, "record_mode", False)) and not bool(self._g(song, "is_counting_in", False)):
                if not take["start"] - 0.05 <= now < take["first"]:   # recording must be running before the first point
                    # Never touch a parameter while recording somewhere other than planned.
                    self._finish_take("failed", "Live started recording at beat %.3f, not %.3f; nothing was touched." % (now, take["start"]))
                    return
                take["state"] = "recording"
            elif self._clock() - take["startedAt"] > 10.0:
                self._finish_take("failed", "Live did not start recording within 10 s.")
                return
            else:
                return
        if not playing or not bool(self._g(song, "record_mode", False)):
            self._finish_take("interrupted", "Playback or recording was stopped at beat %.3f before the take finished." % now)
            return
        limit = (take["end"] - take["start"] + 8.0) * 60.0 / take["tempo"] + 10.0
        if self._clock() - take["startedAt"] > limit:
            self._finish_take("failed", "Recording overran its time limit.")
            return
        # Touch each lane a little before its first point: values are set once per display tick, so a
        # lane touched at the first tick after the point starts its ramp late, from the old value (live,
        # 26 Sep 2026: beat 4 still read -12 dB, not the planned -30 dB). The first value is held until
        # the point, so the jump to it lands at most ~1.5 ticks early.
        step = max(now - take.get("lastNow", now), take["tempo"] / 60.0 * 0.1)
        take["lastNow"] = now
        try:
            for lane in take["plan"]:
                points = lane["points"]
                if lane["released"]:
                    continue
                if now + 1.5 * step >= points[0][0] and not lane["touching"]:
                    lane["param"].begin_gesture()
                    lane["touching"] = True
                if lane["touching"]:
                    value = self._lane_value(points, now)
                    lane["param"].value = float(value)
                    lane["sent"].append((round(now, 6), value))
                    if now >= points[-1][0]:
                        self._release(lane, points[-1][1])
                        lane["sent"].append((round(now, 6), points[-1][1]))
        except Exception as exc:
            self._finish_take("failed", "%s: %s" % (type(exc).__name__, exc))
            return
        if all(lane["released"] for lane in take["plan"]):
            self._finish_take("finished")

    def _take_summary(self, take, running):
        return {"take": take["id"], "running": running, "state": take["state"], "error": take["error"],
                "trackId": take["track"], "startBeat": take["start"], "firstBeat": take["first"], "endBeat": take["end"],
                "ticks": take["ticks"], "startLog": take.get("startLog"),
                "countIn": self._g(self._song(), "count_in_duration", None),
                "lanes": [{"id": l["id"], "name": l["name"], "points": [list(p) for p in l["points"]],
                           "sent": [list(s) for s in l["sent"]], "before": l["before"]} for l in take["plan"]]}

    def op_automation_take_status(self):
        take = getattr(self, "_auto_take", None)
        if take is not None:
            return self._take_summary(take, True)
        last = getattr(self, "_last_take", None)
        return {"running": False} if last is None else self._take_summary(last, False)

    def op_stop_automation_take(self):
        if getattr(self, "_auto_take", None) is None:
            raise self._error("conflict", "No automation recording is running.")
        self._finish_take("interrupted", "Stopped on request.")
        return self._defer(self.op_automation_take_status)

    def op_undo_automation_take(self, take):
        """Undo Scrunch's last recording with Live's undo, only if it is the last thing Scrunch recorded
        and nothing is recording now. Live's undo is global: the caller must have confirmed the take's
        lanes still read exactly as recorded, so a later edit is not what gets undone."""
        last = getattr(self, "_last_take", None)
        if getattr(self, "_auto_take", None) is not None:
            raise self._error("conflict", "A recording is running; stop it first.")
        if last is None or last["id"] != take:
            raise self._error("conflict", "That is not Scrunch's most recent automation recording. Nothing was undone.")
        if last.get("undone"):
            raise self._error("conflict", "That recording was already undone.")
        if not any(lane["touching"] for lane in last["plan"]):
            raise self._error("conflict", "That recording never touched a parameter, so there is nothing of Scrunch's to undo. Nothing was undone.")
        song = self._song()
        if bool(song.is_playing) or bool(self._g(song, "record_mode", False)):
            raise self._error("conflict", "Live is playing or recording. Nothing was undone.")
        if not bool(self._g(song, "can_undo", False)):
            raise self._error("conflict", "Live has nothing to undo.")
        tracks_before = len(song.tracks)
        label = song.undo()
        last["undone"] = True

        def put_back():
            # Live's undo removes the recorded envelope but leaves the parameter at the last recorded
            # value (live, 26 Sep 2026: Frequency stayed 0.9, not 0.8997). A parameter that had no
            # automation before and has none now gets its pre-recording value back.
            restored = []
            for lane in last["plan"]:
                param, before = lane["param"], lane["before"]
                state = self._g(param, "automation_state", 0)
                if before["automation"] == "none" and state == 0 and before["value"] is not None \
                        and abs(float(param.value) - before["value"]) > 0:
                    param.value = float(before["value"])
                    restored.append(lane["name"])
            return self._defer(lambda: {"take": take, "undone": True, "liveUndo": str(label) if label is not None else None,
                                        "valuesPutBack": restored,
                                        "tracksBefore": tracks_before, "tracksAfter": len(self._song().tracks)})
        return self._defer(put_back)

    def op_sample_automation(self, track, targets, beats):
        """Read each target's value with the playhead at each beat (stopped transport), then put the playhead back.

        Live moves automated parameters to their envelope value when the playhead is located; this is
        the only envelope readback Live 12.3.5 exposes. Returns internal value, display and state per beat.
        """
        song = self._song()
        if bool(song.is_playing) or getattr(self, "_auto_take", None) is not None:
            raise self._error("conflict", "Sampling needs Live stopped and no recording running.")
        if not isinstance(beats, list) or not 1 <= len(beats) <= MAX_SAMPLES or not all(
                isinstance(b, (int, float)) and not isinstance(b, bool) and b >= 0 for b in beats):
            raise self._error("invalid", "Give 1 to %d non-negative beats." % MAX_SAMPLES)
        if not isinstance(targets, list) or not 1 <= len(targets) <= 8:
            raise self._error("invalid", "Give 1 to 8 targets.")
        resolved = [self._automation_param(track, t)[1:] for t in targets]
        home = float(song.current_song_time)
        rows = [{"id": ident, "name": self._g(p, "name"), "samples": []} for p, ident in resolved]
        queue = list(beats)

        def step():
            beat = queue.pop(0)
            self._song().current_song_time = float(beat)
            return self._defer(lambda: read(beat))

        def read(beat):
            if bool(self._song().is_playing):
                # Someone pressed play mid-read: leave their playback alone and report nothing half-read.
                raise self._error("conflict", "Live started playing during the read-back; stopped sampling, playhead left where playback put it.")
            actual = float(self._song().current_song_time)
            for row, (param, ident) in zip(rows, resolved):
                rec = self._automation_record(param, ident)
                row["samples"].append({"beat": float(beat), "playhead": actual, "value": rec["value"],
                                       "display": rec["display"], "automation": rec["automation"]})
            if queue:
                return step()
            self._song().current_song_time = home
            return self._defer(lambda: {"trackId": track, "playheadRestored": abs(float(self._song().current_song_time) - home) < 1e-6,
                                                      "targets": rows})
        return step()

    def op_open_plugin_window(self, device, open=True):
        found = self._device_by_host_id(device)
        if self._g(found, "class_name") not in ("PluginDevice", "AuPluginDevice"):
            raise self._error("invalid", "Plug-in windows exist only for VST/AU devices.")
        track, _ = self._device_path(found)
        view = self._song().view
        select = self._need(view, "select_device", "device selection (select_device)")
        view.selected_track = track
        select(found)
        self._show_arrangement()
        app_view = self._g(self._app() if self._app else None, "view")
        if callable(self._g(app_view, "show_view")):
            app_view.show_view("Detail/DeviceChain")
        if self._g(found, "is_editor_open", None) is not None:
            found.is_editor_open = bool(open)
            return {"id": device, "editor": "set"}
        # Live 12.3.5's Python API has no is_editor_open (Producer Pal reaches it through Max).
        # The device is now selected and shown; the client presses its header toggle through
        # accessibility and verifies the editor window title.
        return {"id": device, "editor": "ui-required", "trackName": self._g(track, "name"), "deviceName": self._g(found, "name")}

    def _show_arrangement(self):
        app = self._app() if self._app else None
        view = self._g(app, "view")
        show = self._g(view, "show_view")
        if callable(show):
            show("Arranger")
