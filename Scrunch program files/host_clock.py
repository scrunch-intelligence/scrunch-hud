"""Host timestamp diagnostics on the local analysis thread, never the AU callback.

AudioUnit host ticks share mach_absolute_time's clock on this Mac. This measures
age from the host-provided buffer-start timestamp, not hardware/audio latency.
Missing, future or implausibly old timestamps remain explicitly unavailable.
"""
import ctypes,sys

def tick_age_ms(start,flags,now,numer,denom):
    if not flags & 2 or not start or not denom:return None
    delta=(now-start)*numer/denom/1e6
    return delta if 0<=delta<=10000 else None

_system=None
if sys.platform=='darwin':
    class Timebase(ctypes.Structure):
        _fields_=[('numer',ctypes.c_uint32),('denom',ctypes.c_uint32)]
    try:
        _system=ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        _system.mach_absolute_time.restype=ctypes.c_uint64
        _system.mach_absolute_time.argtypes=[]
        _system.mach_timebase_info.argtypes=[ctypes.POINTER(Timebase)]
        _base=Timebase()
        if _system.mach_timebase_info(ctypes.byref(_base))!=0:_system=None
    except (OSError,AttributeError):_system=None

def age_ms(start,flags):
    if _system is None:return None
    return tick_age_ms(start,flags,_system.mach_absolute_time(),_base.numer,_base.denom)
