"""Official-website provider: reads a project's own site (homepage + careers/team pages),
respecting robots.txt, and extracts identity links, public contact channels, hiring,
audit, funding and network-support statements. Free."""
from __future__ import annotations

import re
import urllib.parse
import urllib.robotparser
from html.parser import HTMLParser

from .. import http
from ..chain.registry import chains_mentioned
from ..config import Settings
from ..safety import sanitize_text, short
from .base import Provider, empty_bundle

JOB_RE = re.compile(r"((senior|staff|lead|principal)\s+)?(solidity|smart[- ]contract|protocol|blockchain|rust|security|"
                    r"backend|devops|full[- ]stack)\s+(engineer|developer|auditor|researcher)", re.I)
FUNDING_RE = re.compile(r"(rais(ed|es|ing)|closes?|secured)\s+(an?\s+)?\$?\s?\d+(\.\d+)?\s*(m\b|mm\b|million|k\b)|"
                        r"\b(pre-?seed|seed|series [abc])\s+(round|funding)", re.I)
INTEGRATION_RE = re.compile(r"\b(integrat(es|ed|ion) with|now (live|available) on|partner(ship|s) with)\s+([A-Z][\w.]+)", re.I)
PERSON_RE = re.compile(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+){1,2})\s*[,\-–—|:]\s*(Co-?founder|Founder|CEO|CTO|COO|"
                       r"Head of [A-Z][A-Za-z ]{2,20}|VP [A-Z][A-Za-z ]{2,20}|(?:Engineering|Protocol|Security) Lead)")
SOCIAL = [
    ("x", re.compile(r"https?://(www\.)?(twitter|x)\.com/([A-Za-z0-9_]{1,15})/?$")),
    ("discord", re.compile(r"https?://(www\.)?(discord\.gg|discord\.com/invite)/[\w-]+")),
    ("telegram", re.compile(r"https?://(www\.)?t\.me/[\w_]+")),
    ("linkedin", re.compile(r"https?://(www\.)?linkedin\.com/company/[\w-]+")),
]


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.text: list[str] = []
        self.title = ""
        self._in_title = False
        self._skip = 0
        self._href: str | None = None
        self._atext: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a" and a.get("href"):
            self._href, self._atext = a["href"], []
        elif tag in ("br", "p", "div", "li", "h1", "h2", "h3", "h4", "tr", "section"):
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._atext).strip()))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        if self._href is not None:
            self._atext.append(data.strip())
        self.text.append(data)


def parse_html(html: str) -> tuple[str, str, list[tuple[str, str]]]:
    p = _Page()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML must not crash a run
        pass
    text = re.sub(r"[ \t]+", " ", "".join(p.text))
    text = re.sub(r"\n\s*\n+", "\n", text)
    return p.title.strip(), text, p.links


