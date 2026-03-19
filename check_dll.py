# Запусти отдельно: python -c "..."
import ctypes, os, time

dll = ctypes.CDLL(r"E:\Mychat\VoiceChat\dlls\InPulseAudioExclusion.dll")
CB = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_float), ctypes.c_int, ctypes.c_int, ctypes.c_int)

def cb(ptr, frames, ch, sr):
    print(f"  PCM получен: frames={frames} ch={ch} sr={sr}")

dll.StartCapture.restype  = ctypes.c_bool
dll.StartCapture.argtypes = [ctypes.c_ulong, CB]
dll.StopCapture.restype   = None

c_cb = CB(cb)
ok = dll.StartCapture(ctypes.c_ulong(os.getpid()), c_cb)
print("StartCapture →", ok)
time.sleep(3)
dll.StopCapture()