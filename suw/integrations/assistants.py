"""Coding-assistant integrations, behind one adapter interface.

The product never requires an assistant and never teaches one about itself: an assistant
simply works inside the work folder. What an adapter adds is *classification* — telling the
user which of the assistant's files follow a project to another computer and which stay
behind, and why.

Every path an assistant uses falls into exactly one class:

    PROJECT_LOCAL      lives inside the project; travels with the work folder
    USER_GLOBAL        the user's own preferences; per computer unless they copy it
    MACHINE_SPECIFIC   true only on this computer (sign-in, absolute paths, local overrides)
    SESSION            transcripts and caches; temporary, never synchronised

Only PROJECT_LOCAL state is treated as portable. Nothing here claims otherwise.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import platform as osplatform
from ..core import paths
from ..core.config import Config

PROJECT_LOCAL, USER_GLOBAL, MACHINE_SPECIFIC, SESSION = "PROJECT_LOCAL", "USER_GLOBAL", "MACHINE_SPECIFIC", "SESSION"


@dataclass
class StateItem:
    path: str                 # shown to the user; `~` for the home folder, project-relative otherwise
    kind: str
    portable: bool
    present: bool
    what: str                 # translation key suffix: assistant.<id>.item.<what>

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    id: str
    name: str
    installed: bool
    version: str = ""
    projects: list[dict] = field(default_factory=list)
    global_state: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class Assistant:
    """One coding assistant. Subclasses describe their files; they never copy opaque state."""

    id = ""
    name = ""
    executables: list[str] = []

    def executable(self) -> str:
        os_ = osplatform.current()
        return next((found for found in (os_.which(exe) for exe in self.executables) if found), "")

    def installed(self) -> bool:
        return bool(self.executable())

    def project_state(self, project: Path) -> list[StateItem]:
        return []

    def global_state(self) -> list[StateItem]:
        return []

    def report(self, cfg: Config) -> Report:
        return Report(self.id, self.name, self.installed())


class ClaudeCode(Assistant):
    id = "claude"
    name = "Claude Code"
    executables = ["claude"]

    def project_state(self, project: Path) -> list[StateItem]:
        def item(rel: str, kind: str, what: str) -> StateItem:
            return StateItem(rel, kind, kind == PROJECT_LOCAL, (project / rel).exists(), what)

        return [
            item("CLAUDE.md", PROJECT_LOCAL, "instructions"),
            item(".claude/settings.json", PROJECT_LOCAL, "settings"),
            item(".claude/commands", PROJECT_LOCAL, "commands"),
            item(".claude/agents", PROJECT_LOCAL, "agents"),
            item(".claude/skills", PROJECT_LOCAL, "skills"),
            item(".claude/rules", PROJECT_LOCAL, "rules"),
            item(".mcp.json", PROJECT_LOCAL, "mcp"),
            item(".claude/memory", PROJECT_LOCAL, "memory"),
            # Inside the project folder, yet meant for one computer by the assistant's own design.
            item(".claude/settings.local.json", MACHINE_SPECIFIC, "local_settings"),
        ]

    def global_state(self) -> list[StateItem]:
        home = paths.home()

        def item(rel: str, kind: str, what: str) -> StateItem:
            return StateItem("~/" + rel, kind, False, (home / rel).exists(), what)

        return [
            item(".claude/CLAUDE.md", USER_GLOBAL, "user_instructions"),
            item(".claude/settings.json", USER_GLOBAL, "user_settings"),
            item(".claude/.credentials.json", MACHINE_SPECIFIC, "credentials"),
            item(".claude.json", MACHINE_SPECIFIC, "machine_state"),
            item(".claude/projects", SESSION, "transcripts"),
            item(".claude/todos", SESSION, "todos"),
            item(".claude/shell-snapshots", SESSION, "snapshots"),
        ]

    def report(self, cfg: Config) -> Report:
        from ..core import capabilities
        from . import claudestate

        exe = self.executable()
        out = Report(self.id, self.name, bool(exe), capabilities._version(exe, ["--version"]) if exe else "")
        for project in claudestate.projects(cfg)[:60]:
            row = claudestate.row(project)
            state = [entry.as_dict() for entry in self.project_state(project) if entry.present]
            out.projects.append({**row, "state": state, "local_overrides": any(e["kind"] == MACHINE_SPECIFIC for e in state)})
        out.global_state = [entry.as_dict() for entry in self.global_state()]
        return out


REGISTRY: list[Assistant] = [ClaudeCode()]


def get(ident: str) -> Assistant | None:
    return next((assistant for assistant in REGISTRY if assistant.id == ident), None)


def reports(cfg: Config) -> list[dict]:
    return [assistant.report(cfg).as_dict() for assistant in REGISTRY]
