"""Entry point of the packaged application (double-click, Start menu, launcher).

With arguments it behaves exactly like the `suw` command, so one executable serves both the
window and the background components the product starts for itself.
"""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1:
        from ..cli.main import main as cli

        if not getattr(sys, "frozen", False):
            return cli()
        # The packaged executable has no console. An unhandled error there opens a modal error
        # box and waits for a click — forever, when a script or a service started the command.
        try:
            return cli()
        except Exception as exc:
            return _report(exc)
    from . import launch

    return launch.run()


def _report(exc: Exception) -> int:
    """One line for whoever started the command; the full error goes to the event log."""
    try:
        from ..core import events

        events.emit("app.error", f"{' '.join(sys.argv[1:3])}: {type(exc).__name__}: {exc}", "error")
    except Exception:
        pass
    try:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    except Exception:
        pass
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
