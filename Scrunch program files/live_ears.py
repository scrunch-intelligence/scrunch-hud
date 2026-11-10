"""Local capture adapter only. Explicit opt-in; no UI, DSP, files of PCM or cloud calls.

LIVE EARS and DEEP ANALYSIS retain separate acquisition lifecycles. The float32
frame-by-channel PCM returned here is ready for reference_dsp outside Live.
Do not silently treat missing blocks or a removed producer as measured silence.
"""
import ctypes as C
from dataclasses import dataclass
from pathlib import Path
import os, json, stat, errno

class _Meta(C.Structure):
    _fields_ = [(n, C.c_uint64) for n in ('sequence', 'host_time', 'receiver_clock', 'dropped')] + [
        ('sample_time', C.c_double), ('sample_rate', C.c_double)] + [
        (n, C.c_uint32) for n in ('frames', 'channels', 'flags', 'closed')]

@dataclass(frozen=True)
class AudioBlock:
    pcm_float32_interleaved: bytes
    frames: int
    channels: int
    sample_rate: float
    sequence: int
    sample_time: float
    dropped: int
    flags: int
    host_time: int
    receiver_clock: int
    timestamp_flags: int = 0

class LiveEarsSource:
    mode = 'LIVE_EARS'
    transport = 'au-posix-shm-v2'

    @staticmethod
    def discover():
        # Discovery is a list of candidates, not a claim that the producer is alive.
        return ['/' + p.name for p in Path(f'/tmp/scrunch-ears-{os.getuid()}').glob('se_*')]

    @staticmethod
    def cleanup_stale():
        lib=C.CDLL(str(Path(__file__).parent / 'native/live_ears/build/libScrunchReceiver.dylib'))
        lib.ears_cleanup.argtypes=[C.c_char_p];lib.ears_cleanup.restype=C.c_int
        return [{'streamId':name,'status':{1:'removed-dead-producer',0:'retained-active-or-leased',-1:'unverified-retained'}[lib.ears_cleanup(name.encode())]} for name in LiveEarsSource.discover()]

    def __init__(self, stream_id):
        self._handle = None
        for fault in capture_faults():
            if fault['streamId']==stream_id:raise RuntimeError(fault['label'])
        self._lib = C.CDLL(str(Path(__file__).parent / 'native/live_ears/build/libScrunchReceiver.dylib'))
        self._lib.ears_open.argtypes = [C.c_char_p, C.c_char_p, C.c_uint32]
        self._lib.ears_open.restype = C.c_void_p
        self._lib.ears_next.argtypes = [C.c_void_p, C.POINTER(C.c_float), C.c_uint32, C.POINTER(_Meta)]
        self._lib.ears_next.restype = C.c_int
        self._lib.ears_close.argtypes = [C.c_void_p]
        self._lib.ears_close.restype = None
        self._lib.ears_timestamp_flags.argtypes=[C.c_void_p]
        self._lib.ears_timestamp_flags.restype=C.c_uint32
        self._pcm = (C.c_float * 8192)()
        self._meta = _Meta()
        error = C.create_string_buffer(256)
        self._handle = self._lib.ears_open(stream_id.encode(), error, len(error))
        if not self._handle:
            raise RuntimeError(error.value.decode())

    def read(self):
        if not self._handle:
            raise RuntimeError('Capture source is closed')
        status = self._lib.ears_next(self._handle, self._pcm, 8192, C.byref(self._meta))
        if status == 0:
            return None
        if status < 0:
            raise RuntimeError('Live Ears producer disconnected' if status == -1 else 'Invalid capture buffer')
        m = self._meta
        return AudioBlock(C.string_at(self._pcm, m.frames*m.channels*4), m.frames, m.channels,
                          m.sample_rate, m.sequence, m.sample_time, m.dropped, m.flags,
                          m.host_time, m.receiver_clock, self._lib.ears_timestamp_flags(self._handle))

    def close(self):
        if self._handle:
            self._lib.ears_close(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

HOUSEKEEPING={'lastChecked':None,'error':None,'objects':[]}

def start_housekeeping():
    """Best-effort conservative cleanup; never runs on a DAW callback."""
    import threading,atexit,time
    stop=threading.Event();atexit.register(stop.set)
    def work():
        while not stop.is_set():
            try:
                objects=LiveEarsSource.cleanup_stale();HOUSEKEEPING.update(lastChecked=time.time(),error=None,objects=objects)
                removed=[r for r in objects if r['status']=='removed-dead-producer']
                if removed:
                    import checkpoints
                    checkpoints.event('live-ears-stale-cleanup',objects=removed)
            except Exception as exc:
                HOUSEKEEPING.update(lastChecked=time.time(),error=str(exc))
            stop.wait(60)
    thread=threading.Thread(target=work,name='live-ears-cleanup',daemon=True);thread.start();return stop


def capture_faults(directory=None):
    """Read initialization diagnostics off the audio thread; dead producers ignored."""
    faults=[]
    for p in Path(directory or f'/tmp/scrunch-ears-{os.getuid()}').glob('se_*'):
        try:
            st=p.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_size>4096:continue
            d=json.loads(p.read_text())
            if d.get('captureUnavailable')!='memory-pin-failed':continue
            pid=d.get('pid')
            if not isinstance(pid,int) or pid<2:continue
            os.kill(pid,0)
            faults.append({'streamId':'/'+p.name,'reason':'memory-pin-failed','errno':d.get('errno'),
                'label':'Live Ears unavailable — memory could not be pinned'})
        except (OSError,ValueError,TypeError):continue
    return faults
