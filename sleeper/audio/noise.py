from __future__ import annotations

import logging
import struct
import subprocess
import threading
import time

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

# Sample rate — 22050 Hz is sufficient for noise and lighter on RPi 3
SAMPLE_RATE = 22050
BLOCK_SIZE = 2048


def _white_noise(frames: int, rng: np.random.Generator) -> np.ndarray:
    return rng.standard_normal(frames).astype(np.float32)


def _brown_noise(frames: int, rng: np.random.Generator, state: list[float]) -> np.ndarray:
    white = rng.standard_normal(frames).astype(np.float32)
    out = np.empty(frames, dtype=np.float32)
    val = state[0]
    for i in range(frames):
        val += white[i] * 0.02
        # Leaky integrator to prevent drift
        val *= 0.998
        out[i] = val
    state[0] = val
    # Normalize to roughly [-1, 1]
    peak = np.abs(out).max()
    if peak > 0:
        out /= peak
    return out


def _pink_noise(frames: int, rng: np.random.Generator, state: dict) -> np.ndarray:
    """Voss-McCartney algorithm for pink noise."""
    rows = state["rows"]
    running_sum = state["running_sum"]
    n_rows = len(rows)
    out = np.empty(frames, dtype=np.float32)

    for i in range(frames):
        # Determine which row to update (trailing zeros of counter)
        idx = state["counter"]
        state["counter"] += 1
        # Find lowest set bit index
        changed = 0
        if idx > 0:
            changed = (idx ^ (idx - 1)).bit_length() - 1
            changed = min(changed, n_rows - 1)

        running_sum -= rows[changed]
        rows[changed] = rng.standard_normal() * 0.5
        running_sum += rows[changed]

        out[i] = running_sum + rng.standard_normal() * 0.5

    # Normalize
    peak = np.abs(out).max()
    if peak > 0:
        out /= peak
    return out


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

        # State for brown noise
        self._brown_state: list[float] = [0.0]

        # State for pink noise (Voss-McCartney)
        n_rows = 16
        self._pink_state: dict = {
            "rows": [0.0] * n_rows,
            "running_sum": 0.0,
            "counter": 0,
        }

    def _callback(self, outdata: np.ndarray, frames: int, time_info: object, status: sd.CallbackFlags) -> None:
        if status:
            log.debug("sounddevice status: %s", status)

        with self._lock:
            vol = self._volume
            if self._noise_type == "brown":
                data = _brown_noise(frames, self._rng, self._brown_state)
            elif self._noise_type == "pink":
                data = _pink_noise(frames, self._rng, self._pink_state)
            else:
                data = _white_noise(frames, self._rng)

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
        with self._lock:
            self._noise_type = noise_type
            self._volume = volume / 100.0
            self._target_volume = self._volume
            self._fade_step = 0.0
            # Reset state
            self._brown_state[0] = 0.0
            self._pink_state["rows"] = [0.0] * len(self._pink_state["rows"])
            self._pink_state["running_sum"] = 0.0
            self._pink_state["counter"] = 0

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
                    if self._noise_type == "brown":
                        data = _brown_noise(BLOCK_SIZE, self._rng, self._brown_state)
                    elif self._noise_type == "pink":
                        data = _pink_noise(BLOCK_SIZE, self._rng, self._pink_state)
                    else:
                        data = _white_noise(BLOCK_SIZE, self._rng)

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
            self._aplay_proc.wait(timeout=5)
            self._aplay_proc = None
        if self._aplay_thread is not None:
            self._aplay_thread.join(timeout=5)
            self._aplay_thread = None
        log.info("Noise stopped")

    def shutdown(self) -> None:
        self.stop()
