"""Humming / single-voice melody -> MIDI. Pitch: CREPE tiny since 4 Oct (see track_crepe), pYIN fallback. Originally pYIN (librosa 0.x, Mauch & Dixon 2014). No model download,
no training: pYIN is a probabilistic pitch tracker built into librosa, the standard choice for one voice.

Tool check before writing this (3 Oct 2026): Basic Pitch (Spotify) handles voice but is not installed and
needs a download; CREPE/torchcrepe likewise; MuScriptor (installed) is trained on instruments, and its piano
success says nothing about humming. pYIN is already on this Mac, so it is the simplest viable choice.

Request: {"audio": path, "startSeconds": float, "endSeconds": float}.
Reply (runner_io protocol): MIDI of exactly the notes found (`midi_base64`, timebase 'seconds') and
meta.parts[0].notes in seconds from the span start, plus the frame pitch track for evidence.

Tuned 3 Oct on 20 real HumTrans hums, checked on 20 held-out hums (scrunch-sound/evidence/humtrans-eval): note F1
with one time offset + one transposition 0.41 -> 0.43 by setting the shortest note to a 1/16 at the Set tempo (0.12 s at
126 bpm), and 0.43 -> 0.45 by rounding pitches relative to the hummer's own tuning (median cents offset, reported as
hummerTuningCents; per-note cents are relative to it). A fixed 0.2 s scored 0.47 but would delete 16th notes in a 126 bpm riff; DIP 0.6 gained only 0.005 and split
held vibrato notes, so DIP stays 0.45.

Note finding, fixed and reported, nothing invented: a note is a run of voiced frames; it is split where the
rounded, 5-frame-median pitch changes for at least MIN_NOTE seconds, or where loudness dips below DIP x the phrase median (a re-sung note;
vibrato does not split notes, which onset detectors did on the first fixture).
Notes shorter than MIN_NOTE are dropped and counted. Pitch = rounded median of the note's frames; the
median's distance from the semitone (cents) is reported so out-of-tune notes are visible.
"""
import io as bytes_io, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner_io as io

SR, HOP = 22050, 256
MIN_NOTE = 0.12   # default; audio_to_midi passes one 1/16 at the Set tempo as minNoteSeconds
DIP = 0.45   # a same-pitch re-articulation: loudness falls below 45% of the phrase's median
PERIODICITY, GATE = 0.5, 0.2   # CREPE: voiced = periodicity > 0.5 and frame loudness > 0.2 x the take's median


def track_crepe(y):
    """CREPE tiny (torchcrepe, weights ship in the pip package, 2 MB) with Viterbi smoothing. Measured 4 Oct on the
    40 real HumTrans hums: aligned note F1 0.477 vs pYIN 0.442; vs the corrected onsets of shubham-gupta-30 (17 hums)
    0.523 vs 0.434. CREPE full scored 0.482/0.524 but takes 6x longer (8 s per 15 s of audio vs 1.3 s)."""
    import types, numpy as np, librosa, torch
    sys.modules.setdefault('torchaudio', types.ModuleType('torchaudio'))   # torchcrepe imports it only to load files
    import torchcrepe
    y16 = librosa.resample(y, orig_sr=SR, target_sr=16000)
    f0, per = torchcrepe.predict(torch.tensor(y16)[None], 16000, int(round(HOP / SR * 16000)), 65.4, 1046.5, 'tiny',
                                 return_periodicity=True, batch_size=512, device='cpu', decoder=torchcrepe.decode.viterbi)
    return f0[0].numpy(), per[0].numpy()


