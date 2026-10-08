"""Entry point of the packaged application (double-click, Start menu, launcher).

With arguments it behaves exactly like the `suw` command, so one executable serves both the
window and the background components the product starts for itself.
"""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1:
        from ..cli.main import main as cli

        return cli()
    from . import launch

    return launch.run()


if __name__ == "__main__":
    raise SystemExit(main())
