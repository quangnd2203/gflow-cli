from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest

from gflow_cli.cloud_references import (
    MAX_CLOUD_REFERENCE_BYTES,
    download_cloud_reference_images,
    validate_cloud_reference_uri,
    validate_cloud_reference_uris,
)
from gflow_cli.errors import CloudReferenceError

_PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\r"


@pytest.mark.parametrize(
    ("uri", "storage_uri"),
    [
        ("s3://allowed/input/ref.png", "s3://allowed/gflow/output/"),
        ("s3://allowed/other-prefix/ref.png", "s3://allowed/gflow/output/"),
    ],
)
def test_configured_bucket_accepts_reference_across_prefixes(uri: str, storage_uri: str) -> None:
    assert validate_cloud_reference_uri(uri, storage_uri=storage_uri) == uri


@pytest.mark.parametrize(
    ("uri", "storage_uri"),
    [
        ("s3://allowed/ref.png", "file:///tmp/output"),
        ("s3://other/ref.png", "s3://allowed/gflow/output/"),
    ],
)
def test_configured_bucket_rejects_missing_invalid_or_foreign_storage(
    uri: str, storage_uri: str | None
) -> None:
    with pytest.raises(CloudReferenceError):
        validate_cloud_reference_uri(uri, storage_uri=storage_uri)


def test_reference_list_requires_configured_storage_bucket() -> None:
    with pytest.raises(CloudReferenceError):
        validate_cloud_reference_uris(("s3://allowed/ref.png",), storage_uri=None)


@pytest.mark.parametrize("uri", ["s3://allowed/a/../ref.png", "s3://allowed/a/%2e%2e/ref.png"])
def test_rejects_traversal_keys(uri: str) -> None:
    with pytest.raises(CloudReferenceError):
        validate_cloud_reference_uri(uri, storage_uri="s3://allowed/gflow/")


@pytest.mark.asyncio
async def test_download_fails_closed_without_storage_uri_before_read(tmp_path: Path) -> None:
    with (
        patch(
            "gflow_cli.cloud_references.upath_from_uri",
            side_effect=AssertionError("S3 read attempted before storage validation"),
        ) as upath_from_uri,
        pytest.raises(CloudReferenceError),
    ):
        await download_cloud_reference_images(
            ("s3://unconfigured/ref.png",), storage_uri=None, temp_dir=tmp_path
        )

    upath_from_uri.assert_not_called()


@pytest.mark.asyncio
async def test_download_accepts_exact_size_image_and_uses_opaque_filename(
    tmp_path: Path,
) -> None:
    payload = _PNG_HEADER + b"x" * (MAX_CLOUD_REFERENCE_BYTES - len(_PNG_HEADER))

    class MemoryObject:
        def open(self, mode: str) -> io.BytesIO:
            assert mode == "rb"
            return io.BytesIO(payload)

    with patch("gflow_cli.cloud_references.upath_from_uri", return_value=MemoryObject()):
        paths = await download_cloud_reference_images(
            ("s3://allowed/elsewhere/ref.png",),
            storage_uri="s3://allowed/gflow/",
            temp_dir=tmp_path,
        )

    assert paths == (tmp_path / "reference-1.png",)
    assert paths[0].read_bytes() == payload


@pytest.mark.asyncio
async def test_download_rejects_oversize_object(tmp_path: Path) -> None:
    payload = _PNG_HEADER + b"x" * MAX_CLOUD_REFERENCE_BYTES

    class MemoryObject:
        def open(self, mode: str) -> io.BytesIO:
            return io.BytesIO(payload)

    with (
        patch("gflow_cli.cloud_references.upath_from_uri", return_value=MemoryObject()),
        pytest.raises(CloudReferenceError, match="20 MiB"),
    ):
        await download_cloud_reference_images(
            ("s3://allowed/ref.png",), storage_uri="s3://allowed", temp_dir=tmp_path
        )


@pytest.mark.asyncio
async def test_download_sanitizes_missing_object_and_rejects_non_image(tmp_path: Path) -> None:
    class MissingObject:
        def open(self, mode: str) -> io.BytesIO:
            raise FileNotFoundError("s3://allowed/private-secret-key")

    with (
        patch("gflow_cli.cloud_references.upath_from_uri", return_value=MissingObject()),
        pytest.raises(CloudReferenceError) as missing,
    ):
        await download_cloud_reference_images(
            ("s3://allowed/missing.png",), storage_uri="s3://allowed", temp_dir=tmp_path
        )
    assert "private-secret-key" not in str(missing.value)

    class TextObject:
        def open(self, mode: str) -> io.BytesIO:
            return io.BytesIO(b"not an image at all")

    with (
        patch("gflow_cli.cloud_references.upath_from_uri", return_value=TextObject()),
        pytest.raises(CloudReferenceError, match="not a supported image"),
    ):
        await download_cloud_reference_images(
            ("s3://allowed/text",), storage_uri="s3://allowed", temp_dir=tmp_path
        )
