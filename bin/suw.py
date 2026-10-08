"""Entry point used by the `suw` launcher (adds this checkout to sys.path)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from suw.cli.main import main  # noqa: E402

raise SystemExit(main())
