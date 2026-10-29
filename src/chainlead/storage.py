"""SQLite persistence. Every row carries a `tenant` column; every query filters on it,
so tenants never see each other's campaigns, evidence, leads or costs (spec §11)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Iterable

from .models import (ChainEvent, Contract, Evidence, Lead, Project, Signal, iso, parse_dt, utcnow)

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
  tenant TEXT NOT NULL, id TEXT NOT NULL, name TEXT, status TEXT, body TEXT, created_at TEXT,
  PRIMARY KEY (tenant, id));
CREATE TABLE IF NOT EXISTS projects (
  tenant TEXT NOT NULL, key TEXT NOT NULL, name TEXT, website TEXT, identity_confidence REAL,
  body TEXT, updated_at TEXT, PRIMARY KEY (tenant, key));
CREATE TABLE IF NOT EXISTS repositories (
  tenant TEXT NOT NULL, project_key TEXT NOT NULL, full_name TEXT NOT NULL,
  PRIMARY KEY (tenant, project_key, full_name));
CREATE TABLE IF NOT EXISTS contracts (
  tenant TEXT NOT NULL, project_key TEXT NOT NULL, chain_id INTEGER NOT NULL, address TEXT NOT NULL,
  label TEXT, verified_source INTEGER, linked_by TEXT,
  PRIMARY KEY (tenant, chain_id, address));
CREATE TABLE IF NOT EXISTS project_sources (
  tenant TEXT NOT NULL, project_key TEXT NOT NULL, url TEXT NOT NULL,
  PRIMARY KEY (tenant, project_key, url));
CREATE TABLE IF NOT EXISTS chain_events (
  tenant TEXT NOT NULL, chain_id INTEGER NOT NULL, tx_hash TEXT NOT NULL, log_index INTEGER NOT NULL,
  block_number INTEGER, block_hash TEXT, address TEXT, name TEXT, args TEXT, ts TEXT, status TEXT,
  project_key TEXT,
  PRIMARY KEY (tenant, chain_id, tx_hash, log_index));
CREATE TABLE IF NOT EXISTS opportunity_signals (
  tenant TEXT NOT NULL, campaign_id TEXT NOT NULL, project_key TEXT NOT NULL, id TEXT NOT NULL,
  type TEXT, body TEXT, PRIMARY KEY (tenant, campaign_id, id));
CREATE TABLE IF NOT EXISTS evidence_links (
  tenant TEXT NOT NULL, project_key TEXT NOT NULL, id TEXT NOT NULL, source_type TEXT, url TEXT,
  claim TEXT, observed_at TEXT, body TEXT, PRIMARY KEY (tenant, id));
CREATE TABLE IF NOT EXISTS project_people (
  tenant TEXT NOT NULL, project_key TEXT NOT NULL, name TEXT NOT NULL, role TEXT, source_url TEXT,
  PRIMARY KEY (tenant, project_key, name));
CREATE TABLE IF NOT EXISTS lead_scores (
  tenant TEXT NOT NULL, campaign_id TEXT NOT NULL, project_key TEXT NOT NULL, lead_id TEXT,
  lead_score REAL, opportunity_score REAL, data_confidence REAL, status TEXT, rank INTEGER,
  body TEXT, created_at TEXT, PRIMARY KEY (tenant, campaign_id, project_key));
CREATE TABLE IF NOT EXISTS provider_calls (
  tenant TEXT NOT NULL, id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, provider TEXT,
  op TEXT, request_key TEXT, cost_usd REAL, status TEXT, cached INTEGER, error TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS budgets (
  tenant TEXT NOT NULL, campaign_id TEXT NOT NULL, limit_usd REAL, spent_usd REAL DEFAULT 0,
  PRIMARY KEY (tenant, campaign_id));
CREATE TABLE IF NOT EXISTS cache (
  request_key TEXT PRIMARY KEY, provider TEXT, body TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS ix_calls ON provider_calls (tenant, campaign_id);
"""


