# Mounting a NAS Drive on Raspberry Pi

This guide explains how to mount a network storage drive so Sleeper can read story files from your NAS.

## Prerequisites

```bash
# For SMB/CIFS shares (Synology, QNAP, Windows shares, most consumer NAS)
sudo apt install cifs-utils

# For NFS shares
sudo apt install nfs-common
```

## 1. Create a Mount Point

```bash
sudo mkdir -p /mnt/nas/stories
```

## 2. Test the Mount Manually

### SMB/CIFS (most common)

```bash
# Basic mount (replace values with your own)
sudo mount -t cifs //NAS_IP/share_name/stories /mnt/nas/stories \
  -o username=YOUR_USER,password=YOUR_PASS,uid=$(id -u sleeper),gid=$(id -g sleeper),ro
```

**Example** for a Synology NAS at `192.168.1.50` with a shared folder called `media`:

```bash
sudo mount -t cifs //192.168.1.50/media/stories /mnt/nas/stories \
  -o username=baris,password=secret,uid=$(id -u sleeper),gid=$(id -g sleeper),ro
```

### NFS

```bash
sudo mount -t nfs NAS_IP:/volume1/stories /mnt/nas/stories -o ro
```

Verify it worked:

```bash
ls /mnt/nas/stories/
# You should see your .mp3 files
```

## 3. Store Credentials Securely (SMB only)

Don't put your password in `/etc/fstab`. Use a credentials file instead:

```bash
sudo nano /etc/nas-credentials
```

Add:

```
username=YOUR_USER
password=YOUR_PASS
```

Lock it down:

```bash
sudo chmod 600 /etc/nas-credentials
sudo chown root:root /etc/nas-credentials
```

## 4. Mount Automatically at Boot

Add an entry to `/etc/fstab`:

### SMB/CIFS

```
//NAS_IP/share_name/stories  /mnt/nas/stories  cifs  credentials=/etc/nas-credentials,uid=sleeper,gid=sleeper,ro,_netdev,x-systemd.automount,x-systemd.after=network-online.target  0  0
```

### NFS

```
NAS_IP:/volume1/stories  /mnt/nas/stories  nfs  ro,_netdev,x-systemd.automount,x-systemd.after=network-online.target  0  0
```

Key options explained:

| Option | Purpose |
|---|---|
| `ro` | Read-only — Sleeper only needs to read MP3 files |
| `_netdev` | Wait for network before mounting |
| `x-systemd.automount` | Mount on first access (don't block boot if NAS is down) |
| `x-systemd.after=network-online.target` | Ensure network is up first |
| `uid=sleeper,gid=sleeper` | Files appear owned by the sleeper user (SMB only) |

Test the fstab entry without rebooting:

```bash
sudo mount -a
ls /mnt/nas/stories/
```

## 5. Update Sleeper Config

Point `stories_dir` to the mount in `config.yaml`:

```yaml
stories_dir: /mnt/nas/stories
```

## 6. Ensure Sleeper Waits for the Mount

The systemd service should start after the mount is available. Add the mount dependency to the service:

```bash
sudo systemctl edit sleeper
```

Add:

```ini
[Unit]
After=mnt-nas-stories.mount
Wants=mnt-nas-stories.mount
```

> **Note:** The mount unit name is the mount path with `/` replaced by `-` and the leading slash removed. So `/mnt/nas/stories` becomes `mnt-nas-stories.mount`.

## Troubleshooting

**Mount hangs or times out:**
- Verify the NAS IP is reachable: `ping NAS_IP`
- Check the share name is correct: `smbclient -L //NAS_IP -U YOUR_USER`

**Permission denied:**
- Double-check credentials in `/etc/nas-credentials`
- Ensure the NAS share allows the user read access

**Stories not found after reboot:**
- Check mount status: `mount | grep nas`
- Check systemd mount: `systemctl status mnt-nas-stories.mount`
- Try manual mount: `sudo mount /mnt/nas/stories`

**NAS goes to sleep / disconnects:**
- Use `x-systemd.automount` (already in the fstab line above) — it will remount on access
- Set a shorter timeout: add `x-systemd.idle-timeout=60` to fstab options
