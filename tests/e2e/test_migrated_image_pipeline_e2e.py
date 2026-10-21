"""Live regression: overlapping two Classic image requests on flow.google.com.

Run only with explicit e2e opt-in and a logged-in profile/project:

    GFLOW_CLI_E2E_PROFILE=<profile> GFLOW_CLI_E2E_PROJECT=<id> \
      uv run pytest -m e2e tests/e2e/test_migrated_image_pipeline_e2e.py

Cost: TWO images against the Google Flow daily image quota; no video credits.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from gflow_cli.api.client import FlowApiClient
from gflow_cli.api.image import GenerateImageRequest
from gflow_cli.api.transports.ui_automation import UiAutomationTransport

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_image]


@pytest.mark.asyncio
async def test_pipelined_image_requests_keep_their_media_attribution(
    e2e_profile_dir: Path, tmp_path: Path
) -> None:
    project = os.environ.get("GFLOW_CLI_E2E_PROJECT", "").strip()
    if not project:
        pytest.skip("GFLOW_CLI_E2E_PROJECT must name an existing Flow project")

    marker = uuid.uuid4().hex[:10]
    prompts = [
        GenerateImageRequest(
            prompt=f"Studio photograph of a cobalt teapot on white background {marker}alpha"
        ),
        GenerateImageRequest(
            prompt=f"Studio photograph of an amber vase on white background {marker}beta"
        ),
    ]
    async with FlowApiClient(profile_dir=e2e_profile_dir, out_dir=tmp_path) as client:
        assert isinstance(client.transport, UiAutomationTransport)
        results = await client.transport.generate_images_pipelined(
            project_id=project,
            prompts=prompts,
            jitter_range=(0, 0),
        )
        assert len(results) == len(prompts)
        assert all(r.status == "ok" and len(r.images) == 1 for r in results)
        assert [r.images[0].prompt for r in results] == [p.prompt for p in prompts]
        assert len({r.images[0].media_name for r in results}) == len(prompts)
        for i, result in enumerate(results):
            target = await client.download_image(result.images[0], tmp_path / f"scene_{i}.png")
            assert target.is_file() and target.stat().st_size > 0
