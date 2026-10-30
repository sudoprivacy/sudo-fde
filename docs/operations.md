# Deployment and operations

## Supported boundaries

- Python 3.12+, PostgreSQL 16, Alembic, private S3-compatible storage and SMTP; virus scanning is an explicit deployment choice.
- The public web container serves the BFF and UI on port 80. The private `fde-server` container runs Pattern, queue consumption and maintenance in one process. The server has no public port.
- Local development uses SQLite, local files, development embeddings and a Router simulator. These are labeled and refused in production.
- First release does not import the old SQLite workspaces. They remain in the original project. Importing old tenant-less data requires a separate audited mapping and migration, not a filesystem copy into production.
- No online DB or MCP endpoint is published. Use the bundled `fde-scan` on the data owner's machine, inspect its metadata output and upload catalog JSON or DDL.
- Enterprise OIDC is a reserved adapter interface in `identity_provider.py`, not an enabled SSO provider. Platform-wide knowledge and fine-tuning are not enabled.

## Production procedure

1. Build and test the image, then pin its immutable digest. Lock files are supplied for Python runtime and development dependencies.
2. Provision an isolated PostgreSQL database. Migration identity owns schema changes; runtime roles are `NOSUPERUSER NOBYPASSRLS`, have USAGE on the schema and SELECT/INSERT/UPDATE/DELETE on runtime tables, and no schema CREATE, TRUNCATE or policy ownership. Revoke unnecessary PUBLIC grants.
3. Run `FDE_MIGRATION_DATABASE_URL=... alembic upgrade head` using the migration credential. This installs FORCE RLS, composite tenant foreign keys, append-only decision/audit/history triggers and immutable evidence bodies. Never call `init-dev` for production.
4. Provision private object storage. Block public ACLs, enforce TLS/encryption and namespace-aware lifecycle/backup policies. Prefer workload identity over static access keys. Downloads pass through FDE authorization, so changing membership or knowledge grants takes effect immediately.
5. Provision SMTP. The single-host profile sets `FDE_VIRUS_BACKEND=disabled`; enable a separately managed scanner before accepting untrusted uploads if virus scanning is required. Password reset/invitation links are sent through SMTP and never returned to the production browser.
6. Provision a 384-dimensional multilingual embedding service. The internal HTTP adapter supports a TEI-style `/embed` endpoint with `inputs` and `truncate`; use a pinned multilingual-e5-small model revision and protected internal networking. Alternatively set embedding_backend=router after confirming its capability and authorized embedding model. Test hash vectors are not semantic embeddings and cannot be selected in production.
7. Bind the actual SudoRouter contract and complete its read-only checks plus separately authorized real billing acceptance. Provide server secrets through the deployment secret manager. All production placeholders and development defaults must be replaced.
8. Copy `.env.production.example` to an operator-owned `.env.production`, configure the selected HTTP/HTTPS entrypoint and an isolated service network, and use `compose.production.yaml` or equivalent orchestrator manifests. Set `FDE_TRUST_PROXY=false` when web is published directly; do not expose internal port 8801.
9. Configure external egress policy: Pattern may reach only PostgreSQL, SudoRouter and approved internal dependencies. BFF may additionally reach the private object store and SMTP. The customer cannot change these destinations. Network policy is part of deployment, not a UI toggle.
10. Start maintenance alongside workers. Its tenant directory contains IDs only; every business-table operation starts a tenant-scoped transaction. Reconciliation and physical cleanup run once per minute. Keep only one maintenance replica unless an external scheduler serializes it.

Production startup validates the database role and forced workspace RLS; readiness also checks the Router contract. Liveness is `/healthz`; readiness is `/readyz`. An unavailable Router leaves the UI usable for non-model work but fails dependency readiness. The application provides request validation, security headers and authentication throttling; HTTPS can be enabled by placing a TLS entrypoint in front of port 80.

## Capacity and reliability

Defaults: 10 MB per file, 500 chunks per document, 10,000 eligible chunks per exact search, six retrieved excerpts, 60,000 context characters, 4,096 output tokens, 16 model subcalls per job, three concurrent active jobs per tenant and a 600-second worker lease. These are explicit product limits, not load-test results. Test on representative customer material before raising them. Job duration must remain below the lease; the private Pattern service additionally locks a workspace across processes. The worker persists each stage and can resume after restart.

The first release uses database filtering plus exact lexical/vector reranking. PostgreSQL has tenant/workspace indexes and a GIN text index. It does not claim to deploy pgvector/HNSW. For larger authorized corpora, add a checked approximate-index adapter that preserves pre-retrieval authorization and post-read version checks; do not silently truncate a larger candidate set.

Reconciliation updates only receipts and usage projections. Failed execution with ambiguous status is not automatically refunded. Background compensation consults Router state before releasing or settling. Monitor unsettled operations, reconciliation mismatches and failed jobs; surface failures before changing an account binding.

## Monitoring

The organization admin dashboard/API provides member/workspace/document/chunk/job counts, failures, tokens and usage projections. `/api/audit` exposes tenant-scoped immutable events with paging. Worker and API logs contain request/job identifiers and error classes, not customer body text, provider payloads or credentials. Health probes do not expose private data.

Suggested alerts: readiness failure > 2 minutes; failed job rate > 5% for 15 minutes; any `router.mismatch`; settlement pending > 10 minutes; ingestion failures > 5%; oldest queued job > 5 minutes; PostgreSQL storage/connection saturation. Connect these signals to the deployment's existing monitoring system; no outbound alert messages are sent by development commands.

## Backup and restore

Back up PostgreSQL with encrypted daily logical dumps plus managed PITR/WAL archiving. Back up versioned objects with matching retention; preserve the Router's own ledger independently. Record the Alembic revision, app image digest, embedding model revision and object manifest with each backup. Secret-manager keys are restored through their own process, never copied into a database dump.

Perform restores into an isolated database and object namespace first. Restore with the migration identity; reinstall/verify FORCE RLS and runtime grants; run PostgreSQL isolation acceptance using the application role; verify object hashes, source authorization and outstanding Router operation IDs; then reconcile against Router without re-executing models. Only after those checks should the operator switch traffic. `scripts/backup.sh` creates a custom-format dump; `scripts/restore-check.sh` restores only when given an explicit empty target database URL. Restore is never run automatically by startup.

Revoking a document immediately removes it from retrieval and authorized source reads. Maintenance physically deletes its objects and chunks. Storage snapshots/backups may retain the old object until their defined expiry; apply organization retention policy to both primary storage and backups. Old derived artifacts remain audit history but their bodies/downloads require source authorization. New exports omit inaccessible artifacts and record their count.

## Release checklist and known external acceptance

Required before live customer rollout: actual Router compatibility/billing tests; SMTP delivery; S3 policy and restore drill; real multilingual retrieval quality; representative model outputs; tenant load tests and TLS/egress policies. These need deployment-owned services and credentials. Local mock acceptance does not certify them.
