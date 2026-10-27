"""Output device and buffer size selection, and detecting a lost device."""
import sys, os, ctypes, numpy as np, tempfile
sys.path.insert(0, os.path.dirname(__file__)); from hj import *
lib = sys.argv[1]; SR = 48000; FR = 480
hj = HJ(lib); L = hj.l
L.t_set_device.argtypes = [ctypes.c_char_p]; L.t_init2.argtypes = [ctypes.c_int, ctypes.c_int]
L.t_list_devices.argtypes = [ctypes.c_char_p, ctypes.c_int]; L.t_device_name.argtypes = [ctypes.c_char_p, ctypes.c_int]
L.t_open_latency.restype = ctypes.c_double; L.t_comp.argtypes = [ctypes.c_double]
FAILS = []
def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)
def dev_name():
    b = ctypes.create_string_buffer(256); L.t_device_name(b, 256); return b.value.decode()

# 1. device list: stereo output devices only (the fake microphone must not appear), default flagged
b = ctypes.create_string_buffer(2048); n = L.t_list_devices(b, 2048)
rows = [r.split("|") for r in b.value.decode().strip().split("\n")]
names = [r[0] for r in rows]; defaults = [r[0] for r in rows if r[1] == "1"]
check(f"lists the 3 output devices ({names})", n == 3 and "Fake Microphone" not in names)
check("marks the system default", defaults == ["FakeDevice"])

# 2. default device, driver-chosen buffer
check("opens the default device", L.t_init2(SR, 0) == 1 and L.t_open_dev() == 0 and dev_name() == "FakeDevice")
check("driver-chosen buffer (0)", L.t_open_fpb() == 0 and L.t_buffer_frames() == 0)
L.t_shutdown()

# 3. a chosen device and a requested buffer size
L.t_set_device(b"Fake Speakers")
check("opens the chosen device", L.t_init2(SR, 256) == 1 and L.t_open_dev() == 2 and dev_name() == "Fake Speakers")
check("requests the chosen buffer size", L.t_open_fpb() == 256 and L.t_buffer_frames() == 256)
check("asks the driver for at least that latency", L.t_open_latency() >= 256 / SR - 1e-9)
L.t_shutdown()

# 4. an unknown device name falls back to the default instead of failing
L.t_set_device(b"Bluetooth Thing That Was Unplugged")
check("unknown device falls back to default", L.t_init2(SR, 0) == 1 and L.t_open_dev() == 0)
L.t_shutdown()

# 5. the engine notices a lost device, and can be restarted on another one and still plays
L.t_set_device(b"Fake Headphones")
assert L.t_init2(SR, 128) == 1
check("running after start", L.t_is_running() == 1)
L.t_kill_stream()
check("a lost device is detected (is_running is False)", L.t_is_running() == 0)
L.t_shutdown()
tmp = tempfile.mkdtemp(); t = np.arange(SR * 2) / SR
p = os.path.join(tmp, "a.wav"); write_wav(p, np.stack([0.3 * np.sin(2 * np.pi * 440 * t)] * 2, 1), SR, "pcm16")
L.t_set_device(b"Fake Speakers"); assert L.t_init2(SR, 0) == 1
hj.playlist(0, [dict(path=p, off=0, dur=1.5, pos=0.0)]); L.t_comp(0.0)
L.t_play(0.0); out, _ = hj.run(40, FR, sleep_us=400)
check("plays after being restarted on another device", np.abs(out).max() > 0.2 and dev_name() == "Fake Speakers")
L.t_shutdown()
print("RESULT", "PASS" if not FAILS else "FAIL " + str(FAILS)); sys.exit(0 if not FAILS else 1)
