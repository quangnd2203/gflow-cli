"""Credit-free live regression: recover a committed project rename with a lost RPC reply.

Requires explicit environment selectors and never submits image/video prompts.
The real Google Flow UI processes the rename; only the local reply read is
simulated as unavailable. The original project title is restored afterward.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from gflow_cli.api.client import FlowApiClient
from gflow_cli.api.transports import migrated_composer as mc
from gflow_cli.errors import TransportTimeoutError

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_auth, pytest.mark.asyncio]


async def test_live_rename_recovers_when_reply_cannot_be_read() -> None:
    project_id = os.environ.get("GFLOW_RENAME_DIAG_PROJECT")
    profile = os.environ.get("GFLOW_RENAME_DIAG_PROFILE")
    if not project_id or not profile:
        pytest.skip("Opt-in only: provide an existing scratch project and Flow profile")

    async with FlowApiClient(profile_dir=Path(profile), headless=False) as client:
        page = await client._checkout_page()
        try:
            target = f"https://flow.google.com/project/{project_id}"
            await page.goto(target, wait_until="domcontentloaded", timeout=45_000)
            field = page.locator(mc.PROJECT_TITLE_INPUT).first
            await field.wait_for(state="visible", timeout=20_000)
            original_title = await field.input_value()
            assert original_title, "Refusing to modify a project without a loaded original title"
            probe_title = original_title + " [rpc-read-recovery]"

            original_rpc_reply = mc._rpc_reply

            async def unreadable_after_real_commit(*args: object, **kwargs: object) -> None:
                # The real RPC must complete. Simulate only the lost reply at
                # the caller, which exercises the recovery on an actual project.
                await original_rpc_reply(*args, **kwargs)
                raise TransportTimeoutError(detail="Simulated unreadable RPC response body")

            try:
                with patch.object(mc, "_rpc_reply", side_effect=unreadable_after_real_commit):
                    await mc.rename_project(page, project_id, probe_title, timeout_s=15)

                # Recovery must have reloaded the real project, not trusted
                # the optimistic draft value left by field.fill().
                assert await page.locator(mc.PROJECT_TITLE_INPUT).first.input_value() == probe_title
            finally:
                await mc.rename_project(page, project_id, original_title, timeout_s=15)
                await page.reload(wait_until="domcontentloaded", timeout=45_000)
                await page.locator(mc.PROJECT_TITLE_INPUT).first.wait_for(
                    state="visible", timeout=20_000
                )
                persisted = await page.locator(mc.PROJECT_TITLE_INPUT).first.input_value()
                assert persisted == original_title, "Scratch project title was not restored"
        finally:
            client._checkin_page(page)
