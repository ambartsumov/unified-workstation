"""Test isolation: every test gets a private HOME/config/state; the real machine is never touched."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


STUBS = {
    # answers like a manager that knows no unit; records what it was asked to do
    "systemctl": '#!/bin/sh\necho "$@" >> "$(dirname "$0")/calls.log"\ncase " $* " in\n  *" is-active "*) echo "${SUW_STUB_ACTIVE:-inactive}" ;;\n  *" is-enabled "*) echo disabled ;;\nesac\nexit 0\n',
    "launchctl": '#!/bin/sh\necho "$@" >> "$(dirname "$0")/calls.log"\nexit 0\n',
    "loginctl": '#!/bin/sh\ncase " $* " in *" show-user "*) echo yes ;; esac\nexit 0\n',
}


def stub_bin(where: Path) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    for name, body in STUBS.items():
        (where / name).write_text(body)
        (where / name).chmod(0o755)
    return where


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SUW_HOME", str(home))
    monkeypatch.setenv("SUW_CONFIG_DIR", str(home / ".config" / "suw"))
    monkeypatch.setenv("SUW_STATE_DIR", str(home / ".local" / "state" / "suw"))
    monkeypatch.setenv("SUW_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # what Path.home() reads on Windows
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "")
    monkeypatch.setenv("SUW_DEFAULT_DEVICE", "legacy")
    monkeypatch.setenv("SUW_PROFILE_FILE", str(Path(__file__).parent / "fixtures" / "legacy-profile.toml"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (home / ".gitconfig").write_text("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n")
    # No test may ever reach the real session manager: SUW's own services run on this machine
    # under the very unit names the code under test would start, stop or restart.
    monkeypatch.setenv("PATH", f"{stub_bin(tmp_path / 'stub-bin')}{os.pathsep}{os.environ['PATH']}")
    # …nor the real tmux server: a session created by a test would live on in the developer's
    # own tmux, carrying this sandbox's environment into every pane opened afterwards.
    tmux_dir = tempfile.mkdtemp(prefix="suwt-")  # short: a Unix socket path has a length limit
    monkeypatch.setenv("TMUX_TMPDIR", tmux_dir)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    from suw.core import notify

    monkeypatch.setattr(notify, "_deliver", lambda *a, **k: True)
    from suw.core import events, proc
    from suw.integrations import apps, browser

    events._device.clear()
    # No test may ever open a real window on the developer's desktop.
    launched: list[list[str]] = []
    for module in (proc, apps, browser):
        monkeypatch.setattr(module, "spawn", lambda cmd, **kw: launched.append(list(cmd)) or True)
    yield home
    if shutil.which("tmux") and os.listdir(tmux_dir):
        subprocess.run(["tmux", "kill-server"], env={**os.environ, "TMUX_TMPDIR": tmux_dir}, capture_output=True)
    shutil.rmtree(tmux_dir, ignore_errors=True)


def sh(cwd, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture
def remote_pair(tmp_path):
    """A bare 'GitHub' plus two workstation clones ('ubuntu' and 'mac')."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    ubuntu, mac = tmp_path / "ubuntu" / "proj", tmp_path / "mac" / "proj"
    subprocess.run(["git", "clone", "-q", str(origin), str(ubuntu)], check=True, capture_output=True)
    (ubuntu / "a.txt").write_text("one\n")
    sh(ubuntu, "add", "-A")
    sh(ubuntu, "commit", "-q", "-m", "init")
    sh(ubuntu, "push", "-q", "-u", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(origin), str(mac)], check=True, capture_output=True)
    return origin, ubuntu, mac


POLICY = {
    "enabled": True,
    "idle_seconds": 0,
    "min_interval_seconds": 0,
    "max_file_mb": 1,
    "deny": [".env", ".env.*", "*.pem", "id_ed25519"],
    "allow": [".env.example"],
    "sync": True,
    "policy": "merge",
    "push": True,
}
# The shipped default: diverged history is never merged behind the user's back.
MANUAL = dict(POLICY, policy="manual")


