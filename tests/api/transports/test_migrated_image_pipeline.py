"""Regression tests for overlapping migrated image requests (no live quota)."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus

import pytest

from gflow_cli.api.image import GenerateImageRequest, ImageRef
from gflow_cli.api.transports.migrated_image_pipeline import ImageBatchCollector
from gflow_cli.errors import ConfigurationError


@dataclass(eq=False)
class FakeRequest:
    url: str
    post_data: str
    method: str = "POST"


class FakeResponse:
    def __init__(self, request: FakeRequest, *, text: str = "reply", status: int = 200):
        self.request = request
        self.url = request.url
        self.status = status
        self._text = text

    async def text(self) -> str:
        return self._text


def _request(text: str) -> FakeRequest:
    return FakeRequest(
        url="https://flow.google.com/_/FlowUi/data/batchexecute?rpcids=ogiZ0b",
        post_data="f.req=" + quote_plus('[["ogiZ0b",["' + text + '"]]]'),
    )


def test_identical_prompts_refused_before_any_submit() -> None:
    with pytest.raises(ConfigurationError, match="distinct"):
        ImageBatchCollector((GenerateImageRequest(prompt="a"), GenerateImageRequest(prompt="a")))


async def test_responses_correlate_to_request_not_arrival_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gflow_cli.api.transports.migrated_image_pipeline as mod

    prompts = (
        GenerateImageRequest(prompt="cobalt kettle"),
        GenerateImageRequest(prompt="amber vase"),
    )
    collector = ImageBatchCollector(prompts)
    req1, req2 = _request("cobalt kettle"), _request("amber vase")

    @dataclass
    class Record:
        media_id: str
        workflow_id: str
        seed: int
        prompt: str
        image_url: str = "https://flow-content.google/image/test"
        dimensions: tuple[int, int] = (768, 1376)
        display_name: str = "sample"
        project_id: str = "project"

    current_label = ["amber vase"]
    monkeypatch.setattr(mod, "parse_frames", lambda _: [("ogiZ0b", "body")])

    def fake_records(_rpc: str, _body: str) -> list[Record]:
        label = current_label[0]
        return [
            Record(
                media_id="m-" + label.replace(" ", "-"),
                workflow_id="wf-" + label.replace(" ", "-"),
                seed=1,
                prompt=label,
            )
        ]

    monkeypatch.setattr(mod, "image_records", fake_records)
    collector.observe_request(req1)
    collector.observe_request(req2)
    assert collector.request_seen(0) and collector.request_seen(1)
    await collector.observe_response(FakeResponse(req2))
    current_label[0] = "cobalt kettle"
    await collector.observe_response(FakeResponse(req1))
    results = await collector.wait_results(project_id="project", timeout_s=0.5)
    assert [x.images[0].prompt for x in results] == ["cobalt kettle", "amber vase"]
    assert [x.prompt_idx for x in results] == [0, 1]
    assert all(x.status == "ok" for x in results)


async def test_response_with_wrong_prompt_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import gflow_cli.api.transports.migrated_image_pipeline as mod

    collector = ImageBatchCollector((GenerateImageRequest(prompt="cobalt kettle"),))
    req = _request("cobalt kettle")
    collector.observe_request(req)
    monkeypatch.setattr(mod, "parse_frames", lambda _: [("ogiZ0b", "payload")])
    monkeypatch.setattr(
        mod,
        "image_records",
        lambda *_: [
            type(
                "Record",
                (),
                {
                    "prompt": "different",
                    "media_id": "mid",
                    "workflow_id": "wf",
                    "seed": 1,
                    "image_url": "https://flow-content.google/image/mid",
                    "dimensions": (768, 1376),
                    "display_name": "name",
                },
            )(),
        ],
    )
    await collector.observe_response(FakeResponse(req))
    results = await collector.wait_results(project_id="project", timeout_s=0.1)
    assert results[0].status == "fail"
    assert not results[0].images


async def test_reference_response_can_prepend_the_mention_to_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gflow_cli.api.transports.migrated_image_pipeline as mod

    ref = ImageRef(
        name="12345678-1234-1234-1234-123456789abc",
        display_name="lemon.jpg",
        in_project=True,
    )
    expected = GenerateImageRequest(prompt="set the lemon on a table", refs=(ref,))
    collector = ImageBatchCollector((expected,))
    request = _request(expected.prompt)
    collector.observe_request(request)

    monkeypatch.setattr(mod, "parse_frames", lambda _: [("ogiZ0b", "payload")])
    monkeypatch.setattr(
        mod,
        "image_records",
        lambda *_: [
            type(
                "Record",
                (),
                {
                    "prompt": "lemon.jpg  set the lemon on a table",
                    "media_id": "generated-media",
                    "workflow_id": "generated-workflow",
                    "seed": 1,
                    "image_url": "https://flow-content.google/image/generated-media",
                    "dimensions": (768, 1376),
                    "display_name": "name",
                },
            )()
        ],
    )

    await collector.observe_response(FakeResponse(request))
    results = await collector.wait_results(project_id="project", timeout_s=0.1)
    assert results[0].status == "ok"
    assert results[0].images[0].media_name == "generated-media"


async def test_missing_response_is_failure_not_wrong_attribution() -> None:
    collector = ImageBatchCollector((GenerateImageRequest(prompt="a"),))
    collector.observe_request(_request("a"))
    results = await collector.wait_results(project_id="p", timeout_s=0.01)
    assert results[0].status == "fail"
    assert not results[0].images
