# SudoRouter adapter contract — fde-router/1

Status: implemented and contract-tested by FDE; real Router deployment acceptance is pending. The existing `sudorouter/nova-gateway` checkout exposes model forwarding, account/quota APIs and internal quota functions, but no verified public reservation/settlement/idempotency contract. This document must not be read as a claim that new-api already supplies these endpoints. The user selected a versioned adapter contract with real Router integration later. No existing Router code or billing database is modified by this project.

## Transport and authority

All endpoints are under `/v1/fde`. Production uses TLS, `Authorization: Bearer <server-only service token>` and `X-FDE-Contract: fde-router/1`. Redirects are refused. Only operators configure the Router hostname and credential. Browser requests cannot provide a provider URL, provider key or Router service credential.

All modifying calls carry `Idempotency-Key`. Idempotency must be durable and transactional on the Router side, across retries, process restarts and concurrent workers. Keys are retained for at least the agreed accounting retention period; they must not silently expire while FDE can replay a job. A reused key with different semantic input returns `409 IDEMPOTENCY_CONFLICT` without running a model or changing a balance.

Router owns every amount, available balance, reservation, settlement, refund and billing receipt. FDE keeps only the call's attribution and a projection of immutable Router receipts. An adapter that merely checks `/billing/usage` before a plain `/chat/completions` call does not satisfy this contract. FDE v1 also supports a separately encrypted user provider path for users who explicitly enter a SudoRouter/API-compatible key. Those calls carry a `provider_id`, are isolated to the owning user, and remain outside organization accounting until the unified Router credential exchange is enabled.

## Endpoints

| Endpoint | Request / response contract |
|---|---|
| `GET /capabilities` | `{contract, simulation, features}`. Required features: reserve, execute, settle, release, idempotency, receipts, operations, chat, structured_output. `embedding` is additionally required when selected. Production refuses `simulation: true`. |
| `POST /bindings/exchange` | Input `{authorization_code, tenant_id, user_id}`. The short-lived single-use code is issued by Router to an authorized account administrator. Response `{account_id, tenant_id}`. Router must verify the code's account and tenant binding. This is not a model API key. |
| `GET /accounts/{account_id}` | `{account_id, available_micros, currency, models: [chat-model-id], embedding_models: [embedding-model-id], recharge_url?}`. Models are Router-authorized; FDE further intersects chat models with tenant policy. The URL, if present, belongs to Router's own charging UI. |
| `POST /reservations` | `{account_id, operation_id, max_cost_micros, model, input_hash, attribution}` → `{reservation_id}`. Atomically reserve capacity or return `402 QUOTA_EXHAUSTED`; never run a model on failure. |
| `POST /executions` | `{operation_id, reservation_id, kind: chat|embedding, request, attribution}` → `{operation_id, request_id, output}`. Chat request contains model, messages, temperature, max_tokens and JSON schema. Embedding contains model and input strings; output is `{vectors: number[][]}` with 384 dimensions. The reservation bounds model execution, including concurrent calls. |
| `POST /settlements` | `{operation_id, reservation_id, request_id}` → immutable receipt below. Router computes the charge from its own usage. FDE never supplies the actual charge. |
| `POST /releases` | `{operation_id, reservation_id}`. Idempotently release only a definitively unexecuted reservation. Reject release of an executing/executed/settled operation. |
| `GET /operations/{operation_id}` | `{operation_id, reservation_id, status: reserved|executing|executed|settled|released, response?}`. `response` is the exact execution result once complete. Unknown operation returns `OPERATION_UNKNOWN`; no creation or execution occurs. |
| `GET /receipts/{operation_id}` | The same immutable settlement receipt, for reconciliation. |

Every attribution has `{tenant_id, user_id, workspace_id, capability, call_id, operation_id}`. Knowledge ingestion outside a workspace uses a null workspace and remains tenant-scoped. Router must derive the actual account's authority from the authenticated binding; an attribution field by itself does not grant account access.

Receipt:

```json
{
  "operation_id": "stable-subcall-identity",
  "account_id": "authorized-router-account",
  "request_id": "authoritative-router-request",
  "model": "actual-router-model-version",
  "status": "settled",
  "tokens_in": 100,
  "tokens_out": 50,
  "amount_micros": 100,
  "currency": "USD"
}
```

Tokens and micro amounts are nonnegative integers; the charge cannot exceed the reservation. The account currency and unit conversion belong to Router. Zero balance must not be treated as unknown/unlimited. Separate currencies must not be summed as if they were the same currency. Any future extension to the receipt must retain immutability and identity semantics.

## Recovery and compensation

- User call ID is unique per tenant. A subcall ID includes the parent job, task, inputs and deterministic sample identity, so independent samples are distinct while retries reuse each sample.
- Intermediate hypothesis/artifact IDs are derived from the pinned request and deterministic creation order. Rolling back a multi-step pipeline does not produce new downstream model identities on retry.
- Pending jobs store an executable/prompt revision digest. A deployment with changed pipeline code refuses to resume them under a new implementation; it does not silently incur additional calls.
- Timeout after an execution request leaves `execution_unknown`; FDE never releases that reservation on an assumption that execution failed.
- A stored response with a missing settlement stays `settlement_pending`. Retry and maintenance settle that exact request without executing it again.
- Maintenance uses read-only operation status, settles completed operations, releases unexecuted reservations of terminal jobs, and compares immutable receipts. Differences generate `router.mismatch` audit events; FDE does not invent refund transactions.
- Invalid output schema can still have a legitimate Router charge. That charge remains visible while the business task is marked failed.
- Production must define reservation expiration, idempotency retention and refund ownership. A plain forwarding proxy is not an acceptable substitute for these semantics.

## Local verification and real acceptance

`fde router-simulator` is a development-only, persistent SQLite contract fixture in its own process/database. It uses deterministic outputs, simulated tokens and simulated balances; it is forbidden by production configuration and capability checks. It is not a second production ledger.

`tests/test_router.py` covers quota refusal, replay, distinct sampling, lost execution responses, settlement failures, runtime reconstruction and background compensation. Real integration is explicitly opt-in (`scripts/check-router.py`): first perform read-only capability/account checks. A paid smoke request requires the operator to select a test account and a bounded budget; never silently run a paid model invocation as part of ordinary unit tests.
