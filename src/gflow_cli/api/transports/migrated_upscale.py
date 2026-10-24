"""Drive Flow's migrated ``flow.google.com`` editor to upscale generated images.

Google moved Flow from ``labs.google`` onto ``flow.google.com`` (#639). On that frontend,
upscaling is triggered through the image detail view download menu, which calls the
``SPrCad`` RPC over ``batchexecute`` and returns base64 image bytes directly.
"""

from __future__ import annotations

import asyncio
import base64
import re
from typing import TYPE_CHECKING, Any, cast

import structlog
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from gflow_cli.api.image_upscale import TargetResolution
from gflow_cli.api.transports.batchexecute import parse_frames, rpc_errors
from gflow_cli.api.transports.migrated_composer import MIGRATED_PROJECT_URL
from gflow_cli.errors import (
    TransportTimeoutError,
    UiSelectorDriftError,
    UpscaleUnavailableError,
    WireFormatError,
)
from gflow_cli.paths import extension_from_magic

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import Locator, Page

log = structlog.get_logger(__name__)

UPSCALE_RPCID = "SPrCad"
_DEFAULT_TIMEOUT_S = 90.0
_MENU_TIMEOUT_MS = 5_000

# The download button in the image / video detail view (icon ligature, not a label).
DOWNLOAD_BUTTON_SELECTOR = (
    'button:has(mat-icon:text-is("download")), button:has(.google-symbols:text-is("download"))'
)


def missing_tile_hint(media_id: str, project_id: str) -> str:
    """A tile that is not in the project is likelier a wrong id or project than drift."""
    return (
        f"Check that media {media_id} belongs to project {project_id}: pass the owning "
        "project with --project (a catalog-resolved project may not hold it). If it does, "
        "Flow's page changed — file a bug at https://github.com/ffroliva/gflow-cli/issues."
    )


def menu_token_pattern(token: str) -> re.Pattern[str]:
    """Match ``token`` (``2K``, ``1080p``, …) as a whole token, case-insensitively.

    ``2K`` matches "2K (Aprimorada)" but not "12K"; ``1080p`` never matches "080p".
    """
    return re.compile(rf"(?<![0-9A-Za-z]){re.escape(token)}(?![0-9A-Za-z])", re.IGNORECASE)


