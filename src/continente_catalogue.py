"""Continente's catalogue and review: the same as `python src/catalogue.py continente ...`.

Kept so the commands used so far still work:
    python src/continente_catalogue.py build | search <words> | review
"""

import sys

from catalogue import candidates, main as _main, normalise, tokens, word_matches  # noqa: F401 (used by other scripts)

if __name__ == "__main__":
    _main(["continente"] + sys.argv[1:])
