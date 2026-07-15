# HTTP API contract

Data Playground publishes the schema served at `/openapi.json` as the committed snapshot
[`kernel/hub/contracts/openapi.json`](../kernel/hub/contracts/openapi.json). The snapshot covers the
core app without optional plugins. It makes route, request, response, and enum changes visible in code
review; it is not a promise that pre-1.0 APIs never change.

Regenerate the snapshot after an intentional API change:

```bash
make openapi
git diff -- kernel/hub/contracts/openapi.json
```

Check it without changing the worktree:

```bash
make check-openapi
```

CI runs the same check from a clean checkout. A mismatch prints a unified diff and fails the kernel
job. Optional out-of-tree plugins own their own contract snapshots; installing one must not change the
committed core schema.

## Error envelope

Every error response under `/api` preserves FastAPI's existing `detail` field where one existed and
adds two machine fields. Unhandled failures use the redacted detail `internal server error`:

```json
{
  "detail": "canvas 'example' not found",
  "code": "canvas_not_found",
  "retryable": false
}
```

- `detail` is human-readable and may change. Clients must not parse it.
- `code` is the stable machine-readable classification. Its allowed values are pinned in OpenAPI.
- `retryable: true` means the server knows that repeating the same request is safe under that
  operation's semantics. Clients must still use bounded backoff and respect `Retry-After` when present.
  Generic and unhandled 5xx responses are `false`: the server may not know whether a non-idempotent
  operation committed before the failure. A known pre-effect failure opts in explicitly.

Specific routes use specific codes such as `canvas_not_found`, `invalid_graph`, and
`upstream_agent_failure`. Other existing `HTTPException` sites receive a stable status-level code such
as `invalid_request`, `permission_denied`, `not_found`, `conflict`, or `service_unavailable`. New API
code should raise `hub.api_errors.APIError` when callers need a more precise machine distinction.

Request validation uses the same envelope while retaining the existing structured list in `detail`.
Non-API protocols, including JSON-RPC errors under `/mcp`, keep their own error contracts.