def notes_from(f0, voiced, rms, hop_s):
    import numpy as np
    midi = np.where(voiced, 69 + 12 * np.log2(np.maximum(np.nan_to_num(f0, nan=1.0), 1e-6) / 440.0), np.nan)
    smooth = midi.copy()
    for i in range(len(midi)):
        w = midi[max(0, i - 2):i + 3]
        w = w[~np.isnan(w)]
        smooth[i] = np.median(w) if voiced[i] and len(w) else np.nan
    min_frames = max(1, int(round(MIN_NOTE / hop_s)))
    out, dropped, i, n = [], 0, 0, len(midi)
    while i < n:
        if np.isnan(smooth[i]):
            i += 1; continue
        j = i
        while j < n and not np.isnan(smooth[j]):
            j += 1
        # split the voiced run [i, j) at stable semitone changes and onsets
        level = float(np.median(rms[i:j])) or 1e-9
        dip = {m: rms[m] < DIP * level and rms[m] <= rms[max(i, m - 1)] and rms[m] <= rms[min(j - 1, m + 1)] for m in range(i, j)}
        cuts, cur, k = [i], int(round(smooth[i])), i + 1
        while k < j:
            r = int(round(smooth[k]))
            if r != cur and all(int(round(smooth[m])) == r for m in range(k, min(j, k + min_frames))) and k + min_frames <= j:
                cuts.append(k); cur = r
            elif dip[k] and k - cuts[-1] >= min_frames and j - k >= min_frames:
                cuts.append(k)
            k += 1
        cuts.append(j)
        for a, b in zip(cuts, cuts[1:]):
            if b - a < min_frames:
                dropped += 1; continue
            med = float(np.median(midi[a:b][~np.isnan(midi[a:b])]))
            p = int(round(med))
            loud = float(np.mean(rms[a:b]))
            out.append({'p': p, 's': round(a * hop_s, 4), 'e': round(b * hop_s, 4), 'cents': round((med - p) * 100, 1), 'loud': loud})
        i = j
    if out:
        top = max(x['loud'] for x in out) or 1.0
        for x in out:
            x['v'] = int(max(40, min(110, 40 + 70 * (x.pop('loud') / top))))
    return out, dropped


def to_midi(notes):
    import mido
    f = mido.MidiFile(ticks_per_beat=480)
    tr = mido.MidiTrack(); f.tracks.append(tr)
    tr.append(mido.MetaMessage('set_tempo', tempo=500000))  # 120 bpm: 960 ticks per second
    ev = sorted([(n['s'], 1, n) for n in notes] + [(n['e'], 0, n) for n in notes], key=lambda x: (x[0], x[1]))
    last = 0
    for t, on, n in ev:
        tick = int(round(t * 960))
        tr.append(mido.Message('note_on' if on else 'note_off', note=n['p'], velocity=n['v'] if on else 0, time=tick - last))
        last = tick
    b = bytes_io.BytesIO(); f.save(file=b)
    return b.getvalue()


def main():
    req = io.request(); t = io.Timer()
    global MIN_NOTE
    MIN_NOTE = float(req.get('minNoteSeconds') or MIN_NOTE)
    import librosa, numpy as np, soundfile
    info = soundfile.info(req['audio'])
    start = max(0.0, float(req.get('startSeconds') or 0.0))
    end = min(info.duration, float(req.get('endSeconds') or info.duration))
    if not end > start:
        raise ValueError('The audio span is empty (%.3f-%.3f s of a %.3f s file).' % (start, end, info.duration))
    y, _ = librosa.load(req['audio'], sr=SR, mono=True, offset=start, duration=end - start)
    t.mark('loadSeconds')
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    try:
        f0, per = track_crepe(y)
        n = min(len(f0), len(rms)); f0, per, rms = f0[:n], per[:n], rms[:n]
        voiced = (per > PERIODICITY) & (rms > GATE * float(np.median(rms[rms > 0]) if np.any(rms > 0) else 0))
        f0 = np.where(voiced, f0, np.nan); tracker = 'CREPE tiny (torchcrepe)'
    except ImportError:   # torchcrepe missing: the earlier pYIN path, named in the reply
        f0, voiced, _ = librosa.pyin(y, fmin=librosa.note_to_hz('C2'), fmax=librosa.note_to_hz('C6'), sr=SR, hop_length=HOP)
        rms = rms[:len(f0)]; tracker = 'pYIN (librosa %s)' % librosa.__version__
    # The hummer's own tuning: the median distance of voiced frames from the nearest semitone (A=440 grid). Notes are
    # rounded relative to it so a singer who sits 40 cents flat is not split between two semitones. Reported, not hidden.
    midi = 69 + 12 * np.log2(np.where(voiced, np.nan_to_num(f0, nan=1.0), np.nan) / 440.0)
    tuning = float(np.nanmedian(((midi + 0.5) % 1) - 0.5)) if np.any(voiced) else 0.0
    notes, dropped = notes_from(f0 * 2 ** (-tuning / 12), voiced, rms, HOP / SR)
    t.mark('transcribeSeconds')
    io.emit(to_midi(notes), 'seconds', {
        'model': tracker + ' humming transcriber', 'device': 'cpu',
        'spanSeconds': [start, end], 'shortNotesDropped': dropped,
        'voicedFraction': round(float(np.mean(voiced)), 3), 'hummerTuningCents': round(tuning * 100, 1),
        'parts': [{'name': 'Voice', 'program': 0, 'drum': False, 'notes': notes}], **t.marks})


if __name__ == '__main__':
    main()
