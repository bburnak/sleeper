from __future__ import annotations

import logging
import threading
from typing import Callable

import mpv

log = logging.getLogger(__name__)


class StoryPlayer:
    """Plays MP3 stories via libmpv. Headless, no video."""

    def __init__(self, audio_device: str = "default") -> None:
        self._lock = threading.Lock()
        self._player: mpv.MPV | None = None
        self._on_end: Callable[[], None] | None = None
        self._audio_device = audio_device
        self._listened_recorded = False
        self._listened_callback: Callable[[str], None] | None = None
        self._current_file: str | None = None
        self._listened_threshold: float = 0.10
        self._expecting_stop: bool = False
        self._overlap_callback: Callable[[], None] | None = None
        self._overlap_seconds: float = 0.0
        self._overlap_fired: bool = False

    def _ensure_player(self) -> mpv.MPV:
        if self._player is None:
            # Force ALSA output so audio_device is honored (mpv otherwise
            # picks pulseaudio/pipewire if available and silently ignores
            # the ALSA device string).
            opts: dict = dict(video=False, terminal=False, ao="alsa")
            if self._audio_device == "default":
                opts["audio_device"] = "alsa"
            else:
                opts["audio_device"] = f"alsa/{self._audio_device}"
            self._player = mpv.MPV(**opts)

            @self._player.property_observer("percent-pos")
            def _pos_observer(_name: str, value: float | None) -> None:
                if value is None:
                    return
                self._check_listened(value / 100.0)

            @self._player.property_observer("time-remaining")
            def _remaining_observer(_name: str, value: float | None) -> None:
                if value is None:
                    return
                self._check_overlap(value)

            @self._player.event_callback("end-file")
            def _end_handler(event: mpv.MpvEvent) -> None:
                # Suppress on_end when the stop was requested by us (skip/stop/replace).
                # Only fire for natural end-of-file (or unknown reasons treated as natural).
                if self._expecting_stop:
                    self._expecting_stop = False
                    return
                if self._on_end:
                    self._on_end()

        return self._player

    def set_on_end(self, callback: Callable[[], None]) -> None:
        self._on_end = callback

    def set_overlap_callback(self, callback: Callable[[], None], seconds: float) -> None:
        """Register a callback invoked once per story when remaining time <= seconds.

        Used to pre-warm the noise stream before the story ends so the audio
        device (and Bluetooth A2DP link) never goes idle at the handover.
        Pass seconds <= 0 to disable.
        """
        self._overlap_callback = callback if seconds > 0 else None
        self._overlap_seconds = max(0.0, seconds)

    def _check_overlap(self, remaining: float) -> None:
        if (
            not self._overlap_fired
            and self._overlap_callback is not None
            and self._overlap_seconds > 0
            and 0 <= remaining <= self._overlap_seconds
        ):
            self._overlap_fired = True
            try:
                self._overlap_callback()
            except Exception:
                log.exception("overlap callback raised")

    def set_listened_callback(self, callback: Callable[[str], None], threshold: float = 0.10) -> None:
        """Register a callback invoked once when a story reaches the listened threshold."""
        self._listened_callback = callback
        self._listened_threshold = threshold

    def _check_listened(self, fraction: float) -> None:
        if (
            not self._listened_recorded
            and fraction >= self._listened_threshold
            and self._listened_callback
            and self._current_file
        ):
            self._listened_recorded = True
            self._listened_callback(self._current_file)

    def play(self, path: str, volume: int = 60) -> None:
        with self._lock:
            player = self._ensure_player()
            self._listened_recorded = False
            self._overlap_fired = False
            self._current_file = path
            # Replacing current file via play() will fire end-file for the
            # outgoing track; suppress it so we don't trigger crossfade-to-noise.
            if player.percent_pos is not None:
                self._expecting_stop = True
            player.volume = volume
            player.play(path)
            log.info("Playing story: %s (vol=%d)", path, volume)

    def pause(self) -> None:
        with self._lock:
            if self._player:
                self._player.pause = True

    def resume(self) -> None:
        with self._lock:
            if self._player:
                self._player.pause = False

    def toggle_pause(self) -> None:
        with self._lock:
            if self._player:
                self._player.pause = not self._player.pause

    def stop(self) -> None:
        with self._lock:
            if self._player:
                self._expecting_stop = True
                self._player.stop(True)
                log.info("Story stopped")

    def set_volume(self, volume: int) -> None:
        with self._lock:
            if self._player:
                self._player.volume = max(0, min(100, volume))

    @property
    def volume(self) -> int:
        with self._lock:
            if self._player:
                return int(self._player.volume or 0)
            return 0

    @property
    def position(self) -> float:
        """Return playback position as fraction 0.0-1.0, or 0.0 if not playing."""
        with self._lock:
            if self._player:
                pct = self._player.percent_pos
                if pct is not None:
                    return pct / 100.0
            return 0.0

    @property
    def is_playing(self) -> bool:
        with self._lock:
            if self._player:
                return not self._player.pause and self._player.percent_pos is not None
            return False

    def shutdown(self) -> None:
        with self._lock:
            if self._player:
                self._player.terminate()
                self._player = None
