# Troubleshooting

Start with **Health → Run system check**. Each row that is not OK explains why and, where the
application can fix it, offers **Fix automatically**.

| Symptom | Likely cause | What to do |
|---|---|---|
| Overview says *Needs your attention* | listed directly under the heading | follow the link beside each item |
| Sync: *Not set up* | Syncthing is not installed, or no computer is paired | Health → Components; Workstations → Add Workstation |
| Sync: *Offline* | the other computer is off or on another network | nothing to fix; it resumes when both are reachable |
| Sync: *Error* | the service stopped, or a file can not be written | Sync page shows the file; Health → Repair → File sync |
| A file exists twice with "sync-conflict" in its name | it changed on two computers at once | Sync → Conflicts → choose the version to keep |
| A large file does not arrive | it is held back by the large-file policy | Work folder → Large files |
| Pairing: "the number does not match" | mistyped, or the code came from a different computer | compare the six digits on both screens; do not pair if they differ |
| Keyboard sharing does not start on macOS | Accessibility / Input Monitoring not granted | Health → Permissions |
| Keyboard sharing does not start on Wayland | the desktop has no input-capture portal, or the prompt was declined | approve the prompt; otherwise use an X11 session |
| Shared keyboard stops in one window (Windows) | the window runs as administrator | expected: Windows blocks it |
| Server: *identity changed* | the server was reinstalled, or the connection is intercepted | Servers → Edit address → review the fingerprint |
| Server: *Sign-in needed* | your key is not accepted on the server | add your public key on the server, then *Test connection* |
| The window says it lost its connection | the application was closed | close the window and open the application again |

## Still stuck

1. **Health → Details and event log** — searchable, with secrets already redacted.
2. **Health → Export support bundle**, review it, and attach it to a
   [bug report](https://github.com/ambartsumov/unified-workstation/issues/new/choose).

See also [Recovery](recovery.md) and [Supported platforms](supported-platforms.md).
