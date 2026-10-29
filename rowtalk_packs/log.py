"""Small, flush-on-write log lines. Never pass article text here (I3: logs
stay small and never carry whole articles)."""

import sys
import time

_START = time.monotonic()


def log(message):
    print("[packs %7.1fs] %s" % (time.monotonic() - _START, message), file=sys.stderr, flush=True)


def error_annotation(message):
    """Log `message` and emit it as a GitHub Actions error annotation (a
    workflow command must START a stdout line, so it cannot go through
    `log`, which prefixes)."""
    log(message)
    print("::error::%s" % " ".join(message.split()), flush=True)


def warning_annotation(message):
    """Like error_annotation, as a warning (visible, does not fail a step)."""
    log(message)
    print("::warning::%s" % " ".join(message.split()), flush=True)
