from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from sleeper.config import load_config
from sleeper.input.base import InputBackend
from sleeper.session import SessionManager

log = logging.getLogger("sleeper")


def _create_input_backend(config) -> InputBackend:
    if config.input_backend == "gamepad":
        from sleeper.input.gamepad import GamepadInput
        return GamepadInput(
            device_path=config.input_device,
            button_mapping=config.gamepad_mapping,
            volume_axis=config.volume_dpad_axis,
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
    else:
        raise ValueError(f"Unknown input backend: {config.input_backend}")


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
    backend = _create_input_backend(config)
    backend.set_callback(session.handle_action)

    # Graceful shutdown on SIGTERM / SIGINT
    shutdown_event = threading.Event()

    def _shutdown(signum, frame):
        log.info("Received signal %d, shutting down...", signum)
        backend.stop()
        session.shutdown()
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Run input backend in a thread so we can wait for shutdown signal from main thread
    input_thread = threading.Thread(target=backend.run, daemon=True, name="input")
    input_thread.start()

    log.info("Sleeper is ready. Waiting for input...")

    # Block main thread until shutdown
    shutdown_event.wait()
    log.info("Goodbye")


if __name__ == "__main__":
    main()
