"""Mix operations for Scrunch mix actions: mixer, routing, native devices on any track, exact values.

Kept in its own module on purpose (26 Sep 2026): other work edits bridge.py and host_ops.py, so the
mix ops are installed onto BridgeCore by `install()` from a few lines at the end of bridge.py. Every
op here is named `mix_*`.

Values are Live's own internal values. Where a caller asks for a musical unit (dB, ms, Hz, %, a
ratio), `op_mix_resolve` finds the internal value by searching Live's own display text
(`str_for_value`), the same method the fader gain path uses - no curve is guessed. Snapshots carry
the raw internal values so an undo can write back exactly what was there.
"""
import math


from .host_ops import parse_label

MIX_WRITE_OPS = frozenset({"mix_restore_view", "mix_set_mixer", "mix_insert_device", "mix_set_routing", "mix_create_audio_track", "mix_play",
                           "mix_restore_parameters", "mix_import_audio", "mix_add_locators"})
ROUTING_KINDS = ("sidechain", "input", "output")
SEARCH_STEPS = 60
LOCATE_TICKS = 10   # ticks for the stopped engine to reach a new current_song_time before a locator is placed there
LOCATOR_ZOOM_IN, LOCATOR_ZOOM_OUT, LOCATOR_ZOOM_STEPS = 3, 2, 30   # Application.View.zoom_view directions measured 8 Oct 2026
VIEW_SONG_FIELDS = (("loop", "loop", bool), ("loopStart", "loop_start", float), ("loopLength", "loop_length", float),
               ("songTime", "current_song_time", float), ("startTime", "start_time", float),
               ("backToArranger", "back_to_arranger", bool), ("recordMode", "record_mode", bool),
               ("punchIn", "punch_in", bool), ("punchOut", "punch_out", bool), ("arrangementOverdub", "arrangement_overdub", bool),
               ("sessionAutomationRecord", "session_automation_record", bool), ("playing", "is_playing", bool))
VIEW_NAMES = ("Session", "Arranger", "Detail", "Detail/Clip", "Detail/DeviceChain", "Browser")
MAX_PARAMETERS = 4096


def display_number(text):
    """The number a Live display shows, in its unit's base (kHz -> Hz, s -> ms), or +-inf, or None."""
    if not isinstance(text, str):
        return None
    t = text.strip().lower()
    if t.startswith("-inf"):
        return float("-inf")
    if t.startswith("inf") or t.startswith("+inf"):
        return float("inf")
    value, unit, direction = parse_label(text)
    if unit == "pan":
        if direction is None:
            return 0.0
        return float(value) * (-1 if str(direction).lower() == "l" else 1)
    if unit == "dB" and t.startswith("-inf"):
        return float("-inf")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


