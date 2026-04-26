#!/usr/bin/env bash
# pair-echo.sh
#
# Interactive helper to pair an Amazon Echo device with this Raspberry Pi
# for A2DP playback (Pi -> Echo speaker), and install the auto-reconnect
# watchdog (`echo-bt-connect.service`).
#
# Run on the Pi:
#     sudo ./scripts/pair-echo.sh
#
# Put the Echo into pairing mode first (Alexa app -> Devices -> your Echo
# -> Bluetooth Devices -> Pair a New Device).
#
# See docs/pairing-echo-bluetooth.md for background.

set -euo pipefail

SINK_UUID="0000110b-0000-1000-8000-00805f9b34fb"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHDOG_SRC="${SCRIPT_DIR}/echo-bt-connect.sh"
SERVICE_SRC="${SCRIPT_DIR}/echo-bt-connect.service"
WATCHDOG_DST="/usr/local/bin/echo-bt-connect.sh"
SERVICE_DST="/etc/systemd/system/echo-bt-connect.service"
SCAN_SECONDS="${SCAN_SECONDS:-25}"

log()  { printf '\e[1;34m[pair-echo]\e[0m %s\n' "$*"; }
warn() { printf '\e[1;33m[pair-echo]\e[0m %s\n' "$*" >&2; }
die()  { printf '\e[1;31m[pair-echo]\e[0m %s\n' "$*" >&2; exit 1; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        die "This script must be run as root (try: sudo $0)"
    fi
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

check_prereqs() {
    require_cmd bluetoothctl
    require_cmd dbus-send
    require_cmd busctl
    require_cmd bluealsa-cli

    if ! systemctl is-active --quiet bluetooth.service; then
        log "Starting bluetooth.service..."
        systemctl start bluetooth.service
    fi

    if ! systemctl is-active --quiet bluealsa.service; then
        log "Starting bluealsa.service..."
        systemctl start bluealsa.service || die \
            "bluealsa.service is not running. Install bluez-alsa-utils and try again."
    fi

    if ! systemctl cat bluealsa.service 2>/dev/null | grep -q 'a2dp-sink'; then
        warn "bluealsa.service does not advertise the a2dp-sink profile."
        warn "Edit its ExecStart to include '-p a2dp-source -p a2dp-sink' if pairing fails."
    fi
}

# Print discovered Echo devices as "MAC<TAB>Name".
discover_echoes() {
    local seconds="$1"
    log "Scanning for Echo devices for ${seconds}s. Make sure your Echo is in pairing mode." >&2
    # Run a scripted bluetoothctl session.
    {
        printf 'power on\nagent on\ndefault-agent\nscan on\n'
        sleep "$seconds"
        printf 'scan off\ndevices\nquit\n'
    } | bluetoothctl >/tmp/pair-echo.scan 2>&1 || true

    # bluetoothctl prints lines like:  Device AA:BB:CC:DD:EE:FF Some Name
    grep -E '^(\[NEW\] )?Device [0-9A-F:]{17} ' /tmp/pair-echo.scan \
        | sed -E 's/^\[NEW\] //' \
        | awk '{ mac=$2; $1=""; $2=""; sub(/^  */,""); print mac"\t"$0 }' \
        | grep -iE 'echo|alexa' \
        | sort -u
}

choose_echo() {
    local list="$1"
    local count
    count="$(printf '%s\n' "$list" | grep -c .)"

    if [[ "$count" -eq 0 ]]; then
        die "No Echo devices found. Confirm pairing mode in the Alexa app and retry."
    fi

    if [[ "$count" -eq 1 ]]; then
        printf '%s\n' "$list" | head -n1 | awk -F'\t' '{print $1}'
        return
    fi

    log "Multiple Echo devices found. Pick one:" >&2
    local i=1
    while IFS=$'\t' read -r mac name; do
        printf '  %d) %s  %s\n' "$i" "$mac" "$name" >&2
        i=$((i + 1))
    done <<<"$list"

    local choice
    read -rp "Selection [1-${count}]: " choice </dev/tty
    [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= count )) \
        || die "Invalid selection."

    printf '%s\n' "$list" | sed -n "${choice}p" | awk -F'\t' '{print $1}'
}

