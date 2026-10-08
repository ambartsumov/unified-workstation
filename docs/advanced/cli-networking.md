# Networking

**Purpose.** Address machines by role, never by IP.

| Name | What it is |
|---|---|
| `ubuntu`, `mac` | workstations (peers) |
| `home` | the always-on server |
| `cloud` | whichever node currently holds the cloud role |

## How names resolve

Tailscale + MagicDNS give every machine a stable name. SUW stores each device's tailnet
name in the inventory and renders `~/.ssh/suw.conf`:

```sshconfig
Host home
    HostName home-server.<tailnet>.ts.net
    User sam

Host cloud
    HostName cloud-20261008
    User root
    HostKeyAlias suw-cloud-20261008
    UserKnownHostsFile ~/.ssh/suw_known_hosts
    StrictHostKeyChecking yes
```

Your own `~/.ssh/config` is left intact apart from one `Include ~/.ssh/suw.conf` block at
the top. Connections to these hosts share one multiplexed channel (`ControlMaster`), so
health probes are cheap.

## Setting up

```bash
tailscale status                         # machines on your tailnet
suw home set <name> --user <ssh-user>
suw home trust                           # shows the host key fingerprint; you confirm once
ssh home
```

## Recommended tailnet policy

- Enable MagicDNS in the admin console.
- Tag servers (`tag:home`, `tag:cloud`) and allow only workstations → servers on port 22.
- For cloud nodes create a **reusable, pre-authorised, tagged** auth key with a short
  expiry and store it with `suw secret set tailscale_authkey`. It never enters Git.
- Remove retired cloud nodes from the admin console (or use ephemeral keys so they
  disappear on their own).

## Failure modes

| Symptom | Fix |
|---|---|
| MagicDNS names do not resolve | `sudo tailscale set --accept-dns=true` |
| "Host key verification failed" for home | `suw home trust` |
| home offline | the workstation is unaffected; `suw doctor` shows it; one notification after 3 misses |
| Tailscale down | local work and GitHub sync continue; server rows show offline |

Rollback: `suw rollback` removes the Include block and both managed files.
