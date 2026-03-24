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

First, find the numeric uid/gid for the sleeper user:

```bash
id sleeper_user_name
# Example output: uid=1000(sleeper) gid=1000(sleeper) ...
```

Then add to `/etc/fstab` (replace uid/gid with your values):

```
//NAS_IP/share_name/stories  /mnt/nas/stories  cifs  credentials=/etc/nas-credentials,uid=1000,gid=1000,ro,nofail,_netdev  0  0
```

### NFS

```
NAS_IP:/volume1/stories  /mnt/nas/stories  nfs  ro,nofail,_netdev  0  0
```

Key options explained:

| Option | Purpose |
|---|---|
| `ro` | Read-only — Sleeper only needs to read MP3 files |
| `_netdev` | Wait for network before mounting |
| `nofail` | Don't block boot if NAS is unreachable |
| `uid=1000,gid=1000` | Files appear owned by the sleeper user (SMB only; use numeric IDs from `id sleeper`) |

Test the fstab entry without rebooting:

```bash
sudo systemctl daemon-reload
sudo mount /mnt/nas/stories
ls /mnt/nas/stories/
```

> **Optional: systemd automount** — If you want the share to mount lazily on first access
> (instead of at boot), you can add `noauto,x-systemd.automount` to the options.
> Note that `x-systemd.*` options are only understood by systemd, not by `mount -a`.

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
