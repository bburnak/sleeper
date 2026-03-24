# Sleeper

A headless sleep-aid daemon for Raspberry Pi. Plays a randomly-selected bedtime story (MP3) followed by continuous noise (white, brown, or pink) until a configured wake time, with gradual fade-out.

## Features

- **Smart story selection** — picks from least-played stories to avoid repetition
- **Noise generation** — real-time white, brown, or pink noise (no audio files needed)
- **Crossfade** — smooth transition from story to noise
- **Scheduled stop** — noise fades out gradually before your configured wake time
- **Multiple input backends** — PS3 gamepad, GPIO buttons, or keyboard
- **Hotkeys** — start, skip, pause, volume, stop — all configurable
- **Systemd service** — auto-starts on boot, restarts on failure

## Requirements

- Raspberry Pi 3 (or any Linux system with ALSA)
- Python 3.10+
- System packages:

```bash
sudo apt update
sudo apt install libmpv2 portaudio19-dev libasound2-dev python3-pip
```

## Installation

```bash
# Clone the repo
cd /opt
sudo git clone <repo-url> sleeper
cd sleeper

# Create virtual environment and install dependencies
sudo apt install python3-full
python3 -m venv /opt/sleeper/venv
/opt/sleeper/venv/bin/pip install -r requirements.txt

# Create stories directory and add your MP3 files
mkdir -p ~/stories
# cp /path/to/your/stories/*.mp3 ~/stories/

# Copy and edit configuration
sudo mkdir -p /etc/sleeper
sudo cp config.yaml /etc/sleeper/config.yaml
# Edit /etc/sleeper/config.yaml to your liking
```

## Configuration

Edit `config.yaml` (see the file for all options with comments). Key settings:

| Setting             | Default       | Description                                    |
|---------------------|---------------|------------------------------------------------|
| `stories_dir`       | `~/stories`   | Directory containing MP3 story files           |
| `noise_type`        | `white`       | Type of noise: `white`, `brown`, or `pink`     |
| `story_volume`      | `60`          | Story playback volume (0-100)                  |
| `noise_volume`      | `30`          | Noise playback volume (0-100)                  |
| `stop_time`         | `07:00`       | When to stop noise (24h format)                |
| `fade_out_minutes`  | `5`           | Minutes before stop_time to begin fading out   |
| `crossfade_seconds` | `10`          | Duration of story → noise crossfade            |
| `input_backend`     | `gamepad`     | Input method: `gamepad`, `gpio`, or `keyboard` |

## Usage

### Manual Run

```bash
python3 -m sleeper --config config.yaml
# Add -v for debug logging
python3 -m sleeper -c config.yaml -v
```

### Controls

| Action          | Gamepad (PS3)     | GPIO        | Keyboard (dev)  |
|-----------------|-------------------|-------------|-----------------|
| Start story     | Start button      | Button 1    | Enter           |
| Skip to next    | △ (short press)   | Btn 2 short | N (short press) |
| Skip to noise   | △ (long press)    | Btn 2 long  | N (long press)  |
| Volume up       | D-pad up          | Button 3    | (configure)     |
| Volume down     | D-pad down        | Button 4    | (configure)     |
| Pause / resume  | ✕ button          | Button 5    | Space           |
| Stop session    | Select (hold 1.5s)| Button 6    | Q (hold 1.5s)   |

All button mappings are configurable in `config.yaml`.

## Systemd Service

```bash
# Create dedicated user
sudo useradd -r -s /usr/sbin/nologin -G audio,input,gpio,bluetooth sleeper
sudo mkdir -p /home/sleeper/.sleeper
sudo chown sleeper:sleeper /home/sleeper/.sleeper

# Install service
sudo cp systemd/sleeper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable sleeper
sudo systemctl start sleeper

# Check status
sudo systemctl status sleeper
sudo journalctl -u sleeper -f
```

## PS3 Controller Pairing

The PS3 (Sixaxis/DualShock 3) controller uses a non-standard Bluetooth protocol. Install `sixad`:

```bash
sudo apt install sixad
sudo sixad --start

# Press the PS button on the controller while connected via USB to pair
# After pairing, disconnect USB and press PS button to connect wirelessly
```

## GPIO Wiring

Connect momentary push buttons between the configured GPIO pins and GND. The internal pull-up resistors are used (active low). Default pin assignments:

| Function     | GPIO Pin |
|--------------|----------|
| Start story  | 17       |
| Skip         | 27       |
| Volume up    | 22       |
| Volume down  | 23       |
| Pause/resume | 24       |
| Stop         | 25       |

## Using Stories from a NAS

If your MP3 stories are on a network-attached storage (NAS) drive, see [docs/mounting-nas.md](docs/mounting-nas.md) for a step-by-step guide on mounting the share and configuring Sleeper to use it.

## How Story Selection Works

1. Scans `stories_dir` for `.mp3` files
2. Loads play history from `~/.sleeper/history.json`
3. Groups stories by play count
4. Picks randomly from the **least-played** group
5. A story counts as "listened" once 10% has been played (configurable)
6. When skipping, the current story is excluded from the next pick

## Architecture

```
sleeper/
├── config.yaml              # Configuration
├── sleeper/
│   ├── main.py              # Entry point & signal handling
│   ├── config.py            # YAML config loader
│   ├── session.py           # State machine orchestrator
│   ├── selector.py          # Story selection logic
│   ├── history.py           # Play history (JSON)
│   ├── audio/
│   │   ├── player.py        # MP3 playback (mpv)
│   │   └── noise.py         # Noise generation (numpy + sounddevice)
│   └── input/
│       ├── base.py          # Action enum & backend interface
│       ├── gamepad.py       # evdev gamepad input
│       ├── gpio.py          # gpiozero GPIO input
│       └── keyboard.py      # evdev keyboard input (dev/testing)
└── systemd/
    └── sleeper.service      # Systemd unit file
```

## License

MIT
