"""GitHub REST provider (free; optional GITHUB_TOKEN raises the rate limit 60 -> 5000 req/h)."""
from __future__ import annotations

import re
import urllib.parse
from datetime import timedelta

from .. import http
from ..chain.registry import chains_mentioned
from ..config import Settings
from ..models import iso, utcnow
from ..safety import sanitize_text, short
from .base import Provider, empty_bundle

API = "https://api.github.com"
ADDR_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
HIRING_RE = re.compile(r"\b(we'?re hiring|we are hiring|join (our|the) team|open positions|careers)\b", re.I)
AUDIT_RE = re.compile(r"\baudit(s|ed| report)?\b", re.I)
# owner/repo names as GitHub allows them; anything else (../, ?, #, %2e ...) is never put into an API path,
# so a campaign spec cannot steer an authenticated request (GITHUB_TOKEN) to another API endpoint.
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


def valid_owner(name: str | None) -> bool:
    return bool(name) and bool(OWNER_RE.match(name))


def valid_repo(full: str | None) -> bool:
    return bool(full) and bool(REPO_RE.match(full)) and full.split("/", 1)[1] not in (".", "..")


class GitHubProvider(Provider):
    name = "github"
    capabilities = frozenset({"discover", "evidence"})
    costs = {"discover": 0.0, "evidence": 0.0}

    def __init__(self, settings: Settings, api_base: str = API, max_repos_per_project: int = 2) -> None:
        self.settings = settings
        self.api = api_base.rstrip("/")
        self.max_repos = max_repos_per_project

    def _headers(self, raw: bool = False) -> dict:
        h = {"Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        if self.settings.github_token:
            h["Authorization"] = f"Bearer {self.settings.github_token}"
        return h

    def _get(self, path: str, raw: bool = False):
        url = f"{self.api}{path}"
        if raw:
            _, _, body = http.request("GET", url, headers=self._headers(True), timeout=self.settings.http_timeout,
                                      user_agent=self.settings.user_agent)
            return body.decode("utf-8", "replace")
        return http.get_json(url, headers=self._headers(), timeout=self.settings.http_timeout,
                             user_agent=self.settings.user_agent)

    # ------------------------------------------------------------------ discover
    def discover(self, campaign: dict) -> list[dict]:
        per_query = max(5, min(30, int(campaign.get("max_candidates", 25))))
        owners: dict[str, dict] = {}
        for q in campaign.get("github_queries", []):
            query = q if "fork:" in q else f"{q} fork:false"
            data = self._get(f"/search/repositories?q={urllib.parse.quote(query)}&sort=updated&order=desc"
                             f"&per_page={per_query}")
            for item in data.get("items", []):
                if item.get("fork") or item.get("archived"):
                    continue
                owner = item["owner"]["login"]
                if not valid_owner(owner) or not valid_repo(item.get("full_name")):
                    continue
                c = owners.setdefault(owner.lower(), {
                    "name": owner, "github_org": owner, "repos": [], "website": item.get("homepage") or None,
                    "description": sanitize_text(item.get("description") or "", 300)[0],
                    "sectors": [], "chains": [], "source_url": item["html_url"], "topics": [],
                })
                if item["full_name"] not in c["repos"]:
                    c["repos"].append(item["full_name"])
                c["topics"] = sorted(set(c["topics"]) | set(item.get("topics") or []))
                if not c["website"] and item.get("homepage"):
                    c["website"] = item["homepage"]
        return list(owners.values())

    # ------------------------------------------------------------------ evidence
    def evidence(self, candidate: dict, campaign: dict) -> dict:
        b = empty_bundle()
        org = candidate.get("github_org")
        if org and not valid_owner(org):
            b["facts"]["invalid_github_org"] = True
            org = None
        if org:
            try:
                u = self._get(f"/users/{urllib.parse.quote(org)}")
                prof = f"https://github.com/{org}"
                if u.get("blog"):
                    b["links"].setdefault("repo_homepages", []).append(u["blog"])
                email = u.get("email")
                if isinstance(email, str) and re.fullmatch(r"[^@\s<>()]{1,64}@[A-Za-z0-9.-]{1,190}", email):
                    b["contacts"].append({"type": "email", "value": email, "source_url": prof})
                tw = u.get("twitter_username")
                if isinstance(tw, str) and re.fullmatch(r"[A-Za-z0-9_]{1,15}", tw):
                    b["contacts"].append({"type": "x", "value": "@" + tw, "source_url": prof})
                b["facts"]["org_type"] = u.get("type")
                b["facts"]["org_name"] = u.get("name")
            except http.HttpError as e:
                if e.status in (403, 429):
                    raise
        since = iso(utcnow() - timedelta(days=90))
        repos = [r for r in candidate.get("repos", []) if valid_repo(r)]
        if len(repos) != len(candidate.get("repos", [])):
            b["facts"]["invalid_repo_names_skipped"] = True
        for full in repos[: self.max_repos]:
            repo = self._get(f"/repos/{full}")
            url = repo["html_url"]
            if repo.get("homepage"):
                b["links"].setdefault("repo_homepages", []).append(repo["homepage"])
            b["facts"].setdefault("forks", {})[full] = bool(repo.get("fork"))
            b["facts"].setdefault("archived", {})[full] = bool(repo.get("archived"))
            b["facts"].setdefault("topics", [])
            b["facts"]["topics"] = sorted(set(b["facts"]["topics"]) | set(repo.get("topics") or []))
            b["facts"].setdefault("stars", 0)
            b["facts"]["stars"] += int(repo.get("stargazers_count") or 0)
            desc = sanitize_text(repo.get("description") or "", 400)
            b["facts"]["description"] = b["facts"].get("description") or desc[0]
            if "prompt_injection_suspected" in desc[1]:
                b["facts"]["flags"] = sorted(set(b["facts"].get("flags", [])) | {"prompt_injection_suspected"})

            # releases -------------------------------------------------------
            for rel in (self._get(f"/repos/{full}/releases?per_page=5") or [])[:5]:
                if rel.get("draft"):
                    continue
                body, flags = sanitize_text(rel.get("body") or "", 3000)
                title, tflags = sanitize_text(str(rel.get("name") or rel.get("tag_name") or "release"), 200)
                flags = sorted(set(flags) | set(tflags))
                if "prompt_injection_suspected" in flags:   # instructions aimed at agents block auto-qualify
                    b["facts"]["flags"] = sorted(set(b["facts"].get("flags", [])) | {"prompt_injection_suspected"})
                b["evidence"].append({
                    "source_type": "github", "url": rel.get("html_url") or url,
                    "claim": f"Release {short(title, 80)} published in {full}",
                    "occurred_at": rel.get("published_at"), "official": True, "verified": True,
                    "data": {"kind": "release", "tag": rel.get("tag_name"), "prerelease": rel.get("prerelease"),
                             "excerpt": short(body, 400), "chains": sorted(chains_mentioned(body + " " + title)),
                             "flags": flags},
                })

            # development activity ------------------------------------------
            commits = self._get(f"/repos/{full}/commits?since={since}&per_page=100") or []
            authors = {(c.get("author") or {}).get("login") or c.get("commit", {}).get("author", {}).get("email")
                       for c in commits}
            last = commits[0]["commit"]["committer"]["date"] if commits else repo.get("pushed_at")
            b["evidence"].append({
                "source_type": "github", "url": f"{url}/commits",
                "claim": f"{len(commits)}{'+' if len(commits) >= 100 else ''} commits by {len(authors)} authors in "
                         f"{full} during the last 90 days",
                "occurred_at": last, "official": True, "verified": True,
                "data": {"kind": "dev_activity", "commits_90d": len(commits), "authors_90d": len(authors)},
            })

            # README: chains, contract addresses, hiring/audit hints -----------
            try:
                readme = self._get(f"/repos/{full}/readme", raw=True)
            except http.HttpError:
                readme = ""
            text, flags = sanitize_text(readme, 60_000)
            if flags:
                b["facts"].setdefault("flags", [])
                b["facts"]["flags"] = sorted(set(b["facts"]["flags"]) | set(flags))
            readme_url = f"{url}#readme"
            chains = sorted(chains_mentioned(text))
            if chains:
                b["evidence"].append({
                    "source_type": "github", "url": readme_url,
                    "claim": f"README of {full} mentions networks {chains}",
                    "occurred_at": None, "official": True, "verified": True,
                    "data": {"kind": "chain_mention", "chains": chains, "flags": flags},
                })
            for line in text.splitlines():
                for addr in ADDR_RE.findall(line):
                    line_chains = chains_mentioned(line)
                    if re.search(r"(?<![a-z0-9])base(?![a-z0-9])", line.lower()):
                        line_chains.add(8453)        # address tables: "| Base | 0x… |"
                    if len(line_chains) == 1:      # only link when the chain is unambiguous on that line
                        b["contracts"].append({"chain_id": line_chains.pop(), "address": addr,
                                               "label": short(line.strip("|-* "), 60), "linked_by": readme_url})
            if HIRING_RE.search(text):
                b["evidence"].append({
                    "source_type": "github", "url": readme_url, "claim": f"README of {full} advertises open positions",
                    "occurred_at": None, "official": True, "verified": False,
                    "data": {"kind": "hiring", "title": "unspecified", "keywords": []},
                })
            if AUDIT_RE.search(text):
                b["facts"]["mentions_audit"] = True
        return b
