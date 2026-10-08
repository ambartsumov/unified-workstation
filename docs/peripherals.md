# Keyboard and mouse

Use one keyboard and one mouse for all of your paired computers. Move the pointer off the edge
of one screen and it appears on the next; the keyboard follows the pointer.

Sharing is done by [Deskflow](https://deskflow.org). The application installs nothing by
itself: it detects Deskflow, creates a private profile and certificate, pairs the computers
and keeps the service running.

## Setting it up

1. Install Deskflow on each computer (**Health → Components** shows how).
2. **Keyboard & mouse → Set up** on each computer.
3. [Pair](workstations.md) the computers (pairing covers both sync and input sharing).
4. Under **Settings → Keyboard & mouse**, choose which computer has the keyboard and mouse
   plugged in and on which side the other screen sits.

## What differs between operating systems

| System | Status | Why |
|---|---|---|
| Linux, X11 | Supported | — |
| Linux, Wayland | Partially supported | needs the desktop's input-capture portal (GNOME 46+, KDE Plasma 6.1+); the desktop asks for approval the first time |
| macOS | Needs permission | Accessibility and Input Monitoring must be granted to the sharing tool |
| Windows | Partially supported | Windows blocks shared input inside windows running as administrator and on the sign-in screen |

**These rows describe design constraints, not test results.** Which combinations have been
verified on real hardware is tracked in [Supported platforms](supported-platforms.md).

## Security

Sharing listens on your private network only — never on a public interface — and accepts only
computers whose certificate fingerprint you approved while pairing.

## Recovery

After sleep, a network change or a disconnect the service reconnects by itself, with growing
pauses between attempts. If it does not: **Health → Repair → Keyboard & mouse sharing**.

Next: [Clipboard](clipboard.md)
