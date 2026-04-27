from __future__ import annotations

import logging
import struct
import subprocess
import threading
import time

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

# 48000 Hz matches BlueALSA's A2DP codec rate, avoiding pitch-shift when
# aplay writes directly to a `bluealsa:...` device (no `plug` wrapper).
SAMPLE_RATE = 48000
BLOCK_SIZE = 4096

# Length of pre-generated noise buffer (seconds). Generated once via FFT
# spectral shaping; output of irfft is naturally periodic so the buffer
# loops seamlessly with no boundary artifacts.
_BUFFER_SECONDS = 30
# Target peak amplitude for generated buffers. Headroom prevents the hard
# clipping that produced the previous "intermittent buzz" artifact.
_TARGET_PEAK = 0.85


def _white_noise(frames: int, rng: np.random.Generator) -> np.ndarray:
    return rng.standard_normal(frames).astype(np.float32)


def _shaped_noise_buffer(
    seconds: int, sample_rate: int, exponent: float, rng: np.random.Generator,
) -> np.ndarray:
    """Generate a seamless looping noise buffer with 1/f**exponent amplitude.

    exponent = 0.5 -> pink noise (-3 dB/oct power)
    exponent = 1.0 -> brown noise (-6 dB/oct power)
    """
    n = seconds * sample_rate
    white = rng.standard_normal(n).astype(np.float32)
    spec = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    # Avoid div-by-zero at DC; we zero out DC explicitly below.
    freqs[0] = 1.0
    spec /= freqs ** exponent
    spec[0] = 0.0  # remove DC offset
    out = np.fft.irfft(spec, n).astype(np.float32)
    peak = float(np.abs(out).max())
    if peak > 0:
        out *= _TARGET_PEAK / peak
    return out


def _brown_buffer(sample_rate: int, rng: np.random.Generator) -> np.ndarray:
    return _shaped_noise_buffer(_BUFFER_SECONDS, sample_rate, 1.0, rng)


def _pink_buffer(sample_rate: int, rng: np.random.Generator) -> np.ndarray:
    return _shaped_noise_buffer(_BUFFER_SECONDS, sample_rate, 0.5, rng)