class PublicWebProvider(Provider):
    name = "public_web"
    capabilities = frozenset({"evidence"})
    costs = {"evidence": 0.0}

    def __init__(self, settings: Settings, respect_robots: bool = True, allow_private_hosts: bool = False) -> None:
        self.settings = settings
        self.respect_robots = respect_robots
        # SSRF guard: website URLs come from untrusted data (GitHub homepages, campaign seeds), so
        # private/loopback/link-local/metadata addresses are refused. Only tests enable this flag.
        self.allow_private_hosts = allow_private_hosts
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urllib.parse.urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                _, _, raw, _ = http.fetch_untrusted(base + "/robots.txt", timeout=self.settings.http_timeout,
                                                    user_agent=self.settings.user_agent, max_bytes=200_000,
                                                    allow_private=self.allow_private_hosts)
                rp.parse(raw.decode("utf-8", "replace").splitlines())
                self._robots[base] = rp
            except http.HttpError:
                self._robots[base] = None     # no robots.txt -> allowed
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.settings.user_agent, url)

    def _fetch(self, url: str) -> str | None:
        if not self._allowed(url):
            return None
        _, headers, raw, _ = http.fetch_untrusted(url, timeout=self.settings.http_timeout,
                                                  user_agent=self.settings.user_agent,
                                                  allow_private=self.allow_private_hosts)
        ctype = {k.lower(): v for k, v in headers.items()}.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return None
        return raw.decode("utf-8", "replace")

    def evidence(self, candidate: dict, campaign: dict) -> dict:
        b = empty_bundle()
        site = candidate.get("website")
        if not site:
            return b
        if not site.startswith(("http://", "https://")):
            site = "https://" + site
        try:
            html = self._fetch(site)
        except http.BlockedUrl:
            b["facts"]["site_blocked_ssrf"] = True
            return b
        if html is None:
            b["facts"]["site_unreachable_or_disallowed"] = True
            return b
        b["links"]["site_url"] = site
        self._extract(site, html, b, homepage=True)
        # follow at most two same-site pages: careers/jobs and team/about
        host = urllib.parse.urlsplit(site).netloc
        title, text, links = parse_html(html)
        wanted = {"careers": re.compile(r"(careers|jobs|join|hiring)", re.I),
                  "team": re.compile(r"(team|about|people)", re.I)}
        for label, rx in wanted.items():
            for href, atext in links:
                full = urllib.parse.urljoin(site, href)
                if urllib.parse.urlsplit(full).netloc == host and (rx.search(href) or rx.search(atext)) and full != site:
                    try:
                        sub = self._fetch(full)
                    except http.HttpError:
                        sub = None
                    if sub:
                        self._extract(full, sub, b, homepage=False)
                    break
        return b

    def _extract(self, url: str, html: str, b: dict, homepage: bool) -> None:
        title, text, links = parse_html(html)
        clean, flags = sanitize_text(text, 80_000)
        if flags:
            b["facts"]["flags"] = sorted(set(b["facts"].get("flags", [])) | set(flags))
        if homepage:
            b["facts"]["site_title"] = sanitize_text(title, 120)[0]
        gh_orgs: list[str] = b["links"].setdefault("site_links_github", [])
        for href, atext in links:
            full = urllib.parse.urljoin(url, href.strip())
            if not full.startswith(("http://", "https://", "mailto:")):
                continue          # javascript:, data:, file: ... are never followed or reported
            atext = sanitize_text(atext, 200)[0]
            m = re.match(r"https?://(www\.)?github\.com/([A-Za-z0-9-]+)", full)
            if m and m.group(2).lower() not in gh_orgs:
                gh_orgs.append(m.group(2).lower())
            if full.startswith("mailto:"):
                addr = urllib.parse.unquote(full[7:].split("?")[0]).strip()
                if not re.fullmatch(r"[^@\s<>()\[\]]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}", addr):
                    continue
                if addr and not any(c["value"] == addr for c in b["contacts"]):
                    b["contacts"].append({"type": "email", "value": addr, "source_url": url})
            for typ, rx in SOCIAL:
                if rx.match(full) and not any(c["value"] == full for c in b["contacts"]):
                    b["contacts"].append({"type": typ, "value": full, "source_url": url})
            if re.search(r"audit", full + " " + atext, re.I) and re.search(r"\.pdf$|audit", full, re.I):
                b["evidence"].append({
                    "source_type": "website", "url": full, "claim": f"Project site links an audit report ({short(atext or full, 60)})",
                    "occurred_at": None, "official": True, "verified": False,
                    "data": {"kind": "audit_published", "auditor": short(atext, 40)}})
        chains = sorted(chains_mentioned(clean))
        if chains:
            b["evidence"].append({"source_type": "website", "url": url,
                                  "claim": f"Official site mentions networks {chains}", "occurred_at": None,
                                  "official": True, "verified": True,
                                  "data": {"kind": "chain_mention", "chains": chains, "flags": flags}})
        seen_jobs: set[str] = set()
        for m in JOB_RE.finditer(clean):
            t = m.group(0).strip()
            if t.lower() in seen_jobs:
                continue
            seen_jobs.add(t.lower())
            b["evidence"].append({"source_type": "website", "url": url, "claim": f"Open role listed: {t}",
                                  "occurred_at": None, "official": True, "verified": True,
                                  "data": {"kind": "hiring", "title": t, "keywords": [m.group(3).lower()]}})
        fm = FUNDING_RE.search(clean)
        if fm:
            b["evidence"].append({"source_type": "website", "url": url,
                                  "claim": f"Official site states funding: “{short(fm.group(0), 60)}”",
                                  "occurred_at": None, "official": True, "verified": False,
                                  "data": {"kind": "funding", "statement": short(fm.group(0), 80)}})
        for im in INTEGRATION_RE.finditer(clean):
            b["evidence"].append({"source_type": "website", "url": url,
                                  "claim": f"Official site announces integration/partnership: {short(im.group(0), 80)}",
                                  "occurred_at": None, "official": True, "verified": False,
                                  "data": {"kind": "integration", "partner": im.group(5)}})
            break
        for pm in PERSON_RE.finditer(clean):
            name, role = pm.group(1), pm.group(2)
            if not any(p["name"] == name for p in b["people"]):
                b["people"].append({"name": name, "role": role, "source_url": url})
