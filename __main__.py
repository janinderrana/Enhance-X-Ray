"""Entry point when the plugin zip is run by an external Python:

    python -I ClaudeXRay.zip --xray <path> --text <path>

Deliberately imports nothing from calibre, so it works outside calibre's
environment (where the `anthropic` SDK actually lives).
"""

import sys

from enrich import main

if __name__ == "__main__":
    sys.exit(main())
