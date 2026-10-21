"""CLI pipeline opt-in: request routing, result order and isolation (no browser)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from gflow_cli.api.dto import BatchSubmissionResult, GeneratedImage, ProjectInfo
from gflow_cli.api.image import ImageRef
from gflow_cli.api.transports.ui_automation import UiAutomationTransport
from gflow_cli.image_batch import BatchPromptItem, run_image_batch


def _image(text: str, number: int) -> GeneratedImage:
    return GeneratedImage(
        media_name=f"media-{number}",
        workflow_id=f"workflow-{number}",
        seed=number,
        prompt=text,
        model_name_type=None,
        aspect_ratio="IMAGE_ASPECT_RATIO_PORTRAIT",
        fife_url=f"https://flow-content.google/image/media-{number}",
        dimensions=(768, 1376),
    )


class FakeClient:
    def __init__(self) -> None:
        self.transport = UiAutomationTransport()
        self.sent = AsyncMock(
            return_value=[
                BatchSubmissionResult(
                    status="ok",
                    project_id="project",
                    prompt_idx=i,
                    prompt_hash="hash",
                    images=(_image(text, i),),
                )
                for i, text in enumerate(("Cobalt kettle", "Amber vase"))
            ]
        )
        self.transport.generate_images_pipelined = self.sent  # type: ignore[method-assign]
        self.single_calls = 0
        self.uploads: list[Path] = []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def create_project(self, title: str) -> ProjectInfo:
        return ProjectInfo(project_id="project", title=title)

    async def upload_reference(self, project_id: str, path: Path) -> ImageRef:
        assert project_id == "project"
        self.uploads.append(path)
        return ImageRef(
            name=f"12345678-1234-1234-1234-{len(self.uploads):012d}",
            display_name=path.stem,
            in_project=True,
        )

    async def generate_image(self, *_: object, **__: object) -> GeneratedImage:
        self.single_calls += 1
        raise AssertionError("pipeline must not call the sequential generator")

    async def download_image(self, img: GeneratedImage, target: Path) -> Path:
        target.write_bytes(img.media_name.encode())
        return target


def test_pipelined_batch_collects_results_without_sequential_calls(tmp_path: Path) -> None:
    client = FakeClient()
    outcomes = asyncio.run(
        run_image_batch(
            profile_dir=tmp_path,
            headless=True,
            transport=None,
            prompts=(
                BatchPromptItem(text="Cobalt kettle", index=0),
                BatchPromptItem(text="Amber vase", index=1),
            ),
            output_dir=tmp_path / "out",
            continue_on_error=True,
            project_title="My project",
            project_id="project",
            client_factory=lambda **_: client,
            jitter_range=(0, 0),
            _command="image t2i",
            pipelined=True,
        )
    )
    assert client.single_calls == 0
    assert client.sent.await_count == 1
    assert [(o.index, o.status) for o in outcomes] == [(0, "ok"), (1, "ok")]
    assert [p.read_text() for o in outcomes for p in o.saved_paths] == ["media-0", "media-1"]


def test_pipeline_one_reference_is_uploaded_once_and_reused(tmp_path: Path) -> None:
    client = FakeClient()
    ref_path = tmp_path / "lemon.jpg"
    ref_path.write_bytes(b"reference input")
    outcomes = asyncio.run(
        run_image_batch(
            profile_dir=tmp_path,
            headless=True,
            transport=None,
            prompts=(
                BatchPromptItem(text="Cobalt kettle", index=0),
                BatchPromptItem(text="Amber vase", index=1),
            ),
            output_dir=tmp_path / "out",
            continue_on_error=True,
            project_title="My project",
            project_id="project",
            client_factory=lambda **_: client,
            jitter_range=(0, 0),
            _command="image t2i",
            pipelined=True,
            pipeline_ref=(ref_path,),
        )
    )
    assert [o.status for o in outcomes] == ["ok", "ok"]
    assert client.uploads == [ref_path]
    submitted = client.sent.await_args.kwargs["prompts"]
    assert len(submitted) == 2
    assert submitted[0].refs == submitted[1].refs
    assert submitted[0].refs[0].display_name == "lemon"


def test_pipeline_two_references_upload_once_each(tmp_path: Path) -> None:
    client = FakeClient()
    path_a, path_b = tmp_path / "lemon.jpg", tmp_path / "kettle.jpg"
    path_a.write_bytes(b"lemon")
    path_b.write_bytes(b"kettle")
    outcomes = asyncio.run(
        run_image_batch(
            profile_dir=tmp_path,
            headless=True,
            transport=None,
            prompts=(
                BatchPromptItem(text="Cobalt kettle", index=0),
                BatchPromptItem(text="Amber vase", index=1),
            ),
            output_dir=tmp_path / "out",
            continue_on_error=True,
            project_title="My project",
            project_id="project",
            client_factory=lambda **_: client,
            jitter_range=(0, 0),
            _command="image t2i",
            pipelined=True,
            pipeline_ref=(path_a, path_b),
        )
    )
    assert [o.status for o in outcomes] == ["ok", "ok"]
    assert client.uploads == [path_a, path_b]
    requests = client.sent.await_args.kwargs["prompts"]
    assert all(len(req.refs) == 2 for req in requests)
    assert requests[0].refs == requests[1].refs
    assert requests[0].refs[0].name != requests[0].refs[1].name


@pytest.mark.parametrize(
    "args,word",
    [
        (["--pipeline", "one"], "multiple prompts"),
        (["--pipeline", "--fail-fast", "one", "two"], "fail-fast"),
        (["--ref", __file__, "one", "two"], "requires --pipeline"),
    ],
)
def test_pipeline_validation_happens_before_browser(args: list[str], word: str) -> None:
    from gflow_cli.cli import main

    result = CliRunner().invoke(main, ["image", "t2i", *args])
    assert result.exit_code == 2
    assert word in result.output
