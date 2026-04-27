from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from sleeper.config import load_config
from sleeper.display.base import DisplayBackend
from sleeper.display.screen_power import ScreenPower
from sleeper.input.base import InputBackend
from sleeper.session import SessionManager

log = logging.getLogger("sleeper")


def _create_input_backend(config, screen_power: ScreenPower | None = None) -> InputBackend:
    if config.input_backend == "gamepad":
        from sleeper.input.gamepad import GamepadInput
        return GamepadInput(
            device_path=config.input_device,
            button_mapping=config.gamepad_mapping,
            long_press_seconds=config.long_press_seconds,
        )
    elif config.input_backend == "gpio":
        from sleeper.input.gpio import GpioInput
        pin_map = {
            "start_story": config.gpio_pins.start_story,
            "skip": config.gpio_pins.skip,
            "volume_up": config.gpio_pins.volume_up,
            "volume_down": config.gpio_pins.volume_down,
            "pause_resume": config.gpio_pins.pause_resume,
            "stop": config.gpio_pins.stop,
        }
        return GpioInput(
            pin_mapping=pin_map,
            long_press_seconds=config.long_press_seconds,
        )
    elif config.input_backend == "keyboard":
        from sleeper.input.keyboard import KeyboardInput
        return KeyboardInput(
            device_path=config.input_device,
            key_mapping=config.keyboard_mapping,
            long_press_seconds=config.long_press_seconds,
        )
    elif config.input_backend == "stdin":
        from sleeper.input.stdin import StdinInput
        return StdinInput()
    elif config.input_backend == "none":
        from sleeper.input.none import NoneInput
        return NoneInput()
    elif config.input_backend == "touchscreen":
        from sleeper.input.touchscreen import TouchscreenInput
        return TouchscreenInput(
            device_path=config.display_touch_device,
            screen_width=config.display_width,
            screen_height=config.display_height,
            raw_x_min=config.touch_raw_x_min,
            raw_x_max=config.touch_raw_x_max,
            raw_y_min=config.touch_raw_y_min,
            raw_y_max=config.touch_raw_y_max,
            swap_xy=config.touch_swap_xy,
            invert_x=config.touch_invert_x,
            invert_y=config.touch_invert_y,
            screen_power=screen_power,
        )
    else:
        raise ValueError(f"Unknown input backend: {config.input_backend}")


def _create_display_backend(config, screen_power: ScreenPower | None = None) -> DisplayBackend:
    if config.display_backend == "pygame":
        from sleeper.display.pygame_fb import PygameFbDisplay
        return PygameFbDisplay(
            width=config.display_width,
            height=config.display_height,
            fb_device=config.display_fb_device,
            screen_power=screen_power,
        )
    else:  # "none"
        from sleeper.display.none import NoneDisplay
        return NoneDisplay()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sleeper — bedtime story + noise machine")
    parser.add_argument("--config", "-c", default="config.yaml", help="Path to config file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    log.info("Loading config from %s", args.config)
    config = load_config(args.config)
    log.info("Stories dir: %s", config.stories_path)
    log.info("Input backend: %s", config.input_backend)
    log.info("Noise type: %s, Stop time: %s", config.noise_type, config.stop_time)

    session = SessionManager(config)

    # Shared screen-power controller: only meaningful with the pygame display.
    screen_power: ScreenPower | None = None
    if config.display_backend == "pygame":
        screen_power = ScreenPower(
            idle_timeout=config.screen_idle_timeout,
            backlight_path=config.screen_backlight_path,
            fb_device=config.display_fb_device,
            backlight_gpio=config.screen_backlight_gpio if config.screen_backlight_gpio >= 0 else None,
            backlight_gpio_active_high=config.screen_backlight_gpio_active_high,
        )
        session.set_screen_power(screen_power)

    backend = _create_input_backend(config, screen_power)
    backend.set_callback(session.handle_action)

    display = _create_display_backend(config, screen_power)
    display.set_action_callback(session.handle_action)
    session.set_display(display)

    # Graceful shutdown on SIGTERM / SIGINT
    shutdown_event = threading.Event()

    def _shutdown(signum, frame):
        log.info("Received signal %d, shutting down...", signum)
        backend.stop()
        session.shutdown()
        display.stop()
        if screen_power is not None:
            screen_power.wake()
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Run input and display backends in threads
    input_thread = threading.Thread(target=backend.run, daemon=True, name="input")
    display_thread = threading.Thread(target=display.run, daemon=True, name="display")
    input_thread.start()
    display_thread.start()

    log.info("Sleeper is ready. Waiting for input...")

    # Block main thread until shutdown
    shutdown_event.wait()
    log.info("Goodbye")


if __name__ == "__main__":
    main()
