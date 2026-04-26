# Pairing an Amazon Echo with a Raspberry Pi for Audio Output

This guide documents how to pair an Amazon Echo device (Echo Dot, etc.) with a
Raspberry Pi running Debian/Raspberry Pi OS so Sleeper can stream audio to it
over Bluetooth A2DP.

## TL;DR

There is a helper script at [scripts/pair-echo.sh](../scripts/pair-echo.sh)
that does the whole flow end-to-end. Put your Echo in pairing mode (Alexa app →
Devices → your Echo → Bluetooth Devices → Pair a New Device) then run on the
Pi:

```bash
sudo ./scripts/pair-echo.sh
```

The rest of this document explains the gotchas.

## Background: why this is fiddlier than a phone

1. **Echo is a multi-role Bluetooth audio device.** It can be both an A2DP
   *source* (when streaming music from Alexa to a paired speaker) and an A2DP
   *sink* (when something else streams audio to it). When you do a plain
   `bluetoothctl connect`, BlueZ tends to land on the Echo's *source* role,
   which is the wrong direction for our use case (Pi → Echo). You will see
   the Echo as `Connected: yes` but no playable PCM exists.
2. **Pi OS uses BlueALSA, not PulseAudio/PipeWire.** On a stock Pi OS
   install with `bluez-alsa-utils`, audio is routed via BlueALSA. You play
   to a Bluetooth device by pointing ALSA at a `bluealsa:DEV=...,PROFILE=a2dp`
   PCM. Pulse/PipeWire are not running.
3. **The Echo never auto-reconnects to the Pi.** If you disconnect, reboot,
   or the Echo "forgets" the link, you have to drive the reconnect from the
   Pi side. A small systemd watchdog handles this.

## Audio stack assumptions

This guide assumes:

- BlueZ (`bluetoothd`) and `bluez-alsa-utils` (`bluealsa.service`) are
  installed and running.
- `bluealsa.service` is started with both `a2dp-source` and `a2dp-sink`
  profiles. Verify with:

  ```bash
  systemctl cat bluealsa.service | grep ExecStart
  # Should include: -p a2dp-source -p a2dp-sink
  ```

- No PulseAudio/PipeWire user session is grabbing the BlueZ media endpoints.
  If `pactl info` prints anything useful, this guide does not apply.

If those preconditions don't hold, install the prerequisites first:

```bash
sudo apt install bluez bluez-alsa-utils alsa-utils
sudo systemctl enable --now bluetooth.service bluealsa.service
```

## Step 1: Pair the Echo

Put the Echo into pairing mode via the Alexa app. Then, on the Pi:

```bash
bluetoothctl
[bluetooth]# power on
[bluetooth]# agent on
[bluetooth]# default-agent
[bluetooth]# scan on
# Wait until you see "Echo Dot-XXX" with its MAC address.
[bluetooth]# scan off
[bluetooth]# pair  D8:FB:D6:CA:D8:1D
[bluetooth]# trust D8:FB:D6:CA:D8:1D
[bluetooth]# exit
```

Replace the MAC with your Echo's. If you have more than one Echo nearby, the
Alexa app's "Bluetooth Devices" screen will tell you which MAC just paired.

After this, `bluetoothctl info <MAC>` should show:

```
Paired:    yes
Trusted:   yes
Connected: yes   <-- but possibly on the wrong role; see next step
```

## Step 2: Force the A2DP **Sink** profile

This is the critical step. Even though the Echo is "Connected", BlueALSA may
not expose a sink PCM. Check:

```bash
bluealsa-cli list-pcms
```

You want to see a line like:

```
/org/bluealsa/hci0/dev_D8_FB_D6_CA_D8_1D/a2dpsrc/sink
```

If it's missing, explicitly request the Audio Sink profile (UUID `0x110b`) via
BlueZ's `Device1.ConnectProfile` D-Bus method:

```bash
MAC=D8:FB:D6:CA:D8:1D
DEV_PATH=/org/bluez/hci0/dev_${MAC//:/_}

# Disconnect the wrong-role connection first, if any.
dbus-send --system --print-reply --dest=org.bluez "$DEV_PATH" \
    org.bluez.Device1.Disconnect

sleep 2

# Connect specifically to the A2DP Sink profile.
dbus-send --system --print-reply --dest=org.bluez "$DEV_PATH" \
    org.bluez.Device1.ConnectProfile \
    string:0000110b-0000-1000-8000-00805f9b34fb
```

