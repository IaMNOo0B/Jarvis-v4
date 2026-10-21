"""Voice I/O — EchoGuard (Goertzel bands), device-by-name audio,
resample/chunk helpers, and the wake-word gate. Synthesised tones,
fake sounddevice: no hardware involved."""
import math
import struct

from jarvis.audio_io import (SoundDeviceIO, chunk_pcm, resolve_device,
                             resample_pcm)
from jarvis.echo import EchoGuard, band_energies, pcm_level
from jarvis.wake import WakeGate


def tone(freq, seconds=0.064, rate=16000, amp=0.5):
    n = int(rate * seconds)
    return struct.pack(
        f"<{n}h",
        *[int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))
          for i in range(n)])


# -- echo ----------------------------------------------------------------------

def test_band_energies_peak_in_the_right_band():
    bands = band_energies(tone(440), 16000)
    assert bands.index(max(bands)) == 1        # 400–700 Hz band
    bands = band_energies(tone(2000), 16000)
    assert bands.index(max(bands)) == 4        # 1700–2600 Hz band


def test_echo_is_dropped_and_a_new_voice_passes():
    guard = EchoGuard()
    out = tone(440, rate=24000)
    for _ in range(3):
        guard.note_output(out, 24000)
    assert guard.is_user_speech(tone(440), 16000) is False
    mixed = tone(2000, amp=0.9)
    assert guard.is_user_speech(mixed, 16000) is True


def test_pcm_level_bounds():
    assert pcm_level(b"\x00" * 2048) == 0.0
    assert pcm_level(tone(440, amp=0.9)) > 0.3


# -- audio helpers ---------------------------------------------------------------

def test_resample_and_chunk():
    pcm = tone(440, seconds=1.0, rate=16000)
    up = resample_pcm(pcm, 16000, 24000)
    assert abs(len(up) - len(pcm) * 1.5) <= 4
    assert resample_pcm(pcm, 16000, 16000) == pcm
    chunks = chunk_pcm(b"a" * 4801, ms=50, rate=24000)
    assert [len(c) for c in chunks] == [2400, 2400, 1]


class FakeSD:
    def __init__(self):
        self.opened = []

    def query_devices(self):
        return [
            {"name": "USB Headset", "max_input_channels": 1,
             "max_output_channels": 2},
            {"name": "Built-in Mic", "max_input_channels": 1,
             "max_output_channels": 0},
        ]

    def InputStream(self, **kw):
        self.opened.append(("in", kw.get("device")))
        if kw.get("device") == 0:
            raise RuntimeError("device busy")

        class S:
            def start(self):
                pass
        return S()

    def RawOutputStream(self, **kw):
        self.opened.append(("out", kw.get("device")))

        class S:
            latency = 0.12

            def start(self):
                pass
        return S()


def test_devices_resolve_by_name_and_fall_back():
    sd = FakeSD()
    assert resolve_device(sd, "USB Headset", "input") == 0
    assert resolve_device(sd, "Built-in Mic", "output") is None
    assert resolve_device(sd, "Gone Headset", "input") is None
    assert resolve_device(sd, None, "input") is None
    io = SoundDeviceIO(sd=sd, input_name="USB Headset")
    io.start_input(lambda b: None)
    # device 0 raised -> retried with the system default (None)
    assert sd.opened == [("in", 0), ("in", None)]


# -- wake gate ---------------------------------------------------------------------

class FakeDetector:
    def __init__(self):
        self.fed = []

    def feed(self, pcm):
        self.fed.append(pcm)


def test_wake_gate_semantics():
    now = [1000.0]
    det = FakeDetector()
    gate = WakeGate(enabled=True, detector=det,
                    clock=lambda: now[0])
    assert gate.feed(b"x" * 2048) is False   # asleep: stays local
    assert len(det.fed) == 1                 # detector got the block
    gate.wake()
    assert gate.feed(b"x" * 2048) is True    # awake: streams
    gate.note_user_speech()
    now[0] += 121.0
    assert gate.check_sleep() is True        # 120 s of quiet -> sleep
    assert gate.feed(b"x" * 2048) is False

    off = WakeGate(enabled=False)
    assert off.feed(b"anything") is True     # gate off = always open