class Storage:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL" if path != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------ helpers
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------------ campaigns
    def save_campaign(self, tenant: str, c: dict) -> None:
        self._exec("INSERT OR REPLACE INTO campaigns VALUES (?,?,?,?,?,?)",
                   (tenant, c["id"], c["name"], c["status"], json.dumps(c, default=str), c["created_at"]))

    def get_campaign(self, tenant: str, cid: str) -> dict | None:
        rows = self._all("SELECT body FROM campaigns WHERE tenant=? AND id=?", (tenant, cid))
        return json.loads(rows[0]["body"]) if rows else None

    def list_campaigns(self, tenant: str) -> list[dict]:
        return [json.loads(r["body"]) for r in
                self._all("SELECT body FROM campaigns WHERE tenant=? ORDER BY created_at DESC", (tenant,))]

    # ------------------------------------------------------------------ projects & evidence
    def save_project(self, tenant: str, p: Project) -> None:
        body = {
            "name": p.name, "key": p.key, "website": p.website, "description": p.description,
            "sectors": p.sectors, "chains": p.chains, "repos": p.repos, "github_org": p.github_org,
            "contact_channels": p.contact_channels, "identity_notes": p.identity_notes,
            "identity_confidence": p.identity_confidence,
        }
        self._exec("INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?,?,?)",
                   (tenant, p.key, p.name, p.website, p.identity_confidence, json.dumps(body), iso(utcnow())))
        for r in p.repos:
            self._exec("INSERT OR IGNORE INTO repositories VALUES (?,?,?)", (tenant, p.key, r))
        for c in p.contracts:
            self.save_contract(tenant, p.key, c)
        for s in p.sources:
            self._exec("INSERT OR IGNORE INTO project_sources VALUES (?,?,?)", (tenant, p.key, s))
        for person in p.people:
            self._exec("INSERT OR REPLACE INTO project_people VALUES (?,?,?,?,?)",
                       (tenant, p.key, person.name, person.role, person.source_url))

    def save_contract(self, tenant: str, project_key: str, c: Contract) -> None:
        self._exec("INSERT OR REPLACE INTO contracts VALUES (?,?,?,?,?,?,?)",
                   (tenant, project_key, c.chain_id, c.address, c.label, int(c.verified_source), c.linked_by))

    def contract_owner(self, tenant: str, chain_id: int, address: str) -> str | None:
        rows = self._all("SELECT project_key FROM contracts WHERE tenant=? AND chain_id=? AND address=?",
                         (tenant, chain_id, address.lower()))
        return rows[0]["project_key"] if rows else None

    def get_project(self, tenant: str, key: str) -> dict | None:
        rows = self._all("SELECT body FROM projects WHERE tenant=? AND key=?", (tenant, key))
        if not rows:
            return None
        body = json.loads(rows[0]["body"])
        body["contracts"] = [dict(r) for r in self._all(
            "SELECT chain_id, address, label, verified_source, linked_by FROM contracts WHERE tenant=? AND project_key=?",
            (tenant, key))]
        body["people"] = [dict(r) for r in self._all(
            "SELECT name, role, source_url FROM project_people WHERE tenant=? AND project_key=?", (tenant, key))]
        return body

    def list_projects(self, tenant: str) -> list[dict]:
        return [json.loads(r["body"]) for r in
                self._all("SELECT body FROM projects WHERE tenant=? ORDER BY name", (tenant,))]

    def save_evidence(self, tenant: str, ev: Evidence) -> None:
        self._exec("INSERT OR REPLACE INTO evidence_links VALUES (?,?,?,?,?,?,?,?)",
                   (tenant, ev.project_key, ev.id, ev.source_type, ev.url, ev.claim, iso(ev.observed_at),
                    json.dumps(ev.to_dict(), default=str)))

    def list_evidence(self, tenant: str, project_key: str) -> list[dict]:
        return [json.loads(r["body"]) for r in self._all(
            "SELECT body FROM evidence_links WHERE tenant=? AND project_key=?", (tenant, project_key))]

    # ------------------------------------------------------------------ chain events
    def upsert_event(self, tenant: str, e: ChainEvent, project_key: str | None) -> bool:
        """Insert on (chain_id, tx_hash, log_index). Returns True when new."""
        exists = self._all("SELECT block_hash FROM chain_events WHERE tenant=? AND chain_id=? AND tx_hash=? AND log_index=?",
                           (tenant, e.chain_id, e.tx_hash.lower(), e.log_index))
        self._exec("INSERT OR REPLACE INTO chain_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (tenant, e.chain_id, e.tx_hash.lower(), e.log_index, e.block_number, e.block_hash.lower(),
                    e.address.lower(), e.name, json.dumps(e.args, default=str), iso(e.timestamp), e.status,
                    project_key))
        return not exists

    def events_from_block(self, tenant: str, chain_id: int, from_block: int) -> list[dict]:
        return [dict(r) for r in self._all(
            "SELECT * FROM chain_events WHERE tenant=? AND chain_id=? AND block_number>=? AND status!='orphaned'",
            (tenant, chain_id, from_block))]

    def set_event_status(self, tenant: str, chain_id: int, tx_hash: str, log_index: int, status: str) -> None:
        self._exec("UPDATE chain_events SET status=? WHERE tenant=? AND chain_id=? AND tx_hash=? AND log_index=?",
                   (status, tenant, chain_id, tx_hash.lower(), log_index))

    def list_events(self, tenant: str, project_key: str | None = None) -> list[dict]:
        if project_key:
            return [dict(r) for r in self._all(
                "SELECT * FROM chain_events WHERE tenant=? AND project_key=?", (tenant, project_key))]
        return [dict(r) for r in self._all("SELECT * FROM chain_events WHERE tenant=?", (tenant,))]

    # ------------------------------------------------------------------ signals & leads
    def save_signals(self, tenant: str, campaign_id: str, project_key: str, signals: list[Signal]) -> None:
        for s in signals:
            self._exec("INSERT OR REPLACE INTO opportunity_signals VALUES (?,?,?,?,?,?)",
                       (tenant, campaign_id, project_key, s.id, s.type, json.dumps(s.to_dict(), default=str)))

    def save_lead(self, tenant: str, lead: Lead, body: dict) -> None:
        self._exec("INSERT OR REPLACE INTO lead_scores VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (tenant, lead.campaign_id, lead.project.key, lead.id, lead.lead_score.total,
                    lead.opportunity_score.total, lead.data_confidence, lead.status, lead.rank,
                    json.dumps(body, default=str), iso(utcnow())))

    def list_leads(self, tenant: str, campaign_id: str, status: str | None = None) -> list[dict]:
        sql = "SELECT body FROM lead_scores WHERE tenant=? AND campaign_id=?"
        params: list[Any] = [tenant, campaign_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY CASE status WHEN 'qualified' THEN 0 WHEN 'review' THEN 1 ELSE 2 END, rank, lead_score DESC"
        return [json.loads(r["body"]) for r in self._all(sql, params)]

    def get_lead(self, tenant: str, lead_id: str) -> dict | None:
        rows = self._all("SELECT body FROM lead_scores WHERE tenant=? AND lead_id=?", (tenant, lead_id))
        return json.loads(rows[0]["body"]) if rows else None

    # ------------------------------------------------------------------ economics
    def ensure_budget(self, tenant: str, campaign_id: str, limit_usd: float) -> None:
        self._exec("INSERT OR IGNORE INTO budgets VALUES (?,?,?,0)", (tenant, campaign_id, limit_usd))
        self._exec("UPDATE budgets SET limit_usd=? WHERE tenant=? AND campaign_id=?", (limit_usd, tenant, campaign_id))

    def budget(self, tenant: str, campaign_id: str) -> dict | None:
        rows = self._all("SELECT limit_usd, spent_usd FROM budgets WHERE tenant=? AND campaign_id=?",
                         (tenant, campaign_id))
        return dict(rows[0]) if rows else None

    def try_spend(self, tenant: str, campaign_id: str, amount: float) -> bool:
        """Atomic check-and-spend. Returns False (and spends nothing) if it would exceed the limit."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE budgets SET spent_usd = spent_usd + ? WHERE tenant=? AND campaign_id=? "
                "AND spent_usd + ? <= limit_usd + 1e-9", (amount, tenant, campaign_id, amount))
            self.conn.commit()
            return cur.rowcount == 1

    def record_call(self, tenant: str, campaign_id: str | None, provider: str, op: str, request_key: str,
                    cost: float, status: str, cached: bool, error: str | None = None) -> None:
        self._exec("INSERT INTO provider_calls (tenant, campaign_id, provider, op, request_key, cost_usd, status, "
                   "cached, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (tenant, campaign_id, provider, op, request_key, cost, status, int(cached), error, iso(utcnow())))

    def calls(self, tenant: str, campaign_id: str) -> list[dict]:
        return [dict(r) for r in self._all(
            "SELECT provider, op, request_key, cost_usd, status, cached, error, created_at FROM provider_calls "
            "WHERE tenant=? AND campaign_id=? ORDER BY id", (tenant, campaign_id))]

    def cache_get(self, key: str, ttl: int) -> Any | None:
        rows = self._all("SELECT body, created_at FROM cache WHERE request_key=?", (key,))
        if rows and time.time() - rows[0]["created_at"] <= ttl:
            return json.loads(rows[0]["body"])
        return None

    def cache_put(self, key: str, provider: str, body: Any) -> None:
        self._exec("INSERT OR REPLACE INTO cache VALUES (?,?,?,?)",
                   (key, provider, json.dumps(body, default=str), time.time()))

    def purge_campaign(self, tenant: str, campaign_id: str) -> None:
        """Forget a campaign's request-specific rows (spec, leads, signals, budget, call ledger). Public
        source data (projects, evidence, chain events, response cache) is kept for reuse."""
        with self._lock:
            for table, col in (("campaigns", "id"), ("lead_scores", "campaign_id"),
                               ("opportunity_signals", "campaign_id"), ("budgets", "campaign_id"),
                               ("provider_calls", "campaign_id")):
                # table/column names come from the constant tuple above, never from input
                self.conn.execute(f"DELETE FROM {table} WHERE tenant=? AND {col}=?",  # nosec B608
                                  (tenant, campaign_id))
            self.conn.commit()

    def prune_cache(self, max_age_s: int) -> None:
        self._exec("DELETE FROM cache WHERE created_at < ?", (time.time() - max_age_s,))
