"""CLI tests for `gflow image upscale` (issue #171).

FlowApiClient and the catalog lookup are patched so no browser/DB is touched.
Covers: explicit --project happy path, catalog-resolved projectId, fail-fast
when the project can't be resolved, --scale validation (1k hint + unknown),
malformed mediaId rejected before any client is built, and the 4K exit-22 path.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from gflow_cli.api.image_upscale import TargetResolution
from gflow_cli.errors import UpscaleUnavailableError

_MEDIA_ID = "3a56bb5e-92a2-44f4-9992-3c6a9bf0cd14"
_PROJECT_ID = "ffb768fb-cf2d-48b7-a135-92978667c37d"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _mock_client(saved: Path) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.upsample_image = AsyncMock(return_value=saved)
    return client


def _invoke(
    runner: CliRunner,
    args: list[str],
    *,
    client: MagicMock | None = None,
    tmp_path: Path | None = None,
    catalog_project: str | None = None,
):
    """Invoke the CLI with the browser + catalog patched out.

    ``catalog_project`` is what ``_lookup_project_in_catalog`` returns (None =
    not recorded). FlowApiClient is patched only when ``client`` is given.
    """
    from gflow_cli.cli import main

    with ExitStack() as stack:
        # Always stub the environment shims so tests never touch the real
        # profile store / catalog DB.
        stack.enter_context(
            patch("gflow_cli.cli_image._lookup_project_in_catalog", return_value=catalog_project)
        )
        stack.enter_context(patch("gflow_cli.cli_image._resolve_profile", return_value="default"))
        stack.enter_context(
            patch(
                "gflow_cli.cli_image._make_provider_dir",
                return_value=(tmp_path or Path()) / "prof",
            )
        )
        if client is not None:
            stack.enter_context(patch("gflow_cli.cli_image.FlowApiClient", return_value=client))
        return runner.invoke(main, args, catch_exceptions=False)


def test_upscale_happy_explicit_project(runner: CliRunner, tmp_path: Path) -> None:
    client = _mock_client(tmp_path / f"{_MEDIA_ID}_2k.png")
    result = _invoke(
        runner,
        ["image", "upscale", _MEDIA_ID, "--scale", "2k", "--project", _PROJECT_ID],
        client=client,
        tmp_path=tmp_path,
    )
    assert result.exit_code == 0, result.output
    kwargs = client.upsample_image.call_args.kwargs
    assert kwargs["media_id"] == _MEDIA_ID
    assert kwargs["project_id"] == _PROJECT_ID
    assert kwargs["target_resolution"] is TargetResolution.RES_2K
    assert "Saved" in result.output and _MEDIA_ID in result.output


def test_upscale_resolves_project_from_catalog(runner: CliRunner, tmp_path: Path) -> None:
    client = _mock_client(tmp_path / f"{_MEDIA_ID}_2k.png")
    result = _invoke(
        runner,
        ["image", "upscale", _MEDIA_ID, "--scale", "2k"],
        client=client,
        tmp_path=tmp_path,
        catalog_project=_PROJECT_ID,
    )
    assert result.exit_code == 0, result.output
    assert client.upsample_image.call_args.kwargs["project_id"] == _PROJECT_ID


def test_upscale_fails_fast_when_project_unresolvable(runner: CliRunner) -> None:
    # No --project and not in the catalog -> exit 2, browser never launched.
    with patch("gflow_cli.cli_image.FlowApiClient") as fake_client:
        result = _invoke(
            runner, ["image", "upscale", _MEDIA_ID, "--scale", "2k"], catalog_project=None
        )
    assert result.exit_code == 2, result.output
    assert "--project" in result.output
    fake_client.assert_not_called()


def test_upscale_unknown_scale_rejected(runner: CliRunner) -> None:
    result = _invoke(runner, ["image", "upscale", _MEDIA_ID, "--scale", "8k"])
    assert result.exit_code == 2, result.output


def test_upscale_1k_rejected_with_hint(runner: CliRunner) -> None:
    result = _invoke(runner, ["image", "upscale", _MEDIA_ID, "--scale", "1k"])
    assert result.exit_code == 2, result.output
    assert "original" in result.output.lower()


def test_upscale_bad_media_id_rejected_before_client(runner: CliRunner) -> None:
    with patch("gflow_cli.cli_image.FlowApiClient") as fake_client:
        result = _invoke(runner, ["image", "upscale", "not-a-uuid", "--scale", "2k"])
    assert result.exit_code == 2, result.output
    fake_client.assert_not_called()


_MEDIA_ID_2 = "312496cb-7633-45c5-8e42-8ad1921c5c68"
_PROJECT_ID_2 = "a5636753-8d21-4c2e-8a5e-d5414d74cffc"


@pytest.mark.parametrize(
    "ids_argument",
    [
        f"{_MEDIA_ID},{_MEDIA_ID_2}",
        f'["{_MEDIA_ID}", "{_MEDIA_ID_2}"]',
    ],
)
def test_upscale_batch_positional_string_or_array(
    runner: CliRunner, tmp_path: Path, ids_argument: str
) -> None:
    client = _mock_client(tmp_path / "2k.jpg")
    result = _invoke(
        runner,
        ["image", "upscale", ids_argument, "--scale", "2k", "--project", _PROJECT_ID],
        client=client,
        tmp_path=tmp_path,
    )
    assert result.exit_code == 0, result.output
    assert client.__aenter__.await_count == 1
    assert client.upsample_image.await_count == 2
    calls = client.upsample_image.await_args_list
    assert [c.kwargs["media_id"] for c in calls] == [_MEDIA_ID, _MEDIA_ID_2]
    assert all(c.kwargs["project_id"] == _PROJECT_ID for c in calls)
    assert calls[0].kwargs["out_path"] != calls[1].kwargs["out_path"]
    assert "2/2" in result.output


@pytest.mark.parametrize(
    "ids_argument,expected",
    [
        (_MEDIA_ID, 1),
        (f'["{_MEDIA_ID}", "{_MEDIA_ID_2}"]', 2),
        (f"{_MEDIA_ID},{_MEDIA_ID_2}", 2),
    ],
)
def test_upscale_media_ids_option(
    runner: CliRunner, tmp_path: Path, ids_argument: str, expected: int
) -> None:
    client = _mock_client(tmp_path / "2k.jpg")
    result = _invoke(
        runner,
        [
            "image",
            "upscale",
            "--media-ids",
            ids_argument,
            "--scale",
            "2k",
            "--project",
            _PROJECT_ID,
        ],
        client=client,
        tmp_path=tmp_path,
    )
    assert result.exit_code == 0, result.output
    assert client.upsample_image.await_count == expected


def test_upscale_batch_per_media_catalog_projects(runner: CliRunner, tmp_path: Path) -> None:
    client = _mock_client(tmp_path / "2k.jpg")
    with patch(
        "gflow_cli.cli_image._resolve_upscale_project_id",
        side_effect=lambda *, media_id, explicit, profile_name: {
            _MEDIA_ID: _PROJECT_ID,
            _MEDIA_ID_2: _PROJECT_ID_2,
        }[media_id],
    ):
        result = _invoke(
            runner,
            ["image", "upscale", f"{_MEDIA_ID},{_MEDIA_ID_2}", "--scale", "2k"],
            client=client,
            tmp_path=tmp_path,
        )
    assert result.exit_code == 0, result.output
    assert [c.kwargs["project_id"] for c in client.upsample_image.await_args_list] == [
        _PROJECT_ID,
        _PROJECT_ID_2,
    ]


@pytest.mark.parametrize(
    "bad_ids",
    [
        f"{_MEDIA_ID},not-a-uuid",
        f'["{_MEDIA_ID}", null]',
        f'["{_MEDIA_ID}", "{_MEDIA_ID}"]',
        "[]",
        "[invalid json",
        f"{_MEDIA_ID},",
    ],
)
def test_upscale_batch_invalid_inputs_rejected_before_browser(
    runner: CliRunner, bad_ids: str
) -> None:
    with patch("gflow_cli.cli_image.FlowApiClient") as client:
        result = _invoke(
            runner,
            ["image", "upscale", "--media-ids", bad_ids, "--scale", "2k", "--project", _PROJECT_ID],
        )
    assert result.exit_code == 2, result.output
    client.assert_not_called()


def test_upscale_batch_missing_project_rejected_before_browser(runner: CliRunner) -> None:
    with patch("gflow_cli.cli_image.FlowApiClient") as client:
        result = _invoke(
            runner, ["image", "upscale", f"{_MEDIA_ID},{_MEDIA_ID_2}", "--scale", "2k"]
        )
    assert result.exit_code == 2, result.output
    client.assert_not_called()


def test_upscale_batch_partial_failure_continues_and_reports_failure(
    runner: CliRunner, tmp_path: Path
) -> None:
    client = _mock_client(tmp_path / "2k.jpg")
    client.upsample_image = AsyncMock(
        side_effect=[
            UpscaleUnavailableError(detail="not available", status=403),
            tmp_path / "2k.jpg",
        ]
    )
    result = _invoke(
        runner,
        [
            "image",
            "upscale",
            f"{_MEDIA_ID},{_MEDIA_ID_2}",
            "--scale",
            "4k",
            "--project",
            _PROJECT_ID,
        ],
        client=client,
        tmp_path=tmp_path,
    )
    assert result.exit_code != 0, result.output
    assert client.upsample_image.await_count == 2
    assert "1/2" in result.output
    assert _MEDIA_ID_2 in result.output


def test_upscale_4k_unavailable_maps_exit_22(runner: CliRunner, tmp_path: Path) -> None:
    client = _mock_client(tmp_path / "x.png")
    client.upsample_image = AsyncMock(
        side_effect=UpscaleUnavailableError(detail="4K requires Ultra", status=403)
    )
    result = _invoke(
        runner,
        ["image", "upscale", _MEDIA_ID, "--scale", "4k", "--project", _PROJECT_ID],
        client=client,
        tmp_path=tmp_path,
    )
    assert result.exit_code == 22, result.output
