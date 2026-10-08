"""SSH client configuration rendered from the inventory.

SUW owns exactly two files (`~/.ssh/suw.conf`, `~/.ssh/suw_known_hosts`) plus a one-line
`Include` block at the top of `~/.ssh/config`. The user's own entries are never rewritten.

Trust model for the cloud role: the host key is verified by a human once, at enrollment, and
its *public* key is stored in the inventory. Each node gets its own `HostKeyAlias`, so handing
the `cloud` name to a new machine never produces a host-key-mismatch prompt and host key
checking is never switched off.
"""

from __future__ import annotations

import re

from ..core import inventory, journal, paths

KEEPALIVE = """\
    ServerAliveInterval 30
    ServerAliveCountMax 3
    ControlMaster auto
    ControlPersist 10m
    ControlPath {ssh}/suw-%C
"""


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_PATH = re.compile(r"^[A-Za-z0-9_./~+@%-]+$")
_KEY = re.compile(r"^[a-z0-9@.-]+ [A-Za-z0-9+/=]+$")


def _port(value) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return 0
    return port if 1 <= port <= 65535 else 0


def problem(name: str, host: str, user: str, port, identity: str = "") -> str:
    """Why an inventory entry must not reach the SSH configuration ('' = it may).

    The inventory is pulled from a shared repository. Every value is checked again here, at
    the point where it would become SSH configuration: a line break or an option smuggled
    into a field could otherwise turn into a command run by the next `ssh home`."""
    if not _NAME.match(name):
        return "name"
    if not inventory.valid_host(host):
        return "host"
    if user and not inventory.valid_user(user):
        return "user"
    if port not in (None, "") and not _port(port):
        return "port"
    if identity and (identity.startswith("-") or not _PATH.match(identity)):
        return "key file path"
    return ""


def cloud_alias(label: str) -> str:
    return f"suw-{label}"


def render(inv: dict, self_name: str) -> str:
    lines = ["# Managed by suw (rendered from the inventory). Do not edit: changes are overwritten.", ""]
    names: list[str] = []
    for name, device in sorted(inv.get("devices", {}).items()):
        host = inventory.device_host(device)
        if name == self_name or not host:
            continue
        bad = problem(name, host, str(device.get("ssh_user", "")), device.get("ssh_port"), str(device.get("ssh_identity", "")))
        if bad:
            lines += [f"# skipped an inventory entry: invalid {bad}", ""]
            continue
        names.append(name)
        lines.append(f"Host {name}")
        lines.append(f"    HostName {host}")
        if device.get("ssh_user"):
            lines.append(f"    User {device['ssh_user']}")
        if device.get("ssh_port") and int(device["ssh_port"]) != 22:
            lines.append(f"    Port {int(device['ssh_port'])}")
        if device.get("ssh_identity"):  # a path to a key file; the key itself never enters the inventory
            lines.append(f"    IdentityFile {device['ssh_identity']}")
            lines.append("    IdentitiesOnly yes")
        lines.append("")
    label, node = inventory.cloud_current(inv)
    if node and problem(cloud_alias(label), str(node.get("tailscale_name") or node.get("endpoint", "")), str(node.get("user", "")), node.get("port")):
        lines += ["# skipped the cloud entry: invalid value in the inventory", ""]
        node = None
    if node:
        names.append("cloud")
        lines.append("Host cloud")
        lines.append(f"    HostName {node.get('tailscale_name') or node.get('endpoint', '')}")
        if node.get("user"):
            lines.append(f"    User {node['user']}")
        if not node.get("tailscale_name") and int(node.get("port", 22)) != 22:
            lines.append(f"    Port {int(node['port'])}")
        lines.append(f"    HostKeyAlias {cloud_alias(label)}")
        lines.append(f"    UserKnownHostsFile {paths.ssh_dir()}/suw_known_hosts")
        lines.append("    StrictHostKeyChecking yes")
        lines.append("")
    if names:
        lines.append(f"Host {' '.join(names)}")
        lines.append(KEEPALIVE.format(ssh=paths.ssh_dir()))
    return "\n".join(lines).rstrip() + "\n"


def render_known_hosts(inv: dict) -> str:
    out = []
    for label, node in sorted(inv.get("cloud", {}).get("nodes", {}).items()):
        if node.get("state") == "active" and _NAME.match(cloud_alias(label)) and _KEY.match(str(node.get("host_key", ""))):
            out.append(f"{cloud_alias(label)} {node['host_key']}")
    return "\n".join(out) + ("\n" if out else "")


def apply(inv: dict, self_name: str) -> list[str]:
    """Write the managed files. Idempotent; returns the list of files that changed."""
    ssh = paths.ensure(paths.ssh_dir(), 0o700)
    changed = []
    if journal.write_file(ssh / "suw.conf", render(inv, self_name), 0o600):
        changed.append(str(ssh / "suw.conf"))
    if journal.write_file(ssh / "suw_known_hosts", render_known_hosts(inv), 0o600):
        changed.append(str(ssh / "suw_known_hosts"))
    # `Include` must precede every Host block to apply, hence top=True.
    if journal.managed_block(ssh / "config", "Include ~/.ssh/suw.conf", top=True):
        (ssh / "config").chmod(0o600)
        changed.append(str(ssh / "config"))
    return changed