async def find_download_menu_item(page: Page, token: str, *, route: str) -> Locator:
    """The open download menu's item for resolution ``token``, picked by that token.

    Measured 2026-10-07 (scripts/dev/spike_upscale_menu_anchors.py, en) against the
    contributor's 2026-09-30 pt capture: every item is a bare
    ``button[role=menuitem][mat-menu-item]`` with no per-option attribute or icon; the
    ORDER differs between the two (video 270p/720p/1080p vs 720p/1080p/270p) and the
    WORDS are translated ("2K (Aprimorada)", "Tamanho original"), but the resolution
    token itself is identical in en and pt. So the token is the only stable anchor —
    it is a format name, not a translated label. A missing item is selector drift
    (exit 23); a present-but-disabled one is the caller's tier gate (exit 22).
    """
    items = page.locator('[role="menuitem"]')
    try:
        await items.first.wait_for(state="visible", timeout=_MENU_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise UiSelectorDriftError(
            detail=f"migrated upscale: the download menu did not open on {page.url}",
            route=route,
        ) from exc
    match = items.filter(has_text=menu_token_pattern(token))
    if await match.count() == 0:
        raise UiSelectorDriftError(
            detail=f"migrated upscale: menu item for {token} was not found on {page.url}",
            route=route,
        )
    return match.first


async def is_menu_item_disabled(item: Locator) -> bool:
    return await item.is_disabled() or await item.get_attribute("aria-disabled") == "true"


async def wait_for_or_none(page: Page, selector: str, timeout_ms: int) -> Locator | None:
    """The first visible match of ``selector``, or ``None`` on a timeout.

    A Locator, never an ``ElementHandle`` (#957): Flow re-renders the grid and the detail
    view after they first appear, and a handle resolved once is then clicked detached —
    ``Element is not attached to the DOM``. A Locator re-resolves on every action.
    """
    target = page.locator(selector).first
    try:
        await target.wait_for(timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return None
    return target


async def click_download_button(page: Page, *, media_id: str, prefix: str, route: str) -> None:
    """Open the download menu of the media shown in the detail view."""
    download_btn = await wait_for_or_none(page, DOWNLOAD_BUTTON_SELECTOR, 15_000)
    if not download_btn:
        raise UiSelectorDriftError(
            detail=f"{prefix}: download button not found for media_id {media_id} on {page.url}",
            route=route,
        )
    await download_btn.click()
    await page.wait_for_timeout(500)


async def open_tile_download_menu(
    page: Page,
    *,
    project_id: str,
    media_id: str,
    tile_selector: str,
    tile_timeout_ms: int,
    kind: str,
    prefix: str,
    route: str,
) -> None:
    """Open ``media_id``'s tile on the project grid, then its download menu."""
    await page.goto(
        MIGRATED_PROJECT_URL.format(project_id=project_id), wait_until="domcontentloaded"
    )
    tile = await wait_for_or_none(page, tile_selector, tile_timeout_ms)
    if not tile:
        raise UiSelectorDriftError(
            detail=f"{prefix}: {kind} tile for media_id {media_id} not found on {page.url}",
            route=route,
            remediation_hint=missing_tile_hint(media_id, project_id),
        )
    await tile.click()
    await page.wait_for_timeout(1000)
    await click_download_button(page, media_id=media_id, prefix=prefix, route=route)


def _unavailable_error(target_resolution: TargetResolution) -> UpscaleUnavailableError:
    if target_resolution is TargetResolution.RES_4K:
        return UpscaleUnavailableError(
            detail=(
                "4K upscale requires a Flow Ultra subscription. "
                "Your account supports up to 2K (use --scale 2k)."
            ),
            route="upsampleImage",
            status=403,
        )
    return UpscaleUnavailableError(
        detail="2K upscale is not available on this account.",
        route="upsampleImage",
        status=403,
    )


def _sprcad_b64(text: str) -> str | None:
    """The upscaled image's base64 in an ``SPrCad`` reply; raises on an RPC refusal."""
    for err in rpc_errors(text):
        if err.rpcid == UPSCALE_RPCID:
            raise WireFormatError(
                detail=f"SPrCad RPC refused: code={err.code} reasons={err.reasons}",
                route="image_upscale",
            )
    for rpcid, payload in parse_frames(text):
        if rpcid != UPSCALE_RPCID or not isinstance(payload, list):
            continue
        items = cast("list[Any]", payload)
        if len(items) >= 2 and isinstance(items[1], str) and items[1]:
            return items[1]
    return None


# Capture in the page's Fetch stream, *before* Chrome discards a download response.
# Live A/B, 2026-10-23: unmodified Playwright response.text() intermittently raised
# Network.getResponseBody/No data; that exact SPrCad request finished ERR_ABORTED
# after HTTP 200. A simultaneous response.clone().text() inside Chrome read the
# complete ~1 MB RPC payload in 12/12 trials. No image bytes enter browser logs.
_SPRCAD_CAPTURE_INSTALL = r"""() => {
    const oldFetch = window.fetch;
    const oldAnchorClick = HTMLAnchorElement.prototype.click;
    const result = {done: false, http_status: null, body: null, error: null};
    const wrapper = function(...args) {
        let relevant = false;
        try {
            const target = typeof args[0] === 'string' ? args[0] : args[0]?.url;
            const url = new URL(target, location.href);
            relevant = url.origin === location.origin && url.pathname.includes('/batchexecute')
                && (url.searchParams.get('rpcids') || '').split(',').includes('SPrCad');
        } catch (_) { /* Preserve Flow's own fetch behavior. */ }
        const promise = oldFetch.apply(this, args);
        if (relevant && !result.seen) {
            result.seen = true;
            promise.then(response => {
                result.http_status = response.status;
                try {
                    response.clone().text().then(body => {
                        if (body.length > 64000000) {
                            result.error = 'SPrCad response exceeds the 64 MB capture limit';
                        } else {
                            result.body = body;
                        }
                        result.done = true;
                    }).catch(error => {
                        result.error = String(error);
                        result.done = true;
                    });
                } catch (error) {
                    result.error = String(error);
                    result.done = true;
                }
            }).catch(error => {
                result.error = String(error);
                result.done = true;
            });
        }
        return promise;
    };
    const suppressDownload = function() {};
    window.fetch = wrapper;
    HTMLAnchorElement.prototype.click = suppressDownload;
    window.__gflowUpscaleCapture = result;
    window.__gflowUpscaleCaptureRestore = function() {
        if (window.fetch === wrapper) window.fetch = oldFetch;
        if (HTMLAnchorElement.prototype.click === suppressDownload) {
            HTMLAnchorElement.prototype.click = oldAnchorClick;
        }
        delete window.__gflowUpscaleCapture;
        delete window.__gflowUpscaleCaptureRestore;
    };
}"""


def _sprcad_captured_b64(captured: Any) -> str:
    """Validate the in-page HTTP response and decode its SPrCad frame in Python."""
    if not isinstance(captured, dict):
        raise WireFormatError(detail="Missing browser SPrCad capture", route="image_upscale")
    response = cast("dict[str, Any]", captured)
    if response.get("error"):
        raise WireFormatError(
            detail=f"Could not read SPrCad response in browser: {str(response['error'])[:200]}",
            route="image_upscale",
        )
    status = response.get("http_status")
    if status != 200:
        raise WireFormatError(detail=f"SPrCad returned HTTP {status}", route="image_upscale")
    body = response.get("body")
    if not isinstance(body, str) or len(body) > 64_000_000:
        raise WireFormatError(detail="Invalid SPrCad browser response body", route="image_upscale")
    b64_data = _sprcad_b64(body)
    if b64_data is None:
        raise WireFormatError(detail="Reply did not contain an SPrCad image", route="image_upscale")
    return b64_data


def settle_exception(future: asyncio.Future[Any], exc: BaseException) -> None:
    """Fail ``future`` with ``exc`` unless it already settled (first outcome wins)."""
    if not future.done():
        future.set_exception(exc)


async def upscale_image_migrated(
    page: Page,
    *,
    project_id: str,
    media_id: str,
    target_resolution: TargetResolution,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> bytes:
    """Upscale an image on the migrated ``flow.google.com`` frontend.

    Navigates to the project, selects the image tile by ``data-media-id``, opens
    the download menu, checks whether the requested scale is available on the
    current account tier, triggers the upscale, and decodes the resulting
    ``SPrCad`` payload into raw image bytes.

    Returns:
        Raw decoded JPEG/PNG bytes.
    """
    log.info("migrated_upscale.navigate", project_id=project_id, media_id=media_id)
    await open_tile_download_menu(
        page,
        project_id=project_id,
        media_id=media_id,
        tile_selector=f'img[data-media-id="{media_id}"]',
        tile_timeout_ms=30_000,
        kind="image",
        prefix="migrated upscale",
        route="image_upscale",
    )

    scale_label = "4K" if target_resolution is TargetResolution.RES_4K else "2K"
    btn_target = await find_download_menu_item(page, scale_label, route="image_upscale")
    if await is_menu_item_disabled(btn_target):
        raise _unavailable_error(target_resolution)

    # Playwright response.text() retrieves the body via Network.getResponseBody.
    # On SPrCad the network request intermittently becomes ERR_ABORTED *after*
    # HTTP 200, so Chrome has no body to give CDP. Clone the fetch response in
    # the page before Flow's UI turns it into a download instead.
    await page.evaluate(_SPRCAD_CAPTURE_INSTALL)
    try:
        await btn_target.click()
        try:
            await page.wait_for_function(
                "() => window.__gflowUpscaleCapture?.done === true",
                timeout=max(1, int(timeout_s * 1000)),
            )
        except PlaywrightTimeoutError as exc:
            raise TransportTimeoutError(
                detail=(
                    f"Timed out after {timeout_s}s waiting for {UPSCALE_RPCID} response from Flow"
                ),
                route="image_upscale",
            ) from exc
        captured = await page.evaluate(
            """() => {
                const value = window.__gflowUpscaleCapture;
                return value ? {
                    http_status: value.http_status, body: value.body, error: value.error
                } : null;
            }"""
        )
        b64_data = _sprcad_captured_b64(captured)
    finally:
        try:
            await page.evaluate("""() => window.__gflowUpscaleCaptureRestore?.()""")
        except Exception:  # noqa: BLE001 - page may have closed
            pass

    try:
        image_bytes = base64.b64decode(b64_data)
    except ValueError as exc:
        raise WireFormatError(
            detail="upsampleImage returned undecodable image data",
            route="upsampleImage",
        ) from exc

    if extension_from_magic(image_bytes) not in (".jpg", ".png"):
        raise WireFormatError(
            detail="upscaled output is not a valid PNG/JPEG",
            route="upsampleImage",
        )

    log.info(
        "migrated_upscale.completed",
        media_id=media_id,
        resolution=target_resolution.name,
        bytes=len(image_bytes),
    )
    return image_bytes
