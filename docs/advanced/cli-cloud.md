# Cloud

**Purpose.** Rent any Linux box, paste its address once, and from then on it is `cloud`.

`cloud` is a logical role held by one disposable machine at a time. The inventory keeps every
node that ever held it (`suw cloud history`).

## Enroll — `suw cloud add`

```bash
suw cloud add                                   # interactive
suw cloud add --host 203.0.113.5 --user root --port 22 \
              --fingerprint SHA256:… --profile cuda -y   # scripted
```

You may paste `user@host` or `host:port` directly. Steps:

1. validate the address (IP/hostname syntax; anything that looks like an option is rejected);
2. fetch the host key and show its fingerprint — you confirm it, or supply the expected one
   with `--fingerprint` (from the provider console). Without a trust decision nothing happens;
3. connect with your SSH key (offers `ssh-copy-id` if the key is not installed yet);
4. discover OS, CPU, RAM, disk, GPU, VRAM, CUDA, Docker;
5. join the tailnet if an auth key is available (keychain entry `tailscale_authkey`, or pasted
   once and optionally remembered). The key reaches the node on stdin, never in a command line;
6. give the node the role: inventory updated, `~/.ssh/suw.conf` re-rendered, `ssh cloud` verified;
7. optionally apply a bootstrap profile.

If the tailnet step fails or is skipped, `ssh cloud` simply uses the public endpoint.

## Replace — `suw cloud replace`

Shows the current node, checks it for unsaved work, enrolls the new one and retires the old.
`ssh cloud`, `suw cloud status` and every script keep working unchanged. Because each node has
its own pinned host key (`HostKeyAlias`), there is no "host identification has changed" prompt
and no need to edit `known_hosts`.

## Remove — `suw cloud remove`

Prints a safety report gathered on the node:

- repositories with uncommitted changes, unpushed commits, or no remote at all;
- active jobs: tmux/screen sessions, containers, GPU processes;
- mounted volumes;
- configured output paths that contain data.

Teardown is refused while any of these exist (`--force` overrides, after typing the node
label). SUW never destroys the rented machine itself — do that in the provider console.

## Saving outputs — `suw cloud backup` (alias: `save`)

```bash
suw config set cloud.outputs '["~/outputs", "/workspace/results"]' --shared
suw cloud backup
```

Copies each path to `~/Projects/archive/cloud-outputs/<node>/` with rsync and verifies the
transfer with a checksum comparison before reporting success.

## Bootstrap profiles — `suw cloud bootstrap [--profile …] [--docker]`

| Profile | Adds |
|---|---|
| `minimal` | git, tmux, curl; tmux OSC 52 clipboard |
| `compute` | + btop, Python, build tools, rsync, jq, uv, gh |
| `cuda` / `inference` / `training` | + verifies a working NVIDIA driver (never installs drivers: pick a GPU image) |

The profile is recommended from detected hardware. Scripts are fixed files in
`suw/cloud/scripts/`, idempotent, and the only code SUW ever runs remotely.

## Config and files

| What | Where |
|---|---|
| nodes, endpoints, public host keys, hardware | `~/.config/suw/shared/inventory.toml` |
| rendered alias | `~/.ssh/suw.conf`, `~/.ssh/suw_known_hosts` |
| auth key | OS keychain: `suw secret set tailscale_authkey` |

## Failure modes

| Symptom | Fix |
|---|---|
| "host key does NOT match" | wrong address or interception — do not proceed; re-check the console |
| "authentication failed" | `ssh-copy-id -p <port> <user>@<host>`, re-run |
| joined tailnet but unreachable over it | containers without TUN; the public endpoint is used automatically |
| `Cloud unreachable` in doctor | machine destroyed → `suw cloud replace` or `suw cloud remove` |

Rollback: `suw cloud remove` empties the role; the retired node stays in history.
The workstation never depends on the cloud node being present.

## Atomic replacement

```text
old cloud → new server validated → fingerprint trusted by you → tailnet → bootstrap
          → health check → switch logical role → verify `ssh cloud` → retire old
```

The inventory and the rendered SSH files are snapshotted before the switch. If the new
machine fails anywhere before it, nothing was changed. If `ssh cloud` does not answer right
after it, the snapshot is restored and the previous node keeps the role.
`suw cloud replace --dry-run` shows the fingerprint and stops before the trust decision.

## Server states

`ONLINE · DEGRADED · OFFLINE · AUTH_REQUIRED · UNTRUSTED · NOT_CONFIGURED`.
`DEGRADED` = reachable over the public endpoint instead of the tailnet. `UNTRUSTED` with a
changed identity blocks the connection and prints a security warning: inspect with
`suw cloud verify`, and replace intentionally with `suw cloud replace`.