pair_and_trust() {
    local mac="$1"
    log "Pairing with $mac..."
    {
        printf 'power on\nagent on\ndefault-agent\n'
        printf 'pair %s\n' "$mac"
        sleep 8
        printf 'trust %s\n' "$mac"
        sleep 1
        printf 'quit\n'
    } | bluetoothctl >/tmp/pair-echo.pair 2>&1 || true

    # Verify with bluetoothctl info
    if ! bluetoothctl info "$mac" 2>/dev/null | grep -q 'Paired: yes'; then
        cat /tmp/pair-echo.pair >&2 || true
        die "Failed to pair with $mac"
    fi
    log "Paired and trusted: $mac"
}

force_sink_profile() {
    local mac="$1"
    local dev_path="/org/bluez/hci0/dev_${mac//:/_}"
    local pcm_path="/org/bluealsa/hci0/dev_${mac//:/_}/a2dpsrc/sink"

    log "Forcing A2DP Sink profile..."

    # Disconnect any existing (likely wrong-role) connection.
    dbus-send --system --print-reply --dest=org.bluez "$dev_path" \
        org.bluez.Device1.Disconnect >/dev/null 2>&1 || true
    sleep 2

    # Try a few times - the Echo can be slow to accept ConnectProfile right
    # after pairing.
    local attempt
    for attempt in 1 2 3 4 5; do
        if dbus-send --system --print-reply --reply-timeout=15000 \
            --dest=org.bluez "$dev_path" \
            org.bluez.Device1.ConnectProfile string:"$SINK_UUID" \
            >/dev/null 2>&1
        then
            sleep 2
            if bluealsa-cli list-pcms 2>/dev/null | grep -Fq "$pcm_path"; then
                log "A2DP sink ready: $pcm_path"
                return 0
            fi
        fi
        warn "ConnectProfile attempt ${attempt}/5 didn't produce the sink PCM yet, retrying..."
        sleep 3
    done

    die "Could not establish A2DP sink profile. Check 'journalctl -u bluetooth -n 50'."
}

install_watchdog() {
    local mac="$1"

    [[ -f "$WATCHDOG_SRC" ]] || die "Missing $WATCHDOG_SRC (run from repo root)"
    [[ -f "$SERVICE_SRC"  ]] || die "Missing $SERVICE_SRC  (run from repo root)"

    log "Installing watchdog with MAC=$mac..."
    local tmp
    tmp="$(mktemp)"
    # Substitute the MAC into the script template.
    sed -E "s|^MAC=.*|MAC=\"$mac\"|" "$WATCHDOG_SRC" >"$tmp"

    install -m 0755 "$tmp" "$WATCHDOG_DST"
    rm -f "$tmp"
    install -m 0644 "$SERVICE_SRC" "$SERVICE_DST"
    systemctl daemon-reload
    systemctl enable --now echo-bt-connect.service
    log "echo-bt-connect.service is active."
}

print_summary() {
    local mac="$1"
    cat <<EOF

----------------------------------------------------------------------
 Pairing complete.

 Echo MAC:           $mac
 BlueALSA PCM:       bluealsa:DEV=$mac,PROFILE=a2dp
 Watchdog service:   echo-bt-connect.service
 Watchdog script:    $WATCHDOG_DST

 Test playback:
   aplay -D bluealsa:DEV=$mac,PROFILE=a2dp \\
         /usr/share/sounds/alsa/Front_Center.wav

 Wire Sleeper to it (in /etc/sleeper/config.yaml):
   audio_output: bluealsa:DEV=$mac,PROFILE=a2dp
   sudo systemctl restart sleeper.service

 Tail the watchdog:
   journalctl -u echo-bt-connect.service -f
----------------------------------------------------------------------
EOF
}

main() {
    require_root
    check_prereqs

    local mac="${1:-}"

    if [[ -z "$mac" ]]; then
        local list
        list="$(discover_echoes "$SCAN_SECONDS")"
        mac="$(choose_echo "$list")"
    fi

    [[ "$mac" =~ ^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$ ]] \
        || die "MAC '$mac' is not a valid Bluetooth address."
    mac="${mac^^}"

    pair_and_trust "$mac"
    force_sink_profile "$mac"
    install_watchdog "$mac"
    print_summary "$mac"
}

main "$@"