class MixOps(object):
    """Mixin installed onto BridgeCore. Uses its _song, _g, _need, _error, _ptr, _track_id,
    _device_id, _param_id, _find_device, _param_record, _all_tracks and _defer."""

    def _mix_defer(self, ticks, then):
        import sys
        return sys.modules[type(self).__module__].Defer(ticks, then)

    # ---- lookup ----
    def _mix_track(self, ident):
        for category, index, track, _ in self._all_tracks():
            if self._track_id(track) == ident:
                return category, index, track
        raise self._error("not_found", "Track no longer exists.")

    def _mix_param(self, owner, param):
        """(object, parameter) for a device parameter id, or a track's mixer 'volume' / 'panning'."""
        if param in ("volume", "panning"):
            _, _, track = self._mix_track(owner)
            mixer = self._need(track, "mixer_device", "the track mixer")
            return track, self._need(mixer, param, "the track " + param)
        _, _, device = self._find_device(owner)
        for p in self._g(device, "parameters", ()) or ():
            if self._param_id(p) == param:
                return device, p
        raise self._error("not_found", "Parameter no longer exists on this device.")

    # ---- reads ----
    def _mix_param_state(self, param):
        value = self._g(param, "value")
        out = {"name": self._g(param, "name"), "value": float(value) if value is not None else None,
               "min": self._g(param, "min"), "max": self._g(param, "max"),
               "quantized": bool(self._g(param, "is_quantized", False)),
               "enabled": self._g(param, "is_enabled", None), "automation": self._g(param, "automation_state", None),
               "display": None}
        items = self._g(param, "value_items")
        if items:
            out["options"] = [str(x) for x in items]
        try:
            out["display"] = self._call_label(param, value)
        except Exception:
            pass
        return out

    def _mix_routing(self, obj, kind):
        prefix = {"sidechain": "input", "input": "input", "output": "output"}[kind]
        current = self._g(obj, prefix + "_routing_type")
        if current is None:
            return None
        channel = self._g(obj, prefix + "_routing_channel")
        name = lambda r: None if r is None else str(self._g(r, "display_name", ""))
        types = list(self._g(obj, "available_" + prefix + "_routing_types", ()) or ())
        return {"type": name(current), "channel": name(channel), "typeTrack": self._mix_attached(current),
                "types": [name(r) for r in types],
                # the track each choice carries (Live's RoutingType.attached_object), so two tracks with one name differ
                "typeTracks": [self._mix_attached(r) for r in types],
                "channels": [name(r) for r in (self._g(obj, "available_" + prefix + "_routing_channels", ()) or ())]}

    def _mix_attached(self, routing):
        obj = self._g(routing, "attached_object") if routing is not None else None
        if obj is None:
            return None
        try:
            return self._track_id(obj)
        except Exception:
            return None

    def _mix_device_state(self, device, position):
        params = list(self._g(device, "parameters", ()) or ())[:MAX_PARAMETERS]
        out = {"id": self._device_id(device), "index": position, "name": self._g(device, "name"),
               "deviceName": self._g(device, "class_display_name"), "className": self._g(device, "class_name"),
               "parameters": [dict(self._mix_param_state(p), id=self._param_id(p)) for p in params]}
        side = self._mix_routing(device, "sidechain")
        if side is not None:
            out["sidechain"] = side
        return out

    def op_mix_state(self, track):
        """One tick's full snapshot of a track (regular, group, return or master) for exact undo."""
        category, index, found = self._mix_track(track)
        mixer = self._need(found, "mixer_device", "the track mixer")
        out = {"id": track, "name": self._g(found, "name"), "category": category, "trackIndex": index,
               "isGroup": bool(self._g(found, "is_foldable", False)),
               "mute": self._g(found, "mute", None), "solo": self._g(found, "solo", None),
               "arm": self._g(found, "arm", None) if self._g(found, "can_be_armed", False) else None,
               "mixer": {k: self._mix_param_state(self._need(mixer, k, "the track " + k)) for k in ("volume", "panning")},
               "devices": [self._mix_device_state(d, i) for i, d in enumerate(self._g(found, "devices", ()) or ())],
               "routing": {k: self._mix_routing(found, k) for k in ("input", "output")}}
        return out

    # ---- the producer's view and transport state (read-only snapshot; put back by op_mix_restore_view) ----
    def _mix_read(self, obj, attr, kind):
        v = self._g(obj, attr)
        if v is None:
            return None
        try:
            return kind(v)
        except Exception:
            return None

    def op_mix_view_state(self, devices=True):
        """Everything a read-only Scrunch pass must leave exactly as found, read in one tick: loop brace (start and
        length even while looping is off) and loop on/off, playhead, start marker, record/punch/overdub flags,
        Back to Arrangement, selected track / scene / detail clip, which main view has focus and which views show,
        every track's (regular, return, master) name, mute, solo, arm, monitoring, fold, routing, mixer values and,
        with `devices`, every device's id, name and raw parameter values. Unreadable fields are None."""
        song = self._song()
        out = {k: self._mix_read(song, a, kind) for k, a, kind in VIEW_SONG_FIELDS}
        view = self._g(song, "view")
        sel = self._g(view, "selected_track")
        out["selectedTrack"] = self._track_id(sel) if sel is not None else None
        scene = self._g(view, "selected_scene")
        scenes = list(self._g(song, "scenes", ()) or ())
        out["selectedScene"] = next((i for i, x in enumerate(scenes) if x == scene), None) if scene is not None else None
        clip = self._g(view, "detail_clip")
        try:
            out["detailClip"] = None if clip is None else "clip:%d" % self._ptr(clip)
        except Exception:
            out["detailClip"] = "unreadable"
        out["followSong"] = self._mix_read(view, "follow_song", bool)
        out["drawMode"] = self._mix_read(view, "draw_mode", bool)
        app_view = self._g(self._app() if self._app else None, "view")
        out["focusedView"] = self._mix_read(app_view, "focused_document_view", str)
        visible = self._g(app_view, "is_view_visible")
        out["visibleViews"] = {v: bool(visible(v)) for v in VIEW_NAMES} if callable(visible) else None
        tracks = []
        for category, index, track, _ in self._all_tracks():
            row = {"id": self._track_id(track), "category": category, "index": index, "name": self._g(track, "name"),
                   "mute": self._mix_read(track, "mute", bool), "solo": self._mix_read(track, "solo", bool),
                   "arm": self._mix_read(track, "arm", bool) if self._g(track, "can_be_armed", False) else None,
                   "monitoring": self._mix_read(track, "current_monitoring_state", int),
                   "folded": self._mix_read(track, "fold_state", bool) if self._g(track, "is_foldable", False) else None,
                   "input": (self._mix_routing(track, "input") or {}).get("type") if category == "regular" else None,
                   "output": (self._mix_routing(track, "output") or {}).get("type") if category != "master" else None}
            sd = self._g(self._g(track, "view"), "selected_device")
            try:
                row["selectedDevice"] = None if sd is None else self._device_id(sd)
            except Exception:
                row["selectedDevice"] = "unreadable"
            mixer = self._g(track, "mixer_device")
            row["mixer"] = {k: self._mix_read(self._g(mixer, k), "value", float) for k in ("volume", "panning", "crossfader")}
            row["mixer"]["sends"] = [self._mix_read(x, "value", float) for x in (self._g(mixer, "sends", ()) or ())]
            if devices:
                row["devices"] = [{"id": self._device_id(d), "path": path, "name": self._g(d, "name"),
                                   "parameters": [[self._g(p, "name"), self._mix_read(p, "value", float)] for p in (self._g(d, "parameters", ()) or ())]}
                                  for path, d in self._walk_devices(self._g(track, "devices", ()) or (), "track")]
            tracks.append(row)
        out["tracks"] = tracks
        return out

    def op_mix_restore_view(self, state):
        """Put the producer's view/transport state back from an op_mix_view_state snapshot, only where it differs:
        loop brace and on/off, start marker, Back to Arrangement, selected track and scene, focused main view, playhead
        (last, with playback stopped). Track mute/solo/arm/devices are NOT written here (a read-only pass never changes
        them; a difference there is reported by the caller's diff, never overwritten). Verified on a later tick."""
        song = self._song()
        if bool(song.is_playing):
            raise self._error("conflict", "Live is playing; the view is put back only with playback stopped.")
        if not isinstance(state, dict):
            raise self._error("invalid", "Give the snapshot from mix_view_state.")
        done = []
        def put(attr, value, key):
            if value is None:
                return
            now = self._g(song, attr)
            if now is None or (abs(float(now) - float(value)) > 1e-9 if isinstance(value, float) else bool(now) != bool(value)):
                setattr(song, attr, value)
                done.append(key)
        if state.get("loopStart") is not None and state.get("loopLength") is not None:
            if abs(float(song.loop_start) - state["loopStart"]) > 1e-9 or abs(float(song.loop_length) - state["loopLength"]) > 1e-9:
                self._set_loop_brace(song, state["loopStart"], state["loopLength"])
                done.append("loop brace")
        put("loop", state.get("loop"), "loop on/off")
        put("start_time", state.get("startTime"), "start marker")
        if state.get("backToArranger") is not None and bool(self._g(song, "back_to_arranger", False)) != state["backToArranger"]:
            song.back_to_arranger = bool(state["backToArranger"]); done.append("Back to Arrangement")
        view = song.view
        want = state.get("selectedTrack")
        if want:
            now = self._g(view, "selected_track")
            if now is None or self._track_id(now) != want:
                for _, _, track, _ in self._all_tracks():
                    if self._track_id(track) == want:
                        view.selected_track = track; done.append("selected track"); break
        if state.get("selectedScene") is not None:
            scenes = list(self._g(song, "scenes", ()) or ())
            i = state["selectedScene"]
            if 0 <= i < len(scenes) and self._g(view, "selected_scene") != scenes[i]:
                view.selected_scene = scenes[i]; done.append("selected scene")
        app_view = self._g(self._app() if self._app else None, "view")
        focus = state.get("focusedView")
        if focus and app_view is not None and self._mix_read(app_view, "focused_document_view", str) != focus and callable(self._g(app_view, "focus_view")):
            app_view.focus_view(focus); done.append("focused view")
        views = state.get("visibleViews") or {}
        visible = self._g(app_view, "is_view_visible")
        if callable(visible):
            for name in ("Browser", "Detail", "Detail/Clip", "Detail/DeviceChain"):
                if name in views and bool(visible(name)) != views[name]:
                    fn = self._g(app_view, "show_view" if views[name] else "hide_view")
                    if callable(fn):
                        fn(name); done.append("view " + name)
        put("current_song_time", state.get("songTime"), "playhead")
        def verify():
            now = self.op_mix_view_state(devices=False)
            keys = ("loop", "loopStart", "loopLength", "songTime", "startTime", "backToArranger", "selectedTrack", "selectedScene", "focusedView", "visibleViews")
            off = [k for k in keys if state.get(k) is not None and now.get(k) != state.get(k)]
            return {"putBack": done, "differ": {k: [state.get(k), now.get(k)] for k in off}}
        return self._mix_defer(3, verify)

    def op_mix_sources(self, track):
        """What makes a track's sound, read-only (mix preflight evidence, 26 Sep 2026): every device on the track and
        inside its rack and drum-pad chains, with its role and class, and for a device that exposes a sample (Simpler)
        the sample's source file, length and rate as Live reports them. Nothing is inferred from names."""
        _, _, found = self._mix_track(track)
        rows = []
        for path, device in self._walk_devices(self._g(found, "devices", ()) or (), "track"):
            role, shown = self._role_and_class(device)
            row = {"id": self._device_id(device), "path": path, "name": self._g(device, "name"), "role": role,
                   "deviceName": shown, "className": self._g(device, "class_name")}
            sample = self._g(device, "sample")
            if sample is not None:
                info = {}
                for key, attr in (("filePath", "file_path"), ("length", "length"), ("sampleRate", "sample_rate")):
                    value = self._g(sample, attr)
                    if isinstance(value, str) or (isinstance(value, (int, float)) and not isinstance(value, bool)):
                        info[key] = value
                row["sample"] = info
            rows.append(row)
            if len(rows) >= 512:
                break
        return {"track": track, "devices": rows, "truncated": len(rows) >= 512}

    def op_mix_resolve(self, owner, param, target):
        """Live's internal value whose own display shows `target` (a number in the display's unit).

        Continuous: bisection on str_for_value; the middle of the run of internal values that all
        display the target is returned, so the written value is centred on what Live shows.
        Quantized: the step whose display number is nearest (reported as snapped).
        Read-only: nothing is written.
        """
        if not isinstance(target, (int, float)) or isinstance(target, bool) or target != target:
            raise self._error("invalid", "Target must be a number in the parameter's display unit.")
        _, p = self._mix_param(owner, param)
        lo, hi = float(self._need(p, "min", "parameter range")), float(self._need(p, "max", "parameter range"))
        show = lambda v: display_number(self._call_label(p, v))
        if self._g(p, "is_quantized", False):
            best = None
            for step in range(int(lo), int(hi) + 1):
                n = show(float(step))
                if n is None or n != n:
                    continue
                d = abs(n - target)
                if best is None or d < best[0]:
                    best = (d, float(step), n)
            if best is None:
                raise self._error("unsupported", "No step of %r shows a number." % self._g(p, "name"))
            return {"value": best[1], "display": self._call_label(p, best[1]), "number": best[2],
                    "snapped": best[0] > 1e-9, "min": lo, "max": hi, "quantized": True}
        a_n, b_n = show(lo), show(hi)
        # A range end may show a word instead of a number (Glue Compressor's Release ends at 'Auto',
        # live 26 Sep 2026): search only the part of the range that shows numbers.
        for end_is_hi in (True, False):
            if (b_n if end_is_hi else a_n) is not None:
                continue
            good, bad = (lo, hi) if end_is_hi else (hi, lo)
            if show(good) is None:
                break
            for _ in range(SEARCH_STEPS):
                m = (good + bad) / 2.0
                if show(m) is None:
                    bad = m
                else:
                    good = m
            if end_is_hi:
                hi, b_n = good, show(good)
            else:
                lo, a_n = good, show(good)
        if a_n is None or b_n is None:
            raise self._error("unsupported", "%r does not display numbers at its range ends." % self._g(p, "name"))
        rising = b_n >= a_n
        if not min(a_n, b_n) <= target <= max(a_n, b_n):
            raise self._error("invalid", "%r is outside %r's range %s..%s (Live's display)." % (
                target, self._g(p, "name"), self._call_label(p, lo), self._call_label(p, hi)))
        # u in [0, 1] runs the way the display rises, so one search covers both directions.
        at = lambda u: lo + u * (hi - lo) if rising else hi - u * (hi - lo)
        def f(u):
            n = show(at(u))
            if n is None:
                raise self._error("unsupported", "%r shows no number at %r." % (self._g(p, "name"), at(u)))
            return n
        def first(strict):
            """Smallest u whose display is >= target (strict: > target)."""
            hit = (lambda n: n > target) if strict else (lambda n: n >= target)
            if hit(f(0.0)):
                return 0.0
            if not hit(f(1.0)):
                return 1.0
            a, b = 0.0, 1.0
            for _ in range(SEARCH_STEPS):
                m = (a + b) / 2.0
                if hit(f(m)):
                    b = m
                else:
                    a = m
            return b
        start = first(False)
        if f(start) == target:
            end = first(True)
            u = (start + end) / 2.0 if end > start else start
            if f(u) != target:
                u = start
        else:
            below = max(0.0, start - 1e-12)
            u = min((start, below), key=lambda x: abs(f(x) - target))
        value = max(min(lo, hi), min(max(lo, hi), at(u)))
        # Some controls report is_quantized False but hold only whole steps (Glue Compressor Ratio,
        # Attack, Release: live 26 Sep 2026, 1.5 was stored as 1.0). When the whole step shows the
        # same display, write the whole step, so the stored value is the one that was asked for.
        whole = float(round(value))
        if whole != value and min(lo, hi) <= whole <= max(lo, hi) and self._call_label(p, whole) == self._call_label(p, value):
            value = whole
        return {"value": value, "display": self._call_label(p, value), "number": show(value),
                "snapped": show(value) != target, "min": lo, "max": hi, "quantized": False}

    # ---- writes ----
    def op_mix_set_mixer(self, track, volume=None, panning=None, expect=None):
        """Set a track's fader and/or pan to exact internal values; verified after settling.

        `expect` = {'volume': v, 'panning': v}: refuse (write nothing) unless the current values match,
        so an undo never overwrites a change made since.
        """
        _, _, found = self._mix_track(track)
        mixer = self._need(found, "mixer_device", "the track mixer")
        plan = []
        for key, value in (("volume", volume), ("panning", panning)):
            if value is None:
                continue
            p = self._need(mixer, key, "the track " + key)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not float(p.min) <= value <= float(p.max):
                raise self._error("invalid", "%s %r is outside %r..%r." % (key, value, p.min, p.max))
            if not self._g(p, "is_enabled", True):
                raise self._error("conflict", "Track %s is disabled in Live." % key)
            if expect and key in expect and abs(float(p.value) - float(expect[key])) > 1e-9:
                raise self._error("conflict", "Track %s is %r, not the expected %r. Nothing was changed." % (
                    key, p.value, expect[key]))
            plan.append((key, p, float(value)))
        if not plan:
            raise self._error("invalid", "Give volume and/or panning.")
        before = {key: float(p.value) for key, p, _ in plan}
        for _, p, value in plan:
            p.value = value

        def verify():
            _, _, again = self._mix_track(track)
            m = again.mixer_device
            out, wrong = {}, []
            for key, _, value in plan:
                state = self._mix_param_state(getattr(m, key))
                out[key] = dict(state, previousValue=before[key], requested=value)
                if state["value"] is None or abs(state["value"] - value) > 1e-7:
                    wrong.append("%s reads %r, not %r" % (key, state["value"], value))
            if wrong:
                raise self._error("unverified", "Mixer write applied but did not confirm: %s. Not retried." % "; ".join(wrong))
            return {"trackId": track, "mixer": out}
        return self._defer(verify)

    def op_mix_restore_parameters(self, device, writes):
        """Write several raw values back to one device, in the order given, each only if it still reads
        `expect`. Used by undo: settings first, then switches, so a band's values are restored while it
        is still enabled. Verified after settling."""
        if not isinstance(writes, list) or not 1 <= len(writes) <= 256:
            raise self._error("invalid", "Give between 1 and 256 writes.")
        _, _, found = self._find_device(device)
        by_id = {self._param_id(p): p for p in self._g(found, "parameters", ()) or ()}
        plan = []
        for w in writes:
            p = by_id.get(w.get("id")) if isinstance(w, dict) else None
            if p is None:
                raise self._error("not_found", "Parameter %r is not on this device." % (w,))
            if self._g(p, "name") != w.get("name"):
                raise self._error("conflict", "Parameter is now named %r, not %r." % (self._g(p, "name"), w.get("name")))
            value = w.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not float(p.min) <= value <= float(p.max):
                raise self._error("invalid", "%r is outside %r's range." % (value, w.get("name")))
            if "expect" in w and w["expect"] is not None and abs(float(p.value) - float(w["expect"])) > 1e-7:
                raise self._error("conflict", "%r is %r, not the expected %r. Nothing was changed." % (
                    w.get("name"), p.value, w["expect"]))
            plan.append((w["id"], w["name"], p, float(value)))
        for _, _, p, value in plan:
            p.value = value

        def verify():
            _, _, again = self._find_device(device)
            now = {self._param_id(p): p for p in self._g(again, "parameters", ()) or ()}
            wrong = [name for ident, name, _, value in plan
                     if ident not in now or abs(float(now[ident].value) - value) > 1e-7]
            if wrong:
                raise self._error("unverified", "Restore applied but %s did not confirm. Not retried." % ", ".join(wrong))
            return {"deviceId": device, "restored": len(plan)}
        return self._defer(verify)

    def op_mix_insert_device(self, track, device_name, name, index=None, off=False):
        """Insert a native Live device on any track (regular, group, return or master), optionally at a
        position and switched off in the same tick so its default settings are never heard."""
        category, _, found = self._mix_track(track)
        if not isinstance(device_name, str) or not device_name or not isinstance(name, str) or not name:
            raise self._error("invalid", "Device and display names are required.")
        existing = len(found.devices)
        if index is not None and (not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= existing):
            raise self._error("invalid", "Insertion index must be a whole number in 0..%d for this track." % existing)
        insert = self._need(found, "insert_device", "native device insertion (Track.insert_device)")
        before = [self._ptr(d) for d in found.devices]
        try:
            if index is None:
                insert(device_name)
            else:
                insert(device_name, index)
        except TypeError:
            for stray in [d for d in found.devices if self._ptr(d) not in before]:
                self._mix_remove(found, stray)
            raise self._error("unsupported", "This Live's insert_device takes no position. Nothing was added.")
        created = [d for d in found.devices if self._ptr(d) not in before]
        if len(created) != 1:
            raise self._error("unverified", "Live did not report exactly one new device after inserting %r; inspect the track." % device_name)
        device = created[0]
        try:
            device.name = name
            if off:
                switch = [p for p in device.parameters if self._g(p, "name") == "Device On"]
                if len(switch) != 1:
                    raise self._error("unsupported", "%r has no single Device On switch." % device_name)
                switch[0].value = float(switch[0].min)
            at = list(found.devices).index(device)
            if index is not None and at != index:
                raise self._error("unverified", "Live placed %r at %d, not %d." % (device_name, at, index))
        except Exception:
            self._mix_remove(found, device)
            raise
        return {"id": self._device_id(device), "deviceIndex": at, "trackId": track, "category": category}

    def _mix_remove(self, track, device):
        try:
            for position, existing in enumerate(track.devices):
                if self._ptr(existing) == self._ptr(device):
                    track.delete_device(position)
                    return True
        except Exception:
            pass
        return False

    def op_mix_set_routing(self, target, kind, type, channel=None, expect_type=None, source=None):
        """Set a routing by the display names Live offers: a device's sidechain input (kind 'sidechain'),
        or a track's 'input' / 'output'. Verified after settling. `expect_type` refuses if the current
        type is not what the caller read (undo safety). `source` (a track id) picks, among choices that share
        the display name `type` (two tracks with one name), the one whose attached track is that track, and
        the result is verified by that identity too."""
        if kind not in ROUTING_KINDS:
            raise self._error("invalid", "Routing kind is sidechain, input or output.")
        if kind == "sidechain":
            _, _, obj = self._find_device(target)
        else:
            _, _, obj = self._mix_track(target)
        prefix = "output" if kind == "output" else "input"
        current = self._mix_routing(obj, kind)
        if current is None:
            raise self._error("unsupported", "This %s exposes no %s routing." % ("device" if kind == "sidechain" else "track", kind))
        if expect_type is not None and current["type"] != expect_type:
            raise self._error("conflict", "Routing is %r, not the expected %r. Nothing was changed." % (current["type"], expect_type))
        def pick(options_attr, wanted, by=None):
            found = [r for r in (self._g(obj, options_attr, ()) or ()) if str(self._g(r, "display_name", "")) == wanted]
            if by is not None:
                found = [r for r in found if self._mix_attached(r) == by]
            if len(found) != 1:
                raise self._error("invalid", "%d routing choices are called %r%s." % (len(found), wanted, "" if by is None else " and carry track " + by))
            return found[0]
        setattr(obj, prefix + "_routing_type", pick("available_%s_routing_types" % prefix, type, source))
        # Channel choices depend on the type; they are read again after the type has settled.
        def set_channel():
            if channel is not None:
                setattr(obj, prefix + "_routing_channel", pick("available_%s_routing_channels" % prefix, channel))
            return self._defer(verify)

        def verify():
            now = self._mix_routing(obj, kind)
            if now["type"] != type or (channel is not None and now["channel"] != channel) or (source is not None and now.get("typeTrack") != source):
                raise self._error("unverified", "Routing reads %r / %r, not %r / %r. Not retried." % (
                    now["type"], now["channel"], type, channel))
            return dict(now, previous={"type": current["type"], "channel": current["channel"]})
        return self._defer(set_channel)

    def op_mix_play(self, start, end):
        """Loop the arrangement over [start, end) and play from `start`, verified by the playhead.

        Live 12.3.5's start_playing begins at the start marker, not at current_song_time (found by the
        automation work, 26 Sep 2026), so this locates first and continues on a later tick. The
        producer's loop is remembered and given back on stop, as op_play_arrangement does.
        """
        song = self._song()
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (start, end)) or not 0 <= start < end:
            raise self._error("invalid", "Play needs 0 <= start < end beats.")
        if self._user_loop is None:
            self._user_loop = self._loop_of(song)
        self._set_loop_brace(song, start, end - start)
        song.loop = True
        # The loop Scrunch asked for, not a read-back: Live may show the new loop only on a later tick, and a stale
        # read here stopped the loop being given back (live, 26 Sep 2026: John's loop-off Set was left looping).
        self._scrunch_loop = ((True, float(start), float(end - start)), False)
        same_tick = self._loop_of(song)          # evidence: what Live reads back on the tick it was set
        if getattr(song, "back_to_arranger", False):
            song.back_to_arranger = False
        if bool(song.is_playing):
            song.stop_playing()
        song.current_song_time = float(start)
        tries = [0, 0]

        def go():
            # Reading current_song_time shows the new position at once, but the engine only gets there a
            # few ticks later: continuing earlier resumes from the old position (live, 26 Sep 2026). Wait,
            # continue, and if the playhead is still elsewhere, locate again while playing.
            if tries[0] < 4:
                tries[0] += 1
                if abs(float(song.current_song_time) - float(start)) > 1e-3:
                    song.current_song_time = float(start)
                return self._mix_defer(1, go)
            self._need(song, "continue_playing", "playing from the playhead (continue_playing)")()
            return self._defer(check)

        def check():
            now = float(song.current_song_time)
            inside = bool(song.is_playing) and float(start) <= now < float(end)
            if not inside and tries[1] < 3 and bool(song.is_playing):
                tries[1] += 1
                song.current_song_time = float(start)
                return self._defer(check)
            if not inside:
                raise self._error("unverified", "Playhead is at %.2f (playing %s), not inside %.2f..%.2f." % (
                    now, bool(song.is_playing), start, end))
            return {"playing": True, "beat": now, "loop": {"start": float(start), "end": float(end)}, "loopReadSameTick": list(same_tick),
                    "tempo": float(song.tempo), "relocated": tries[1]}
        return self._defer(go)

    def op_mix_create_audio_track(self, name, index=None, mute=True):
        song = self._song()
        if not isinstance(name, str) or not name:
            raise self._error("invalid", "Track name is required.")
        count = len(song.tracks)
        at = -1 if index is None else int(index)
        if at != -1 and not 0 <= at <= count:
            raise self._error("invalid", "Track index is outside the Set.")
        self._need(song, "create_audio_track", "audio track creation")(at)
        position = count if at == -1 else at
        track = song.tracks[position]
        track.name = name
        track.mute = bool(mute)
        if self._g(track, "can_be_armed", False):
            track.arm = False
        return {"id": self._track_id(track), "trackIndex": position}

    def op_mix_import_audio(self, track, path, start=0.0):
        """Place an audio file on an audio track's Arrangement at beat `start` (Live 12 Track.create_audio_clip).
        Refuses an occupied position rather than overwriting it."""
        import os
        if not isinstance(path, str) or not os.path.isfile(path):
            raise self._error("invalid", "Audio file not found.")
        _, t = self._find_track(track)
        if not self._g(t, "has_audio_input", False) or self._g(t, "has_midi_input", False):
            raise self._error("invalid", "Not an audio track.")
        start = float(start)
        for c in self._arrangement_clips(t):
            if c.start_time <= start < c.end_time:
                raise self._error("invalid", "That position already holds a clip.")
        clip = self._need(t, "create_audio_clip", "audio clip import")(path, start)
        return {"id": self._clip_id(clip), "start": float(clip.start_time), "end": float(clip.end_time),
                "warping": bool(self._g(clip, "warping", False))}

    def op_mix_add_locators(self, items):
        """Add named Arrangement locators [{beat, name}], verified. Never renames or removes an existing locator:
        a position within a quarter beat of one is reported as alreadyThere. Refuses while playing.

        Measured live 8 Oct 2026 (Live 12.3.5): a locator added while stopped goes on the Arrangement's visible grid
        (8 bars when zoomed out; asked 40 -> got 32), CuePoint.time cannot be set, and set_or_delete_cue on an
        occupied grid line deletes that locator. So the Arrangement is zoomed in (exact to 1/16 beat measured), each
        locator is placed where the playhead reads back, checked against the asked beat, and the zoom and playhead
        are given back afterwards."""
        import Live
        song = self._song()
        if self._g(song, "is_playing", False):
            raise self._error("invalid", "Stop playback before adding locators.")
        todo = [(round(float(it["beat"]) * 16) / 16.0, str(it["name"])) for it in items]
        view = Live.Application.get_application().view
        before = float(song.current_song_time)
        added, kept, state = [], [], {"i": 0, "wait": 0, "zoomed": 0}

        def near(beat, tol):
            return next((c for c in song.cue_points if abs(float(c.time) - beat) < tol), None)

        def restore():
            for _ in range(state["zoomed"]):
                view.zoom_view(LOCATOR_ZOOM_OUT, "Arranger", False)
            state["zoomed"] = 0
            song.current_song_time = before

        def fail(msg):
            restore()
            raise self._error("unverified", msg + " (added so far: %d)" % len(added))

        def finish():
            restore()
            return {"added": added, "alreadyThere": kept,
                    "locators": [{"beat": float(c.time), "name": c.name} for c in song.cue_points]}

        def step():
            if state["i"] >= len(todo):
                return finish()
            beat, _ = todo[state["i"]]
            if near(beat, 0.25) is not None:
                kept.append(beat); state["i"] += 1
                return step()
            song.current_song_time = beat
            state["wait"] = 0
            return self._mix_defer(LOCATE_TICKS, place)

        def place():
            beat, _ = todo[state["i"]]
            state["seen"] = [float(c.time) for c in song.cue_points]
            song.set_or_delete_cue()
            state["wait"] = 0
            return self._mix_defer(1, name)

        def name():
            beat, label = todo[state["i"]]
            now = [float(c.time) for c in song.cue_points]
            if any(t not in now for t in state["seen"]):
                fail("Live removed an existing locator while adding one at beat %s" % beat)
            new = [c for c in song.cue_points if float(c.time) not in state["seen"]]
            if not new:
                if state["wait"] < 6:
                    state["wait"] += 1
                    return self._mix_defer(1, name)
                fail("Live did not show a new locator near beat %s" % beat)
            cue = new[0]
            cue.name = label
            if abs(float(cue.time) - beat) > 0.07:
                fail("Live put the locator for beat %s at beat %s" % (beat, float(cue.time)))
            added.append({"beat": float(cue.time), "name": label})
            state["i"] += 1
            return step()

        for _ in range(LOCATOR_ZOOM_STEPS):
            view.zoom_view(LOCATOR_ZOOM_IN, "Arranger", False)
        state["zoomed"] = LOCATOR_ZOOM_STEPS
        return self._mix_defer(1, step)

def install(cls):
    """Copy the mix ops onto the bridge core class (also what op_reload rebinds)."""
    for name, value in list(vars(MixOps).items()):
        if name.startswith("op_mix_") or name.startswith("_mix_"):
            setattr(cls, name, value)
    return cls
