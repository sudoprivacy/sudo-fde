# Requirement-to-implementation acceptance map

| Requirement | Implementation | Evidence |
|---|---|---|
| FR-1: password authentication, organizations, membership | `api.py`, `security.py`, `models.py`; Argon2, HMAC-protected opaque server sessions, CSRF, expiry, invitations, password reset, tenant switching, immediate role checks | `test_access.py`; browser login and organization UI |
| FR-2: private/shared workspaces and tenant isolation | Workspace owner/editor/viewer; tenant-scoped resources; PostgreSQL FORCE RLS and composite foreign keys; storage authorization proxy | `test_access.py`, `test_postgres.py`, `test_storage_config.py` |
| FR-3: sole model/billing gateway | `router.py`; directory/policy intersection, quota precheck, bounded reservations, durable subcall identity, authoritative model/token/amount receipts | `test_router.py`; real HTTP Compose receipt reconciliation |
| FR-3: no duplicated charges after retry/restart | Response persistence before settlement, original-key replay, workspace execution locks, compensation from Router status; pending jobs pinned to pipeline/prompt revision | Timeout/settlement/reconstruction/concurrency/revision-pin tests in `test_router.py` |
| FR-4: original FDE capability regression | Vendored `ps/pipelines` and prompts; BFF → signed private Pattern calls; scoped decisions and explicit source provenance | `test_workflow.py`, `test_revision_refinement.py`; real multi-process Compose acceptance |
| FR-4: natural-language revision | Source/version-checked proposal, selected changes, retained corrections, optimistic concurrency and undo | `test_revision_refinement.py` |
| FR-4: refinement and portable output | Catalog-checked fields and grain, gap/offline classification, immutable refinement history; YAML, RDF/OWL, BPMN, R2RML, hash manifest | `test_revision_refinement.py`, `test_workflow.py` |
| FR-5: scoped professional knowledge | Workspace-first authorized search, tenant grants, document version/retention, asynchronous parsing and indexing, fixed 384-dimensional backend | `test_knowledge.py` |
| FR-5: deletion and downstream source authorization | Immediate revocation at retrieval, chunk read, generated-artifact read and export download; later physical purge | Source-revocation and derived-output tests in `test_knowledge.py` |
| FR-5: embeddings follow approved boundary | Router embeddings with quota/receipts or operator-managed internal embeddings; development vectors rejected in production | Router embedding accounting test; configuration and failure-closed tests |
| Sensitive/unsafe files | Type/size/archive validation, ClamAV protocol, text extraction, restricted/sensitive rejection before embedding or model transport | `test_storage_config.py`, `test_knowledge.py`, `tests/upstream_scanner/` |
| Data-source safety | SaaS has no public DB/MCP connection route; catalog/DDL upload only; offline scanner retained | `test_workflow.py`, scanner regression suite |
| Production data/operations | Alembic, application/migration identity separation, encrypted private S3, maintenance, TLS templates, locked dependencies, backup and isolated restore tools | PostgreSQL acceptance, container smoke/full-stack acceptance, database restore check |
| Audit and usage | Append-only tenant events, security failures, workspace access, model attribution, per-currency usage, reconciliation discrepancies | Access and reconciliation tests; audit/metrics HTTP endpoints |
| Deferred by scope | Enterprise SSO adapter activation, platform-wide knowledge operations, model fine-tuning, original SQLite data import, online customer network connectors | Explicit boundaries in the development plan and operations guide |
| Real Router deployment | Versioned `fde-router/1` client and read-only compatibility checker; real reservation/billing acceptance waits for the actual Router | User-approved deferral; opt-in `test_router_live.py` is not silently replaced by simulator results |

The development simulator is isolated test infrastructure. Its balance, tokens, generated content and receipts are synthetic; none are claimed as real provider quality or financial acceptance.
