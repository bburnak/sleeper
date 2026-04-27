from __future__ import annotations

import logging
import mmap
import os
import threading
from pathlib import Path

from sleeper.display.base import DisplayBackend, DisplayState
from sleeper.display.screen_power import ScreenPower
from sleeper.input.base import Action

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Layout constants (Hosyond 3.5": 480 x 320, landscape)
# ---------------------------------------------------------------------------
HEADER_H = 60
COLS = 3
ROWS = 2

# ---------------------------------------------------------------------------
# Colour palette (dark theme — comfortable for night-time use)
# ---------------------------------------------------------------------------
C_BG = (15, 15, 25)
C_HEADER_BG = (25, 25, 42)
C_HEADER_TEXT = (200, 205, 220)
C_BORDER = (55, 65, 90)

C_BTN_DEFAULT = (38, 50, 72)
C_BTN_ACTIVE = (55, 105, 160)    # highlighted when relevant state is active
C_BTN_NOISE = (38, 80, 68)
C_BTN_STOP = (110, 38, 38)
C_BTN_VOL = (50, 50, 80)
C_BTN_PRESSED = (90, 120, 180)   # brief press flash
C_BTN_TEXT = (210, 215, 230)
C_BTN_SUBTEXT = (130, 140, 160)  # smaller secondary label
C_DISABLED = (40, 42, 48)
C_DISABLED_TEXT = (70, 75, 85)

# State name → short label shown in header
_STATE_LABELS = {
    "idle": "Idle",
    "playing_story": "Story",
    "crossfading": "Fading",
    "playing_noise": "Noise",
    "fading_out": "Fading out",
}


class _Button:
    """Describes one on-screen button."""

    def __init__(
        self,
        row: int,
        col: int,
        label: str,
        color: tuple[int, int, int],
        action_fn,  # callable(DisplayState) -> Action | None
        active_states: tuple[str, ...] = (),
    ) -> None:
        self.row = row
        self.col = col
        self.label = label
        self.color = color
        self.action_fn = action_fn
        self.active_states = active_states


def _play_pause_label(state: DisplayState) -> str:
    if state.session_state == "idle":
        return "Start"
    if state.is_paused:
        return "Resume"
    return "Pause"


def _play_pause_action(state: DisplayState) -> Action:
    if state.session_state == "idle":
        return Action.START_STORY
    return Action.PAUSE_RESUME


_BUTTONS: list[_Button] = [
    _Button(
        row=0, col=0,
        label="Play/Pause",
        color=C_BTN_DEFAULT,
        action_fn=_play_pause_action,
        active_states=("playing_story", "crossfading"),
    ),
    _Button(
        row=0, col=1,
        label="Vol +",
        color=C_BTN_VOL,
        action_fn=lambda _s: Action.VOLUME_UP,
    ),
    _Button(
        row=0, col=2,
        label="Next",
        color=C_BTN_DEFAULT,
        action_fn=lambda _s: Action.SKIP_TO_NEXT,
        active_states=("playing_story", "crossfading"),
    ),
    _Button(
        row=1, col=0,
        label="Noise",
        color=C_BTN_NOISE,
        action_fn=lambda _s: Action.SKIP_TO_NOISE,
        active_states=("playing_noise", "fading_out"),
    ),
    _Button(
        row=1, col=1,
        label="Vol -",
        color=C_BTN_VOL,
        action_fn=lambda _s: Action.VOLUME_DOWN,
    ),
    _Button(
        row=1, col=2,
        label="Stop",
        color=C_BTN_STOP,
        action_fn=lambda _s: Action.STOP,
    ),
]


class PygameFbDisplay(DisplayBackend):
    """Pygame framebuffer display for small LCD screens (e.g. Hosyond 3.5" 480×320).

    Runs entirely in a dedicated thread. Handles touch/mouse events to emit
    actions back into the session. No X11/desktop environment is required.

    Renders using SDL's offscreen driver (no X11/desktop/VT needed) and blits
    the result directly to the framebuffer device via mmap.  The display is
    RGB565 (16-bit, 2 bytes per pixel).

    Touch input is handled separately by TouchscreenInput (input/touchscreen.py)
    so this backend does NOT consume any evdev events.
    """

    def __init__(
        self,
        width: int = 480,
        height: int = 320,
        fb_device: str = "/dev/fb1",
        fps: int = 15,
        screen_power: ScreenPower | None = None,
    ) -> None:
        super().__init__()
        self._width = width
        self._height = height
        self._fb_device = fb_device
        self._fps = fps
        self._fb_size = width * height * 2  # RGB565: 2 bytes per pixel

        self._state = DisplayState(session_state="idle")
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._pressed_btn: tuple[int, int] | None = None  # (row, col) for press flash
        self._screen_power = screen_power

    # ------------------------------------------------------------------
    # DisplayBackend interface
    # ------------------------------------------------------------------

    def update(self, state: DisplayState) -> None:
        with self._state_lock:
            self._state = state

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        # Use offscreen SDL driver — no VT/X11/fbcon needed.
        os.environ["SDL_VIDEODRIVER"] = "offscreen"
        # Suppress SDL audio init noise (we don't use it here)
        os.environ["SDL_AUDIODRIVER"] = "dummy"

        # Hide the framebuffer console cursor so it doesn't blink over the GUI.
        # tty1 is mapped to fb1 on this Pi (con2fbmap 1 → 1).
        try:
            with open("/dev/tty1", "wb") as tty:
                tty.write(b"\x1b[?25l")  # DECTCEM hide cursor
        except OSError:
            pass

        try:
            import pygame
        except ImportError:
            log.error("pygame is not installed — display backend disabled. Run: pip install pygame")
            return

        try:
            fb_file = open(self._fb_device, "r+b")
            fb_mm = mmap.mmap(fb_file.fileno(), self._fb_size)
        except OSError as exc:
            log.error("Cannot open framebuffer %s: %s", self._fb_device, exc)
            return

        try:
            pygame.init()
            # 32-bit offscreen surface for rendering; we'll convert to 16-bit for blitting
            screen = pygame.Surface((self._width, self._height))
        except Exception as exc:
            fb_mm.close()
            fb_file.close()
            log.error("Failed to initialise pygame: %s", exc)
            return

        try:
            font_large = pygame.font.SysFont("freesans", 22, bold=True)
            font_medium = pygame.font.SysFont("freesans", 18)
            font_small = pygame.font.SysFont("freesans", 14)
        except Exception:
            font_large = pygame.font.Font(None, 28)
            font_medium = pygame.font.Font(None, 22)
            font_small = pygame.font.Font(None, 18)

        clock = pygame.time.Clock()

        log.info("Pygame display running (%dx%d) -> %s", self._width, self._height, self._fb_device)

        try:
            while not self._stop_event.is_set():
                with self._state_lock:
                    state = self._state

                # Idle blanking: if user has been inactive long enough,
                # turn the backlight off and stop redrawing until input wakes us.
                sp = self._screen_power
                if sp is not None and sp.enabled:
                    if sp.should_sleep():
                        sp.sleep()
                    if not sp.is_on:
                        clock.tick(self._fps)
                        continue

                self._draw(screen, state, font_large, font_medium, font_small)
                self._blit_to_fb(screen, fb_mm)
                clock.tick(self._fps)
        finally:
            fb_mm.close()
            fb_file.close()
            pygame.quit()
            log.info("Pygame display stopped")

    # ------------------------------------------------------------------
    # Internal drawing
    # ------------------------------------------------------------------

    def _blit_to_fb(self, surface, fb_mm: mmap.mmap) -> None:
        """Convert surface to RGB565 and write to framebuffer via mmap."""
        import pygame
        surface_16 = surface.convert(16)
        # pygame.Surface.get_buffer() returns raw bytes in the surface's pixel format.
        # On little-endian ARM (Pi), this is already RGB565 LE — matching the TFT.
        raw = surface_16.get_buffer().raw
        fb_mm.seek(0)
        fb_mm.write(raw)

    def _btn_rect(self, row: int, col: int) -> tuple[int, int, int, int]:
        btn_w = self._width // COLS
        btn_h = (self._height - HEADER_H) // ROWS
        x = col * btn_w
        y = HEADER_H + row * btn_h
        return x, y, btn_w, btn_h

    def notify_pressed(self, row: int, col: int) -> None:
        """Called by TouchscreenInput to trigger a press flash on the display."""
        self._pressed_btn = (row, col)

    def _draw(self, screen, state: DisplayState, font_large, font_medium, font_small) -> None:
        import pygame

        screen.fill(C_BG)

        # ---- Header ----
        pygame.draw.rect(screen, C_HEADER_BG, (0, 0, self._width, HEADER_H))
        pygame.draw.line(screen, C_BORDER, (0, HEADER_H - 1), (self._width, HEADER_H - 1), 1)

        state_label = _STATE_LABELS.get(state.session_state, state.session_state)
        state_surf = font_medium.render(state_label, True, C_HEADER_TEXT)
        screen.blit(state_surf, (10, (HEADER_H - state_surf.get_height()) // 2))

        vol_text = f"VOL {state.volume}%"
        vol_surf = font_medium.render(vol_text, True, C_HEADER_TEXT)
        screen.blit(vol_surf, (self._width - vol_surf.get_width() - 10, (HEADER_H - vol_surf.get_height()) // 2))

        if state.story_name:
            name = Path(state.story_name).stem.replace("_", " ").replace("-", " ").title()
            max_name_w = self._width - state_surf.get_width() - vol_surf.get_width() - 30
            name_surf = font_small.render(name, True, C_BTN_SUBTEXT)
            if name_surf.get_width() > max_name_w:
                # Truncate by character
                while name_surf.get_width() > max_name_w and len(name) > 3:
                    name = name[:-1]
                    name_surf = font_small.render(name + "…", True, C_BTN_SUBTEXT)
            cx = (self._width - name_surf.get_width()) // 2
            screen.blit(name_surf, (cx, (HEADER_H - name_surf.get_height()) // 2))

        # ---- Buttons ----
        btn_w = self._width // COLS
        btn_h = (self._height - HEADER_H) // ROWS

        for btn in _BUTTONS:
            x, y, w, h = self._btn_rect(btn.row, btn.col)

            is_active = state.session_state in btn.active_states
            is_pressed = self._pressed_btn == (btn.row, btn.col)

            if is_pressed:
                bg = C_BTN_PRESSED
                self._pressed_btn = None  # flash once
            elif is_active:
                bg = C_BTN_ACTIVE
            else:
                bg = btn.color

            pygame.draw.rect(screen, bg, (x, y, w, h))
            pygame.draw.rect(screen, C_BORDER, (x, y, w, h), 1)

            # Dynamic label for play/pause button
            if btn.label == "Play/Pause":
                label = _play_pause_label(state)
            else:
                label = btn.label

            text_surf = font_large.render(label, True, C_BTN_TEXT)
            tx = x + (w - text_surf.get_width()) // 2
            ty = y + (h - text_surf.get_height()) // 2
            screen.blit(text_surf, (tx, ty))
