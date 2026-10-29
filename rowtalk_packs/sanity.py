"""Sanity gate (I6) — runs before anything is published.

Checks, per pack:
  * structural validation (validate.validate_pack) — NEVER overridable;
  * every golden query answers as expected;
  * size drift: article_count and installed_bytes within ±tolerance of the
    previous edition of the SAME pack id, where "previous" is the most
    recent editions-log line naming that pack (a publish or a rollback —
    i.e. what the app is currently being served); with no previous edition,
    the absolute first-edition floors/ceilings from config apply.
A failure of the overridable checks blocks publish unless force=true; the
verdict records that it was forced, and the editions log keeps that record.
"""


def _drift(current, previous):
    if not previous:
        return None
    return abs(current - previous) / float(previous)


def evaluate(article_count, installed_bytes, golden, validation_errors, previous, first_edition,
             tolerance=0.15, force=False):
    checks = []

    def add(name, ok, detail, overridable=True):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "overridable": overridable})

    add("validation", not validation_errors,
        "; ".join(validation_errors[:5]) if validation_errors else "ok", overridable=False)
    for item in golden:
        add("golden:%s:%s" % (item["mode"], item["query"]), item["ok"],
            "expected %r within %d, got %r" % (item["expect"], item["within"], item["got"]))
    if previous:
        for name, current, before in (("article_count", article_count, previous.get("article_count")),
                                      ("installed_bytes", installed_bytes,
                                       previous.get("installed_bytes"))):
            drift = _drift(current, before)
            ok = drift is not None and drift <= tolerance
            add("drift:%s" % name, ok, "%s -> %s (%s)" % (
                before, current, "n/a" if drift is None else "%+.1f%%" % (
                    100.0 * (current - before) / before)))
    else:
        floors = first_edition or {}
        add("first:min_articles", article_count >= floors.get("min_articles", 1),
            "%d >= %s" % (article_count, floors.get("min_articles", 1)))
        if "min_installed_bytes" in floors:
            add("first:min_installed_bytes", installed_bytes >= floors["min_installed_bytes"],
                "%d >= %d" % (installed_bytes, floors["min_installed_bytes"]))
        if "max_installed_bytes" in floors:
            add("first:max_installed_bytes", installed_bytes <= floors["max_installed_bytes"],
                "%d <= %d" % (installed_bytes, floors["max_installed_bytes"]))
    hard_fail = any(not c["ok"] and not c["overridable"] for c in checks)
    soft_fail = any(not c["ok"] and c["overridable"] for c in checks)
    passed = not hard_fail and (not soft_fail or force)
    return {"passed": passed, "forced": bool(force and soft_fail and not hard_fail),
            "checks": checks}


def summarize(gate):
    failed = [c for c in gate["checks"] if not c["ok"]]
    if not failed:
        return "gate passed (%d checks)" % len(gate["checks"])
    head = "gate %s" % ("FORCED" if gate["forced"] else ("passed" if gate["passed"] else "FAILED"))
    return head + ": " + "; ".join("%s (%s)" % (c["name"], c["detail"]) for c in failed[:6])
