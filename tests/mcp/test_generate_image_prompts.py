"""Contract: MCP single-string vs multi-prompt image generation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from gflow_cli.errors import QueueSchemaError
from gflow_cli.mcp.tools import _TokenBucket, gflow_generate_image
from gflow_cli.worker.codec import decode_payload


@pytest.fixture(autouse=True)
def _isolated_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gflow_cli.mcp.tools._rate_limiter", _TokenBucket())


@pytest.mark.parametrize(
    "input_key,input_val,expected_type",
    [
        ("prompt", "An old-style prompt", "t2i"),
        ("prompts", "A new single prompt", "t2i"),
        ("prompts", ["Red apple on a table", "Blue teapot in a studio"], "t2i_batch"),
    ],
)
async def test_image_mcp_prompts_route_to_expected_task(
    input_key: str, input_val: str | list[str], expected_type: str
) -> None:
    with (
        patch("gflow_cli.mcp.tools._resolve_and_validate_profile", return_value="testprof"),
        patch("gflow_cli.mcp.tools._rate_limiter") as bucket,
        patch("gflow_cli.mcp.tools._run_generation_task", new_callable=AsyncMock) as runner,
    ):
        bucket.acquire = AsyncMock(return_value=True)
        runner.return_value = {"status": "pending", "task_id": "task-123"}
        result = await gflow_generate_image(**{input_key: input_val}, wait=False)
    assert result["status"] == "pending"
    assert runner.await_args.kwargs["task_type"] == expected_type
    payload = runner.await_args.kwargs["payload"]
    if isinstance(input_val, list):
        assert payload["prompts"] == input_val
        assert result["params"]["prompts"] == input_val
    else:
        assert payload["prompt"] == input_val


@pytest.mark.parametrize(
    "value",
    [
        [],
        ["valid prompt", ""],
        ["one prompt", "one prompt"],
        [123],
        [["nested"]],
    ],
)
async def test_array_validation_fails_before_queue(value: object) -> None:
    with patch("gflow_cli.mcp.tools._run_generation_task", new_callable=AsyncMock) as runner:
        result = await gflow_generate_image(prompts=value, wait=False)  # type: ignore[arg-type]
    assert result["status"] == "error"
    assert result["error"]["status"] == 400
    runner.assert_not_awaited()


async def test_both_legacy_and_new_parameter_refused() -> None:
    result = await gflow_generate_image(prompt="first", prompts="second")
    assert result["status"] == "error"
    assert result["error"]["status"] == 400


def test_batch_queue_codec_requires_valid_prompt_list() -> None:
    doc = {"prompt": "a", "prompts": ["a", "b"], "model": "nano2", "count": 1}
    decoded = decode_payload("t2i_batch", doc)
    assert decoded.fields["prompts"] == ["a", "b"]
    with pytest.raises(QueueSchemaError):
        decode_payload("t2i_batch", {**doc, "prompts": ["a", "a"]})
    with pytest.raises(QueueSchemaError):
        decode_payload("t2i_batch", {**doc, "prompts": "a"})