class NoiseGenerator:
    """Generate continuous noise via sounddevice or aplay subprocess."""

    def __init__(self, device: str | None = None) -> None:
        self._device_name = device or "default"
        # Use aplay subprocess for devices PortAudio can't see (e.g. bluealsa)
        self._use_aplay = self._device_name not in ("default", None)
        self._device = None if self._device_name == "default" else self._device_name
        self._stream: sd.OutputStream | None = None
        self._aplay_proc: subprocess.Popen | None = None
        self._aplay_thread: threading.Thread | None = None
        self._running = False
        self._volume: float = 0.3  # 0.0 - 1.0
        self._target_volume: float = 0.3
        self._fade_step: float = 0.0
        self._noise_type: str = "white"
        self._lock = threading.Lock()
        self._rng = np.random.default_rng()

        # Pre-generated looping buffer for brown/pink noise. Allocated lazily
        # in start() so we don't pay the FFT cost at import time.
        self._buffer: np.ndarray | None = None
        self._buffer_pos: int = 0

    def _read_block(self, frames: int) -> np.ndarray:
        """Return `frames` samples of the active noise type. Caller holds lock."""
        if self._noise_type in ("brown", "pink") and self._buffer is not None:
            buf = self._buffer
            n = buf.shape[0]
            pos = self._buffer_pos
            end = pos + frames
            if end <= n:
                data = buf[pos:end].copy()
                self._buffer_pos = end % n
            else:
                # Wrap around the looping buffer.
                first = buf[pos:n]
                second = buf[: end - n]
                data = np.concatenate((first, second))
                self._buffer_pos = end - n
            return data
        return _white_noise(frames, self._rng)

    def _callback(self, outdata: np.ndarray, frames: int, time_info: object, status: sd.CallbackFlags) -> None:
        if status:
            log.debug("sounddevice status: %s", status)

        with self._lock:
            vol = self._volume
            data = self._read_block(frames)

            # Apply fade towards target volume
            if self._fade_step != 0.0:
                remaining = self._target_volume - self._volume
                if abs(remaining) < abs(self._fade_step):
                    self._volume = self._target_volume
                    self._fade_step = 0.0
                else:
                    self._volume += self._fade_step
                vol = self._volume

        outdata[:, 0] = data * vol

    def start(self, noise_type: str = "white", volume: int = 30) -> None:
        # Generate the looping buffer outside the lock — the FFT is fast on
        # 30 s @ 48 kHz but still O(100 ms) on a Pi 3, no need to block the
        # audio callback on it.
        buffer: np.ndarray | None = None
        if noise_type == "brown":
            buffer = _brown_buffer(SAMPLE_RATE, self._rng)
        elif noise_type == "pink":
            buffer = _pink_buffer(SAMPLE_RATE, self._rng)

        with self._lock:
            self._noise_type = noise_type
            self._volume = volume / 100.0
            self._target_volume = self._volume
            self._fade_step = 0.0
            self._buffer = buffer
            self._buffer_pos = 0

        if self._use_aplay:
            self._start_aplay()
        else:
            self._start_sounddevice()
        log.info("Noise started: type=%s, volume=%d%%", noise_type, volume)

    def _start_sounddevice(self) -> None:
        self._stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=BLOCK_SIZE,
            callback=self._callback,
            device=self._device,
        )
        self._stream.start()

    def _start_aplay(self) -> None:
        self._running = True
        self._aplay_proc = subprocess.Popen(
            ["aplay", "-D", self._device_name, "-f", "S16_LE",
             "-r", str(SAMPLE_RATE), "-c", "1", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._aplay_thread = threading.Thread(
            target=self._aplay_loop, daemon=True, name="noise-aplay",
        )
        self._aplay_thread.start()

    def _aplay_loop(self) -> None:
        """Generate noise blocks and write PCM to aplay stdin."""
        try:
            while self._running and self._aplay_proc and self._aplay_proc.poll() is None:
                with self._lock:
                    vol = self._volume
                    data = self._read_block(BLOCK_SIZE)

                    if self._fade_step != 0.0:
                        remaining = self._target_volume - self._volume
                        if abs(remaining) < abs(self._fade_step):
                            self._volume = self._target_volume
                            self._fade_step = 0.0
                        else:
                            self._volume += self._fade_step
                        vol = self._volume

                pcm = (data * vol * 32767).astype(np.int16).tobytes()
                try:
                    self._aplay_proc.stdin.write(pcm)
                except (BrokenPipeError, OSError):
                    break
        except Exception:
            log.exception("aplay noise loop error")

    def set_volume(self, volume_pct: int) -> None:
        with self._lock:
            self._volume = max(0.0, min(1.0, volume_pct / 100.0))
            self._target_volume = self._volume
            self._fade_step = 0.0

    def fade_to(self, target_pct: int, duration_sec: float) -> None:
        """Begin a gradual fade to target volume over duration_sec seconds."""
        with self._lock:
            target = max(0.0, min(1.0, target_pct / 100.0))
            self._target_volume = target
            if duration_sec <= 0:
                self._volume = target
                self._fade_step = 0.0
                return
            # Calculate step per callback block
            blocks = (duration_sec * SAMPLE_RATE) / BLOCK_SIZE
            if blocks > 0:
                self._fade_step = (target - self._volume) / blocks
            else:
                self._volume = target
                self._fade_step = 0.0
        log.info("Noise fading to %d%% over %.1fs", target_pct, duration_sec)

    @property
    def is_fading(self) -> bool:
        with self._lock:
            return self._fade_step != 0.0

    @property
    def volume(self) -> int:
        with self._lock:
            return int(self._volume * 100)

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._aplay_proc is not None:
            try:
                self._aplay_proc.stdin.close()
            except OSError:
                pass
            self._aplay_proc.terminate()
            try:
                self._aplay_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                log.warning("aplay did not exit after terminate; killing")
                self._aplay_proc.kill()
                try:
                    self._aplay_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    log.error("aplay did not exit after kill; abandoning")
            self._aplay_proc = None
        if self._aplay_thread is not None:
            self._aplay_thread.join(timeout=5)
            self._aplay_thread = None
        log.info("Noise stopped")

    def shutdown(self) -> None:
        self.stop()
