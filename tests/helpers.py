"""Test helpers: synthetic CirrusSearch docs/shards, a local dump server
(nginx-autoindex-like listings, HTTP Range, scripted connection drops), and
an in-memory fake of the GitHub seam."""

import bz2
import copy
import hashlib
import http.server
import json
import os
import re
import socket
import threading
import urllib.parse

from rowtalk_packs.config import load_config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def doc(page_id, title, ns=0, opening="Default lead sentence.", pop=None, links=None,
        redirects=None, categories=None, templates=None, source_text=None, extra=None):
    d = {"title": title, "namespace": ns, "opening_text": opening, "wiki": "testwiki",
         "text": "Body text that the builder ignores. " * 5, "page_id": page_id}
    if pop is not None:
        d["popularity_score"] = pop
    if links is not None:
        d["incoming_links"] = links
    if redirects is not None:
        d["redirect"] = [{"namespace": 0, "title": r} if isinstance(r, str) else r for r in redirects]
    if categories is not None:
        d["category"] = categories
    if templates is not None:
        d["template"] = templates
    if source_text is not None:
        d["source_text"] = source_text
    d.update(extra or {})
    return d


def bulk_lines(docs, action_ids=True, page_id_in_doc=True):
    out = []
    for d in docs:
        d = dict(d)
        pid = d.get("page_id")
        if not page_id_in_doc:
            d.pop("page_id", None)
        if action_ids:
            out.append(json.dumps({"index": {"_type": "page", "_id": str(pid)}}))
        out.append(json.dumps(d, ensure_ascii=False))
    return "\n".join(out) + "\n"


