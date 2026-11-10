"""Bounded Studio rendering; comparison buffers and transcripts stay on the laptop.

Connection failures are their own error type (StudioUnavailable) so the app can say plainly that the Mac Studio
could not be reached, keep any engineer reply it already has, and offer to finish with that same reply. ssh exits
255 for its own connection failures; a render that runs and fails returns a JSON error instead."""
import base64
import json
import subprocess
import shlex
import numpy as np
from preset_variations import VariationError

HOST = 'scrunch-studio'
SSH = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']


class StudioUnavailable(VariationError):
    """The Mac Studio could not be reached or did not finish; nothing about the sound was decided."""


def _ssh(command, *, data=None, timeout, doing):
    try:
        done = subprocess.run(SSH + [HOST, command], input=data, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise StudioUnavailable('The Mac Studio did not finish %s within %d seconds. No automatic retry.' % (doing, timeout)) from None
    except OSError as error:
        raise StudioUnavailable('Could not start the connection to the Mac Studio: %s.' % error) from None
    if done.returncode == 255:
        detail = done.stderr.decode(errors='replace').strip().splitlines()
        raise StudioUnavailable('The Mac Studio could not be reached while %s (%s). Check it is switched on and on this network.'
                                % (doing, detail[-1] if detail else 'no detail from ssh'))
    return done


def check(timeout=12):
    """A cheap reachability check, used before spending an engineer call."""
    _ssh('true', timeout=timeout, doing='checking the connection')


def render(state,phrase,changes):
    payload={'state':base64.b64encode(state).decode(),'phrase':phrase,'changes':changes}
    command='cd $HOME/Documents/Scrunch-Mixing && exec nice -n 10 env OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 .venv/bin/python3 -B preset_variation_studio_render.py'
    done=_ssh(command,data=json.dumps(payload).encode(),timeout=120,doing='rendering')
    if done.returncode:raise VariationError('Studio render failed: '+done.stderr.decode(errors='replace')[-1000:])
    try:value=json.loads(done.stdout)
    except ValueError:raise StudioUnavailable('The Mac Studio connection ended before the render result arrived. No automatic retry.') from None
    if 'error' in value:raise VariationError(value['error'])
    audio=np.frombuffer(base64.b64decode(value['audio']),dtype='<f4').reshape(value['shape']).copy()
    return {'audio':audio,'end_state':base64.b64decode(value['end_state']),'result':value['result']}


def selected(path,phrase,*,locked_controls=()):
    from pathlib import PurePosixPath
    p=PurePosixPath(path)
    if p.suffix!='.SerumPreset' or not str(p).startswith('/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Presets/'):
        raise VariationError('Choose an installed Studio Serum preset.')
    done=_ssh('cat '+shlex.quote(str(p)),timeout=30,doing='reading the preset')
    if done.returncode:raise VariationError('Studio preset unavailable.')
    return uploaded(done.stdout,p.name,phrase,locked_controls=locked_controls)


def uploaded(raw,name,phrase,*,locked_controls=()):
    from preset_variation_serum import selected_bytes
    import serum_design
    original=serum_design.render
    try:
        serum_design.render=render
        return selected_bytes(raw,name,phrase,locked_controls=locked_controls)
    finally:serum_design.render=original


def main():
    import sys
    import serum_design
    try:
        data=json.load(sys.stdin)
        out=serum_design.render(base64.b64decode(data['state']),data['phrase'],tuple(tuple(x) for x in data['changes']))
        audio=np.asarray(out['audio'],dtype='<f4')
        result={'audio':base64.b64encode(audio.tobytes()).decode(),'shape':list(audio.shape),
                'end_state':base64.b64encode(out['end_state']).decode(),'result':out['result']}
    except Exception as error:result={'error':type(error).__name__+': '+str(error)}
    sys.stdout.write(json.dumps(result,allow_nan=False))


if __name__=='__main__':main()
