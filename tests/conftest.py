"""Make `curie` (src layout) and `tests.*` importable without an install,
and pin the conventions the fixtures were written with: Polish and
English word lists, and the first customer's document key (an agreement
written number/year, stored as a zero padded number and a year). The
product itself takes these from the configuration (CURIE_LANGUAGES,
KEY_PATTERN, KEY_ROLES, VALUE_STOP_CODES)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from curie import config  # noqa: E402

config.CURIE_LANGUAGES = ["pl", "en"]
config.KEY_PATTERN = r"\b(\d{1,7})/(\d{4})\b"
config.KEY_ROLES = "nrum:7,rokum:0"
config.VALUE_STOP_CODES = ["PZST", "TOW", "DX", "DW"]

from curie.memory import shape  # noqa: E402

shape.configure()


@pytest.fixture(autouse=True)
def _conventions():
    """Every test starts from the pinned conventions, whatever another
    test changed through configure()."""
    shape.configure(config.KEY_PATTERN, config.KEY_ROLES, config.VALUE_STOP_CODES)
    yield
