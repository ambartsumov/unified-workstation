# FAQ

**Do I need an account?**
No. There is no account and no central service.

**Does it upload my files anywhere?**
No. Files travel directly between computers you paired, and to a Cloud server only when you
send a specific project.

**Does it work offline?**
Yes. Each computer keeps working; computers on the same local network keep syncing; the rest
catches up when the connection returns.

**Do I need Tailscale?**
No. Computers on the same network connect directly. A private network is only needed to reach
your computers from elsewhere, and you can use the one you already have.

**Do I need Claude Code?**
No. Nothing depends on it. If you use it, project context stored in the work folder follows
the project.

**Is this a replacement for Git?**
No. Git history is not copied by file sync. Use Git for history and collaboration; the work
folder keeps your working files identical.

**Will it rearrange my desktop?**
Not in Default Mode, and not at all if you chose *Personal computer*. Workstation Mode opens
your tools only when you enter it.

**Can I use it with only one computer?**
Yes — as a work folder with version history on a Home server, or just for Workstation Mode.

**Why does a feature say "Partially supported"?**
Because your operating system or desktop limits it, and the application says so instead of
pretending. See [Supported platforms](supported-platforms.md).

**What happens when two computers edit the same file?**
Both versions are kept and you choose. Nothing is overwritten silently.

**Where are my settings?**
In the application's own folder, shown under **Settings → Advanced**. You never need to open
them.

**How do I report a problem?**
**Health → Export support bundle**, then open an
[issue](https://github.com/ambartsumov/unified-workstation/issues/new/choose). For
security problems see [SECURITY.md](../SECURITY.md).