Re-run `bluealsa-cli list-pcms` and confirm the `a2dpsrc/sink` line appears.

## Step 3: Test playback

```bash
MAC=D8:FB:D6:CA:D8:1D
speaker-test -D bluealsa:DEV=$MAC,PROFILE=a2dp -c 2 -r 48000 -t sine -l 1
# Or play a WAV:
aplay -D bluealsa:DEV=$MAC,PROFILE=a2dp /usr/share/sounds/alsa/Front_Center.wav
```

You should hear sound from the Echo. The Echo will (annoyingly) say "Now
connected to <hostname>" once each time the link is re-established.

## Step 4: Auto-reconnect watchdog

The Echo will not initiate a connection back to the Pi after a reboot or
disconnect, so we run a small watchdog on the Pi that:

1. Powers the Bluetooth adapter on.
2. Checks whether `bluealsa-cli list-pcms` lists the Echo's `a2dpsrc/sink`
   PCM.
3. If not, calls `Device1.ConnectProfile` with the Audio Sink UUID.
4. Sleeps 15 seconds and repeats.

The script lives at `/usr/local/bin/echo-bt-connect.sh` and is run by the
systemd unit `echo-bt-connect.service`. Both are installed automatically by
[scripts/pair-echo.sh](../scripts/pair-echo.sh).

> **Important:** the PCM-presence check must use `bluealsa-cli list-pcms`.
> Older drafts of this script called the D-Bus method
> `org.bluealsa.Manager1.GetPCMs`, which **does not exist** in BlueALSA 4.x —
> the call always fails, the watchdog assumes the sink is missing, forces a
> reconnect every cycle, and the Echo announces "Now connected to ..." every
> ~25 seconds forever.

### Manual install of the watchdog

If you ever need to install it by hand:

```bash
sudo install -m 0755 scripts/echo-bt-connect.sh /usr/local/bin/echo-bt-connect.sh
sudo install -m 0644 scripts/echo-bt-connect.service /etc/systemd/system/echo-bt-connect.service
sudo systemctl daemon-reload
sudo systemctl enable --now echo-bt-connect.service
```

Edit `MAC=` at the top of the script if your Echo's address differs.

## Step 5: Wire Sleeper to the Echo

Set Sleeper's `audio_output` to the BlueALSA PCM string for your Echo:

```yaml
# /etc/sleeper/config.yaml
audio_output: bluealsa:DEV=D8:FB:D6:CA:D8:1D,PROFILE=a2dp
```

Then restart Sleeper:

```bash
sudo systemctl restart sleeper.service
```

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `aplay: ... PCM not found` | Connected on wrong (source) role | Re-run Step 2 |
| Echo announces "Now connected" repeatedly | Watchdog using `GetPCMs` D-Bus call | Reinstall current script (uses `bluealsa-cli`) |
| `bluealsa-cli` missing | Wrong package | `sudo apt install bluez-alsa-utils` |
| `Connection refused (111)` from `bluetoothd` for `a2dp-source` | The Echo is the source; you tried to connect that profile from the Pi | Ignore — the script targets the *sink* profile only |
| Choppy audio | 2.4 GHz Wi-Fi + Bluetooth coexistence on Pi 3 | Switch the Pi to 5 GHz Wi-Fi |
| `Device1.ConnectProfile` returns `org.bluez.Error.Failed` | Echo not in range or not powered on | Wake the Echo, retry |

## Useful one-liners

```bash
# Show the Echo's connection state
bluetoothctl info D8:FB:D6:CA:D8:1D | grep -E 'Paired|Trusted|Connected'

# Show all BlueALSA PCMs
bluealsa-cli list-pcms

# Tail the watchdog
journalctl -u echo-bt-connect.service -f

# Cycle the connection by hand
sudo systemctl restart echo-bt-connect.service
```

## References

- BlueZ D-Bus API: `/usr/share/doc/bluez/dbus-api/` or
  <https://github.com/bluez/bluez/tree/master/doc>
- BlueALSA: <https://github.com/arkq/bluez-alsa>
- A2DP Sink profile UUID: `0000110b-0000-1000-8000-00805f9b34fb`
