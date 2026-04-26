from __future__ import annotations

import enum
import logging
import subprocess
import threading
import time
from datetime import datetime, timedelta

from sleeper.audio.noise import NoiseGenerator
from sleeper.audio.player import StoryPlayer
from sleeper.config import Config
from sleeper.display.base import DisplayBackend, DisplayState
from sleeper.history import PlayHistory
from sleeper.input.base import Action
from sleeper.selector import StorySelector

log = logging.getLogger(__name__)


class State(enum.Enum):
    IDLE = "idle"
    PLAYING_STORY = "playing_story"
    CROSSFADING = "crossfading"
    PLAYING_NOISE = "playing_noise"
    FADING_OUT = "fading_out"


class SessionManager:
    """Orchestrates the full sleep session lifecycle."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = State.IDLE
        self._lock = threading.Lock()

        self.history = PlayHistory(config.history_path)
        self.selector = StorySelector(config.stories_path, self.history)
        self.player = StoryPlayer(audio_device=config.audio_output)
        self.noise = NoiseGenerator(
            device=config.audio_output if config.audio_output != "default" else None,
        )

        self._current_story: str | None = None
        self._stop_timer: threading.Timer | None = None
        self._fade_timer: threading.Timer | None = None
        self._crossfade_thread: threading.Thread | None = None

        self._display: DisplayBackend | None = None
        self._current_volume: int = config.story_volume
        self._is_paused: bool = False

        # Wire up listened tracking
        self.player.set_listened_callback(
            self._on_story_listened, threshold=config.listened_threshold
        )
        self.player.set_on_end(self._on_story_ended)

    # ── Public actions (called from input layer) ──

    def handle_action(self, action: Action) -> None:
        log.info("Action received: %s (state=%s)", action.name, self.state.name)

        if action == Action.START_STORY:
            self._start_session()
        elif action == Action.SKIP_TO_NEXT:
            self._skip_to_next()
        elif action == Action.SKIP_TO_NOISE:
            self._skip_to_noise()
        elif action == Action.VOLUME_UP:
            self._change_volume(self.config.volume_step)
        elif action == Action.VOLUME_DOWN:
            self._change_volume(-self.config.volume_step)
        elif action == Action.PAUSE_RESUME:
            # Touchscreen play/pause cell emits PAUSE_RESUME; when idle, interpret it as START.
            if self.state == State.IDLE:
                self._start_session()
            else:
                self._pause_resume()
        elif action == Action.STOP:
            self._stop_session()

    def set_display(self, display: DisplayBackend) -> None:
        """Register a display backend to receive state updates."""
        self._display = display
        self._notify_display()

    def _notify_display(self) -> None:
        if self._display is None:
            return
        with self._lock:
            ds = DisplayState(
                session_state=self.state.value,
                story_name=self._current_story,
                volume=self._current_volume,
                is_paused=self._is_paused,
            )
        self._display.update(ds)
        log.info("Display update: state=%s volume=%d paused=%s", ds.session_state, ds.volume, ds.is_paused)

    # ── Session lifecycle ──

    def _start_session(self) -> None:
        with self._lock:
            if self.state != State.IDLE:
                log.info("Session already active, ignoring START_STORY")
                return
            self.state = State.PLAYING_STORY
            self._is_paused = False

        self._notify_display()
        self._play_next_story()
        self._schedule_stop_time()

    def _play_next_story(self, exclude: str | None = None) -> None:
        story = self.selector.pick(exclude=exclude)
        if story is None:
            log.warning("No stories available, going directly to noise")
            self._transition_to_noise()
            return

        self._current_story = story
        path = str(self.selector.story_path(story))
        self.player.play(path, volume=self._current_volume)
        self._notify_display()

    def _skip_to_next(self) -> None:
        with self._lock:
            if self.state not in (State.PLAYING_STORY, State.CROSSFADING):
                return
            self.state = State.PLAYING_STORY
            self._is_paused = False

        self._notify_display()
        self.player.stop()
        self.noise.stop()
        previous = self._current_story
        self._play_next_story(exclude=previous)

    def _skip_to_noise(self) -> None:
        with self._lock:
            if self.state in (State.PLAYING_NOISE, State.FADING_OUT, State.IDLE):
                return

        self.player.stop()
        self._transition_to_noise()

    def _pause_resume(self) -> None:
        with self._lock:
            if self.state == State.PLAYING_STORY:
                self.player.toggle_pause()
                self._is_paused = not self._is_paused
            # Don't pause noise — it should always play
        self._notify_display()

    def _stop_session(self) -> None:
        log.info("Stopping session")
        self._cancel_timers()
        self.player.stop()
        self.noise.stop()
        with self._lock:
            self.state = State.IDLE
            self._current_story = None
            self._is_paused = False
        self._notify_display()

    def _change_volume(self, delta: int) -> None:
        try:
            # Always update app-level volume first so UI/input feel responsive,
            # even if system mixer control is unavailable on this device.
            with self._lock:
                old_vol = self._current_volume
                new_vol = max(0, min(100, self._current_volume + delta))
                self._current_volume = new_vol
                state = self.state
            log.info("App volume: %d%% -> %d%%", old_vol, new_vol)

            if state in (State.PLAYING_STORY, State.CROSSFADING):
                self.player.set_volume(new_vol)
            if state in (State.PLAYING_NOISE, State.FADING_OUT):
                self.noise.set_volume(new_vol)

            self._notify_display()

            # Try configured card first, then common fallbacks.
            amixer_bases: list[list[str]] = []
            if self.config.alsa_card is not None:
                amixer_bases.append(["amixer", "-c", str(self.config.alsa_card)])
            amixer_bases.extend([
                ["amixer", "-c", "0"],
                ["amixer", "-c", "1"],
                ["amixer"],
            ])

            # De-duplicate while preserving order
            unique_bases: list[list[str]] = []
            seen: set[tuple[str, ...]] = set()
            for base in amixer_bases:
                key = tuple(base)
                if key not in seen:
                    seen.add(key)
                    unique_bases.append(base)

            last_error = ""
            for amixer_base in unique_bases:
                set_result = subprocess.run(
                    [*amixer_base, "set", self.config.alsa_mixer_control, f"{new_vol}%"],
                    capture_output=True, text=True, timeout=5,
                )
                if set_result.returncode != 0:
                    last_error = (set_result.stderr or set_result.stdout).strip()
                    continue
                log.info("System volume set to %d%%", new_vol)
                return

            log.debug(
                "System mixer sync failed for control=%s (app volume still updated). Last error: %s",
                self.config.alsa_mixer_control,
                last_error,
            )
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            log.debug("System mixer sync failed (app volume still updated): %s", e)

    # ── Internal transitions ──

    def _on_story_listened(self, filename: str) -> None:
        log.info("Story '%s' reached listened threshold, recording", filename)
        self.history.record_listen(filename)

    def _on_story_ended(self) -> None:
        """Called by mpv when a story finishes naturally."""
        with self._lock:
            if self.state != State.PLAYING_STORY:
                return
            self.state = State.CROSSFADING

        self._do_crossfade()

    def _do_crossfade(self) -> None:
        """Crossfade from story to noise."""
        log.info("Crossfading to noise over %ds", self.config.crossfade_seconds)

        # Start noise at 0% and fade up
        self.noise.start(self.config.noise_type, volume=0)
        self.noise.fade_to(self._current_volume, self.config.crossfade_seconds)

        # Story is already ended; if it were still playing we'd fade it down here.
        # Wait for crossfade to complete, then update state.
        def _finish_crossfade() -> None:
            time.sleep(self.config.crossfade_seconds)
            with self._lock:
                if self.state == State.CROSSFADING:
                    self.state = State.PLAYING_NOISE
                    log.info("Now playing noise")
            self._notify_display()

        self._crossfade_thread = threading.Thread(target=_finish_crossfade, daemon=True)
        self._crossfade_thread.start()

    def _transition_to_noise(self) -> None:
        """Go directly to noise (no crossfade, e.g. after skip to noise)."""
        self.noise.start(self.config.noise_type, volume=self._current_volume)
        with self._lock:
            self.state = State.PLAYING_NOISE
        log.info("Playing noise: %s at %d%%", self.config.noise_type, self._current_volume)
        self._notify_display()

    # ── Stop-time scheduling ──

    def _schedule_stop_time(self) -> None:
        now = datetime.now()
        stop = now.replace(
            hour=self.config.stop_hour,
            minute=self.config.stop_minute,
            second=0,
            microsecond=0,
        )
        # If stop time is before now, it's tomorrow
        if stop <= now:
            stop += timedelta(days=1)

        fade_start = stop - timedelta(minutes=self.config.fade_out_minutes)
        seconds_until_fade = max(0, (fade_start - now).total_seconds())
        seconds_until_stop = max(0, (stop - now).total_seconds())

        log.info(
            "Scheduled: fade at %s (in %.0fs), stop at %s (in %.0fs)",
            fade_start.strftime("%H:%M"),
            seconds_until_fade,
            stop.strftime("%H:%M"),
            seconds_until_stop,
        )

        self._fade_timer = threading.Timer(seconds_until_fade, self._begin_fade_out)
        self._fade_timer.daemon = True
        self._fade_timer.start()

        self._stop_timer = threading.Timer(seconds_until_stop, self._auto_stop)
        self._stop_timer.daemon = True
        self._stop_timer.start()

    def _begin_fade_out(self) -> None:
        with self._lock:
            if self.state not in (State.PLAYING_NOISE, State.CROSSFADING, State.PLAYING_STORY):
                return
            self.state = State.FADING_OUT

        fade_secs = self.config.fade_out_minutes * 60
        self.noise.fade_to(0, fade_secs)
        log.info("Beginning fade-out over %d minutes", self.config.fade_out_minutes)
        self._notify_display()

    def _auto_stop(self) -> None:
        log.info("Stop time reached, ending session")
        self.player.stop()
        self.noise.stop()
        with self._lock:
            self.state = State.IDLE
            self._current_story = None
            self._is_paused = False
        self._notify_display()

    def _cancel_timers(self) -> None:
        for timer in (self._stop_timer, self._fade_timer):
            if timer is not None:
                timer.cancel()
        self._stop_timer = None
        self._fade_timer = None

    # ── Cleanup ──

    def shutdown(self) -> None:
        self._cancel_timers()
        self.player.shutdown()
        self.noise.shutdown()
        log.info("Session manager shut down")