def bz2_bytes(text, streams=1):
    data = text.encode("utf-8")
    if streams <= 1:
        return bz2.compress(data, 9)
    step = max(1, len(data) // streams)
    parts = [data[i:i + step] for i in range(0, len(data), step)]
    return b"".join(bz2.compress(p, 9) for p in parts)


def test_config(**overrides):
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg["dump"]["retry"].update({"backoff_initial_s": 0, "backoff_max_s": 0})
    cfg["dump"]["listing_retry"].update({"backoff_initial_s": 0, "backoff_max_s": 0})
    cfg["dump"]["socket_timeout_s"] = 10
    cfg["dump"]["read_chunk_bytes"] = 4096
    cfg["dump"]["decompress_batch_bytes"] = 2000
    cfg["search"]["heavy_prefix_rows"] = 5
    cfg["staging"]["prune_every"] = 50
    cfg["staging"]["batch"] = 7
    for lang, essential, extended in (("pt", 30, 120), ("en", 40, 150)):
        lcfg = cfg["wiki"]["languages"][lang]
        lcfg["tiers"] = {"essential": essential, "extended": extended}
        lcfg["first_edition"] = {
            "essential": {"min_articles": essential, "min_installed_bytes": 1000,
                          "max_installed_bytes": 10 ** 8},
            "extended": {"min_articles": extended, "min_installed_bytes": 1000,
                         "max_installed_bytes": 10 ** 8}}
    for key, value in overrides.items():
        cfg[key] = value
    return cfg


# --------------------------------------------------------------------------
# Synthetic dump tree
# --------------------------------------------------------------------------

PT_TITLES = ["Brasil", "Rio de Janeiro", "Portugal", "São Paulo", "Lisboa", "Amazonas",
             "Brasília", "Rio Grande do Sul", "Rio Negro", "Riodejaneirense"]
EN_TITLES = ["United States", "Rio de Janeiro", "Brazil", "United Kingdom", "London",
             "Paris", "Brazilian Portuguese", "Rio Grande", "Unity", "Union"]


def synthetic_docs(lang, count, seed_titles):
    """Deterministic docs: the seed titles are the most popular; the rest are
    filler. Includes every exclusion case so the builder must filter."""
    docs = []
    pid = 1000
    for index, title in enumerate(seed_titles):
        pid += 1
        aliases = {"Brasil": ["Brazil", "República Federativa do Brasil", "BR"],
                   "United States": ["USA", "U.S.A.", "United States of America", "US"],
                   "Rio de Janeiro": ["Rio", "Cidade Maravilhosa"]}.get(title, [])
        # incoming_links runs OPPOSITE to popularity (TH-07), so any build
        # that ranked a tier by links instead would visibly differ.
        docs.append(doc(pid, title, opening="%s is a place. It has people. %s" % (title, "x " * 5),
                        pop=1.0 - index * 0.01, links=index, redirects=aliases))
    for n in range(count):
        pid += 1
        title = "%s Article %04d" % ("Filler" if n % 2 else "Item", n)
        docs.append(doc(pid, title, opening="Sentence one about %s. Sentence two." % title,
                        pop=0.5 / (n + 2), links=20 + n, redirects=["Alias %04d" % n]))
    # exclusions
    docs.append(doc(9_000_001, "Talk page", ns=1, pop=2.0))
    docs.append(doc(9_000_002, "Mercury (%s)" % ("desambiguação" if lang == "pt" else "disambiguation"),
                    pop=2.0))
    docs.append(doc(9_000_003, "Empty lead", opening="", pop=2.0))
    docs.append(doc(9_000_004, "Redirect page", pop=2.0,
                    source_text="#REDIRECT [[Brasil]]" if lang == "en" else "#REDIRECIONAMENTO [[Brasil]]"))
    return docs


def write_dump_tree(files, base, date, wiki, docs, shards, success=True, action_ids=True,
                    streams=1):
    """Fill `files` ({url path: bytes}) with a dump index dir for `wiki`."""
    prefix = "%s%s/index_name=%s_content/" % (base, date, wiki)
    per = (len(docs) + shards - 1) // shards
    for index in range(shards):
        chunk = docs[index * per:(index + 1) * per]
        name = "%s_content-%s-%05d.json.bz2" % (wiki, date, index)
        files[prefix + name] = bz2_bytes(bulk_lines(chunk, action_ids=action_ids), streams)
    if success:
        files[prefix + "_SUCCESS"] = b""
    return prefix


class DumpHandler(http.server.BaseHTTPRequestHandler):
    server_version = "nginx-test"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        state = self.server.state
        with state["lock"]:
            until = state.get("hold_until", 2)
            # TH4-01: holds wait on LATCHED events, never on the instantaneous
            # `active`. `active` is a level: the request that raises it to
            # `until` can be served and lower it again before a parked
            # handler re-acquires the lock, and that handler would miss its
            # wakeup and sit out `hold_s` on a CORRECT client (a red test
            # once hold_timeouts is asserted). `overlaps` / `excess` count
            # the ARRIVALS that made `active` reach / pass `until`; they only
            # grow, so no wakeup is lost. A handler compares them with their
            # values from just BEFORE its own arrival: its own arrival
            # counts, an overlap from an earlier phase of a shared server
            # never does. Who clears them: nothing (monotonic per server).
            overlaps_before = state.get("overlaps", 0)
            excess_before = state.get("excess", 0)
            state["active"] += 1
            if state["active"] >= until:
                state["overlaps"] = overlaps_before + 1
            if state["active"] > until:
                state["excess"] = excess_before + 1
            state["max_active"] = max(state["max_active"], state["active"])
            state["requests"].append((self.path, self.headers.get("Range"),
                                      self.headers.get("User-Agent")))
            state["lock"].notify_all()
            # Make concurrency OBSERVABLE (TH-03): hold the first
            # `hold_requests` file GETs until an arrival has made `hold_until`
            # requests (default 2) active at once, or `hold_s` passes. Connections the client opens
            # therefore overlap deterministically, up to `hold_until`; with
            # fewer the hold simply times out (that bounded wait is the
            # observation window of a negative assertion). `hold_linger_s`
            # (TH2-03): once `hold_until` are active, keep holding up to that
            # long for one MORE — the bounded window in which an excess
            # connection is observed, while the overlap itself waited on a
            # condition, not a duration.
            # TH3-01: a hold that TIMES OUT is counted in `hold_timeouts`. A
            # test asserting overlap must assert it stayed 0 AND keep hold_s
            # below the client's socket timeout: otherwise a SERIAL client
            # times out, reconnects while this handler is still parked, and
            # the dead request plus its retry fake the overlap.
            if state["hold_requests"] > 0 and not self.path.endswith("/"):
                state["hold_requests"] -= 1
                if not state["lock"].wait_for(
                        lambda: state.get("overlaps", 0) > overlaps_before,
                        timeout=state["hold_s"]):
                    state["hold_timeouts"] = state.get("hold_timeouts", 0) + 1
                linger = state.get("hold_linger_s", 0.0)
                if linger:
                    state["lock"].wait_for(lambda: state.get("excess", 0) > excess_before,
                                           timeout=linger)
        try:
            self._serve(state)
        finally:
            with state["lock"]:
                state["active"] -= 1

    def _listing(self, path):
        files = self.server.files
        children = set()
        for key in files:
            if key.startswith(path) and key != path:
                rest = key[len(path):]
                head = rest.split("/", 1)[0]
                children.add(head + ("/" if "/" in rest else ""))
        if not children:
            return None
        links = "".join('<a href="%s">%s</a>  01-Jan-2026 00:00  -\n'
                        % (urllib.parse.quote(c), c) for c in sorted(children))
        return ('<html><head><title>Index of %s</title></head><body><h1>Index of %s</h1>'
                '<hr><pre><a href="../">../</a>\n%s</pre><hr></body></html>' % (path, path, links)
                ).encode("utf-8")

    def _serve(self, state):
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        files = self.server.files
        listing_errors = state.get("errors", {}).get(path) if path.endswith("/") else None
        if listing_errors:  # scripted failures of a LISTING request (OPS2-06)
            count = state["counts"].get(path, 0)
            state["counts"][path] = count + 1
            if count < len(listing_errors) and listing_errors[count] is not None:
                status, retry_after = listing_errors[count]
                return self._send(status, b"busy",
                                  {"Retry-After": retry_after} if retry_after else {})
        if path.endswith("/"):
            body = self._listing(path)
            if body is None:
                return self._send(404, b"not found")
            return self._send(200, body, {"Content-Type": "text/html"})
        if path not in files:
            return self._send(404, b"not found")
        data = files[path]
        versions = state.get("versions", {}).get(path)
        if versions:  # a file whose content changes per request
            data = versions[min(state["counts"].get(path, 0), len(versions) - 1)]
        etag = '"%s"' % hashlib.sha1(data).hexdigest()
        count = state["counts"].get(path, 0)
        state["counts"][path] = count + 1
        errors = state.get("errors", {}).get(path, [])
        if count < len(errors) and errors[count] is not None:
            status, retry_after = errors[count]  # e.g. (503, "120")
            return self._send(status, b"busy", {"Retry-After": retry_after} if retry_after else {})
        start = 0
        status = 200
        range_header = self.headers.get("Range")
        if range_header and path not in state["ignore_range"]:
            if_range = self.headers.get("If-Range")
            if if_range is None or if_range == etag:
                match = re.match(r"bytes=(\d+)-$", range_header)
                if match:
                    start = int(match.group(1))
                    status = 206
        if range_header and path in state["ignore_range"]:
            state["ignore_range"].discard(path)
        body = data[start:]
        headers = {"Content-Length": str(len(body)), "ETag": etag,
                   "Content-Type": "application/octet-stream"}
        if status == 206:
            headers["Content-Range"] = "bytes %d-%d/%d" % (start, len(data) - 1, len(data))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        drops = state["drops"].get(path, [])
        if count < len(drops) and drops[count] is not None:
            cut = drops[count]
            self.wfile.write(body[:cut])
            self.wfile.flush()
            # Abrupt close mid-body: the client sees a short read.
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
            hook = state.get("on_drop")
            if hook is not None:
                hook(path, count)  # runs before the client can reconnect
            return
        self.wfile.write(body)

    def _send(self, status, body, headers=None):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


class DumpServer:
    """files: {absolute url path: bytes}. drops: {path: [cut offset per
    request or None]}. ignore_range: paths whose first Range request is
    answered 200 with the whole file."""

    def __init__(self, files, drops=None, ignore_range=None):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DumpHandler)
        self.httpd.daemon_threads = True
        self.httpd.files = files
        self.httpd.state = {"lock": threading.Condition(), "active": 0, "max_active": 0,
                            "requests": [], "counts": {}, "drops": drops or {},
                            "ignore_range": set(ignore_range or ()),
                            "hold_requests": 0, "hold_s": 0.0, "hold_timeouts": 0,
                            "overlaps": 0, "excess": 0}
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02},
                                       daemon=True)

    @property
    def state(self):
        return self.httpd.state

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.httpd.server_address[1], path)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------
# Fake GitHub seam
# --------------------------------------------------------------------------

