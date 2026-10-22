"""MCP → queue → worker → Pipeline → catalog contract, browser-free."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gflow_cli.api.dto import BatchSubmissionResult, GeneratedImage
from gflow_cli.api.image import ImageRef
from gflow_cli.api.transports.ui_automation import UiAutomationTransport
from gflow_cli.data.store import DataStore
from gflow_cli.errors import GFlowError
from gflow_cli.mcp.tools import _TokenBucket, gflow_generate_image

PROJECT = "a4d4d475-9b4c-4c63-b46f-cec081c8267c"
MEDIA_IDS = (
    "7f4fc9cd-e11d-4eee-9b01-bb956609533b",
    "5d36a411-59e4-4c12-aa5a-91d183a23955",
)
PROMPTS = ["Cobalt teapot on a white table", "Amber vase on a marble table"]


class FakeFlowClient:
    def __init__(self, **_: object) -> None:
        self.transport = UiAutomationTransport()
        self.transport.generate_images_pipelined = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._pipeline
        )
        self.uploaded: list[Path] = []
        self.seen_refs: list[tuple[ImageRef, ...]] = []

    async def __aenter__(self) -> FakeFlowClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def _pipeline(self, *, project_id: str, prompts: list, jitter_range: tuple) -> list:
        assert project_id == PROJECT
        assert [req.prompt for req in prompts] == PROMPTS
        assert jitter_range == (0.0, 0.0)
        self.seen_refs = [req.refs for req in prompts]
        return [
            BatchSubmissionResult(
                status="ok",
                project_id=PROJECT,
                prompt_idx=index,
                prompt_hash="hash",
                images=(
                    GeneratedImage(
                        media_name=MEDIA_IDS[index],
                        workflow_id=f"workflow-{index}",
                        seed=index + 123,
                        prompt=prompt,
                        model_name_type=None,
                        aspect_ratio="IMAGE_ASPECT_RATIO_PORTRAIT",
                        fife_url="https://flow.google/test",
                        dimensions=(768, 1376),
                    ),
                ),
            )
            for index, prompt in enumerate(PROMPTS)
        ]

    async def upload_reference(self, project_id: str, path: Path) -> ImageRef:
        assert project_id == PROJECT
        self.uploaded.append(path)
        return ImageRef(
            name=f"12345678-1234-1234-1234-{len(self.uploaded):012d}",
            display_name=path.stem,
            in_project=True,
        )

    async def download_image(self, img: GeneratedImage, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x89PNG\r\n\x1a\n" + img.media_name.encode())
        return target

    async def create_project(self, title: str) -> SimpleNamespace:
        return SimpleNamespace(project_id=PROJECT, title=title)


@pytest.mark.asyncio
async def test_queue_worker_multi_prompt_returns_every_media_and_file(tmp_path: Path) -> None:
    db_path = tmp_path / "queue.db"
    with DataStore.open(db_path):
        pass
    profile_dir = tmp_path / "profile_testprof"
    profile_dir.mkdir()
    settings = SimpleNamespace(
        resolved_db_path=lambda: db_path,
        profile_subdir=lambda _: profile_dir,
        headless=True,
        transport=None,
        output_dir=tmp_path / "output",
        history_prompts=False,
        storage_uri=None,
    )
    with (
        patch("gflow_cli.mcp.tools._resolve_and_validate_profile", return_value="testprof"),
        patch("gflow_cli.mcp.tools._rate_limiter", _TokenBucket()),
        patch("gflow_cli.mcp.tools.get_settings", return_value=settings),
        patch("gflow_cli.worker.daemon.get_settings", return_value=settings),
        patch("gflow_cli.image_batch.FlowApiClient", FakeFlowClient),
    ):
        result = await gflow_generate_image(
            prompts=PROMPTS,
            project=PROJECT,
            profile="testprof",
            aspect="9:16",
        )
    assert result["status"] == "completed", result
    assert result["flow_media_id"] == MEDIA_IDS[0]
    assert len(result["prompt_results"]) == 2
    for index, row in enumerate(result["prompt_results"]):
        assert row["prompt_index"] == index
        assert row["prompt"] == PROMPTS[index]
        assert row["images"][0]["flow_media_id"] == MEDIA_IDS[index]
        assert Path(row["images"][0]["files"][0]).is_file()
    assert len(result["files"]) == 2


@pytest.mark.asyncio
async def test_partial_batch_returns_only_successful_media_with_exact_indexes(
    tmp_path: Path,
) -> None:
    """An RPC failure at index 0 must never reassign the image from index 1."""
    db_path = tmp_path / "queue.db"
    with DataStore.open(db_path):
        pass
    profile_dir = tmp_path / "profile_testprof"
    profile_dir.mkdir()
    settings = SimpleNamespace(
        resolved_db_path=lambda: db_path,
        profile_subdir=lambda _: profile_dir,
        headless=True,
        transport=None,
        output_dir=tmp_path / "output",
        history_prompts=False,
        storage_uri=None,
    )
    client = FakeFlowClient()

    async def partial_pipeline(*, project_id: str, prompts: list, jitter_range: tuple) -> list:
        results = await client._pipeline(
            project_id=project_id, prompts=prompts, jitter_range=jitter_range
        )
        results[0] = BatchSubmissionResult(
            status="fail",
            project_id=PROJECT,
            prompt_idx=0,
            prompt_hash="hash",
            error=GFlowError(detail="Remote image generation error", route="image_pipeline"),
        )
        return results

    client.transport.generate_images_pipelined = AsyncMock(side_effect=partial_pipeline)  # type: ignore[method-assign]
    with (
        patch("gflow_cli.mcp.tools._resolve_and_validate_profile", return_value="testprof"),
        patch("gflow_cli.mcp.tools._rate_limiter", _TokenBucket()),
        patch("gflow_cli.mcp.tools.get_settings", return_value=settings),
        patch("gflow_cli.worker.daemon.get_settings", return_value=settings),
        patch("gflow_cli.image_batch.FlowApiClient", return_value=client),
    ):
        result = await gflow_generate_image(
            prompts=PROMPTS, project=PROJECT, profile="testprof", aspect="9:16"
        )

    assert result["status"] == "completed", result
    assert result["flow_media_id"] == MEDIA_IDS[1]
    assert len(result["prompt_results"]) == 2
    assert result["prompt_results"][0] == {
        "prompt_index": 0,
        "prompt": PROMPTS[0],
        "status": "failed",
        "images": [],
    }
    assert result["prompt_results"][1]["status"] == "ok"
    assert result["prompt_results"][1]["images"][0]["flow_media_id"] == MEDIA_IDS[1]
    assert len(result["files"]) == 1


@pytest.mark.asyncio
async def test_mcp_batch_shares_two_references_across_prompts(tmp_path: Path) -> None:
    db_path = tmp_path / "queue.db"
    with DataStore.open(db_path):
        pass
    profile_dir = tmp_path / "profile_testprof"
    profile_dir.mkdir()
    refs = (tmp_path / "reference_a.png", tmp_path / "reference_b.png")
    for path in refs:
        path.write_bytes(b"reference image for fake Flow")

    settings = SimpleNamespace(
        resolved_db_path=lambda: db_path,
        profile_subdir=lambda _: profile_dir,
        headless=True,
        transport=None,
        output_dir=tmp_path / "output",
        history_prompts=False,
        storage_uri=None,
    )
    client = FakeFlowClient()
    with (
        patch("gflow_cli.mcp.tools._resolve_and_validate_profile", return_value="testprof"),
        patch("gflow_cli.mcp.tools._rate_limiter", _TokenBucket()),
        patch("gflow_cli.mcp.tools.get_settings", return_value=settings),
        patch("gflow_cli.worker.daemon.get_settings", return_value=settings),
        patch("gflow_cli.image_batch.FlowApiClient", return_value=client),
    ):
        result = await gflow_generate_image(
            prompts=PROMPTS,
            reference_images=[str(ref) for ref in refs],
            project=PROJECT,
            profile="testprof",
            aspect="9:16",
        )

    assert result["status"] == "completed", result
    assert client.uploaded == list(refs)
    assert len(client.seen_refs) == len(PROMPTS)
    assert all(len(row) == 2 for row in client.seen_refs)
    assert client.seen_refs[0] == client.seen_refs[1]
    assert len({ref.name for ref in client.seen_refs[0]}) == 2
    assert len(result["prompt_results"]) == 2
