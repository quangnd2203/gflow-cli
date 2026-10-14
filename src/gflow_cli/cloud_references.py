"""Bounded, configured-bucket S3 reads for queued image references."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlsplit

from gflow_cli.errors import CloudReferenceError
from gflow_cli.paths import extension_from_magic
from gflow_cli.storage import upath_from_uri

MAX_CLOUD_REFERENCE_BYTES = 20 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


def validate_cloud_reference_uri(uri: str, *, storage_uri: str | None = None) -> str:
    """Validate an S3 object URI and, when supplied, its configured bucket.

    S3 keys are opaque object names. Dot segments and encoded separators are
    rejected so URI parsers cannot reinterpret a path across validation and read.
    """
    if (
        not uri
        or not uri.startswith("s3://")
        or uri != uri.strip()
        or "\\" in uri
        or "?" in uri
        or "#" in uri
    ):
        raise CloudReferenceError("Cloud references must be canonical s3://bucket/key URIs.")

    try:
        parsed = urlsplit(uri)
    except ValueError:
        raise CloudReferenceError(
            "Cloud references must be canonical s3://bucket/key URIs."
        ) from None
    if (
        parsed.scheme != "s3"
        or not parsed.netloc
        or parsed.hostname != parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        raise CloudReferenceError("Cloud references must be canonical s3://bucket/key URIs.")

    key = parsed.path[1:]
    decoded_key = unquote(key)
    if (
        not key
        or "%2f" in key.lower()
        or "%5c" in key.lower()
        or "\\" in decoded_key
        or any(segment in ("", ".", "..") for segment in decoded_key.split("/"))
    ):
        raise CloudReferenceError("The S3 object key is empty or unsafe.")

    if storage_uri is not None:
        try:
            configured = urlsplit(storage_uri)
        except ValueError:
            raise CloudReferenceError(
                "GFLOW_CLI_STORAGE_URI must identify a valid S3 bucket."
            ) from None
        if (
            configured.scheme != "s3"
            or not configured.netloc
            or configured.hostname != configured.netloc
        ):
            raise CloudReferenceError(
                "S3 image references require GFLOW_CLI_STORAGE_URI to configure an S3 bucket."
            )
        if parsed.netloc != configured.netloc:
            raise CloudReferenceError(
                "The S3 reference bucket must match the bucket in GFLOW_CLI_STORAGE_URI."
            )
    return uri


def validate_cloud_reference_uris(
    uris: object, *, storage_uri: str | None = None
) -> tuple[str, ...]:
    """Validate a wire-format cloud-reference list and return an immutable copy."""
    if not isinstance(uris, (list, tuple)):
        raise CloudReferenceError("cloud_reference_images must be a list of S3 URI strings.")
    uri_values_raw = cast(list[object] | tuple[object, ...], uris)
    uri_values: list[str] = []
    for uri in uri_values_raw:
        if not isinstance(uri, str):
            raise CloudReferenceError("cloud_reference_images must be a list of S3 URI strings.")
        uri_values.append(uri)
    return tuple(validate_cloud_reference_uri(uri, storage_uri=storage_uri) for uri in uri_values)


def _read_bounded(uri: str) -> bytes:
    """Read at most the configured limit plus one byte from the S3 object."""
    path = upath_from_uri(uri)
    chunks: list[bytes] = []
    total = 0
    with path.open("rb") as stream:
        while total <= MAX_CLOUD_REFERENCE_BYTES:
            chunk = stream.read(min(_READ_CHUNK_BYTES, MAX_CLOUD_REFERENCE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    if total > MAX_CLOUD_REFERENCE_BYTES:
        raise CloudReferenceError("An S3 reference image exceeds the 20 MiB limit.")

    data = b"".join(chunks)
    extension = extension_from_magic(data[:12])
    if len(data) < 12 or extension is None:
        raise CloudReferenceError("An S3 reference object is not a supported image.")
    return data


async def _read_bounded_cancellable(uri: str) -> bytes:
    """Finish any active I/O thread before propagating cancellation to cleanup."""
    read_task = asyncio.create_task(asyncio.to_thread(_read_bounded, uri))
    try:
        return await asyncio.shield(read_task)
    except asyncio.CancelledError:
        try:
            await read_task
        except Exception:
            pass
        raise
    except CloudReferenceError:
        raise
    except ImportError:
        raise CloudReferenceError(
            "S3 image references require the optional gflow-cli[s3] extra."
        ) from None
    except Exception:
        # fsspec/botocore exceptions can include object keys or endpoint details.
        raise CloudReferenceError(
            "Could not read an S3 reference image. Check that it exists and that the "
            "configured S3 credentials permit reading it."
        ) from None


async def download_cloud_reference_images(
    uris: tuple[str, ...], *, storage_uri: str | None, temp_dir: Path
) -> tuple[Path, ...]:
    """Download validated references sequentially to opaque private filenames."""
    validated = validate_cloud_reference_uris(uris, storage_uri=storage_uri)
    paths: list[Path] = []
    for index, uri in enumerate(validated, start=1):
        try:
            data = await _read_bounded_cancellable(uri)
            extension = extension_from_magic(data[:12])
            if extension is None:  # defensive; _read_bounded already checks it
                raise CloudReferenceError("An S3 reference object is not a supported image.")
            target = temp_dir / f"reference-{index}{extension}"
            with target.open("xb") as stream:
                stream.write(data)
            paths.append(target)
        except CloudReferenceError:
            raise
        except Exception:
            raise CloudReferenceError("Could not prepare an S3 reference image.") from None
    return tuple(paths)
