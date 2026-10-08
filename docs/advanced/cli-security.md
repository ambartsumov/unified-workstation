# Security model

## Threats and answers

| Threat | Mitigation |
|---|---|
| Laptop theft | disk encryption (OS); secrets only in the keychain; cloud access via revocable tailnet identity |
| Malicious local process | control socket is 0600 in the per-user runtime dir; no TCP listener at all |
| Compromised cloud server | the workstation only ever sends *fixed scripts* to it; it holds no workstation credentials; agent forwarding is not used |
| Accidental secret commit | checkpoint preflight deny-list + size limit; refusal instead of commit |
| MITM on first connection | fingerprint shown and confirmed (or matched against `--fingerprint`); key pinned per node |
| Provider exposure | tailnet for steady-state access; public IP needed only at enrollment |
| Malicious repository content | SUW runs `git` plumbing only; it never executes project code |
| Bad automation | pure, tested policy; worktree never overwritten; backup refs; everything in the event log |

## Invariants

1. Host key checking is on for every SSH connection SUW makes.
2. Remote history is never overwritten: pushes are plain fast-forward pushes only. No
   hard resets, no cleaning of untracked files, no branch deletion, no amending.
3. Uncommitted work is never modified by automation.
4. Secrets are referenced (`keychain://name`), never stored, never logged. Auth keys reach
   remote scripts on stdin and are deleted from the remote temp file on exit.
5. Remote execution = the fixed scripts in `suw/cloud/scripts/` + `suw/core/probe.sh`.
   Arguments are validated and shell-quoted.
6. Destructive cloud actions need an interactive confirmation that includes typing the
   node label.

## Secrets

```bash
suw secret set tailscale_authkey     # prompts, hidden
suw secret backend                   # which store is in use
```

Linux: Secret Service (GNOME Keyring). macOS: Keychain. Configuration may contain
`keychain://name`; it must never contain the value.

## Logs

`~/.local/state/suw/events.jsonl` — timestamped JSON lines, rotated at 2 MB × 3.
Tokens (`ghp_…`, `tskey-…`, `hf_…`, `sk-…`), `password=` pairs and URL credentials are
redacted before writing.

## Known gaps

- Tailscale ACLs are your tailnet's policy; SUW documents a recommendation but does not
  manage it.
- On macOS the `security` tool receives a secret as a process argument when storing it
  (local, same user, momentary).
- Checkpoints rely on a filename deny-list; a secret pasted into an ordinary source file
  is not detected. Use a scanner such as gitleaks in CI for that.
