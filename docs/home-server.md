# Home server

A Home server is **optional**. It is any always-on computer of yours — a mini server, a NAS
that offers SSH, a spare PC, or a VPS you use as storage. It gives you:

- a second, always-available copy of the work folder
- recovery: older versions of files are kept there longer
- a place for small services of your own

## Adding it

**Servers → Add Home server**

1. Enter the address or name, the user name and (if not 22) the port.
2. **Check server identity** — the application shows the server's fingerprint. Compare it
   with the fingerprint shown on the server itself and confirm only if they match.
3. The connection is tested and the result is shown.

Afterwards the server is simply *Home*: **Connect** opens a terminal session, and in a terminal
`ssh home` works (if you enabled *Server names for SSH*).

## Changing the address

**Servers → Edit address**. Nothing else needs to change — no file, no script, no other
setting. The identity is checked again.

## Removing it

**Servers → Remove**. Only the connection from this computer is removed; nothing on the server
is deleted.

## A copy of the work folder on Home

Setting up the replica currently uses the command line
(see [advanced notes](advanced/cli-sync.md)); a button for it is on the
[roadmap](../ROADMAP.md). The server needs Syncthing; it runs for your user account, limited in
memory and CPU, and listens on the private network only.