# ── a real, throw-away SSH server on 127.0.0.1 (no root, no user files touched) ──


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LocalSSHD:
    """An unprivileged sshd with its own host key that accepts one throw-away client key."""

    def __init__(self, root: Path, client_pub: Path, port: int | None = None, home: Path | None = None, path: str = ""):
        import shutil

        self.binary = shutil.which("sshd") or "/usr/sbin/sshd"
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.client_pub = client_pub
        self.port = port or _free_port()
        self.proc = None
        # `home`: sessions get this directory as $HOME, so scripts that write below "$HOME"
        # stay inside the sandbox. `path`: a PATH for the session (stub commands first).
        self.home, self.path = home, path
        self.rekey()

    def rekey(self) -> None:
        for stale in self.root.glob("hostkey*"):
            stale.unlink()
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.root / "hostkey")], check=True)

    @property
    def host_key(self) -> str:
        kind, blob = (self.root / "hostkey.pub").read_text().split()[:2]
        return f"{kind} {blob}"

    @property
    def fingerprint(self) -> str:
        done = subprocess.run(["ssh-keygen", "-lf", str(self.root / "hostkey.pub")], capture_output=True, text=True, check=True)
        return done.stdout.split()[1]

    def start(self) -> "LocalSSHD":
        import socket
        import time

        (self.root / "authorized_keys").write_text(self.client_pub.read_text())
        (self.root / "authorized_keys").chmod(0o600)
        config = self.root / "sshd_config"
        config.write_text(
            f"Port {self.port}\nListenAddress 127.0.0.1\nHostKey {self.root / 'hostkey'}\n"
            f"AuthorizedKeysFile {self.root / 'authorized_keys'}\nPidFile {self.root / 'sshd.pid'}\n"
            "StrictModes no\nUsePAM no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
            "PubkeyAuthentication yes\nPrintMotd no\nLogLevel ERROR\n"
            + (f"SetEnv HOME={self.home}" + (f" PATH={self.path}" if self.path else "") + "\n" if self.home else "")
        )
        self.proc = subprocess.Popen([self.binary, "-D", "-e", "-f", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("sshd exited: " + self.proc.stderr.read().decode(errors="replace"))
            with socket.socket() as sock:
                sock.settimeout(0.3)
                if sock.connect_ex(("127.0.0.1", self.port)) == 0:
                    return self
            time.sleep(0.1)
        raise RuntimeError("sshd did not start")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def restart(self) -> "LocalSSHD":
        self.stop()
        return self.start()


@pytest.fixture
def ssh_lab(tmp_path, sandbox, monkeypatch):
    """Factory for local SSH servers plus a client config that points `ssh` at the sandbox.

    Skips when no sshd binary is installed. Yields (make_server, client_key_path).
    """
    import getpass
    import shutil

    if not (shutil.which("sshd") or Path("/usr/sbin/sshd").exists()) or not shutil.which("ssh-keygen"):
        pytest.skip("no sshd binary available")
    lab = tmp_path / "ssh-lab"
    lab.mkdir()
    key = lab / "client"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    ssh_dir = sandbox / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    config = lab / "ssh_config"
    config.write_text(
        f"Include {ssh_dir / 'suw.conf'}\n"
        "Host *\n"
        f"    IdentityFile {key}\n    IdentitiesOnly yes\n"
        f"    UserKnownHostsFile {ssh_dir / 'known_hosts'}\n    GlobalKnownHostsFile /dev/null\n"
    )
    monkeypatch.setenv("SUW_SSH_CONFIG", str(config))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    servers: list[LocalSSHD] = []

    def make(name: str = "a", client_pub: Path | None = None, home: Path | None = None, path: str = "") -> LocalSSHD:
        try:
            server = LocalSSHD(lab / name, client_pub or key.with_suffix(".pub"), home=home, path=path).start()
        except RuntimeError as exc:
            pytest.skip(f"cannot start a local sshd: {exc}")
        servers.append(server)
        return server

    make.user = getpass.getuser()
    make.key = key
    make.lab = lab
    yield make
    for server in servers:
        server.stop()


class FakeClock:
    """Injectable time source: tests advance it instead of sleeping."""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now
