"""Putting a device where the tap can hear it.

A tap only hears what is in front of it. The Scope plug-in measures at its own device position, so
a device appended to the end of the chain sits *behind* the tap and is inaudible to measurement: on
20 September a -15 dB cut at 400 Hz measured as **0.00 dB** of change, four passes, because the EQ
had been appended after Scope. Live's `insert_device` appends, so position has to be asked for.

This is shared by anything that has to be measured after it acts - the EQ moves today, the measured
ducker's Compressor next.
"""
LISTENING=('Scope','Live Ears Bound','Live Ears Proof')       # devices that tap the signal at their own position

def _kind(device):
    return str(device.get('type') or '').split(':')[-1].strip() or str(device.get('deviceName') or '')

def tap_index(devices):
    """Where the first listening device sits, or None if nothing is listening on this track.

    The *first* one, so every tap on the track hears the new device rather than only the last.
    """
    for position,device in enumerate(devices or ()):
        if _kind(device) in LISTENING:return position
    return None

def insert_at(devices):
    """The position a device must be inserted at to be in front of every tap.

    `None` means append: nothing on this track is listening, so position carries no meaning and the
    caller should not demand one from a Live that may not support it.
    """
    return tap_index(devices)

def describe(devices,at):
    """Why this position, in one line, for the journal and for John."""
    if at is None:return 'appended: nothing on this track taps the signal'
    names=[_kind(d) for d in devices]
    return 'inserted at %d, in front of %s so the tap hears it'%(at,names[at])
