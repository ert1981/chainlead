"""Entity resolution: deduplicate candidates and decide how sure we are that a website,
GitHub org and contracts really belong to one project (spec §6 project_resolver /
confidence). A wallet or contract address never implies a real-world identity by itself;
only links published by the project's own channels count as proof."""
from __future__ import annotations

import urllib.parse

from .models import Contract, Project, Person, slugify


def domain(url: str | None) -> str | None:
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    host = urllib.parse.urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else (host or None)


def _keys(c: dict) -> set[str]:
    ks = set()
    if c.get("github_org"):
        ks.add("gh:" + c["github_org"].lower())
    d = domain(c.get("website"))
    if d and d not in ("github.com", "medium.com", "linktr.ee", "gitbook.io", "notion.site"):
        ks.add("dom:" + d)
    return ks


def deduplicate(candidates: list[dict]) -> list[dict]:
    """Union-find over shared GitHub org / website domain. Merged groups keep all members."""
    parent = list(range(len(candidates)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for i, c in enumerate(candidates):
        for k in _keys(c):
            if k in owner:
                parent[find(i)] = find(owner[k])
            else:
                owner[k] = i
    groups: dict[int, list[dict]] = {}
    for i, c in enumerate(candidates):
        groups.setdefault(find(i), []).append(c)
    merged = []
    for members in groups.values():
        primary = sorted(members, key=lambda m: (m.get("github_org") is None, -len(m.get("repos", []))))[0]
        m = {
            "name": primary["name"], "github_org": primary.get("github_org"),
            "website": primary.get("website") or next((x.get("website") for x in members if x.get("website")), None),
            "description": primary.get("description") or "",
            "repos": sorted({r for x in members for r in x.get("repos", [])}),
            "sectors": sorted({s for x in members for s in x.get("sectors", [])}),
            "chains": sorted({c for x in members for c in x.get("chains", [])}),
            "source_urls": sorted({x.get("source_url") for x in members if x.get("source_url")}),
            "topics": sorted({t for x in members for t in x.get("topics", [])}),
            "members": members,
        }
        if not m["github_org"]:
            m["github_org"] = next((x.get("github_org") for x in members if x.get("github_org")), None)
        merged.append(m)
    return merged


def _official_url(url: str, website: str | None, org: str | None) -> bool:
    if not url:
        return False
    d = domain(url)
    if website and d == domain(website):
        return True
    if org and d == "github.com":
        path = urllib.parse.urlsplit(url).path.strip("/").lower()
        return path.split("/")[0] == org.lower()
    return False


def build_project(cand: dict, bundle: dict) -> Project:
    p = Project(name=cand["name"], website=cand.get("website"), description=cand.get("description", ""),
                sectors=list(cand.get("sectors", [])), chains=list(cand.get("chains", [])),
                repos=list(cand.get("repos", [])), github_org=cand.get("github_org"))
    p.key = slugify(p.github_org or domain(p.website) or p.name)
    p.sources = list(cand.get("source_urls", []))
    seen = set()
    for c in bundle.get("contracts", []):
        k = (int(c["chain_id"]), c["address"].lower())
        if k in seen:
            continue
        seen.add(k)
        p.contracts.append(Contract(chain_id=k[0], address=k[1], label=c.get("label", ""),
                                    linked_by=c.get("linked_by", "")))
        if k[0] not in p.chains:
            p.chains.append(k[0])
    for e in bundle.get("evidence", []):          # networks the project itself says it runs on
        if e.get("official") and e.get("data", {}).get("kind") == "chain_mention":
            for cid in e["data"].get("chains", []):
                if cid not in p.chains:
                    p.chains.append(cid)
    for x in bundle.get("people", []):
        if _official_url(x.get("source_url", ""), p.website, p.github_org):
            p.people.append(Person(name=x["name"], role=x.get("role", ""), source_url=x["source_url"]))
    seen_c = set()
    for x in bundle.get("contacts", []):
        if x["value"] in seen_c:
            continue
        seen_c.add(x["value"])
        x = dict(x)
        x["official"] = _official_url(x.get("source_url", ""), p.website, p.github_org)
        p.contact_channels.append(x)
    return p


def identity_confidence(p: Project, bundle: dict, discovered_sources: int) -> tuple[float, list[str]]:
    score, notes = 0.0, []
    links = bundle.get("links", {})
    facts = bundle.get("facts", {})
    if p.website:
        score += 15
        notes.append("+15 official website known")
    if p.github_org:
        score += 15
        notes.append("+15 GitHub organisation known")
    if p.github_org and p.github_org.lower() in [x.lower() for x in links.get("site_links_github", [])]:
        score += 30
        notes.append("+30 website links to the GitHub organisation (site → GitHub)")
    if p.website and any(domain(h) == domain(p.website) for h in links.get("repo_homepages", [])):
        score += 20
        notes.append("+20 GitHub repo/org points back to the website (GitHub → site)")
    if any(_official_url(c.linked_by, p.website, p.github_org) for c in p.contracts):
        score += 10
        notes.append("+10 contract addresses published by an official source")
    code = {(e["data"].get("chain_id"), e["data"].get("address")): e["data"].get("has_code")
            for e in bundle.get("evidence", []) if e.get("data", {}).get("kind") == "contract_code"}
    if any(v for v in code.values()):
        score += 5
        notes.append("+5 linked contract has bytecode on-chain")
    if any(v is False for v in code.values()):
        score -= 15
        notes.append("-15 a claimed contract has no bytecode on-chain")
    if discovered_sources > 1:
        score += 5
        notes.append("+5 project discovered by more than one source")
    forks = facts.get("forks", {})
    if forks and all(forks.values()):
        score -= 30
        notes.append("-30 all known repositories are forks")
    if facts.get("site_unreachable_or_disallowed"):
        notes.append("website unreachable or disallowed by robots.txt")
    return max(0.0, min(100.0, score)), notes


def resolve_contract_conflicts(projects: list[Project], storage=None, tenant: str = "local") -> None:
    """If two projects claim the same (chain_id, address), the one with stronger identity keeps it;
    the other loses the contract and gets a -35 penalty (spec: chain_id + address is the identity)."""
    claims: dict[str, list[Project]] = {}
    for p in projects:
        for c in p.contracts:
            claims.setdefault(c.key, []).append(p)
    for key, owners in claims.items():
        chain_id, addr = key.split(":", 1)
        prior = storage.contract_owner(tenant, int(chain_id), addr) if storage else None
        if len(owners) == 1 and (prior is None or prior == owners[0].key):
            continue
        ranked = sorted(owners, key=lambda p: (p.key == prior, p.identity_confidence), reverse=True)
        winner = ranked[0] if prior is None or any(p.key == prior for p in owners) else None
        for p in owners:
            if p is winner:
                continue
            p.contracts = [c for c in p.contracts if c.key != key]
            p.identity_confidence = max(0.0, p.identity_confidence - 35)
            p.identity_notes.append(f"-35 claims contract {key} that is attributed to another project")
