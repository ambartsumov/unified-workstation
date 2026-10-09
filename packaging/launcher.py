"""What the packaged executable runs. A frozen script has no parent package, so this file
imports the product by its absolute name instead of living inside it."""

import sys

from suw.app.entry import main

if __name__ == "__main__":
    sys.exit(main())
