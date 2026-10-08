# Claude Code

Claude Code needs no special setup: it simply works inside your work folder. The application
never requires an assistant, and nothing depends on one being installed.

What the application adds is **clarity about what follows a project** to your other computers.
The **Assistant & Git** page classifies every relevant file:

| Class | Examples | Follows the project? |
|---|---|---|
| Project context — portable | `CLAUDE.md`, `.claude/settings.json`, `.claude/commands/`, `.claude/agents/`, `.claude/skills/`, `.mcp.json` | **Yes** — it lives in the work folder |
| Your personal preferences | `~/.claude/CLAUDE.md`, `~/.claude/settings.json` | No — per computer |
| Machine-specific | sign-in (`~/.claude/.credentials.json`), `.claude/settings.local.json` | No |
| Temporary session data | conversation history, to-do lists | No |

Only the first class is called portable. The application does not copy opaque global or
session state between computers and does not claim that it can.

## Two things worth knowing

- **`.claude/settings.local.json`** is meant for one computer, but it sits inside the project
  folder, so it is copied with it. Keep paths that are true on one computer only out of it.
- **Memory** is kept per computer by default. **Make memory follow the project** points it at
  a folder inside the project, so it syncs. Existing memory files are copied, never
  overwritten, and the change is previewed first.

## Other assistants

Claude Code is the first integration. Integrations are adapters behind one interface
(`suw/integrations/assistants.py`), so other coding agents and IDE assistants can be added
without changing the rest of the product.
