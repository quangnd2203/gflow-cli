"""Unit tests for the gflow_upscale_image MCP tool."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gflow_cli.api.image_upscale import TargetResolution
from gflow_cli.mcp.tools import gflow_upscale_image

_VALID_MEDIA_ID = "3a56bb5e-92a2-44f4-9992-3c6a9bf0cd14"
_VALID_PROJECT_ID = "ffb768fb-cf2d-48b7-a135-92978667c37d"


@pytest.fixture(autouse=True)
def _mock_profile(monkeypatch) -> None:
    monkeypatch.setattr("gflow_cli.mcp.tools._resolve_and_validate_profile", lambda p: "default")


@pytest.mark.asyncio
async def test_upscale_invalid_scale() -> None:
    res = await gflow_upscale_image(media_id=_VALID_MEDIA_ID, scale="8k", project=_VALID_PROJECT_ID)
    assert res["status"] == "error"
    assert "Invalid Scale" in res["error"]["title"]


@pytest.mark.asyncio
async def test_upscale_invalid_media_id() -> None:
    res = await gflow_upscale_image(media_id="not-a-uuid", scale="2k", project=_VALID_PROJECT_ID)
    assert res["status"] == "error"
    assert "Invalid Media ID" in res["error"]["title"]


@pytest.mark.asyncio
async def test_upscale_invalid_project_id() -> None:
    res = await gflow_upscale_image(
        media_id=_VALID_MEDIA_ID, scale="2k", project="bad_id with spaces"
    )
    assert res["status"] == "error"
    assert "Invalid Project" in res["error"]["title"]


@pytest.mark.asyncio
async def test_upscale_missing_project_not_in_catalog(monkeypatch) -> None:
    monkeypatch.setattr("gflow_cli.mcp.tools.lookup_project_in_catalog", lambda m, p: None)

    res = await gflow_upscale_image(media_id=_VALID_MEDIA_ID, scale="2k", project=None)
    assert res["status"] == "error"
    assert "Project Required" in res["error"]["title"]


@pytest.mark.asyncio
async def test_upscale_happy_path(tmp_path: Path, monkeypatch) -> None:
    out_file = tmp_path / "upscaled.png"
    out_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    monkeypatch.setattr(
        "gflow_cli.mcp.tools.lookup_project_in_catalog",
        lambda m, p: _VALID_PROJECT_ID,
    )

    mock_client = AsyncMock()
    mock_client.upsample_image = AsyncMock(return_value=out_file)

    class FakeClientContext:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return mock_client

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    monkeypatch.setattr("gflow_cli.mcp.tools.FlowApiClient", FakeClientContext)

    res = await gflow_upscale_image(
        media_id=_VALID_MEDIA_ID,
        scale="2k",
        project=_VALID_PROJECT_ID,
        out_dir=str(tmp_path),
    )

    assert res["status"] == "ok"
    assert res["media_id"] == _VALID_MEDIA_ID
    assert res["project_id"] == _VALID_PROJECT_ID
    assert res["scale"] == "2k"
    assert res["path"] == str(out_file)
    assert res["bytes"] == out_file.stat().st_size
    mock_client.upsample_image.assert_awaited_once()
    assert (
        mock_client.upsample_image.call_args.kwargs["target_resolution"] == TargetResolution.RES_2K
    )


_MEDIA_ID_2 = "312496cb-7633-45c5-8e42-8ad1921c5c68"


@pytest.fixture
def mock_upscale_client(tmp_path: Path, monkeypatch):
    """Capture calls and browser session count, writing distinct fake outputs."""
    stats = {"entered": 0, "closed": 0}
    client = AsyncMock()

    async def do_upscale(*, media_id, project_id, target_resolution, out_path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\\xff\\xd8\\xff" + b"x" * 40)
        return out_path

    client.upsample_image = AsyncMock(side_effect=do_upscale)

    class FakeClientContext:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            stats["entered"] += 1
            return client

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            stats["closed"] += 1
            return None

    monkeypatch.setattr("gflow_cli.mcp.tools.FlowApiClient", FakeClientContext)
    return client, stats


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "media_ids",
    [
        [_VALID_MEDIA_ID, _MEDIA_ID_2],
        f"{_VALID_MEDIA_ID},{_MEDIA_ID_2}",
        f'["{_VALID_MEDIA_ID}","{_MEDIA_ID_2}"]',
    ],
)
async def test_mcp_upscale_batch_array_or_string(
    media_ids, mock_upscale_client, tmp_path: Path
) -> None:
    client, stats = mock_upscale_client
    result = await gflow_upscale_image(
        media_ids=media_ids,
        project=_VALID_PROJECT_ID,
        scale="2k",
        out_dir=str(tmp_path),
    )
    assert result["status"] == "ok"
    assert result["total"] == 2
    assert result["succeeded"] == 2
    assert result["failed"] == 0
    assert len(result["results"]) == 2
    assert [entry["media_id"] for entry in result["results"]] == [_VALID_MEDIA_ID, _MEDIA_ID_2]
    assert all(entry["status"] == "ok" for entry in result["results"])
    assert all(Path(entry["path"]).is_file() for entry in result["results"])
    assert stats == {"entered": 1, "closed": 1}
    assert client.upsample_image.await_count == 2


@pytest.mark.asyncio
async def test_mcp_upscale_batch_one_id_returns_batch_shape(
    mock_upscale_client, tmp_path: Path
) -> None:
    res = await gflow_upscale_image(
        media_ids=_VALID_MEDIA_ID, project=_VALID_PROJECT_ID, out_dir=str(tmp_path)
    )
    assert res["status"] == "ok"
    assert res["total"] == 1
    assert res["results"][0]["media_id"] == _VALID_MEDIA_ID


@pytest.mark.asyncio
async def test_mcp_upscale_batch_resolves_projects_per_image(
    monkeypatch, mock_upscale_client, tmp_path: Path
) -> None:
    client, _stats = mock_upscale_client
    second_project = "a5636753-8d21-4c2e-8a5e-d5414d74cffc"
    lookup = {_VALID_MEDIA_ID: _VALID_PROJECT_ID, _MEDIA_ID_2: second_project}
    monkeypatch.setattr(
        "gflow_cli.mcp.tools.lookup_project_in_catalog", lambda mid, profile: lookup[mid]
    )
    result = await gflow_upscale_image(
        media_ids=[_VALID_MEDIA_ID, _MEDIA_ID_2], out_dir=str(tmp_path)
    )
    assert result["status"] == "ok"
    assert [c.kwargs["project_id"] for c in client.upsample_image.await_args_list] == [
        _VALID_PROJECT_ID,
        second_project,
    ]


@pytest.mark.asyncio
async def test_mcp_upscale_batch_partial_failure(mock_upscale_client, tmp_path: Path) -> None:
    from gflow_cli.errors import UpscaleUnavailableError

    client, stats = mock_upscale_client

    async def one_bad_one_good(*, media_id, project_id, target_resolution, out_path):
        if media_id == _VALID_MEDIA_ID:
            raise UpscaleUnavailableError(detail="Not available", status=403)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"JPEG")
        return out_path

    client.upsample_image.side_effect = one_bad_one_good
    result = await gflow_upscale_image(
        media_ids=[_VALID_MEDIA_ID, _MEDIA_ID_2],
        project=_VALID_PROJECT_ID,
        out_dir=str(tmp_path),
    )
    assert result["status"] == "partial"
    assert result["succeeded"] == 1 and result["failed"] == 1
    assert result["results"][0]["status"] == "error"
    assert "Not available" in result["results"][0]["error"]["detail"]
    assert result["results"][1]["status"] == "ok"
    assert stats == {"entered": 1, "closed": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "media_ids",
    [
        [],
        ["bad-id"],
        [_VALID_MEDIA_ID, _VALID_MEDIA_ID],
        f'["{_VALID_MEDIA_ID}",null]',
        f"{_VALID_MEDIA_ID},",
        "[bad json",
        [_VALID_MEDIA_ID] * 51,
    ],
)
async def test_mcp_upscale_batch_rejects_bad_input_before_browser(
    media_ids, mock_upscale_client
) -> None:
    _client, stats = mock_upscale_client
    result = await gflow_upscale_image(media_ids=media_ids, project=_VALID_PROJECT_ID)
    assert result["status"] == "error"
    assert stats["entered"] == 0


@pytest.mark.asyncio
async def test_mcp_upscale_batch_rejects_ambiguous_inputs(mock_upscale_client) -> None:
    _client, stats = mock_upscale_client
    result = await gflow_upscale_image(
        media_id=_VALID_MEDIA_ID, media_ids=[_MEDIA_ID_2], project=_VALID_PROJECT_ID
    )
    assert result["status"] == "error"
    assert stats["entered"] == 0


@pytest.mark.asyncio
async def test_mcp_upscale_batch_requires_every_project(monkeypatch, mock_upscale_client) -> None:
    _client, stats = mock_upscale_client
    monkeypatch.setattr(
        "gflow_cli.mcp.tools.lookup_project_in_catalog",
        lambda media_id, profile: _VALID_PROJECT_ID if media_id == _VALID_MEDIA_ID else None,
    )
    result = await gflow_upscale_image(media_ids=[_VALID_MEDIA_ID, _MEDIA_ID_2])
    assert result["status"] == "error"
    assert "Project Required" in result["error"]["title"]
    assert stats["entered"] == 0


@pytest.mark.asyncio
async def test_upscale_mcp_schema_exposes_string_or_array_media_ids() -> None:
    from gflow_cli.mcp.server import server

    registered = await server.list_tools()
    tool = next(item for item in registered if item.name == "gflow_upscale_image")
    props = tool.input_schema["properties"]
    assert "media_id" in props and "media_ids" in props
    variants = props["media_ids"]["anyOf"]
    assert any(item.get("type") == "string" for item in variants)
    assert any(item.get("type") == "array" for item in variants)


@pytest.mark.asyncio
async def test_video_upscale_invalid_scale() -> None:
    from gflow_cli.mcp.tools import gflow_upscale_video

    res = await gflow_upscale_video(
        media_id=_VALID_MEDIA_ID,
        scale="4k",  # 4k is not valid for video
        project=_VALID_PROJECT_ID,
    )
    assert res["status"] == "error"
    assert "Invalid Scale" in res["error"]["title"]


@pytest.mark.asyncio
async def test_video_upscale_happy_path(tmp_path: Path, monkeypatch) -> None:
    from gflow_cli.mcp.tools import gflow_upscale_video

    out_file = tmp_path / "upscaled_video.mp4"
    out_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)

    monkeypatch.setattr(
        "gflow_cli.mcp.tools.lookup_project_in_catalog",
        lambda m, p: _VALID_PROJECT_ID,
    )

    mock_client = AsyncMock()
    mock_client.upsample_video = AsyncMock(return_value=out_file)

    class FakeClientContext:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return mock_client

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    monkeypatch.setattr("gflow_cli.mcp.tools.FlowApiClient", FakeClientContext)

    res = await gflow_upscale_video(
        media_id=_VALID_MEDIA_ID,
        scale="1080p",
        project=_VALID_PROJECT_ID,
        out_dir=str(tmp_path),
    )

    assert res["status"] == "ok"
    assert res["media_id"] == _VALID_MEDIA_ID
    assert res["project_id"] == _VALID_PROJECT_ID
    assert res["scale"] == "1080p"
    assert res["path"] == str(out_file)
    assert res["bytes"] == out_file.stat().st_size
    mock_client.upsample_video.assert_awaited_once()
    assert mock_client.upsample_video.call_args.kwargs["scale"] == "1080p"
