#!/usr/bin/env python3
"""Backend for NDS Save Sync: move DraStic saves to/from a TWiLight SD card.

The card can be reached two ways, and everything above the transport is
identical:

  ftp   a console running ftpd on the network (3DS, DSi, ...)
  sd    the console's SD card physically inserted in this device

A target is any card following the TWiLight Menu++ / nds-bootstrap layout with
SAVE_LOCATION = 0: the save lives in a "saves" folder next to the ROM, named
after the ROM. Confirmed on a 3DS (letter folders, /roms/nds/<L>/) and a DSi XL
(flat, /roms/nds/) - the ROM lookup handles either without configuration.

TWiLight's per-game "save number" picks the file: slot 0 is <rom>.sav,
slot N (1-9) is <rom>.savN. DraStic has one save per game.

On this device DraStic saves either as a raw <rom>.sav (drastic-legacy) or as
<rom>.dsv, raw data plus a DeSmuME footer (drastic-trngaje,
backup_use_sav_format = 0). Push reads whichever is newer. Pull always writes a
raw .sav and moves any .dsv into the backup folder: with no .dsv present
drastic-trngaje imports the .sav (seen with Custom Robo, 2026-09-29).

    syncnds.py [--tag=T] [--name=N] ping   <where>
    syncnds.py [--tag=T] [--name=N] status <where> <rom base name>
    syncnds.py [--tag=T] [--name=N] push   <where> <rom base name> [slot]
    syncnds.py [--tag=T] [--name=N] pull   <where> <rom base name> [slot]

<where> is an IP address for a networked console, or "sd:" to find an inserted
TWiLight card automatically, or "sd:/mnt/sdcard" to name one explicitly.

push sends this device's save to the target; pull brings the target's back.

--name is the console's display name for messages ("3DS", "DSi XL").
--tag is the short key used in backup filenames so each target's backups stay
distinguishable. It defaults to "3ds", which keeps backups made by the older
SaveSync3DS matching the names it already wrote.

Output is key=value lines for the LOVE front end. "step=" lines report progress
and are emitted while transfers run; the front end shows the most recent one.
The last lines are always ok=1|0 and msg=<one line for the result screen>.
"""
import ftplib
import glob
import io
import os
import re
import sys
import time

PORT = 5000
USER = "a"
PASSWORD = "a"
# Transfers to a DSi over the network are slow and highly variable: 85 KB/s
# measured from a wired PC, but 7 KB/s from the handheld when it is associated
# with a Wi-Fi extender on another band. The old 10s timeout was tighter than
# the work it was meant to allow. This is per socket operation, not per
# transfer. An sd target ignores it entirely.
TIMEOUT = 60
SLOTS = 10
PROGRESS_EVERY = 0.4        # seconds between step= lines during a transfer
SD_CHUNK = 256 * 1024

LOCAL_SAVE_DIR = "/mnt/mmc/MUOS/save/drastic/backup"
# Deliberately still sync3ds_backups: this directory already holds the existing
# backup history, and the pruning below only sees what it can list.
BACKUP_DIR = LOCAL_SAVE_DIR + "/sync3ds_backups"
BACKUPS_KEPT = 10
REMOTE_ROM_ROOT = "/roms/nds"
GAMESETTINGS_DIR = "/_nds/TWiLightMenu/gamesettings"

# A card is identified by TWiLight's own folder, which this device's muOS card
# and ROM card do not have - so detection cannot pick the wrong one.
CARD_MARKER = "_nds/TWiLightMenu"
CARD_HINTS = ("/mnt/sdcard", "/mnt/sdcard1", "/mnt/sdcard2", "/mnt/usb")

# Replaced by --name / --tag; the defaults keep older command lines working.
TARGET_NAME = "3DS"
TARGET_TAG = "3ds"


class SyncError(Exception):
    pass


def emit(key, value):
    print("%s=%s" % (key, str(value).replace("\n", " ")), flush=True)


def step(text):
    emit("step", text)
    # -2 means "no transfer in flight", so the front end hides the bar between
    # phases instead of leaving it stuck at the previous percentage.
    emit("pct", -2)


