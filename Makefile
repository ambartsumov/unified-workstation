# Developer entry points. Everything here runs without private infrastructure.
PY ?= python3
RUN = env -u PYTHONPATH $(PY)

.PHONY: help test lint check sanitize docs demo run package clean
help:
	@echo "make test      run the test-suite"
	@echo "make lint      static checks (ruff, if installed) + syntax"
	@echo "make sanitize  scan for secrets, private endpoints and personal data"
	@echo "make docs      check documentation links and product metadata"
	@echo "make check     everything CI runs"
	@echo "make demo      open the application window with sample data"
	@echo "make run       open the application window"
	@echo "make package   build a packaged application for this platform (needs: pip install '.[package]')"

test:
	$(RUN) -m pytest tests

lint:
	$(RUN) -m compileall -q suw bin scripts
	@if $(RUN) -m ruff --version >/dev/null 2>&1; then $(RUN) -m ruff check suw scripts tests; else echo "ruff not installed: skipped (pip install ruff)"; fi

sanitize:
	$(RUN) scripts/sanitize.py

docs:
	$(RUN) scripts/check_docs.py
	$(RUN) scripts/product.py --check

check: lint sanitize docs test

demo:
	$(RUN) bin/suw.py app --demo

run:
	$(RUN) bin/suw.py app

package:
	$(RUN) -m PyInstaller --noconfirm packaging/unified-workstation.spec

clean:
	$(RUN) -c "import shutil; [shutil.rmtree(p, ignore_errors=True) for p in ('build', 'dist', '.pytest_cache', '.ruff_cache')]"
