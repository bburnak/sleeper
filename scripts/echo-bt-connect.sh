#!/bin/bash
# Maintain an A2DP-source connection from this Pi to the Echo Dot so audio
# routed to bluealsa:DEV=$MAC,PROFILE=a2dp is delivered to the speaker.
#
# Echo is multi-role (audio source + sink). Plain `bluetoothctl connect`
# tends to land on the Echo's source role (wrong direction for playback).
# We explicitly request the Audio Sink (0x110b) profile via BlueZ
# ConnectProfile so BlueALSA exposes an a2dpsrc/sink PCM.
#
# The MAC= line below is rewritten by scripts/pair-echo.sh during install.
set -u

MAC="D8:FB:D6:CA:D8:1D"
ADAPTER_PATH="/org/bluez/hci0"
DEV_PATH="${ADAPTER_PATH}/dev_${MAC//:/_}"
SINK_UUID="0000110b-0000-1000-8000-00805f9b34fb"
PCM_PATH="/org/bluealsa/hci0/dev_${MAC//:/_}/a2dpsrc/sink"
RETRY_DELAY=15

is_connected() {
    busctl get-property org.bluez "$DEV_PATH" org.bluez.Device1 Connected 2>/dev/null \
        | awk '{print $2}' | grep -q true
}

# NOTE: must use `bluealsa-cli list-pcms`. The D-Bus method
# `org.bluealsa.Manager1.GetPCMs` does not exist in BlueALSA 4.x, so a
# busctl-based check always reports "missing" and the watchdog ends up
# reconnecting in a loop -- which makes the Echo announce
# "Now connected to <hostname>" every cycle. Don't change this.
has_a2dp_sink_pcm() {
    bluealsa-cli list-pcms 2>/dev/null | grep -Fq "$PCM_PATH"
}

power_on_adapter() {
    busctl set-property org.bluez "$ADAPTER_PATH" org.bluez.Adapter1 Powered b true 2>/dev/null || true
}

connect_sink_profile() {
    dbus-send --system --print-reply --reply-timeout=15000 \
        --dest=org.bluez "$DEV_PATH" \
        org.bluez.Device1.ConnectProfile string:"$SINK_UUID" >/dev/null 2>&1
}

echo "[echo-bt-connect] starting watchdog for $MAC"

while true; do
    power_on_adapter
    if has_a2dp_sink_pcm; then
        sleep "$RETRY_DELAY"
        continue
    fi

    if is_connected; then
        # Connected on wrong role; cycle the connection.
        dbus-send --system --print-reply --dest=org.bluez "$DEV_PATH" \
            org.bluez.Device1.Disconnect >/dev/null 2>&1 || true
        sleep 3
    fi

    echo "[echo-bt-connect] attempting A2DP sink profile connect"
    if connect_sink_profile; then
        sleep 3
        if has_a2dp_sink_pcm; then
            echo "[echo-bt-connect] A2DP sink ready"
        fi
    else
        echo "[echo-bt-connect] connect attempt failed"
    fi

    sleep "$RETRY_DELAY"
done
