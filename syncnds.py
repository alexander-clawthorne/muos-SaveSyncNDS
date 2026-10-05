#!/usr/bin/env python3
"""Backend for 3DS Save Sync: move DraStic saves to/from a 3DS running ftpd.

The 3DS side follows TWiLight Menu++ / nds-bootstrap with SAVE_LOCATION = 0:
the save lives in a "saves" folder next to the ROM, named after the ROM.
TWiLight's per-game "save number" picks the file: slot 0 is <rom>.sav,
slot N (1-9) is <rom>.savN. DraStic has one save per game.

On this device DraStic saves either as a raw <rom>.sav (drastic-legacy) or as
<rom>.dsv, raw data plus a DeSmuME footer (drastic-trngaje,
backup_use_sav_format = 0). Push reads whichever is newer. Pull always writes a
raw .sav and moves any .dsv into the backup folder: with no .dsv present
drastic-trngaje imports the .sav (seen with Custom Robo, 2026-09-29).

    sync3ds.py ping   <ip>
    sync3ds.py status <ip> <rom base name>
    sync3ds.py push   <ip> <rom base name> [slot]     H -> 3DS
    sync3ds.py pull   <ip> <rom base name> [slot]     3DS -> H

Output is key=value lines for the LOVE front end. The last lines are always
ok=1|0 and msg=<one line for the result screen>.
"""
import ftplib
import io
import os
import re
import sys
import time

PORT = 5000
USER = "a"
PASSWORD = "a"
TIMEOUT = 10
SLOTS = 10

LOCAL_SAVE_DIR = "/mnt/mmc/MUOS/save/drastic/backup"
BACKUP_DIR = LOCAL_SAVE_DIR + "/sync3ds_backups"
BACKUPS_KEPT = 10
REMOTE_ROM_ROOT = "/roms/nds"
GAMESETTINGS_DIR = "/_nds/TWiLightMenu/gamesettings"


class SyncError(Exception):
    pass


def emit(key, value):
    print("%s=%s" % (key, str(value).replace("\n", " ")), flush=True)


def connect(ip):
    ftp = ftplib.FTP()
    try:
        ftp.connect(ip, PORT, timeout=TIMEOUT)
        ftp.login(USER, PASSWORD)
    except (OSError, ftplib.Error) as exc:
        raise SyncError("Cannot reach 3DS at %s:%d - is ftpd open? (%s)" % (ip, PORT, exc))
    return ftp


def close(ftp):
    try:
        ftp.quit()
    except (OSError, ftplib.Error):
        pass


# ftpd on the 3DS reports a missing file as "450 No such file or directory"
# (a temporary error), where most servers use 550.
MISSING = (ftplib.error_perm, ftplib.error_temp)


def list_names(ftp, path):
    """Entry names in a remote dir; [] if it does not exist."""
    try:
        names = ftp.nlst(path)
    except MISSING:
        return []
    # ftpd may return bare names or full paths
    return [n.rstrip("/").rsplit("/", 1)[-1] for n in names]


def find_remote_rom_dir(ftp, base):
    """Directory on the 3DS holding <base>.nds, or None."""
    rom = base + ".nds"
    first = base[:1].upper()
    top = list_names(ftp, REMOTE_ROM_ROOT)
    if rom in top:
        return REMOTE_ROM_ROOT
    # Letter folders first (the usual layout), then everything else.
    subdirs = [n for n in top if "." not in n and n != "saves"]
    subdirs.sort(key=lambda n: (n.upper() != first, n))
    for sub in subdirs:
        if rom in list_names(ftp, REMOTE_ROM_ROOT + "/" + sub):
            return REMOTE_ROM_ROOT + "/" + sub
    return None


def remote_save_dir(ftp, base):
    rom_dir = find_remote_rom_dir(ftp, base)
    if rom_dir is None:
        raise SyncError("ROM not found on 3DS under %s: %s.nds" % (REMOTE_ROM_ROOT, base))
    return rom_dir + "/saves"


def slot_name(base, slot):
    return base + ".sav" + ("" if slot == 0 else str(slot))


