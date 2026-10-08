# Working on this repository (for coding agents and people alike)

- Python standard library only at runtime; Python 3.11+. Must run on Linux, macOS and Windows.
- Run `make check` (or at least `python3 -m pytest tests`) before claiming anything works.
  Tests are isolated from the real machine through `SUW_HOME` / `SUW_CONFIG_DIR` /
  `SUW_STATE_DIR`; never run the product against a real setup to "see if it works".
- New code does not branch on the operating system: add a method to `suw/platform/base.py`
  and implement it in each adapter. (Parts of the original engine still do — `core/bootstrap.py`
  and service control in `integrations/` — and are being moved behind the adapters.)
- Capabilities are detected, never assumed. Do not report an unsupported feature as healthy,
  and do not mark a platform verified without evidence.
- Hard invariants (tests enforce several): SSH host key checking stays on; remote history is
  never overwritten; work trees are never reset; no secrets in configuration, inventory or
  logs; the control channel and the window stay local-only.
- Every change to the user's machine goes through `suw.core.journal` (backup + record). User
  data is never journaled for removal.
- No user-facing sentence in logic: add keys to both `suw/resources/i18n/*.json` catalogs.
- Every state shown to a person is a symbol plus a word, never colour alone; no stack traces.
- Fixtures use documentation addresses and `example.com` only. `make sanitize` must stay clean.
- The product must work without any coding assistant installed.
- Design context for UI work: `PRODUCT.md` and `DESIGN.md`.
