"""Overlapping Classic image submissions on flow.google.com.

One Playwright collector binds every ogiZ0b reply to its originating Request.
Neither response arrival order nor page tiles are used for media attribution.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from typing import TYPE_CHECKING, Any

import structlog

from gflow_cli.api.dto import BatchSubmissionResult, GeneratedImage
from gflow_cli.api.transports.batchexecute import image_records, parse_frames
from gflow_cli.api.transports.migrated_composer import (
    IMAGE_REPLY_BUDGET_S,
    IMAGE_SUBMIT_RPC,
    MigratedComposer,
    _image_body_problem,  # pyright: ignore[reportPrivateUsage]
    _post_data,  # pyright: ignore[reportPrivateUsage]
    _rpcid,  # pyright: ignore[reportPrivateUsage]
    _submit_refusal,  # pyright: ignore[reportPrivateUsage]
)
from gflow_cli.errors import (
    ConfigurationError,
    GFlowError,
    MediaAttributionError,
    ReferenceNotFoundError,
    WireFormatError,
)

if TYPE_CHECKING:
    from playwright.async_api import Page, Request, Response

    from gflow_cli.api.image import GenerateImageRequest

log = structlog.get_logger(__name__)


def _hash(prompt: str) -> str:
    """Log the identity of a prompt without logging its text."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


def _request_mentions_prompt(body: str, prompt: str) -> bool:
    """Match literal or encoded prompt text in a form-decoded batchexecute body."""
    escaped = json.dumps(prompt, ensure_ascii=False)[1:-1]
    escaped_ascii = json.dumps(prompt, ensure_ascii=True)[1:-1]
    double_escaped = json.dumps(escaped, ensure_ascii=False)[1:-1]
    return any(part in body for part in (prompt, escaped, escaped_ascii, double_escaped))