class FakeGitHub:
    def __init__(self, store_dir, repo_root=None):
        self.store_dir = store_dir
        self.repo_root = repo_root
        self.releases = {}
        self.next_id = 100
        self.calls = []
        self.upstream = {}
        self.corrupt_digest = set()
        # Fault injection (each defaults to "no fault"):
        self.size_offset = {}        # asset name -> bytes added to the LISTED size only
        self.public_failures = {}    # asset name -> number of public GETs that fail (-1 = all)
        self.publish_error = None    # exception publish_release raises (before undrafting)
        self.main_root = None        # file_on_branch reads here instead of repo_root
        self.public_requests = []

    def _id(self):
        self.next_id += 1
        return self.next_id

    def list_releases(self):
        self.calls.append("list_releases")
        return [dict(r, assets=None) for r in self.releases.values()]

    def get_release(self, release_id):
        return dict(self.releases[int(release_id)])

    def find_release(self, tag):
        for release in self.releases.values():
            if release["tag_name"] == tag:
                return dict(release)
        return None

    def create_draft(self, tag, name, body, target="main"):
        rid = self._id()
        self.releases[rid] = {"id": rid, "tag_name": tag, "name": name, "draft": True,
                              "assets": {}}
        self.calls.append(("create_draft", tag))
        return dict(self.releases[rid])

    def publish_release(self, release_id):
        if self.publish_error is not None:
            self.calls.append(("publish-failed", release_id))
            raise self.publish_error
        self.releases[int(release_id)]["draft"] = False
        self.calls.append(("publish", release_id))
        return dict(self.releases[int(release_id)])

    def delete_release(self, release):
        self.calls.append(("delete", release["tag_name"]))
        del self.releases[int(release["id"])]

    def list_assets(self, release_id):
        release = self.releases[int(release_id)]
        return [{"id": a["id"], "name": name, "size": len(a["data"]) + self.size_offset.get(name, 0),
                 "state": "uploaded",
                 "digest": "sha256:" + (("0" * 64) if name in self.corrupt_digest
                                        else hashlib.sha256(a["data"]).hexdigest())}
                for name, a in release["assets"].items()]

    def upload_asset(self, release_id, path, name, content_type="application/octet-stream"):
        with open(path, "rb") as handle:
            data = handle.read()
        self.releases[int(release_id)]["assets"][name] = {"id": self._id(), "data": data}
        self.calls.append(("upload", name))
        return {"name": name}

    def _asset_data(self, asset_id):
        for release in self.releases.values():
            for a in release["assets"].values():
                if a["id"] == asset_id:
                    return a["data"]
        raise KeyError(asset_id)

    def asset_sha256(self, asset):
        return asset["digest"].split(":", 1)[1], "api-digest"

    def download_asset_text(self, asset, limit=1 << 20):
        return self._asset_data(asset["id"]).decode("utf-8")

    def asset_bytes(self, tag, name):
        return self.find_release_raw(tag)["assets"][name]["data"]

    def find_release_raw(self, tag):
        for release in self.releases.values():
            if release["tag_name"] == tag:
                return release
        raise KeyError(tag)

    def public_url_ok(self, url):
        self.public_requests.append(url)
        match = re.search(r"/releases/download/([^/]+)/([^/]+)$", url)
        if not match:
            return False
        left = self.public_failures.get(match.group(2), 0)
        if left:
            if left > 0:
                self.public_failures[match.group(2)] = left - 1
            return False
        try:
            release = self.find_release_raw(match.group(1))
        except KeyError:
            return False
        return not release["draft"] and match.group(2) in release["assets"]

    def file_on_branch(self, path, branch="main"):
        full = os.path.join(self.main_root or self.repo_root, path)
        if not os.path.exists(full):
            return None
        with open(full, "r", encoding="utf-8") as handle:
            return handle.read()

    def latest_release_of(self, repository):
        return self.upstream[repository]
