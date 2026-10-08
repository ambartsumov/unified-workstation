# Clipboard

Three transports, each for one job. No clipboard daemon ever runs on a server.

## 1. Server → your laptop (SSH, tmux)

Copying in tmux on `home` or `cloud` lands in the clipboard of the laptop you are sitting
at, through any number of SSH hops, using the OSC 52 terminal escape. No X11 forwarding.

Requirements:

| Piece | State |
|---|---|
| tmux `set-clipboard on`, `allow-passthrough on` | installed by `suw bootstrap` (workstations) and `suw cloud bootstrap` (cloud) |
| terminal that honours OSC 52 | **WezTerm** (recommended, both OSes), kitty, ghostty, iTerm2 |

GNOME Terminal on Ubuntu 24.04 (VTE 0.76) ignores OSC 52, so remote copies do not arrive
there; `suw doctor` flags this. Install WezTerm with `sudo installers/ubuntu/packages.sh`.

Use it:

- tmux copy mode: `prefix [`, select, `y` (or drag with the mouse);
- from a shell, anywhere: `some-command | clip`.

On the home server add the same tmux block once: copy `dotfiles/tmux/tmux.conf` there or
append `set -g set-clipboard on` and `set -g allow-passthrough on` to its `~/.tmux.conf`.

## 2. Your laptop → server

Ordinary paste (`Ctrl+Shift+V` / `Cmd+V`) — the terminal types the text into the SSH
session. Nothing to configure.

## 3. Ubuntu ↔ MacBook (keyboard, mouse, clipboard)

[Deskflow](https://deskflow.org) shares one keyboard and mouse across both laptops and
carries the clipboard with them.

- Ubuntu (server): `flatpak install flathub org.deskflow.deskflow`. GNOME 46 on Wayland
  is supported through the input-capture portal; approve the prompt on first start.
- Mac (client): `brew install --cask deskflow` or the download from deskflow.org; grant
  Accessibility permission.
- In Deskflow on Ubuntu: add a screen named exactly like the Mac's computer name, place
  it beside the Ubuntu screen, keep **TLS enabled**, and set the listen address to the
  Ubuntu machine's Tailscale IP (`tailscale ip -4`) so the control channel is not exposed
  on Wi-Fi networks.
- On the Mac: connect to `ubuntu`'s tailnet name, confirm the TLS fingerprint once.

## Validation checklist

```text
[ ] echo hello | clip          on Ubuntu       → paste in the browser
[ ] ssh home;  echo hi | clip                  → paste locally
[ ] ssh cloud; tmux; copy-mode y               → paste locally
[ ] copy on Ubuntu, move the mouse to the Mac, paste
```

Verified on this installation so far: local `clip` and the tmux configuration. The SSH,
cloud and Deskflow rows need WezTerm, a reachable server and the Mac respectively.

## Proving it — `suw clipboard test`

Run it inside the terminal you actually use. It writes a random token to the terminal as an
OSC 52 sequence and reads the system clipboard back:

```bash
suw clipboard test              # terminal (and tmux, if you are inside it) → clipboard
suw clipboard test --via home   # the sequence is printed on the server: server → your clipboard
suw clipboard status
```

So: use WezTerm for anything that involves SSH. The test leaves its token on the clipboard
and never reads or writes the clipboard before the terminal had its turn — on GNOME/Wayland
`wl-copy` and `wl-paste` briefly take keyboard focus, and a terminal without focus is not
allowed to set the clipboard (an earlier version of the test tripped over exactly that).
Pasting *into* an SSH session is ordinary terminal paste and works everywhere.