def kb(n):
    return "%d KB" % (n // 1024) if n >= 1024 else "%d B" % n


def progress_reporter(label, total):
    """Callback emitting step= and pct= as bytes move, at most every 0.4s.

    pct is -1 when the total is unknown, which tells the front end to show an
    indeterminate bar rather than a wrong one.
    """
    seen = [0]
    last = [0.0]

    def report(chunk):
        seen[0] += len(chunk)
        now = time.time()
        if now - last[0] >= PROGRESS_EVERY:
            last[0] = now
            if total:
                step("%s %s / %s" % (label, kb(seen[0]), kb(total)))
                emit("pct", min(100, 100 * seen[0] // total))
            else:
                step("%s %s" % (label, kb(seen[0])))
                emit("pct", -1)
    return report


# ---------------------------------------------------------------- transports
#
# Both expose the same small interface over the same logical paths
# ("/roms/nds/..."), so every path rule above this line is shared.


class FtpTarget(object):
    kind = "ftp"

    # ftpd reports a missing file as "450 No such file or directory"
    # (a temporary error), where most servers use 550.
    MISSING = (ftplib.error_perm, ftplib.error_temp)

    def __init__(self, ip):
        self.ip = ip
        self.ftp = None

    def describe(self):
        return "%s:%d" % (self.ip, PORT)

    def open(self):
        self.ftp = ftplib.FTP()
        try:
            self.ftp.connect(self.ip, PORT, timeout=TIMEOUT)
            self.ftp.login(USER, PASSWORD)
            self.ftp.voidcmd("TYPE I")
        except (OSError, ftplib.Error) as exc:
            raise SyncError("Cannot reach %s at %s:%d - is ftpd open? (%s)"
                            % (TARGET_NAME, self.ip, PORT, exc))
        return self

    def close(self):
        try:
            if self.ftp:
                self.ftp.quit()
        except (OSError, ftplib.Error):
            pass
        self.ftp = None

    def listdir(self, path):
        try:
            names = self.ftp.nlst(path)
        except self.MISSING:
            return []
        # ftpd may return bare names or full paths
        return [n.rstrip("/").rsplit("/", 1)[-1] for n in names]

    def read(self, path, label=None, total=None):
        buf = io.BytesIO()
        report = progress_reporter(label, total) if label else None

        def sink(chunk):
            buf.write(chunk)
            if report:
                report(chunk)
        try:
            self.ftp.retrbinary("RETR " + path, sink)
        except self.MISSING:
            return None
        return buf.getvalue()

    def write(self, path, data, label=None):
        report = progress_reporter(label, len(data)) if label else None
        self.ftp.storbinary("STOR " + path, io.BytesIO(data), callback=report)

    def size(self, path):
        try:
            value = self.ftp.size(path)
        except ftplib.Error:
            return -1
        return -1 if value is None else value

    def mkdir(self, path):
        try:
            self.ftp.mkd(path)
        except self.MISSING:
            pass  # already exists


class SdTarget(object):
    """The console's SD card, inserted in this device."""

    kind = "sd"

    def __init__(self, root=None):
        self.root = root or None

    def describe(self):
        return self.root or "card not found"

    def open(self):
        self.root = find_card(self.root)
        return self

    def close(self):
        pass

    def _real(self, path):
        return os.path.join(self.root, path.lstrip("/"))

    def listdir(self, path):
        try:
            return os.listdir(self._real(path))
        except OSError:
            return []

    def read(self, path, label=None, total=None):
        real = self._real(path)
        report = progress_reporter(label, total) if label else None
        try:
            with open(real, "rb") as handle:
                buf = io.BytesIO()
                while True:
                    chunk = handle.read(SD_CHUNK)
                    if not chunk:
                        break
                    buf.write(chunk)
                    if report:
                        report(chunk)
                return buf.getvalue()
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return None
        except OSError as exc:
            raise SyncError("Could not read from the card: %s" % exc)

    def write(self, path, data, label=None):
        real = self._real(path)
        report = progress_reporter(label, len(data)) if label else None
        tmp = real + ".syncnds.part"
        try:
            with open(tmp, "wb") as handle:
                for at in range(0, len(data), SD_CHUNK):
                    chunk = data[at:at + SD_CHUNK]
                    handle.write(chunk)
                    if report:
                        report(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, real)
        except OSError as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise SyncError("Could not write to the card: %s" % exc)

    def size(self, path):
        try:
            return os.path.getsize(self._real(path))
        except OSError:
            return -1

    def mkdir(self, path):
        try:
            os.makedirs(self._real(path), exist_ok=True)
        except OSError as exc:
            raise SyncError("Could not create %s on the card: %s" % (path, exc))


def looks_like_card(root):
    return os.path.isdir(os.path.join(root, CARD_MARKER))


def candidate_mounts():
    seen, out = set(), []
    for path in list(CARD_HINTS) + sorted(glob.glob("/mnt/*")) \
            + sorted(glob.glob("/media/*")) + sorted(glob.glob("/run/media/*/*")):
        if path not in seen and os.path.isdir(path):
            seen.add(path)
            out.append(path)
    return out


def find_card(explicit=None):
    """Mount point of an inserted TWiLight card, or raise."""
    if explicit:
        if looks_like_card(explicit):
            return explicit
        raise SyncError("No TWiLight card at %s (no %s folder there)"
                        % (explicit, CARD_MARKER))
    tried = candidate_mounts()
    for path in tried:
        if looks_like_card(path):
            return path
    raise SyncError("No TWiLight SD card found. Looked in: %s"
                    % (", ".join(tried) or "nothing mounted"))


def make_target(where):
    """"sd:", "sd:/mnt/sdcard" or an IP address."""
    if where.startswith("sd:"):
        return SdTarget(where[3:].strip() or None)
    if where == "sd":
        return SdTarget()
    return FtpTarget(where)


# ---------------------------------------------------------------- card paths


def find_remote_rom_dir(tgt, base):
    """Directory on the target holding <base>.nds, or None.

    Handles both layouts seen so far: ROMs directly under /roms/nds (the
    DSi XL) and ROMs in letter folders /roms/nds/<L>/ (the 3DS).
    """
    rom = base + ".nds"
    first = base[:1].upper()
    top = tgt.listdir(REMOTE_ROM_ROOT)
    if rom in top:
        return REMOTE_ROM_ROOT
    # Letter folders first (the usual layout), then everything else.
    subdirs = [n for n in top if "." not in n and n != "saves"]
    subdirs.sort(key=lambda n: (n.upper() != first, n))
    for sub in subdirs:
        if rom in tgt.listdir(REMOTE_ROM_ROOT + "/" + sub):
            return REMOTE_ROM_ROOT + "/" + sub
    return None


def remote_save_dir(tgt, base):
    rom_dir = find_remote_rom_dir(tgt, base)
    if rom_dir is None:
        raise SyncError("ROM not found on %s under %s: %s.nds"
                        % (TARGET_NAME, REMOTE_ROM_ROOT, base))
    return rom_dir + "/saves"


def slot_name(base, slot):
    return base + ".sav" + ("" if slot == 0 else str(slot))


def slot_sizes(tgt, rdir, base):
    """{slot: size} for every save slot file that exists on the target."""
    names = set(tgt.listdir(rdir))
    return {slot: tgt.size(rdir + "/" + slot_name(base, slot))
            for slot in range(SLOTS) if slot_name(base, slot) in names}


def twilight_slot(tgt, base):
    """Save number TWiLight is currently set to use for this game (0 if unset)."""
    for name in (base + ".nds.ini", base + ".ini"):
        text = tgt.read(GAMESETTINGS_DIR + "/" + name)
        if text is None:
            continue
        match = re.search(r"^\s*SAVE_NUMBER\s*=\s*(-?\d+)", text.decode("utf-8", "replace"), re.M)
        if match:
            slot = int(match.group(1))
            return slot if 0 <= slot < SLOTS else 0
        return 0
    return 0


# ---------------------------------------------------------------- this device


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


def slot_label(slot):
    return "slot %d (%s)" % (slot, ".sav" if slot == 0 else ".sav%d" % slot)


# ---------------------------------------------------------------- commands


def cmd_ping(where):
    tgt = make_target(where)
    step("Looking for the card" if tgt.kind == "sd" else "Connecting to " + TARGET_NAME)
    tgt.open()
    try:
        found = tgt.describe()
    finally:
        tgt.close()
    if tgt.kind == "sd":
        emit("card", found)
        return "Found the %s at %s" % (TARGET_NAME, found)
    return "Connected to %s at %s" % (TARGET_NAME, found)


def cmd_status(where, base):
    local, fmt, _ = local_save(base)
    emit("local_size", -1 if local is None else len(local))
    emit("local_format", fmt or "none")
    tgt = make_target(where)
    step("Looking for the card" if tgt.kind == "sd" else "Connecting to " + TARGET_NAME)
    tgt.open()
    try:
        emit("card", tgt.describe())
        step("Looking for the ROM")
        rdir = remote_save_dir(tgt, base)
        emit("remote_dir", rdir)
        step("Reading TWiLight settings")
        emit("twilight_slot", twilight_slot(tgt, base))
        step("Checking save slots")
        sizes = slot_sizes(tgt, rdir, base)
        for slot, size in sorted(sizes.items()):
            emit("slot_%d" % slot, size)
    finally:
        tgt.close()
    return "%d save slot(s) on the %s" % (len(sizes), TARGET_NAME)


def cmd_push(where, base, slot):
    local, _, _ = local_save(base)
    if local is None:
        raise SyncError("No DraStic save on this device for that game")
    tgt = make_target(where)
    step("Looking for the card" if tgt.kind == "sd" else "Connecting to " + TARGET_NAME)
    tgt.open()
    try:
        step("Looking for the ROM")
        rdir = remote_save_dir(tgt, base)
        rpath = rdir + "/" + slot_name(base, slot)
        existing = tgt.size(rpath)
        old = tgt.read(rpath, "Reading the current save",
                       existing if existing > 0 else None)
        if old is not None:
            step("Backing up the old save")
            emit("backup", backup(base, "%s%d" % (TARGET_TAG, slot), old))
            data = fit(local, len(old), old[-1] if old else 0xFF)
        else:
            # New slot: match the size nds-bootstrap uses for this game's other slots.
            sizes = [s for s in slot_sizes(tgt, rdir, base).values() if s > 0]
            data = fit(local, max(sizes), 0x00) if sizes else local
            tgt.mkdir(rdir)
        tgt.write(rpath, data, "Uploading" if tgt.kind == "ftp" else "Copying to the card")
    finally:
        tgt.close()

    # Verify through a fresh handle. Over FTP this matters: a session that
    # performed three data transfers back to back wedged with the control
    # socket still open. On a card it is just a reopen and costs nothing.
    step("Verifying")
    tgt = make_target(where)
    tgt.open()
    try:
        written = tgt.read(rpath, "Verifying", len(data))
    finally:
        tgt.close()
    if written != data:
        raise SyncError("Write did not verify - the %s copy differs. Backup kept."
                        % TARGET_NAME)

    note = ""
    if len(data) > len(local):
        note = " (padded from %s to the size nds-bootstrap uses)" % kb(len(local))
    return "Sent %s to %s %s and verified%s" % (kb(len(data)), TARGET_NAME,
                                                slot_label(slot), note)


def cmd_pull(where, base, slot):
    lpath, dsv_path = local_paths(base)
    tgt = make_target(where)
    step("Looking for the card" if tgt.kind == "sd" else "Connecting to " + TARGET_NAME)
    tgt.open()
    try:
        step("Looking for the ROM")
        rdir = remote_save_dir(tgt, base)
        rpath = rdir + "/" + slot_name(base, slot)
        existing = tgt.size(rpath)
        remote = tgt.read(rpath, "Downloading" if tgt.kind == "ftp" else "Reading the card",
                          existing if existing > 0 else None)
    finally:
        tgt.close()
    if remote is None:
        raise SyncError("No save in %s %s for that game" % (TARGET_NAME, slot_label(slot)))
    old, _, _ = local_save(base)
    data = remote if old is None else fit(remote, len(old), 0xFF)
    # Back up both formats before touching either; the .dsv is moved away so
    # DraStic picks up the new .sav instead of its own older copy.
    step("Backing up this device's save")
    backups = []
    if os.path.exists(lpath):
        backups.append(backup(base, "h", local_read(lpath)))
    if os.path.exists(dsv_path):
        backups.append(backup(base, "h", local_read(dsv_path), "dsv"))
    if backups:
        emit("backup", " + ".join(backups))
    step("Writing the save")
    os.makedirs(LOCAL_SAVE_DIR, exist_ok=True)
    tmp = lpath + ".syncnds.tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, lpath)
    step("Verifying")
    if local_read(lpath) != data:
        raise SyncError("Write did not verify - local copy differs. Backup kept.")
    if os.path.exists(dsv_path):
        os.remove(dsv_path)  # already copied into BACKUP_DIR above
    note = ""
    if len(data) < len(remote):
        note = " (%s file is %s; the rest is empty padding nds-bootstrap adds)" % (
            TARGET_NAME, kb(len(remote)))
    return "Saved %s from %s %s and verified%s" % (kb(len(data)), TARGET_NAME,
                                                   slot_label(slot), note)


def take_options(argv):
    """Pull --tag= / --name= out of argv and apply them. Returns the rest."""
    global TARGET_NAME, TARGET_TAG
    rest = []
    for arg in argv:
        if arg.startswith("--name="):
            TARGET_NAME = arg[len("--name="):] or TARGET_NAME
        elif arg.startswith("--tag="):
            TARGET_TAG = arg[len("--tag="):] or TARGET_TAG
        else:
            rest.append(arg)
    return rest


def main(argv):
    argv = take_options(argv)
    if len(argv) < 3 or argv[1] not in ("ping", "status", "push", "pull"):
        raise SyncError("usage: syncnds.py [--tag=T] [--name=N] "
                        "ping|status|push|pull <ip|sd:[path]> [rom base name] [slot]")
    command, where = argv[1], argv[2]
    if command == "ping":
        return cmd_ping(where)
    if len(argv) < 4:
        raise SyncError("missing rom base name")
    base = argv[3]
    if command == "status":
        return cmd_status(where, base)
    slot = int(argv[4]) if len(argv) > 4 else 0
    if not 0 <= slot < SLOTS:
        raise SyncError("slot must be 0-%d" % (SLOTS - 1))
    return {"push": cmd_push, "pull": cmd_pull}[command](where, base, slot)


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
