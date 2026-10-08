# Architecture

The architecture is described in [docs/architecture.md](docs/architecture.md); the reasoning
behind individual decisions is in [docs/advanced/decisions/](docs/advanced/decisions/).

In one sentence: a standard-library Python **core engine**, **platform adapters** for Linux,
macOS and Windows, **integrations** with the mature tools that do the hard parts (Syncthing,
Deskflow, OpenSSH, Tailscale), and a small local **application window** on top.
