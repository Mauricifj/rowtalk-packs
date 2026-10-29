"""Minimal GitHub REST client (stdlib) — the seam the publish logic talks to.

Everything the workflows do to releases goes through `GitHubClient`; tests
swap in a fake with the same methods. Requests are few per run (tens), well
under the GITHUB_TOKEN limit of 1000/h.
"""

import base64
import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .log import log

API = "https://api.github.com"
UPLOADS_HOST = "uploads.github.com"


class GitHubError(Exception):
    def __init__(self, status, message):
        super().__init__("GitHub %s: %s" % (status, message))
        self.status = status


class GitHubClient:
    def __init__(self, repository, token=None, user_agent="rowtalk-packs", retries=3, sleep=time.sleep):
        self.repository = repository
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.user_agent = user_agent
        self.retries = retries
        self.sleep = sleep

    # -- plumbing -------------------------------------------------------
    def _headers(self, extra=None):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": self.user_agent,
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        headers.update(extra or {})
        return headers

    def _request(self, method, path_or_url, body=None, expect=(200,)):
        url = path_or_url if path_or_url.startswith("http") else API + path_or_url
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(1, self.retries + 1):
            request = urllib.request.Request(url, data=data, method=method,
                                             headers=self._headers({"Content-Type": "application/json"} if data else None))
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = response.read()
                    if response.status not in expect:
                        raise GitHubError(response.status, payload[:200])
                    return json.loads(payload) if payload else None
            except urllib.error.HTTPError as error:
                if error.code in expect:
                    return None
                if error.code >= 500 and attempt < self.retries:
                    self.sleep(2 * attempt)
                    continue
                raise GitHubError(error.code, error.read()[:300].decode("utf-8", "replace"))
            except (OSError, http.client.HTTPException) as error:
                if attempt < self.retries:
                    self.sleep(2 * attempt)
                    continue
                raise GitHubError("network", str(error))
        raise GitHubError("retries", url)

    def _repo(self, suffix):
        return "/repos/%s%s" % (self.repository, suffix)

    # -- releases -------------------------------------------------------
    def list_releases(self):
        releases = []
        page = 1
        while True:
            batch = self._request("GET", self._repo("/releases?per_page=100&page=%d" % page))
            releases.extend(batch or [])
            if not batch or len(batch) < 100:
                return releases
            page += 1

    def get_release(self, release_id):
        return self._request("GET", self._repo("/releases/%d" % int(release_id)))

    def find_release(self, tag):
        """Draft or published release with `tag` (drafts are only visible in
        the list endpoint), or None."""
        for release in self.list_releases():
            if release.get("tag_name") == tag:
                return release
        return None

    def create_draft(self, tag, name, body, target="main"):
        return self._request("POST", self._repo("/releases"), {
            "tag_name": tag, "name": name, "body": body, "draft": True,
            "prerelease": False, "target_commitish": target}, expect=(201,))

    def publish_release(self, release_id):
        return self._request("PATCH", self._repo("/releases/%d" % int(release_id)),
                             {"draft": False, "make_latest": "false"})

    def delete_release(self, release):
        self._request("DELETE", self._repo("/releases/%d" % int(release["id"])), expect=(204,))
        if not release.get("draft"):
            ref = urllib.parse.quote("tags/%s" % release["tag_name"], safe="/")
            try:
                self._request("DELETE", self._repo("/git/refs/%s" % ref), expect=(204,))
            except GitHubError as error:
                if error.status not in (404, 422):
                    raise

    def list_assets(self, release_id):
        return self._request("GET", self._repo("/releases/%d/assets?per_page=100" % int(release_id))) or []

    def delete_asset(self, asset_id):
        self._request("DELETE", self._repo("/releases/assets/%d" % int(asset_id)), expect=(204,))

    def upload_asset(self, release_id, path, name, content_type="application/octet-stream"):
        """Stream `path` as asset `name`. A failed attempt may leave a broken
        asset with that name; it is deleted before the next attempt."""
        size = os.path.getsize(path)
        query = urllib.parse.urlencode({"name": name})
        target = "/repos/%s/releases/%d/assets?%s" % (self.repository, int(release_id), query)
        for attempt in range(1, self.retries + 1):
            for asset in self.list_assets(release_id):
                if asset.get("name") == name:
                    self.delete_asset(asset["id"])
            connection = http.client.HTTPSConnection(UPLOADS_HOST, timeout=600, blocksize=1 << 20)
            try:
                with open(path, "rb") as handle:
                    connection.request("POST", target, body=handle, headers=self._headers({
                        "Content-Type": content_type, "Content-Length": str(size)}))
                    response = connection.getresponse()
                    payload = response.read()
                if response.status == 201:
                    return json.loads(payload)
                error = GitHubError(response.status, payload[:300])
            except (OSError, http.client.HTTPException) as exc:
                error = GitHubError("network", str(exc))
            finally:
                connection.close()
            log("upload %s attempt %d failed: %s" % (name, attempt, error))
            if attempt < self.retries:
                self.sleep(5 * attempt)
        raise error

    def asset_sha256(self, asset):
        """sha256 of an asset: the API `digest` when present, else a streamed
        download (authenticated, works for drafts)."""
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:"):
            return digest.split(":", 1)[1].lower(), "api-digest"
        url = API + self._repo("/releases/assets/%d" % int(asset["id"]))
        request = urllib.request.Request(url, headers=self._headers({"Accept": "application/octet-stream"}))
        hasher = hashlib.sha256()
        with urllib.request.urlopen(request, timeout=600) as response:
            for block in iter(lambda: response.read(1 << 20), b""):
                hasher.update(block)
        return hasher.hexdigest(), "download"

    def download_asset_text(self, asset, limit=1 << 20):
        url = API + self._repo("/releases/assets/%d" % int(asset["id"]))
        request = urllib.request.Request(url, headers=self._headers({"Accept": "application/octet-stream"}))
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read(limit).decode("utf-8")

    def public_url_ok(self, url):
        """True when the public download URL answers a one-byte Range GET
        (after redirects) with 206 or 200 — proof the asset is reachable the
        way the app fetches it, Range included."""
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent,
                                                       "Range": "bytes=0-0"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                response.read(1)
                return response.status in (200, 206)
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            return False

    # -- repository content ----------------------------------------------
    def file_on_branch(self, path, branch="main"):
        """Raw text of `path` on `branch`, or None if absent."""
        try:
            data = self._request("GET", self._repo("/contents/%s?ref=%s" % (
                urllib.parse.quote(path), urllib.parse.quote(branch))))
        except GitHubError as error:
            if error.status == 404:
                return None
            raise
        return base64.b64decode(data["content"]).decode("utf-8")

    def latest_release_of(self, repository):
        return self._request("GET", "/repos/%s/releases/latest" % repository)
