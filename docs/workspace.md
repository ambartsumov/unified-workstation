# Work folder

The work folder is one folder that is the same on every computer you pair.

- Default: `Desktop/Work` in your home folder (on Linux, macOS and Windows).
- Change it: **Work folder → Change folder…** — a folder browser, no path typing.
  Files in the previous folder are not moved.

## What is shared

**Everything inside the work folder**, including:

- hidden files and dot-folders
- source code, scripts, documents
- project configuration, including a project's `.env` file and keys or certificates you
  deliberately keep with a project
- assistant instructions and project context (`CLAUDE.md`, `.claude/`)

The application does not use blanket rules such as "skip every hidden file" or "skip every
`.env`": inside the work folder, your files are your files.

## What never leaves this computer

System identity lives outside the work folder and is never offered for sharing:

- operating-system passwords and the credential store
- SSH host keys and your `~/.ssh` folder
- private-network (Tailscale) identity
- sign-in sessions
- this computer's own identity in the application

To keep that boundary, system folders, credential folders and your whole home folder can not
be chosen as the work folder.

## What is not copied

Folders every computer rebuilds for itself: Git's internal data (`.git`), installed
dependencies (`node_modules`, virtual environments) and caches. Git history travels through
Git, not through file sync. The list is on the **Work folder** page; change it under
**Settings → Sync** (advanced).

## Large files

A file above the threshold (2 GB by default, **Settings → Sync**) is **held back and listed**
instead of being sent without asking. Nothing is moved or deleted. You can allow a specific
file, raise the threshold, or keep large data on a server instead.

Next: [Sync](sync.md)
