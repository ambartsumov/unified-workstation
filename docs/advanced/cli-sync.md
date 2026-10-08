# Project synchronisation

Ubuntu and the MacBook are equal workstations. A project changed on one becomes available on
the other without anyone remembering to push; a workstation that is switched off simply
catches up when it returns. Nothing local is ever silently destroyed.

## Three layers, three mechanisms

| Layer | What | Mechanism |
|---|---|---|
| A — working tree | the files being edited | stays on the workstation; checkpointed into B |
| B — history | source, config, docs | Git; GitHub is the durable common history |
| C — artifacts | datasets, checkpoints, media, archives | the artifact store (`suw artifact …`), never Git |

Secrets are a fourth thing and live in the OS keychain. `.git` directories are never copied
between machines: each workstation has its own repository and both talk to the remote.

```text
Ubuntu .git ──push──► GitHub ◄──fetch── Mac .git
```

## What happens when you save a file

1. the file watcher (inotify on Linux) notices the change — no polling of file contents;
2. the daemon waits for a quiet period (`idle_seconds`, 20 s) and never checkpoints more
   often than `min_interval_seconds`; if you never stop typing, it checkpoints anyway after
   `max_wait_seconds`;
3. `git status` is read, Git's own ignore rules apply;
4. every changed file is checked: secret file names, secret *content*, size, artifact types;
5. allowed files are staged and committed as
   `chore(suw-autosync): checkpoint ubuntu 2026-10-08T12:00:00+08:00`;
6. the commit is pushed; if the remote is unreachable it stays queued and is retried with
   exponential backoff and jitter. Committing never waits for the network.

Human commits stay exactly as written. Automation never amends, rebases, resets, deletes a
branch or overwrites remote history — there is no code path that can.

## States

Every managed repository is in exactly one state (`suw sync status`, `--json`):

| State | Meaning | Needs you? |
|---|---|---|
| `CLEAN` | nothing to commit; remote not checked (daemon not running) | no |
| `LOCAL_CHANGES` | uncommitted work, automatic checkpoints are off | no |
| `CHECKPOINT_PENDING` | uncommitted work, waiting for the quiet period | no |
| `LOCAL_COMMITTED` | committed, nowhere to push (no remote, or push disabled) | no |
| `PUSH_PENDING` | commits queued for the remote | no |
| `REMOTE_AHEAD` | the remote has commits this machine has not taken yet | no |
| `SYNCED` | identical to the remote | no |
| `OFFLINE` | remote unreachable; work continues, pushes are queued | no |
| `PUSH_FAILED` | the remote refused the push (notified after 3 attempts) | maybe |
| `AUTH_REQUIRED` | the remote rejected the credentials | yes |
| `POLICY_BLOCKED` | a file was kept out of the checkpoint (secret / oversized / artifact) | yes |
| `DIVERGED` | both sides have new commits | yes |
| `CONFLICT` | a merge is in progress, or the opted-in automatic merge hit conflicts | yes |
| `RECOVERY_REQUIRED` | the repository itself is broken or unreadable | yes |

## Reconciliation

| Situation | What SUW does |
|---|---|
| remote ahead, tree clean | fast-forward |
| remote ahead, uncommitted work in *other* files | fast-forward (Git refuses by itself if a file overlaps) |
| remote ahead, uncommitted work in the *same* files | nothing; your work is left untouched |
| local ahead | plain push |
| both ahead (**diverged**) | **nothing is merged**; your side is pinned on `suw/recovery/<device>/<timestamp>`, you are notified once |
| merge/rebase in progress | hold; your in-flight operation is not disturbed |

Order within one cycle: take what the remote has, then checkpoint on top of it, then push.
That keeps history linear whenever it can be.

### Diverged: why SUW stops

Two machines committed independently. Choosing a winner, or merging without being asked, is
exactly the kind of clever automation that eventually loses someone's work, so the default
(`[sync] policy = "manual"`) is to stop and explain:

```text
$ suw project recovery speech-model
Local HEAD        4f1c2aa  (main)
Remote HEAD       9be0d13  (origin/main)
Last synced HEAD  73bf562
Unpushed commits (2) …   Incoming commits (1) …   Uncommitted files (0)
No data was deleted.
Actions:
  1. Inspect   2. Create recovery branch (--branch)   3. Rebase (--rebase)
  4. Merge (--merge)   5. Abort        After resolving: --done
```

`--merge` / `--rebase` are aborted automatically on conflict and put the tree back exactly as
it was. `--done` removes recovery branches that are fully merged (Git's safe delete).

If you prefer non-overlapping changes to be merged automatically, opt in — globally or per
project: `suw config set sync.policy merge`. A real conflict still aborts and waits for you.

## What is never committed automatically

| Rule | Applies to | Result |
|---|---|---|
| deny list (`.env`, `*.pem`, `id_ed25519`, …) | every changed file | held back |
| secret content (private keys, GitHub/AWS/Telegram/HF tokens, quoted passwords, URLs with credentials) | every changed text file ≤ 1 MB | held back |
| larger than `max_file_mb` (50) | new files | held back |
| model/dataset/archive types (`*.ckpt`, `*.safetensors`, `*.pt`, `*.tar.gz`, …) | new files | held back → `suw artifact put` |
| `[artifacts] paths` in `.suw.toml` | everything under them | held back |
| larger than `warn_file_mb` (5) | new files | committed, with a warning in `suw history` |

"Held back" means: not staged, not committed, **not deleted and not modified**. Everything
else in the same checkpoint is still committed. The project shows `POLICY_BLOCKED` with the
file and the reason, and you get one notification. Files you committed by hand are your
decision: size rules do not apply to them.

False positives: `suw project allow-once <file>` (that exact content, once), a
`# suw:allow-secret` marker on the line, or `secret_ignore = ["tests/fixtures/*"]`.
An ignored `.env` does not mean secrets are safe — content is scanned regardless of name.

## Which repositories are managed

- every Git repository directly inside `projects.roots` (default `~/Projects/active`);
- anything added with `suw project add <path>`;
- minus anything removed with `suw project remove <name>` (the repository is not touched).

`suw project scan` looks one level deep in `~/Projects/{active,experiments,university}` and
only *reports* what it finds; `--add` enrolls after confirmation. `~/Desktop`, `~/Downloads`,
`~/Documents`, `~/repos` are never scanned.

Managed projects get automatic checkpoints by default. Opt out per project:
`suw project autosync off [name]`.

## Project manifest: `.suw.toml`

All behaviour = global policy + device policy + project policy. Nothing else.

```toml
[project]
name = "speech-model"

[autosync]
enabled = true              # default true for managed projects
debounce_seconds = 20       # alias of idle_seconds
min_interval_seconds = 120
max_wait_seconds = 900
max_file_mb = 50
warn_file_mb = 5
deny = ["*.secret"]         # added to the global deny list
allow = ["fixtures/test.pem"]
secret_ignore = ["tests/fixtures/*"]

[artifacts]
paths = ["datasets/", "checkpoints/"]   # never staged; store with `suw artifact put`

[sync]
enabled = true
policy = "manual"           # manual | merge | rebase
push = true
```

Never put secrets in this file.

## Commands

```bash
suw sync                 # reconcile everything now
suw sync status [--json]
suw sync retry           # forget backoff and try again now
suw project list | status [name] | add <path> | remove <name> | scan [--add]
suw project autosync on|off [name]
suw project recovery <name> [--branch|--merge|--rebase|--done]
suw project restore      # clone projects from the shared list that this machine lacks
suw history --sync       # what happened
```
