from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gflow_cli.api.dto import GeneratedImage
from gflow_cli.data.store import DataStore
from gflow_cli.worker.daemon import FlowWorker
from gflow_cli.worker.queue import QueueRepository


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DataStore]:
    data = DataStore.open(tmp_path / "cloud-refs.db")
    data.conn.execute(
        "INSERT INTO profiles(name, profile_dir, first_seen_at) "
        "VALUES ('default', '/profiles/default', '2026-06-29T00:00:00Z')"
    )
    try:
        yield data
    finally:
        data.close()


def _settings(tmp_path: Path) -> MagicMock:
    return MagicMock(
        storage_uri="s3://bucket/output/",
        profile_subdir=lambda _: tmp_path / "profile",
        headless=True,
        transport=None,
        output_dir=tmp_path / "out",
        history_prompts="full",
    )


@pytest.mark.asyncio
async def test_worker_materializes_cloud_refs_for_flow_and_cleans_after_success(
    store: DataStore, tmp_path: Path
) -> None:
    repo = QueueRepository(store)
    task = repo.enqueue_task(
        "cloud-success",
        "default",
        "i2i",
        {
            "prompt": "make it blue",
            "count": 1,
            "ref_paths": [str(tmp_path / "local.png")],
            "cloud_reference_images": ["s3://bucket/input/reference.png"],
        },
    )
    worker = FlowWorker("default", str(store.path))
    cloud_paths: list[Path] = []
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.create_project = AsyncMock(return_value=MagicMock(project_id="p", title="p"))
    fake_client.generate_image = AsyncMock()
    fake_client.generate_images_batch = AsyncMock()
    fake_client.download_image = AsyncMock(return_value=tmp_path / "generated.png")

    async def download(uris: tuple[str, ...], *, storage_uri: str, temp_dir: Path):
        assert uris == ("s3://bucket/input/reference.png",)
        assert storage_uri == "s3://bucket/output/"
        assert temp_dir.exists()
        cloud_path = temp_dir / "reference-1.png"
        cloud_path.write_bytes(b"image")
        cloud_paths.append(cloud_path)
        return (cloud_path,)

    fake_client.generate_image.return_value = GeneratedImage(
        media_name="image-1",
        workflow_id="workflow-1",
        seed=1,
        prompt="make it blue",
        model_name_type="NARWHAL",
        aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
        fife_url="https://example.invalid/image.png",
        dimensions=(1024, 1024),
    )

    with (
        patch("gflow_cli.worker.daemon.get_settings", return_value=_settings(tmp_path)),
        patch("gflow_cli.worker.daemon.download_cloud_reference_images", side_effect=download),
        patch("gflow_cli.worker.daemon.FlowApiClient", return_value=fake_client),
        patch("gflow_cli.worker.daemon.OperationRecorder") as recorder,
    ):
        recorder.return_value.verify_media_attribution.return_value = None
        await worker.process_task(task)

    request = fake_client.generate_image.await_args.kwargs["req"]
    assert request.ref_paths == (tmp_path / "local.png", cloud_paths[0])
    assert request.cloud_reference_images == ()
    assert not cloud_paths[0].exists()
    updated = repo.get_task("cloud-success")
    assert updated is not None and updated.status == "completed"
    worker.close()


@pytest.mark.asyncio
async def test_worker_download_failure_never_opens_flow_and_cleans_temp(
    store: DataStore, tmp_path: Path
) -> None:
    repo = QueueRepository(store)
    task = repo.enqueue_task(
        "cloud-failure",
        "default",
        "i2i",
        {"prompt": "make it blue", "cloud_reference_images": ["s3://bucket/missing.png"]},
    )
    worker = FlowWorker("default", str(store.path))
    temporary_dirs: list[Path] = []

    async def fail_download(uris: tuple[str, ...], *, temp_dir: Path, **kwargs: object):
        temporary_dirs.append(temp_dir)
        raise OSError("S3 unavailable")

    with (
        patch("gflow_cli.worker.daemon.get_settings", return_value=_settings(tmp_path)),
        patch("gflow_cli.worker.daemon.download_cloud_reference_images", side_effect=fail_download),
        patch("gflow_cli.worker.daemon.FlowApiClient") as flow_client,
    ):
        await worker.process_task(task)

    flow_client.assert_not_called()
    assert len(temporary_dirs) == 1 and not temporary_dirs[0].exists()
    updated = repo.get_task("cloud-failure")
    assert updated is not None and updated.status == "failed"
    worker.close()


@pytest.mark.asyncio
async def test_worker_cancellation_cleans_cloud_temp_directory(
    store: DataStore, tmp_path: Path
) -> None:
    repo = QueueRepository(store)
    task = repo.enqueue_task(
        "cloud-cancel",
        "default",
        "i2i",
        {"prompt": "make it blue", "cloud_reference_images": ["s3://bucket/ref.png"]},
    )
    worker = FlowWorker("default", str(store.path))
    temporary_dirs: list[Path] = []

    async def cancel_download(uris: tuple[str, ...], *, temp_dir: Path, **kwargs: object):
        temporary_dirs.append(temp_dir)
        raise asyncio.CancelledError

    with (
        patch("gflow_cli.worker.daemon.get_settings", return_value=_settings(tmp_path)),
        patch(
            "gflow_cli.worker.daemon.download_cloud_reference_images", side_effect=cancel_download
        ),
        patch("gflow_cli.worker.daemon.FlowApiClient") as flow_client,
        pytest.raises(asyncio.CancelledError),
    ):
        await worker.process_task(task)

    flow_client.assert_not_called()
    assert len(temporary_dirs) == 1 and not temporary_dirs[0].exists()
    worker.close()
