# Backup and disaster recovery

## Where durable data lives

| Data | Primary | Second copy |
|---|---|---|
| Source code, docs, scripts | workstation clones | GitHub |
| SUW itself | this repository | GitHub |
| Inventory, URLs, layout | `~/.config/suw/shared` | its GitHub remote + every workstation |
| Secrets | OS keychain per machine | re-issued, not copied |
| Cloud outputs | cloud node | `~/Projects/archive/cloud-outputs` after `suw cloud save` |
| Home-server state | the server | **your backup job** (not provided by SUW) |
| Datasets, model weights | wherever they were created | **explicit storage you choose** (HF Hub, object storage) |

GitHub is not a backup for binaries or operational state. The last two rows are the gaps
to close deliberately.

## New laptop (old one destroyed)

1. Install the OS and sign in to Tailscale.
2. Get SUW: `git clone` it, or unpack a handoff bundle made earlier.
3. `./install.sh --packages`
4. `gh auth login`
5. Shared configuration: `git clone <shared-config remote> ~/.config/suw/shared`
   (the bundle does this for you), then `suw bootstrap`.
6. `gh repo clone …` each project into `~/Projects/active`.
7. Secrets: `suw secret set tailscale_authkey` (issue a new key; revoke the old one).
8. SSH: generate a new key, add it to the home server from another device, then
   `suw home trust`. For cloud, `suw cloud replace` (or add the new key on the node).
9. Revoke the lost laptop: remove it in the Tailscale admin console and delete its SSH
   key from GitHub and from `authorized_keys` on the home server.
10. `suw doctor`.

## Adding another workstation

Same as "MacBook: day one". If the default name (`ubuntu`/`mac`) is taken, name it first
by creating `~/.config/suw/config.toml` with:

```toml
[device]
name = "thinkpad"
```

then run `./install.sh`. Any number of devices with `role = "workstation"` are supported.
