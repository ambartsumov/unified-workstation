"""Password sign-in to a server without ever writing the password down.

Keys are the norm. A password is only a bootstrap: it is typed once (hidden), kept in the
operating system's secret store (GNOME Keyring / macOS Keychain) and used to install this
machine's public key on the server. From then on every connection uses the key.

`ssh` asks for the password through SSH_ASKPASS; the helper answers only password prompts.
A host-key question ("are you sure you want to continue connecting?") is never answered:
an unknown or changed server identity still stops the connection.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import paths, secrets
from .proc import Result, run, ssh_cmd

HELPER = Path(__file__).resolve().parents[2] / "bin" / "suw-askpass"


def ref(role: str) -> str:
    return f"ssh-password/{role}"


def is_password_prompt(prompt: str) -> bool:
    text = prompt.lower()
    return "password" in text and "yes/no" not in text and "fingerprint" not in text and "passphrase" not in text


def answer(prompt: str, role: str) -> str | None:
    """What the helper prints for one prompt (None = refuse)."""
    if not role or not is_password_prompt(prompt):
        return None
    return secrets.get(ref(role))


def environment(role: str) -> dict[str, str]:
    return {"SSH_ASKPASS": str(HELPER), "SSH_ASKPASS_REQUIRE": "force", "SUW_ASKPASS_ROLE": role, "DISPLAY": os.environ.get("DISPLAY", ":0")}


def public_key(identity: str = "") -> Path | None:
    """The public half of the key this machine uses for the server."""
    candidates = [paths.expand(identity + ".pub")] if identity else []
    candidates += [paths.ssh_dir() / name for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")]
    return next((path for path in candidates if path.is_file()), None)


def install_key(alias: str, role: str, key_line: str, timeout: float = 40) -> Result:
    """Append one public key to the server's authorized_keys, signing in with the stored
    password. Host-key checking is untouched (the alias pins it)."""
    key_line = key_line.strip()
    if "\n" in key_line or not key_line.startswith(("ssh-", "ecdsa-", "sk-")):
        return Result(1, "", "not a public key")
    script = 'umask 077; mkdir -p "$HOME/.ssh"; k=$(cat); f="$HOME/.ssh/authorized_keys"; touch "$f"; grep -qxF "$k" "$f" || printf "%s\\n" "$k" >> "$f"; echo installed'
    cmd = [
        *ssh_cmd(),
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        "-o", "PubkeyAuthentication=no",
        "-o", "NumberOfPasswordPrompts=1",
        "-o", "ConnectTimeout=10",
        "-o", "ControlPath=none",
        alias,
        "--",
        "sh", "-c", "'" + script.replace("'", "'\\''") + "'",
    ]
    return run(cmd, stdin=key_line + "\n", env=environment(role), timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    value = answer(argv[0] if argv else "", os.environ.get("SUW_ASKPASS_ROLE", ""))
    if value is None:
        return 1
    sys.stdout.write(value + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
