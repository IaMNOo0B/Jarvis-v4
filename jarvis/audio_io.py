"""Audio devices — Mark-LVI's rules, v4's stdlib-first shape.

- Devices are stored by NAME, never by sounddevice index: indices
  shift every time a USB device is plugged in. A saved name that
  has vanished resolves to the system default instead of raising
  (an unplugged headset must not kill the voice loop).
- The speaker stream opens ONCE and stays open, playing silence
  between turns — an always-open stream is their gap solution;
  stopping/restarting per turn is what makes replies stutter.
- Mic: 16 kHz int16 mono, 1024-frame blocks. Speaker: 24 kHz int16
  mono — the exact rates the Live API speaks.
- sounddevice is an install-time dependency (setup.bat); when it
  is absent this module says so plainly instead of faking audio.
"""

from __future__ import annotations

import struct
from typing import Callable, Optional

MIC_RATE = 16000
SPEAKER_RATE = 24000
CHUNK = 1024


class AudioUnavailable(Exception):
    pass


def _load_sd():
    try:
        import sounddevice as sd
        return sd
    except Exception as e:
        raise AudioUnavailable(
            "sounddevice is not installed — setup.bat installs it "
            f"with the voice pack ({e})") from e


def resolve_device(sd, name: Optional[str], kind: str):
    """Saved device NAME -> current index, or None (system default)
    for no name / a name that no longer exists."""
    if not name:
        return None
    try:
        devices = sd.query_devices()
    except Exception:
        return None
    key = "max_input_channels" if kind == "input" \
        else "max_output_channels"
    for idx, dev in enumerate(devices):
        try:
            if dev.get("name") == name and dev.get(key, 0) > 0:
                return idx
        except Exception:
            continue
    return None


def resample_pcm(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """Linear-interpolation resample of int16 mono PCM (the repo's
    providers/audio.py approach): a rate mismatch must present as a
    correct conversion, never as a chipmunk voice."""
    if from_rate == to_rate or not pcm:
        return pcm
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[:n * 2])
    out_n = int(n * to_rate / from_rate)
    out = []
    for i in range(out_n):
        pos = i * from_rate / to_rate
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        out.append(int(samples[lo] * (1 - frac) + samples[hi] * frac))
    return struct.pack(f"<{len(out)}h", *out)


def chunk_pcm(pcm: bytes, ms: int = 50, rate: int = SPEAKER_RATE):
    """Slice PCM into ~ms pieces (interrupt granularity)."""
    size = int(rate * 2 * ms / 1000)
    size -= size % 2
    return [pcm[i:i + size] for i in range(0, len(pcm), size)]


class SoundDeviceIO:
    """Mic in + speaker out over sounddevice. The mic callback is
    the caller's: it receives raw PCM blocks and must be fast (the
    repo's gates all live there for the same reason)."""

    def __init__(self, sd=None, input_name: Optional[str] = None,
                 output_name: Optional[str] = None):
        self._sd = sd if sd is not None else _load_sd()
        self._input_name = input_name
        self._output_name = output_name
        self._in_stream = None
        self._out_stream = None

    def start_input(self, on_block: Callable[[bytes], None]):
        sd = self._sd
        device = resolve_device(sd, self._input_name, "input")

        def callback(indata, _frames, _time, _status):
            on_block(bytes(indata))

        def open_stream(dev):
            stream = sd.InputStream(
                samplerate=MIC_RATE, channels=1, dtype="int16",
                blocksize=CHUNK, device=dev, callback=callback)
            stream.start()
            return stream

        try:
            self._in_stream = open_stream(device)
        except Exception:
            if device is None:
                raise
            # The repo's rule: a chosen device that fails to open
            # falls back to the system default, never to silence.
            self._in_stream = open_stream(None)
        return self._in_stream

    def start_output(self):
        sd = self._sd
        device = resolve_device(sd, self._output_name, "output")

        def open_stream(dev):
            stream = sd.RawOutputStream(
                samplerate=SPEAKER_RATE, channels=1, dtype="int16",
                blocksize=CHUNK, device=dev)
            stream.start()
            return stream

        try:
            self._out_stream = open_stream(device)
        except Exception:
            if device is None:
                raise
            self._out_stream = open_stream(None)
        return self._out_stream

    def write(self, pcm: bytes):
        if self._out_stream is not None:
            self._out_stream.write(pcm)

    @property
    def output_latency(self) -> float:
        """Measured device latency, sanity-clamped (sizes the echo
        tail); 0.20 s when unknown — the repo's default."""
        try:
            lat = float(self._out_stream.latency)
            return lat if 0.0 < lat < 1.0 else 0.20
        except Exception:
            return 0.20

    def close(self):
        for attr in ("_in_stream", "_out_stream"):
            stream = getattr(self, attr)
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
                setattr(self, attr, None)
