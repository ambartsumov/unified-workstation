# Acceptance procedures

Deterministic, manual procedures for real devices. Automated tests can not press a key on a
second computer or put a laptop to sleep; these procedures can. A platform or a device pair is
recorded as verified only when its procedure was run against a named build and the results
were written into the table at the end of this page.

Record for every run: date, build (version and artifact file name), operating-system version,
desktop and session type, tool versions (**Health → Components**), result per step.

## A. Single computer — Linux, macOS, Windows

| # | Step | Expected |
|---|---|---|
| A1 | Install from the release artifact, without a terminal | installs; on macOS and Windows no "unknown developer" warning |
| A2 | Open the application | first-run assistant appears |
| A3 | Choose *Personal computer*, default folder, finish | only the settings folder and the work folder were created; no shell file changed; nothing running in the background |
| A4 | Overview | *Ready to work*; Sync *Not set up*; mode Default |
| A5 | Settings → change appearance and language, save | applied at once; a snapshot exists under Recovery |
| A6 | Settings → Work folder → choose the home folder | refused, with the reason |
| A7 | Health → Run system check | every row has a status and a reason; nothing unsupported shows OK |
| A8 | Health → Export support bundle; open the zip | no real name, address, path, key or work file inside |
| A9 | Re-run the assistant as *Workstation* with sync | the change list is shown before anything changes; sync service runs |
| A10 | Enter and leave Workstation Mode | tools open; leaving closes only what the mode opened |
| A11 | Sign out and in | background components are running again |
| A12 | Recovery → restore the snapshot from A5 | settings return |
| A13 | Uninstall, keeping data | work folder untouched; services gone; marked blocks removed |
| A14 | Install the previous version over a newer settings format | refused or migrated safely; never a broken state |

## B. Two computers — sync and pairing

| # | Step | Expected |
|---|---|---|
| B1 | A: Add Workstation; B: Pair with a code; exchange codes | both show the same six digits |
| B2 | Enter a wrong number once | refused; nothing trusted |
| B3 | Confirm on both | both list each other as online |
| B4 | Create a file on A | appears on B; Sync shows *In sync* on both |
| B5 | Hidden file, nested folders, a project `.env`, `.claude/` | all arrive unchanged |
| B6 | Edit the same file on both while B is offline, reconnect | *Conflict*; both versions kept; resolving keeps one and moves the other to the trash |
| B7 | Delete a file on A | gone on B; restorable under Sync → Earlier versions |
| B8 | A file above the large-file threshold | held back and listed; not sent |
| B9 | Pair when both folders already contain different files | merge is announced and needs confirmation; nothing deleted |
| B10 | Unpair | sync stops; files remain on both |

## C. Three computers — the critical test

A and B are workstations; H is a Home server holding a replica.

| # | Step | Expected |
|---|---|---|
| C1 | A creates a file | B receives it; H receives it |
| C2 | B modifies it | A and H have the new version; H keeps the old one as a version |
| C3 | A goes offline; B keeps working | B's changes reach H |
| C4 | B goes offline; A comes back and keeps working | A receives B's changes from H; A's new changes reach H |
| C5 | Both online | all three identical; no file lost; any conflict kept as two versions |
| C6 | Restore an old version from H onto A | restored under a new name; nothing overwritten |

## D. Keyboard, mouse and clipboard

Run for each pair the peripheral stack permits, in both directions:
Linux↔macOS, Linux↔Windows, macOS↔Windows.

| # | Step | Expected |
|---|---|---|
| D1 | Move the pointer across the shared edge | continues on the other computer; keyboard follows |
| D2 | Type text, including non-Latin layout and modifier shortcuts | arrives correctly |
| D3 | Copy text on one, paste on the other | pasted |
| D4 | Copy an image | pasted, or the documented limit applies |
| D5 | Disconnect and reconnect the network | sharing resumes without action |
| D6 | Sleep and wake each computer in turn | sharing resumes |
| D7 | Switch the other computer off | this computer is unaffected; status shows *Waiting* |
| D8 | Check from a third device on the same network | the sharing port does not accept an unpaired device |

If a pair has a limit, write it into [Supported platforms](supported-platforms.md) — honestly.

## E. Servers

| # | Step | Expected |
|---|---|---|
| E1 | Add Home with a wrong address | *The server did not answer*, with what to check |
| E2 | Add Home; compare the fingerprint; confirm | *Online* |
| E3 | Change Home's address in the application | still *Home*; nothing else needed editing |
| E4 | Replace the server's host key | connection blocked with *identity changed*; reconnects only after review |
| E5 | Add Cloud whose health check fails | not activated; previous Cloud (if any) still active |
| E6 | Add a working Cloud | *Online*; hardware shown |
| E7 | Send a project containing `.env` | review lists it; needs confirmation; arrives complete |
| E8 | Cancel a transfer midway and send again | continues; verified |
| E9 | Replace Cloud with a new machine | old one retired only after the new one passed |

## Results

| Date | Build | Platform(s) | Procedure | Result |
|---|---|---|---|---|
| — | — | — | — | No acceptance run has been recorded for a 1.0 build yet. |
