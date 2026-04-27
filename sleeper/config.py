from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class GpioPins:
    start_story: int = 17
    skip: int = 27
    volume_up: int = 22
    volume_down: int = 23
    pause_resume: int = 24
    stop: int = 25


@dataclass
class Config:
    stories_dir: str = "~/stories"
    noise_type: str = "white"
    noise_volume: int = 30
    story_volume: int = 60
    stop_time: str = "07:00"
    fade_out_minutes: int = 5
    crossfade_seconds: int = 10
    listened_threshold: float = 0.10
    volume_step: int = 5
    input_backend: str = "gamepad"
    input_device: str = "auto"
    audio_output: str = "default"
    history_file: str = "~/.sleeper/history.json"
    long_press_seconds: float = 1.5
    gpio_pins: GpioPins = field(default_factory=GpioPins)

    # Gamepad button mapping: evdev key code -> action name
    gamepad_mapping: dict[int, str] = field(default_factory=lambda: {
        # Defaults for PS3 controller (sixaxis); override in config.yaml
        # BTN_START (315) -> start_story
        315: "start_story",
        # BTN_NORTH (307, triangle) -> skip (short=next, long=noise)
        307: "skip",
        # BTN_SOUTH (304, cross) -> pause_resume
        304: "pause_resume",
        # BTN_SELECT (314) -> stop (long press)
        314: "stop",
    })

    # Keyboard mapping: evdev key code -> action name (for dev/testing)
    keyboard_mapping: dict[int, str] = field(default_factory=lambda: {
        # KEY_SPACE (57) -> pause_resume
        57: "pause_resume",
        # KEY_ENTER (28) -> start_story
        28: "start_story",
        # KEY_N (49) -> skip
        49: "skip",
        # KEY_Q (16) -> stop
        16: "stop",
    })

    # Display backend: pygame (framebuffer LCD) or none
    display_backend: str = "none"
    # Framebuffer device (e.g. /dev/fb0 or /dev/fb1)
    display_fb_device: str = "/dev/fb1"
    # Touch/mouse evdev input device for the display
    display_touch_device: str = "/dev/input/event0"
    # LCD resolution
    display_width: int = 480
    display_height: int = 320

    # Screen power management (only relevant for display_backend: pygame).
    # Seconds of inactivity before the LCD backlight turns off. 0 disables.
    screen_idle_timeout: float = 60.0
    # Path to a sysfs bl_power file. "auto" auto-discovers under
    # /sys/class/backlight/. Falls back to /sys/class/graphics/<fb>/blank.
    screen_backlight_path: str = "auto"

    # Touchscreen input calibration (used by input_backend: touchscreen)
    touch_raw_x_min: int = 150
    touch_raw_x_max: int = 3950
    touch_raw_y_min: int = 150
    touch_raw_y_max: int = 3950
    touch_swap_xy: bool = True
    touch_invert_x: bool = False
    touch_invert_y: bool = False

    # ALSA mixer control name for volume adjustment (use 'PCM' on Pi, 'Master' on desktop)
    alsa_mixer_control: str = "PCM"
    # ALSA card index used by amixer (None = auto-detect/fallback)
    alsa_card: int | None = None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        valid_noise = ("white", "brown", "pink")
        if self.noise_type not in valid_noise:
            raise ValueError(f"noise_type must be one of {valid_noise}, got '{self.noise_type}'")

        for name in ("noise_volume", "story_volume", "volume_step"):
            val = getattr(self, name)
            if not 0 <= val <= 100:
                raise ValueError(f"{name} must be 0-100, got {val}")

        if not 0.0 < self.listened_threshold <= 1.0:
            raise ValueError(f"listened_threshold must be in (0, 1], got {self.listened_threshold}")

        parts = self.stop_time.split(":")
        if len(parts) != 2:
            raise ValueError(f"stop_time must be HH:MM, got '{self.stop_time}'")
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"stop_time out of range: '{self.stop_time}'")

        valid_backends = ("gamepad", "gpio", "keyboard", "stdin", "none", "touchscreen")
        if self.input_backend not in valid_backends:
            raise ValueError(f"input_backend must be one of {valid_backends}, got '{self.input_backend}'")

        valid_displays = ("pygame", "none")
        if self.display_backend not in valid_displays:
            raise ValueError(f"display_backend must be one of {valid_displays}, got '{self.display_backend}'") 

    @property
    def stories_path(self) -> Path:
        return Path(os.path.expanduser(self.stories_dir))

    @property
    def history_path(self) -> Path:
        return Path(os.path.expanduser(self.history_file))

    @property
    def stop_hour(self) -> int:
        return int(self.stop_time.split(":")[0])

    @property
    def stop_minute(self) -> int:
        return int(self.stop_time.split(":")[1])


def load_config(path: str | Path = "config.yaml") -> Config:
    """Load config from a YAML file, falling back to defaults for missing keys."""
    path = Path(path)
    if not path.exists():
        return Config()

    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}

    gpio_raw = raw.pop("gpio_pins", None)
    gpio = GpioPins(**gpio_raw) if isinstance(gpio_raw, dict) else GpioPins()

    # Convert gamepad_mapping keys from YAML (loaded as int) to int
    gm = raw.pop("gamepad_mapping", None)
    km = raw.pop("keyboard_mapping", None)

    cfg = Config(gpio_pins=gpio, **raw)

    if isinstance(gm, dict):
        cfg.gamepad_mapping = {int(k): v for k, v in gm.items()}
    if isinstance(km, dict):
        cfg.keyboard_mapping = {int(k): v for k, v in km.items()}

    return cfg
