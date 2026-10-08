# Clipboard

There are two different clipboards to think about.

## Between your computers

When keyboard and mouse sharing is on, text you copy on one computer can be pasted on the
other. Switch it off under **Settings → Clipboard**.

- Linux on Wayland: text is shared; images and files depend on the desktop.
- macOS and Windows: text and images.

## From a server session

When you copy text inside a terminal session on a server, it should land in the clipboard of
the computer you are sitting at. That depends on the terminal: it must support the "OSC 52"
clipboard sequence.

- **WezTerm** supports it on Linux, macOS and Windows — the recommended terminal.
- Many built-in terminals do not. The application then reports *Clipboard from server
  sessions: Partially supported* and everything else keeps working.

The application is fully usable without WezTerm; choose any terminal under
**Settings → Terminal**.

For details and a live test, see the [advanced clipboard notes](advanced/cli-clipboard.md).