def remote_read(ftp, path):
    buf = io.BytesIO()
    try:
        ftp.retrbinary("RETR " + path, buf.write)
    except MISSING:
        return None
    return buf.getvalue()


def remote_size(ftp, path):
    try:
        size = ftp.size(path)
    except ftplib.Error:
        return -1
    return -1 if size is None else size


def slot_sizes(ftp, rdir, base):
    """{slot: size} for every save slot file that exists on the 3DS."""
    names = set(list_names(ftp, rdir))
    ftp.voidcmd("TYPE I")
    return {slot: remote_size(ftp, rdir + "/" + slot_name(base, slot))
            for slot in range(SLOTS) if slot_name(base, slot) in names}


def twilight_slot(ftp, base):
    """Save number TWiLight is currently set to use for this game (0 if unset)."""
    for name in (base + ".nds.ini", base + ".ini"):
        text = remote_read(ftp, GAMESETTINGS_DIR + "/" + name)
        if text is None:
            continue
        match = re.search(r"^\s*SAVE_NUMBER\s*=\s*(-?\d+)", text.decode("utf-8", "replace"), re.M)
        if match:
            slot = int(match.group(1))
            return slot if 0 <= slot < SLOTS else 0
        return 0
    return 0


def local_read(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


DSV_MARKER = b"|<--Snip above here to create a raw sav"


def local_paths(base):
    return (os.path.join(LOCAL_SAVE_DIR, base + ".sav"),
            os.path.join(LOCAL_SAVE_DIR, base + ".dsv"))


def local_save(base):
    """(raw save bytes, format, path) for the newest local save, or (None, None, None)."""
    found = []
    for path, fmt in zip(local_paths(base), ("sav", "dsv")):
        if os.path.exists(path):
            found.append((os.path.getmtime(path), path, fmt))
    if not found:
        return None, None, None
    _, path, fmt = max(found)
    data = local_read(path)
    if fmt == "dsv":
        cut = data.rfind(DSV_MARKER)
        if cut < 0:
            raise SyncError("Unrecognised .dsv format: " + os.path.basename(path))
        data = data[:cut]
    return data, fmt, path


def uniform(data):
    return len(data) == 0 or data.count(data[:1]) == len(data)


def fit(data, target_len, pad_byte):
    """Resize a save to the size the other side expects.

    nds-bootstrap and DraStic can disagree on save size (e.g. 512 KB vs 64 KB)
    while the game only uses the start. Grow by padding; shrink only when the
    cut-off tail carries no data.
    """
    if target_len is None or target_len == len(data):
        return data
    if target_len > len(data):
        return data + bytes([pad_byte]) * (target_len - len(data))
    if uniform(data[target_len:]):
        return data[:target_len]
    return data


def backup(base, side, data, ext="sav"):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = "%s/%s.%s.%s.%s" % (BACKUP_DIR, base, side, stamp, ext)
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    prefix = "%s.%s." % (base, side)
    old = sorted(n for n in os.listdir(BACKUP_DIR) if n.startswith(prefix))
    for name in old[:-BACKUPS_KEPT]:
        os.remove(os.path.join(BACKUP_DIR, name))
    return path


def kb(n):
    return "%d KB" % (n // 1024) if n >= 1024 else "%d B" % n


def slot_label(slot):
    return "slot %d (%s)" % (slot, ".sav" if slot == 0 else ".sav%d" % slot)


def cmd_ping(ip):
    ftp = connect(ip)
    close(ftp)
    return "Connected to 3DS at %s:%d" % (ip, PORT)


def cmd_status(ip, base):
    local, fmt, _ = local_save(base)
    emit("local_size", -1 if local is None else len(local))
    emit("local_format", fmt or "none")
    ftp = connect(ip)
    try:
        rdir = remote_save_dir(ftp, base)
        emit("remote_dir", rdir)
        emit("twilight_slot", twilight_slot(ftp, base))
        sizes = slot_sizes(ftp, rdir, base)
        for slot, size in sorted(sizes.items()):
            emit("slot_%d" % slot, size)
    finally:
        close(ftp)
    return "%d save slot(s) on the 3DS" % len(sizes)


def cmd_push(ip, base, slot):
    local, _, _ = local_save(base)
    if local is None:
        raise SyncError("No DraStic save on this device for that game")
    ftp = connect(ip)
    try:
        rdir = remote_save_dir(ftp, base)
        rpath = rdir + "/" + slot_name(base, slot)
        old = remote_read(ftp, rpath)
        if old is not None:
            emit("backup", backup(base, "3ds%d" % slot, old))
            data = fit(local, len(old), old[-1] if old else 0xFF)
        else:
            # New slot: match the size nds-bootstrap uses for this game's other slots.
            sizes = [s for s in slot_sizes(ftp, rdir, base).values() if s > 0]
            data = fit(local, max(sizes), 0x00) if sizes else local
            try:
                ftp.mkd(rdir)
            except MISSING:
                pass  # already exists
        ftp.storbinary("STOR " + rpath, io.BytesIO(data))
        if remote_read(ftp, rpath) != data:
            raise SyncError("Upload did not verify - 3DS copy differs. Backup kept.")
    finally:
        close(ftp)
    note = ""
    if len(data) > len(local):
        note = " (padded from %s to the size nds-bootstrap uses)" % kb(len(local))
    return "Sent %s to 3DS %s and verified%s" % (kb(len(data)), slot_label(slot), note)


def cmd_pull(ip, base, slot):
    lpath, dsv_path = local_paths(base)
    ftp = connect(ip)
    try:
        rdir = remote_save_dir(ftp, base)
        remote = remote_read(ftp, rdir + "/" + slot_name(base, slot))
    finally:
        close(ftp)
    if remote is None:
        raise SyncError("No save in 3DS %s for that game" % slot_label(slot))
    old, _, _ = local_save(base)
    data = remote if old is None else fit(remote, len(old), 0xFF)
    # Back up both formats before touching either; the .dsv is moved away so
    # DraStic picks up the new .sav instead of its own older copy.
    backups = []
    if os.path.exists(lpath):
        backups.append(backup(base, "h", local_read(lpath)))
    if os.path.exists(dsv_path):
        backups.append(backup(base, "h", local_read(dsv_path), "dsv"))
    if backups:
        emit("backup", " + ".join(backups))
    os.makedirs(LOCAL_SAVE_DIR, exist_ok=True)
    tmp = lpath + ".sync3ds.tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, lpath)
    if local_read(lpath) != data:
        raise SyncError("Write did not verify - local copy differs. Backup kept.")
    if os.path.exists(dsv_path):
        os.remove(dsv_path)  # already copied into BACKUP_DIR above
    note = ""
    if len(data) < len(remote):
        note = " (3DS file is %s; the rest is empty padding nds-bootstrap adds)" % kb(len(remote))
    return "Saved %s from 3DS %s and verified%s" % (kb(len(data)), slot_label(slot), note)


def main(argv):
    if len(argv) < 3 or argv[1] not in ("ping", "status", "push", "pull"):
        raise SyncError("usage: sync3ds.py ping|status|push|pull <ip> [rom base name] [slot]")
    command, ip = argv[1], argv[2]
    if command == "ping":
        return cmd_ping(ip)
    if len(argv) < 4:
        raise SyncError("missing rom base name")
    base = argv[3]
    if command == "status":
        return cmd_status(ip, base)
    slot = int(argv[4]) if len(argv) > 4 else 0
    if not 0 <= slot < SLOTS:
        raise SyncError("slot must be 0-%d" % (SLOTS - 1))
    return {"push": cmd_push, "pull": cmd_pull}[command](ip, base, slot)


if __name__ == "__main__":
    try:
        message = main(sys.argv)
        emit("ok", 1)
    except SyncError as exc:
        message = str(exc)
        emit("ok", 0)
    except Exception as exc:  # anything unexpected still reaches the screen
        message = "%s: %s" % (type(exc).__name__, exc)
        emit("ok", 0)
    emit("msg", message)
