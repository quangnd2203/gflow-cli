# Scenario: MCP Cloud Reference Images

## Coverage map

- **D6 — Data layer:** queue payloads persist S3 object URIs until the worker claims them.
- **D7 — Error propagation:** unavailable buckets, missing objects, oversize files, and invalid images must fail with redacted details before Flow submission.
- **D11 — Input validation:** malformed S3 URIs and model reference caps must be rejected.
- **D12 — Observability:** credentials, object keys, and image bytes must not enter logs or error envelopes.
- **D13 — MCP parity:** the MCP handler, queued codec, worker, and tool documentation must preserve the field end to end.

Skipped dimensions: auth/reCAPTCHA, selectors, batch resume, page-pool concurrency, cross-platform CLI paths, and Flow transport are not changed. The only temporary paths are private worker-owned files.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D11 Input validation | `cloud_reference_images` contains a non-S3 URI, malformed URI, or bucket different from configured S3 storage | High | Reject before queueing; never read an unconfigured bucket | Unit |
| 2 | D13 MCP parity | `wait=false` queues cloud references and the worker later claims the task | Critical | Queue retains the URI; download occurs in the worker, not in the MCP handler | Integration |
| 3 | D11 Input validation | Combined local paths, Flow UUIDs, cloud paths, and Characters exceed the selected model cap | High | Reject before download or Flow submission | Unit |
| 4 | D7 Error propagation | S3 object is missing or credentials deny access | High | Task fails with a useful, redacted cloud-reference error; Flow is not called | Integration |
| 5 | D7 Error propagation | Object exceeds 20 MiB or its bytes are not a supported image | High | Reject before Flow submission; remove partial temporary files | Integration |
| 6 | D12 Observability | S3 read fails with a provider exception containing bucket/key details | High | Persist and return only sanitized error details; logs omit URI and credentials | Integration |
| 7 | D6 Data layer | Generation succeeds, fails, or is cancelled after download | High | Private task directory remains through upload and is removed on exit | Integration |
| 8 | D13 MCP parity | Both reference parameters are provided | Medium | Attach `reference_images` first, then cloud images, preserving each list's order | Integration |

## Must-cover before merge (Critical + High)

1. Queued `wait=false` requests retain S3 URIs until worker execution.
2. The configured S3 bucket is the only readable bucket; malformed queue payloads are rejected.
3. Combined references obey the model cap before any download or generation.
4. Missing, inaccessible, oversized, or invalid image objects fail before Flow and expose no URI or credentials.
5. Temporary files are private, survive through Flow upload, and are cleaned after success, failure, and cancellation.

## Deferred (Medium + Low — log as issues, not blockers)

1. Cross-bucket allowlists and new AWS configuration are out of scope.

## Suggested BDD scenarios

The boundary tests are owned by the user for this implementation pass. The scenarios above are the acceptance matrix for that test work.

## Known-issues cross-reference

No blocking S3 input issue or ADR was found. Existing S3 support is an optional output-storage extra; this change reuses its `universal_pathlib` and `s3fs` dependencies.
