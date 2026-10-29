"""fold_v1 — the ONE search-key function shared by the builder and the app.

Invariant I1 (FOLD PARITY): a query typed in the app finds an index key iff
fold(query) is a prefix of fold(title or alias). Both sides must therefore
compute the identical function. To make that portable to Dart (which has no
Unicode normalization), every Unicode decision lives in the frozen DATA file
`fold/fold_table.json`; the code below is the complete algorithm:

  1. Map every code point through the table (absent -> itself).
  2. Collapse every run of U+0020 SPACE to one U+0020.
  3. Remove leading and trailing U+0020.

This module never calls unicodedata or str.lower() — doing so would make the
Python side depend on the runner's Unicode version, which is exactly the
drift the table exists to prevent.
"""

import json
import os
import re

FOLD_VERSION = 1

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLE_PATH = os.path.join(REPO_ROOT, "fold", "fold_table.json")
VECTORS_PATH = os.path.join(REPO_ROOT, "fold", "fold_vectors.json")

_SPACES = re.compile(" +")


def _load_table(path=TABLE_PATH):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("fold_version") != FOLD_VERSION:
        raise ValueError("fold table %s is version %r, expected %d"
                         % (path, data.get("fold_version"), FOLD_VERSION))
    # str.translate takes {code point: replacement string}.
    return {int(key, 16): value for key, value in data["entries"].items()}


_TABLE = _load_table()


def fold(text):
    """Return the fold_v1 key of `text` (may be the empty string)."""
    return _SPACES.sub(" ", text.translate(_TABLE)).strip(" ")


def cp_len(text):
    """Length in Unicode code points (Python str length; Dart `runes.length`)."""
    return len(text)


# The exclusive upper bound of a prefix range: every key starting with p is
# in [p, p + PREFIX_SENTINEL). Exact because fold_v1 never emits U+10FFFF
# (a noncharacter, mapped to "" by rule R1) — see SCHEMA.md.
PREFIX_SENTINEL = "\U0010FFFF"


def prefix_upper_bound(prefix):
    return prefix + PREFIX_SENTINEL
