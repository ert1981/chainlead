# ChainLead security review (v0.2.0)

Scope: the core (`http.py`, `providers/*`, `engine.py`, `api.py`, `mcp_server.py`, `cli.py`, `storage.py`, `safety.py`) and the paid tool (`src/chainlead/tool/*`).
Methods: manual review, bandit, pip-audit, detect-secrets, plus regression tests for every fix (`tests/security/test_hardening.py`, `tests/tool/test_tool.py`).

## Automated scans

| Tool | Result |
|---|---|
| bandit -r src | 1 Low finding (B110 `try/except/pass` in HTML parsing, accepted because a malformed page must not abort a run). Reviewed and annotated: B310 (scheme checked), B104 (container bind), B608 (table names are constants). |
| pip-audit | No runtime dependencies (standard library only). No known vulnerabilities. |
| detect-secrets | No real secrets. Only placeholder keys in README and tests, and a `user:pw@` URL in an SSRF test. |

## Findings

| # | Severity | Area | Finding | Fix | Status |
|---|---|---|---|---|---|
| 1 | High | SSRF (`public_web.py`, `http.py`) | Project websites come from untrusted GitHub and seed data. They were fetched with no check on private, loopback, link-local or metadata IPs, redirects were followed blindly, and DNS rebinding was possible. | `fetch_untrusted()` resolves the host once, requires every address to be global, pins the connection to the checked IP (SNI and Host stay the real name), re-checks every redirect (at most 3), allows only ports 80/443, refuses userinfo, and caps the response at 1 MB and 12 s. Only http(s) and mailto links are kept. | Fixed + tests |
| 2 | Medium | GitHub path injection | Repo and owner names from search or seed input were inserted into API paths (`../`, `?`), which could reach other GitHub endpoints with the operator's token. | Strict `OWNER_RE`/`REPO_RE`. Invalid names are skipped before any request. | Fixed + tests |
| 3 | Medium | Budget controls | `NaN` and `inf` bypassed the `< 0` check and the MCP agent cap. NaN was stored as NULL and crashed the summary. | `check_budget()` accepts only finite values from 0 to 1e6 in REST, MCP and the engine. JSON `NaN` and `Infinity` are rejected. | Fixed + tests |
| 4 | Medium | REST DoS | There was no rate limiting. A negative `Content-Length` blocked a handler thread, and there was no socket timeout (slowloris). | Per-tenant and per-IP rate limits (120/min, 10 runs/min), a digits-only Content-Length check, and a 30 s socket timeout. | Fixed + tests |
| 5 | Medium | Prompt injection | Unicode tag characters (U+E0000–E007F) could smuggle hidden instructions. Release titles and repo descriptions were not sanitized or flagged. | Tag characters and BOM are stripped and flagged. All third-party text is sanitized and the flags reach `facts.flags`. Persian ZWNJ and ZWJ are kept, not flagged, so there are no false positives. | Fixed + tests |
| 6 | Medium | Campaign overwrite | A caller-supplied `id`, `tenant`, `status` or `created_at` in a spec overwrote server state. | Server-assigned fields are rejected. | Fixed + tests |
| 7 | Medium | Resource limits | `max_candidates`, `max_leads` and list fields had no bounds (1e9 was accepted). | Integer and list limits are enforced. At most 10 contracts per project and 50 events per contract. | Fixed + tests |
| 8 | Low | Source hygiene | A literal bidi control character in `safety.py` triggered bandit's trojansource check. | Written as escape sequences. | Fixed |
| 9 | Low | The Graph | The subgraph id was not validated. The mutation check was a substring match. | Id format check, a 10k-character query cap, and word-boundary `mutation`/`subscription` rejection. | Fixed + tests |
| 10 | Low | `--out` path traversal | Report file names are derived from slugified keys. The brief file stem was not sanitized. | Stems are sanitized and files are always written inside `--out`. | Fixed + tests |
| 11 | Info | SQL injection | All queries are parameterised. The only f-string is over constant table names. | — | Verified |
| 12 | Info | RPC write methods | The JSON-RPC client allows only read methods. `eth_sendRawTransaction` and similar are refused before any network call. | — | Verified + tests |
| 13 | Info | Auth and tenancy | API keys are compared in constant time. Every table and the cache key are tenant-scoped. Empty `tenant:` key pairs are now ignored. | Empty-key fix | Verified + tests |
| 14 | Info | Secrets in input | Private keys, seed phrases and fields with those names are rejected on every interface. | — | Verified |

## Paid tool (`chainlead tool-serve`)

- **No custody.** The server never holds a private key. Payment goes to the configured payee through the x402 facilitator (PayAI). Onchain registration is signed in the creator's own wallet on `/register-onchain`, and the server only builds unsigned calldata.
- **Payment order.** Input is validated (422) before any payment. Then: verify, reserve the replay key `(payer, nonce)`, run, settle. A result is returned only after settlement succeeds. The reservation is released only when the settlement is retryable. Nothing is charged when no candidate is found.
- **Replay.** A SQLite replay store. On Render free the disk is ephemeral, but an EIP-3009 nonce is consumed onchain at settlement, so a replayed authorization can't settle and gets no result.
- **DoS.** At most 2 concurrent runs (503 *before* verify). 60 requests/min per client IP, using the last `X-Forwarded-For` hop added by the platform proxy. 32 KB body cap. `Transfer-Encoding` is refused (411). A 35 s time budget and at most 8 candidates.
- **Output.** Third-party text in results is sanitized and labelled as untrusted (`content_notice`). Campaign data is purged after each call (metadata-only retention, as declared in the manifest).
- **Register page.** A strict CSP with a sha256-hashed inline script and `connect-src 'self'`. Duplicate detection scans ToolRegistry `ToolRegistered` logs for the same manifest URL and creator, and asks for confirmation.
- **Host header.** Set `CHAINLEAD_PUBLIC_BASE_URL` in production. Without it, resource URLs fall back to the request Host header (dev only), and the manifest is not served.

## Residual risks

- Lead quality depends on public data. Results are probabilistic signals, not proof of budget or intent.
- Unauthenticated GitHub allows 60 requests/hour, so a token with no scopes is needed in production.
- Self-attested verifiability: buyers trust the operator's server.