class ImageBatchCollector:
    """Attribute responses to submitted requests, never completion order."""

    def __init__(self, prompts: tuple[GenerateImageRequest, ...]) -> None:
        if not prompts:
            raise ConfigurationError("pipeline needs at least one prompt")
        texts = [req.prompt for req in prompts]
        if len(texts) != len(set(texts)) or any(
            a in b for i, a in enumerate(texts) for j, b in enumerate(texts) if i != j
        ):
            raise ConfigurationError(
                "pipeline needs distinct, non-overlapping prompt texts so requests can be "
                "attributed unambiguously; use the sequential mode for repeated prompts"
            )

        self.prompts = prompts
        self.project_id = ""
        self.requests: dict[Any, int] = {}
        self.seen = [asyncio.Event() for _ in prompts]
        self.done = [asyncio.Event() for _ in prompts]
        self.results: dict[int, BatchSubmissionResult] = {}
        self.response_tasks: set[asyncio.Task[None]] = set()

    def request_seen(self, index: int) -> bool:
        return self.seen[index].is_set()

    def observe_request(self, request: Request) -> None:
        if _rpcid(str(request.url)) != IMAGE_SUBMIT_RPC:
            return
        body = _post_data(request)
        hits = [
            i for i, req in enumerate(self.prompts) if _request_mentions_prompt(body, req.prompt)
        ]
        if len(hits) != 1:
            log.warning("image_pipeline.unattributed_request", matched=len(hits))
            return

        index = hits[0]
        if self.seen[index].is_set():
            log.warning("image_pipeline.duplicate_request", index=index)
            return
        self.requests[request] = index
        self.seen[index].set()
        log.info(
            "image_pipeline.submitted", index=index, prompt_hash=_hash(self.prompts[index].prompt)
        )

    def _failure(self, index: int, exc: BaseException) -> None:
        error = (
            exc
            if isinstance(exc, GFlowError)
            else GFlowError(detail=type(exc).__name__, route="image_pipeline")
        )
        self.results[index] = BatchSubmissionResult(
            status="fail",
            project_id=self.project_id,
            prompt_idx=index,
            prompt_hash=_hash(self.prompts[index].prompt),
            error=error,
        )
        self.done[index].set()

    def mark_submission_failed(self, index: int, exc: BaseException) -> None:
        if index not in self.results:
            self._failure(index, exc)

    def observe_response_event(self, response: Response) -> None:
        if _rpcid(str(response.url)) != IMAGE_SUBMIT_RPC:
            return
        task = asyncio.create_task(self.observe_response(response))
        self.response_tasks.add(task)
        task.add_done_callback(self.response_tasks.discard)

    async def observe_response(self, response: Response) -> None:
        index = self.requests.get(response.request)
        if index is None or self.done[index].is_set():
            return
        expected = self.prompts[index]
        try:
            if response.status != 200:
                raise WireFormatError(
                    detail=f"image pipeline prompt {index} returned HTTP {response.status}",
                    route=f"batchexecute:{IMAGE_SUBMIT_RPC}",
                )
            raw = await response.text()
            refusal = _submit_refusal(raw, (IMAGE_SUBMIT_RPC,))
            if refusal is not None:
                raise refusal
            records = [
                record
                for rpc, payload in parse_frames(raw)
                if rpc == IMAGE_SUBMIT_RPC
                for record in image_records(rpc, payload)
            ]
            # For I2I Flow may include mention chips in generatedImage.prompt.
            # The authoritative link is response.request -> submitted ogiZ0b,
            # whose body was checked for both the prompt and shared reference.
            # Plain T2I has no chips, so retain strict prompt equality there.
            prompt_match = expected.refs or all(x.prompt == expected.prompt for x in records)
            if len(records) != expected.count or not prompt_match:
                raise MediaAttributionError(
                    detail=f"image pipeline prompt {index}: count/prompt mismatch "
                    f"(received {len(records)}, expected {expected.count})",
                    route="image_pipeline",
                )
            images = tuple(
                GeneratedImage(
                    media_name=x.media_id,
                    workflow_id=x.workflow_id,
                    seed=x.seed,
                    prompt=x.prompt,
                    model_name_type=None,
                    aspect_ratio=expected.aspect.value,
                    fife_url=x.image_url,
                    dimensions=x.dimensions,
                    display_name=x.display_name,
                )
                for x in records
            )
            self.results[index] = BatchSubmissionResult(
                status="ok",
                project_id=self.project_id,
                prompt_idx=index,
                prompt_hash=_hash(expected.prompt),
                images=images,
            )
            log.info("image_pipeline.completed", index=index, images=len(images))
        except Exception as exc:
            self._failure(index, exc)
            log.warning(
                "image_pipeline.response_failed", index=index, error_class=type(exc).__name__
            )
        finally:
            self.done[index].set()

    async def await_submission(self, index: int, *, timeout_s: float = 12.0) -> None:
        await asyncio.wait_for(self.seen[index].wait(), timeout=timeout_s)

    async def wait_results(
        self, *, project_id: str, timeout_s: float
    ) -> list[BatchSubmissionResult]:
        self.project_id = project_id
        pending = [
            i
            for i in range(len(self.prompts))
            if self.request_seen(i) and not self.done[i].is_set()
        ]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(self.done[i].wait() for i in pending)),
                    timeout=timeout_s,
                )
            except TimeoutError:
                for i in pending:
                    if not self.done[i].is_set():
                        self._failure(
                            i,
                            WireFormatError(
                                detail=f"image pipeline prompt {i} timed out waiting for reply",
                                route="image_pipeline",
                            ),
                        )

        if self.response_tasks:
            finished, outstanding = await asyncio.wait(self.response_tasks, timeout=2.0)
            for task in outstanding:
                task.cancel()
            if outstanding:
                await asyncio.gather(*outstanding, return_exceptions=True)
            for task in finished:
                if not task.cancelled():
                    task.exception()

        for i in range(len(self.prompts)):
            if i not in self.results:
                self._failure(
                    i,
                    WireFormatError(
                        detail=f"image pipeline prompt {i} had no attributed reply",
                        route="image_pipeline",
                    ),
                )

        media_ids = [img.media_name for result in self.results.values() for img in result.images]
        if len(set(media_ids)) != len(media_ids):
            raise MediaAttributionError(
                detail="image pipeline received the same media ID for multiple prompts",
                route="image_pipeline",
            )
        return [self.results[i] for i in range(len(self.prompts))]


