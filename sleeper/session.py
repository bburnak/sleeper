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
            self._pause_resume()
        elif action == Action.STOP:
            self._stop_session()

    # ── Session lifecycle ──

    def _start_session(self) -> None:
        with self._lock:
            if self.state != State.IDLE:
                log.info("Session already active, ignoring START_STORY")
                return
            self.state = State.PLAYING_STORY

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
        self.player.play(path, volume=self.config.story_volume)

    def _skip_to_next(self) -> None:
        with self._lock:
            if self.state not in (State.PLAYING_STORY, State.CROSSFADING):
                return
            self.state = State.PLAYING_STORY

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
            # Don't pause noise — it should always play

    def _stop_session(self) -> None:
        log.info("Stopping session")
        self._cancel_timers()
        self.player.stop()
        self.noise.stop()
        with self._lock:
            self.state = State.IDLE
            self._current_story = None

    def _change_volume(self, delta: int) -> None:
        try:
            # Read current system volume, adjust, clamp
            result = subprocess.run(
                ["amixer", "get", "Master"],
                capture_output=True, text=True, timeout=5,
            )
            # Parse current percentage from output like "[75%]"
            import re
            match = re.search(r"\[(\d+)%\]", result.stdout)
            if match:
                current = int(match.group(1))
                new_vol = max(0, min(100, current + delta))
                subprocess.run(
                    ["amixer", "set", "Master", f"{new_vol}%"],
                    capture_output=True, timeout=5,
                )
                log.info("System volume: %d%% -> %d%%", current, new_vol)
            else:
                # Fallback: relative adjustment
                direction = f"{abs(delta)}%+" if delta > 0 else f"{abs(delta)}%-"
                subprocess.run(
                    ["amixer", "set", "Master", direction],
                    capture_output=True, timeout=5,
                )
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            log.warning("Failed to change volume: %s", e)

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
        self.noise.fade_to(self.config.noise_volume, self.config.crossfade_seconds)

        # Story is already ended; if it were still playing we'd fade it down here.
        # Wait for crossfade to complete, then update state.
        def _finish_crossfade() -> None:
            time.sleep(self.config.crossfade_seconds)
            with self._lock:
                if self.state == State.CROSSFADING:
                    self.state = State.PLAYING_NOISE
                    log.info("Now playing noise")

        self._crossfade_thread = threading.Thread(target=_finish_crossfade, daemon=True)
        self._crossfade_thread.start()

    def _transition_to_noise(self) -> None:
        """Go directly to noise (no crossfade, e.g. after skip to noise)."""
        self.noise.start(self.config.noise_type, volume=self.config.noise_volume)
        with self._lock:
            self.state = State.PLAYING_NOISE
        log.info("Playing noise: %s at %d%%", self.config.noise_type, self.config.noise_volume)

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

    def _auto_stop(self) -> None:
        log.info("Stop time reached, ending session")
        self.player.stop()
        self.noise.stop()
        with self._lock:
            self.state = State.IDLE
            self._current_story = None

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
