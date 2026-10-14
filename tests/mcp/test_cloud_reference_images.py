from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gflow_cli.data.store import DataStore
from gflow_cli.worker.queue import QueueRepository


@pytest.mark.asyncio
async def test_cloud_references_are_queued_for_i2i_without_download() -> None:
    from gflow_cli.mcp import tools

    captured: dict[str, Any] = {}

    async def run_generation(
        *, profile: str, task_type: str, payload: dict[str, Any], **kwargs: Any
    ):
        captured.update(task_type=task_type, payload=payload, wait=kwargs.get("wait"))
        return {"status": "queued", "task_id": "task-1", "cloud_uris": []}

    with (
        patch(
            "gflow_cli.mcp.tools._rate_limiter",
            tools._TokenBucket(capacity=1, refill_rate=0),
        ),
        patch("gflow_cli.mcp.tools._resolve_and_validate_profile", return_value="default"),
        patch(
            "gflow_cli.mcp.tools.get_settings",
            return_value=type("Settings", (), {"storage_uri": "s3://bucket/output/"})(),
        ),
        patch.object(tools, "_run_generation_task", run_generation),
        patch("gflow_cli.cloud_references.download_cloud_reference_images") as download,
    ):
        result = await tools.gflow_generate_image(
            prompt="a landscape",
            cloud_reference_images=["s3://bucket/input/reference.png"],
            wait=False,
        )

    assert result["status"] == "queued"
    assert captured["task_type"] == "i2i"
    assert captured["payload"]["cloud_reference_images"] == ["s3://bucket/input/reference.png"]
    assert captured["wait"] is False
    download.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_uri", [None, "file:///tmp/output", "s3://other/output/"])
async def test_cloud_reference_rejects_unconfigured_or_foreign_bucket(
    storage_uri: str | None,
) -> None:
    from gflow_cli.mcp import tools

    with (
        patch(
            "gflow_cli.mcp.tools._rate_limiter",
            tools._TokenBucket(capacity=1, refill_rate=0),
        ),
        patch("gflow_cli.mcp.tools._resolve_and_validate_profile", return_value="default"),
        patch(
            "gflow_cli.mcp.tools.get_settings",
            return_value=type("Settings", (), {"storage_uri": storage_uri})(),
        ),
        patch.object(tools, "_run_generation_task") as run_generation,
    ):
        result = await tools.gflow_generate_image(
            prompt="a landscape", cloud_reference_images=["s3://bucket/input/ref.png"]
        )

    assert result["status"] == "error"
    assert result["error"]["title"] == "Invalid Cloud Reference"
    run_generation.assert_not_called()


@pytest.mark.asyncio
async def test_wait_false_persists_cloud_uri_in_queue_without_reading_object(
    tmp_path: Path,
) -> None:
    from gflow_cli.mcp import tools

    db_path = tmp_path / "queue.db"
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    settings = type(
        "Settings",
        (),
        {
            "storage_uri": "s3://bucket/output/",
            "resolved_db_path": lambda self: db_path,
            "profile_subdir": lambda self, _: profile_dir,
        },
    )()

    with (
        patch(
            "gflow_cli.mcp.tools._rate_limiter",
            tools._TokenBucket(capacity=1, refill_rate=0),
        ),
        patch("gflow_cli.mcp.tools._resolve_and_validate_profile", return_value="default"),
        patch("gflow_cli.mcp.tools.get_settings", return_value=settings),
        patch("gflow_cli.cloud_references.download_cloud_reference_images") as download,
    ):
        result = await tools.gflow_generate_image(
            prompt="a landscape",
            cloud_reference_images=["s3://bucket/input/reference.png"],
            wait=False,
        )

    assert result["status"] == "pending"
    with DataStore.open(db_path) as store:
        queued = QueueRepository(store).get_task(result["task_id"])
        assert queued is not None
        assert queued.task_type == "i2i"
        assert queued.payload["cloud_reference_images"] == ["s3://bucket/input/reference.png"]
    download.assert_not_called()
