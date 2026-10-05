# SaveSyncNDS

A muOS application that pushes and pulls DraStic save files between an Anbernic
handheld and a Nintendo console running [ftpd](https://github.com/mtheall/ftpd)
- a 3DS, a DSi, or anything else whose SD card follows the TWiLight Menu++ /
nds-bootstrap layout.

It is the successor to SaveSync3DS, which only knew about one console.

## What it does

Pick a DS game, pick a save slot, and move the save in either direction. Every
overwrite is backed up first and verified by reading the file back afterwards.

- **PUSH** - this device's DraStic save replaces the console's save
- **PULL** - the console's save replaces this device's DraStic save

## Supported consoles

Any target running ftpd on port 5000 with user/password `a`/`a`. Two SD layouts
are handled automatically, with no configuration:

| Console | ROM layout | Save path |
|---|---|---|
| 3DS | letter folders, `/roms/nds/<L>/` | `/roms/nds/<L>/saves/<rom>.sav` |
| DSi XL | flat, `/roms/nds/` | `/roms/nds/saves/<rom>.sav` |

The DSi needs **Unlaunch** installed - `ftpd.nds` requires DSi mode for both SD
card access and the DSi's WPA2-capable Wi-Fi chip. Configure its network under
*System Settings -> Internet -> Advanced Setup* (connections 4-6); the legacy
connections 1-3 are DS-mode only and cannot do WPA2.

## Controls

| Button | List screen | Game screen |
|---|---|---|
| D-pad up/down | move | change option |
| D-pad left/right | page | change save slot |
| **L1 / R1** | **switch console** | - |
| A | select | choose |
| B | exit | back / cancel a running transfer |
| X | edit this console's IP | - |
| Y | test connection | - |

## Two ways to reach the card

Each target in `config.ini` is one of two types, and everything above the
transport is shared - the same ROM lookup, save-size fitting, backup and
read-back verification run either way.

| Type | Reaches the console by |
|---|---|
| `ftp` | the network, with ftpd running on the console |
| `sd` | the console's SD card inserted in this device |

The `sd` type is much faster and needs no Wi-Fi at all. With `target3.path`
left blank it finds the card itself, by looking for TWiLight's own
`_nds/TWiLightMenu` folder across `/mnt/sdcard`, `/mnt/usb` and the other
mount points. That marker is what makes detection safe: neither this device's
muOS card nor its ROM card has it, so the wrong card cannot be picked. Set
`target3.path` explicitly to skip the search.

On a target of type `sd` the IP editor is disabled, and **Y** tests whether the
card is present rather than pinging a console.

## Transfers and progress

Throughput to a DSi is slow and depends heavily on how the handheld is
connected. Measured on 2026-10-04:

| Path | 512 KB |
|---|---|
| wired PC -> DSi, upload | 6.0s (85 KB/s) |
| wired PC -> DSi, download | 10.8s (47 KB/s) |
| handheld -> DSi, download | 70.2s (7.3 KB/s) |

The handheld figure is the one that matters, and it is poor because the device
associates with a Wi-Fi extender on 5 GHz while the DSi is 2.4 GHz only, so
every byte crosses bands through the extender's backhaul. A push is three
transfers - read the existing save, upload the new one, read it back to verify
- so roughly 1.5 MB, which took **4m42s** in that configuration.

Because of that the backend runs **detached**, writing `key=value` progress
lines to `/tmp/savesyncnds.out` which the UI polls each frame. You get a live
progress bar, the current phase, and an elapsed counter, and the app stays
responsive instead of freezing for the whole transfer.

Press **B** after three seconds to abort a running transfer. Anything already
backed up is kept.

The read-back verification deliberately uses a **fresh connection**: a DSi
asked to serve three data transfers back-to-back in one session wedged with the
control socket still open, blocking every later connection until the client was
killed.

## config.ini

Strict `key=value`, never spaced - muOS ini parsing treats `key ` (with the
space) as the key name.

```ini
target=2
target1.name=3DS
target1.ip=192.168.1.10
target2.name=DSi XL
target2.ip=192.168.1.11
```

`target` is the console selected at startup. Add `target3.*` and so on for more.
A bare `ip=...` left over from SaveSync3DS is read as target 1.

ftpd has **no mDNS on NDS**, so there is no hostname discovery - give each
console a static IP rather than relying on a DHCP reservation.

## How saves are handled

- **Size mismatch is normal.** DraStic and nds-bootstrap often disagree (64 KB
  vs 512 KB) while the game only uses the start. Saves are padded to grow, and
  trimmed only when the discarded tail is uniform.
- **Two DraStic formats.** `drastic-trngaje` can write `<rom>.dsv` (raw data plus
  a 122-byte DeSmuME footer); `drastic-legacy` writes a raw `<rom>.sav`. Push
  reads whichever is newer; pull always writes a raw `.sav` and moves any `.dsv`
  into the backup folder so DraStic imports the new file.
- **Save slots.** TWiLight's per-game save number picks the file: slot 0 is
  `<rom>.sav`, slot N is `<rom>.savN`. The app reads the current setting from
  `/_nds/TWiLightMenu/gamesettings/<rom>.nds.ini` and defaults to it.
- **Backups** go to `/mnt/mmc/MUOS/save/drastic/backup/sync3ds_backups/`, ten per
  game per side. The directory keeps its old name so existing history is not
  orphaned. Each target gets its own tag (`3ds`, `dsixl`) so backups from
  different consoles stay distinguishable.

## Backend

`syncnds.py` does all the transferring and can be run on its own over SSH, which
is how it is tested:

```
syncnds.py [--tag=T] [--name=N] ping   <ip>
syncnds.py [--tag=T] [--name=N] status <ip> <rom base name>
syncnds.py [--tag=T] [--name=N] push   <ip> <rom base name> [slot]
syncnds.py [--tag=T] [--name=N] pull   <ip> <rom base name> [slot]
```

It prints `key=value` lines; the last two are always `ok=` and `msg=`.
`--name` is the display name used in messages, `--tag` the short key used in
backup filenames. Both default to the 3DS so older command lines still work.

## Install

Copy the folder to `/mnt/mmc/MUOS/application/SaveSyncNDS/` and make
`mux_launch.sh` and `syncnds.py` executable. Files must have **LF** line
endings - a CRLF `mux_launch.sh` fails on the device with
`env: 'bash\r': No such file or directory`.

## Notes

- Close DraStic before syncing, or it will write its in-memory save back over
  whatever was just transferred.
- ftpd reports a missing file as `450`, not the usual `550`; both are treated as
  "missing".
- The icon art is currently inherited from SaveSync3DS and still reads "3DS".