async def run_images_pipelined(
    page: Page,
    prompts: list[GenerateImageRequest],
    *,
    project_id: str,
    jitter_range: tuple[float, float],
) -> list[BatchSubmissionResult]:
    """Submit independent prompts before awaiting their image replies.

    Never retry a click automatically: the first gesture may already have
    spent image quota even if Playwright subsequently reports a timeout.
    """
    requests = tuple(prompts)
    collector = ImageBatchCollector(requests)
    collector.project_id = project_id
    first = requests[0]
    settings = (
        first.aspect,
        first.model,
        first.count,
        first.refs,
        first.ref_paths,
        first.reference_entities,
        first.instructions,
    )
    if any(
        (
            r.aspect,
            r.model,
            r.count,
            r.refs,
            r.ref_paths,
            r.reference_entities,
            r.instructions,
        )
        != settings
        for r in requests
    ):
        raise ConfigurationError("pipeline needs the same image settings for every prompt")
    if first.ref_paths or first.reference_entities or first.instructions:
        raise ConfigurationError("pipeline uses pre-uploaded shared image refs only")
    if any(not ref.in_project or not ref.display_name for ref in first.refs):
        raise ConfigurationError("pipeline references must be uploaded to this project")
    references = first.refs
    reference_ids = tuple(ref.name for ref in references)

    composer = MigratedComposer()
    await composer.ensure_editor(page, project_id)
    if references:
        # New uploads may not be indexed in the @ picker immediately. The
        # existing-reference path reloads until the media appears in the grid,
        # then matches the option by thumbnail identity, not caption alone.
        await composer.reference_existing(page, project_id, first)
        reference_tokens = await composer.await_existing_references(page, first.refs)
    else:
        await composer.apply_image_settings(page, first)
        reference_tokens = {}

    def on_request(request: Request) -> None:
        collector.observe_request(request)

    async def guard_reference(route: Any, raw_request: Request) -> None:
        if _rpcid(str(raw_request.url)) != IMAGE_SUBMIT_RPC:
            await route.continue_()
            return
        body = _post_data(raw_request)
        problem = _image_body_problem(body, reference_ids, first.model) if references else None
        if problem is not None:
            for i, req in enumerate(requests):
                if _request_mentions_prompt(body, req.prompt):
                    collector.mark_submission_failed(
                        i, WireFormatError(detail=problem, route=f"batchexecute:{IMAGE_SUBMIT_RPC}")
                    )
            log.error(
                "image_pipeline.reference_submit_aborted",
                reason="missing_reference_or_model",
            )
            await route.abort()
            return
        if references:
            log.info("image_pipeline.reference_on_wire", media_ids=reference_ids)
        await route.continue_()

    def image_submit_url(url: str) -> bool:
        return _rpcid(url) == IMAGE_SUBMIT_RPC

    page.on("request", on_request)
    page.on("response", collector.observe_response_event)
    if references:
        await page.route(image_submit_url, guard_reference)
    try:
        for index, req in enumerate(requests):
            if index:
                await asyncio.sleep(random.uniform(*jitter_range))
            try:
                field = page.locator(
                    'flow-base-prompt-box div.ProseMirror[contenteditable="true"]'
                ).first
                await field.wait_for(state="visible", timeout=10000)
                if references:
                    # The first chips were already bound by reference_existing.
                    # On following submissions, reattach the SAME image by its
                    # grid token; never upload it twice or choose by caption.
                    if index > 0:
                        await composer.clear_composer(page)
                        await composer.attach_existing_references(
                            page, first.refs, reference_tokens
                        )
                    chips = await composer.read_chips(page)
                    if len(chips) != len(references) or any(
                        chip["reference_type"] != "media" for chip in chips
                    ):
                        raise ReferenceNotFoundError(
                            detail=f"pipeline prompt {index}: reference chip count/type mismatch"
                        )
                elif (await field.inner_text()).strip():
                    await field.click()
                    await page.keyboard.press("ControlOrMeta+A")
                    await page.keyboard.press("Backspace")

                await composer.send_prompt(page, req.prompt, append=bool(references))
                submit = await composer._pre_submit_gate(page)  # pyright: ignore[reportPrivateUsage]
                await submit.wait_for(state="visible", timeout=9000)
                await submit.click(timeout=6500)
                await collector.await_submission(index)
                if collector.done[index].is_set() and collector.results[index].status == "fail":
                    for rest in range(index + 1, len(requests)):
                        collector.mark_submission_failed(
                            rest, ConfigurationError("not submitted after reference guard failed")
                        )
                    break
            except Exception as exc:
                if not collector.request_seen(index):
                    collector.mark_submission_failed(index, exc)
                    for rest in range(index + 1, len(requests)):
                        collector.mark_submission_failed(
                            rest,
                            ConfigurationError(
                                "not submitted after earlier pipeline submit failure"
                            ),
                        )
                    break
        return await collector.wait_results(project_id=project_id, timeout_s=IMAGE_REPLY_BUDGET_S)
    finally:
        page.remove_listener("request", on_request)
        page.remove_listener("response", collector.observe_response_event)
        if references:
            await page.unroute(image_submit_url, guard_reference)
