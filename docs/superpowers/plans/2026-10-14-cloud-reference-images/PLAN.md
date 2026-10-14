# MCP Cloud Reference Images Implementation Plan

> **For agentic workers:** Run `/gflow:status --feature cloud-reference-images` to find the next unchecked task. Run `/gflow:check` before every commit.

**Goal:** Let `gflow_generate_image` accept S3 reference images and defer reading them until the queued worker is ready to submit image-to-image generation.

**Architecture:** Add a distinct MCP/queue field and carry it on the typed image request only until the worker materializes it. A small cloud-reference reader reuses the optional `universal_pathlib`/`s3fs` stack, restricts reads to the bucket configured by `GFLOW_CLI_STORAGE_URI`, and writes bounded, validated images into a task-scoped temporary directory. The existing Flow transport receives normal local `ref_paths`, and `cloud_uris` remains the generated-output response field.

**Predict verdict:** GO with mitigations — confidence 8/10. The S3 bucket source, bounded read, redacted errors, and temp-file lifetime are specified below.

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| High | Ambient AWS credentials could read unrelated buckets | Require the S3 bucket in `GFLOW_CLI_STORAGE_URI`; reject all other buckets |
| High | Queued worker payloads may bypass handler validation | Validate field shape in the codec and recheck URI/bucket in the worker |
| High | Files can disappear before Playwright finishes upload | Keep the private temporary directory until the awaited generation completes; clean it in `finally`, including cancellation |
| High | S3 exceptions can expose object keys | Wrap provider failures in a stable sanitized `CloudReferenceError` |

## File structure

### New files

```text
src/gflow_cli/cloud_references.py
  Validate and download configured-bucket S3 image references with bounded reads.
```

### Modified files

```text
src/gflow_cli/mcp/tools.py
  Add the MCP argument and preserve URIs in the queued payload.
src/gflow_cli/worker/codec.py
  Validate and decode the queued cloud-reference field.
src/gflow_cli/worker/daemon.py
  Materialize cloud references immediately before generation and clean temporary files.
src/gflow_cli/api/image.py
  Count cloud references with local, UUID, and Character references at the model cap.
src/gflow_cli/errors.py
  Add a sanitized cloud-reference error in the existing storage error family.
docs/MCP.md
docs/EXTERNAL_STORAGE.md
  Document the MCP input contract and S3 read boundary.
```

## Task 1 — Queue and MCP contract

**What:** Add a distinct `cloud_reference_images` argument without changing `reference_images` or response fields.

**Files:** `mcp/tools.py`, `worker/codec.py`, `api/image.py`

**Steps:**
- [x] Preserve the URI list in the queue payload; never download in the MCP handler.
- [x] Select `i2i` when either reference parameter has values.
- [x] Decode only a list of strings and include cloud refs in the model-cap invariant.
- [x] Keep local paths from `reference_images` before cloud paths; Flow UUIDs retain picker handling.

**Tests:** Covered by the follow-up regression tests in `tests/mcp/test_cloud_reference_images.py`
and `tests/worker/test_codec.py`.

## Task 2 — Bounded S3 reader and worker integration

**What:** Read S3 objects only when a worker executes the task, convert them to local paths, and retain them through the Flow upload.

**Files:** `cloud_references.py`, `worker/daemon.py`, `errors.py`

**Steps:**
- [x] Require canonical `s3://bucket/key` URIs and the bucket configured by `GFLOW_CLI_STORAGE_URI`.
- [x] Reject traversal-like keys and require the S3 optional extra.
- [x] Read no more than 20 MiB per object, validate image magic bytes, and use opaque private temporary filenames.
- [x] Sanitize S3 exceptions and perform no Flow work after a download failure.
- [x] Clear cloud URIs from the request after replacing them with local paths; clean the directory on all task exits.

**Tests:** Covered by `tests/test_cloud_references.py` and
`tests/worker/test_cloud_references.py`, including failure-before-Flow and cleanup paths.

## Task 3 — Documentation

**What:** Explain how MCP S3 references differ from local paths and Flow UUIDs, the configured-bucket rule, optional dependency, size/type limits, ordering, and asynchronous task status.

**Files:** `docs/MCP.md`, `docs/EXTERNAL_STORAGE.md`

**Steps:**
- [x] Document `cloud_reference_images` and an example request.
- [x] Explain that queued URIs are read by the worker and may be inspected through MCP task status when `wait=false`.
- [x] Clarify that `cloud_uris` still describes generated outputs.

**Tests:** None; documentation only.

## Definition of done

- [x] Tasks 1–3 implementation and documentation complete.
- [x] MCP, codec, worker, reader, and model-cap regression tests added and passing.
- [x] Full repository quality gates and live MCP schema verification complete.
- [x] Live `tools/list` exposes `cloud_reference_images`; existing `reference_images`
  and generated `cloud_uris` behavior remain covered and unchanged.
- [x] Read-only SeaweedFS object access confirmed; no generation was submitted.
