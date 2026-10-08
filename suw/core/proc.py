"""Subprocess helper: bounded, never raises, never prompts."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass
class Result:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def text(self) -> str:
        return (self.out or self.err).strip()


def run(
    cmd: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float = 30,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> Result:
    """Run a command. rc 127 = not installed, rc 124 = timed out."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        done = subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            env=full_env,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        return Result(done.returncode, done.stdout, done.stderr)
    except FileNotFoundError:
        return Result(127, "", f"{cmd[0]}: not installed")
    except subprocess.TimeoutExpired:
        return Result(124, "", f"{cmd[0]}: timed out after {timeout:g}s")
    except OSError as exc:
        return Result(126, "", f"{cmd[0]}: {exc}")


def ssh_cmd() -> list[str]:
    """`ssh`, optionally pinned to an explicit client config (SUW_SSH_CONFIG).

    The override exists so the test-suite can exercise real SSH against a throw-away
    local server without ever reading or writing the user's own ~/.ssh.
    """
    config = os.environ.get("SUW_SSH_CONFIG")
    return ["ssh", "-F", config] if config else ["ssh"]


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def spawn(cmd: Sequence[str], *, cwd: str | Path | None = None) -> bool:
    """Start a detached GUI/long-lived process. Returns False if it could not start."""
    try:
        subprocess.Popen(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False
