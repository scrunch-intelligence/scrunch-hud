"""The saved .als, only for what genuinely lives there, never as the list of tracks.

Tracks, devices and routing come from the open Set in Live (the bridge). A few jobs need the
plug-in's saved state or parameter page, which only the file holds. Offline migration/analysis
only: nothing here saves the Set, and the live engineer path never imports this module.
"""
import gzip
import xml.etree.ElementTree as ET
from pathlib import Path

NOTES = []   # plain sentences for the reply ("Saved the Set first ...")


def _read(path):
    return ET.fromstring(gzip.decompress(Path(path).read_bytes()))


def _find(doc, track, plugin, track_index):
    import wrapper_migrate
    if plugin is None:
        import plugin_knobs
        hits = [t for t in doc.iter() if t.tag in ('MidiTrack', 'AudioTrack') and plugin_knobs.track_name(t) == track]
        if len(hits) != 1:
            raise LookupError(track)
        return hits[0]
    try:
        return wrapper_migrate.track_device(doc, track, plugin, track_index)
    except ValueError as exc:
        if track_index is None or ' is not ' not in str(exc):
            raise
        return wrapper_migrate.track_device(doc, track, plugin)   # numbering differs: the name decides


def _missing(exc):
    """A miss the open Set contradicts (stale file), as opposed to a real ambiguity like two same-named tracks."""
    s = str(exc)
    return (isinstance(exc, LookupError) or 'no track called' in s or 'holds 0 copies' in s
            or (s.startswith('Track ') and ' is not ' in s))


def locate(path, track, plugin=None, track_index=None, save=None, wait=None, poll=None):
    """(doc, device-or-track element, False) for `track` in the saved Set at `path`. Read-only.

    Offline tools only. Scrunch never saves the Set to make the file current: if the file predates the
    track, the caller is told to save in Live themselves.
    """
    doc = _read(path)
    try:
        return doc, _find(doc, track, plugin, track_index), False
    except (ValueError, LookupError) as exc:
        if not _missing(exc):
            raise
    raise ValueError('The saved Set file does not have %r%s yet (it was saved before that). Scrunch does not save '
                     'your Set; save it in Live yourself if you want this offline tool to see it.'
                     % (track, ' with %s' % plugin if plugin else ''))


def drain():
    out = NOTES[:]
    del NOTES[:]
    return out
