"""Project create / rename on flow.google.com (#864).

The fake models what `docs/superpowers/spikes/2026-09-17-project-create-on-flow-google-com.md`
measured: the projects page's `add` FAB fires `jHPbke` and answers `[id, [title]]`; the
project header's `flow-editable-text` input renames through `o8DA4`, answering `[title]`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gflow_cli.api.transports import migrated_composer as mc
from gflow_cli.errors import TransportTimeoutError, UiSelectorDriftError, WireFormatError

PID = "57c099ee-a5d6-4e2d-83c9-0346f9644b09"


def _frame(rpcid: str, payload: Any) -> str:
    frame = [["wrb.fr", rpcid, json.dumps(payload), None, None, None, "generic"]]
    return ")]}'\n\n10\n" + json.dumps(frame) + "\n"


def _batch(rpcid: str) -> str:
    return f"https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute?rpcids={rpcid}"


class _Response:
    def __init__(self, url: str, text: str) -> None:
        self.url, self._text = url, text

    async def text(self) -> str:
        if self._text == "__UNREADABLE__":
            raise RuntimeError("Protocol error reading response body")
        return self._text


class _Loc:
    def __init__(self, page: FakePage, sel: str) -> None:
        self.page, self.sel = page, sel

    @property
    def first(self) -> _Loc:
        return self

    async def wait_for(self, **_: Any) -> None:
        if not self.page.present.get(self.sel, False):
            raise mc.PlaywrightTimeoutError(f"waiting for {self.sel}")

    async def click(self, **_: Any) -> None:
        self.page.clicks.append(self.sel)
        await self.page.fire(self.page.on_click.get(self.sel))

    async def fill(self, value: str, **_: Any) -> None:
        self.page.filled = value
        self.page.current_title = value

    async def press(self, key: str, **_: Any) -> None:
        if key == "Enter":
            await self.page.fire(self.page.on_enter)

    async def input_value(self) -> str:
        return (
            self.page.server_title if self.page.current_title is None else self.page.current_title
        )

    async def is_visible(self) -> bool:
        return False

    async def count(self) -> int:
        return 1 if self.page.present.get(self.sel, False) else 0


class FakePage:
    def __init__(self, *, url: str = "about:blank") -> None:
        self.url = url
        self.gotos: list[str] = []
        self.clicks: list[str] = []
        self.filled: str | None = None
        self.current_title: str | None = None
        self.server_title = "original title"
        self.persist_rename_on_enter = False
        self.reload_count = 0
        self.present: dict[str, bool] = {mc.NEW_PROJECT_BUTTON: True, mc.PROJECT_TITLE_INPUT: True}
        self.on_click: dict[str, tuple[str, str] | None] = {}
        self.on_enter: tuple[str, str] | None = None
        self.navigate_on_create: str | None = None
        self._handlers: list[Any] = []
        self.keyboard = self

    async def goto(self, url: str, **_: Any) -> None:
        self.gotos.append(url)
        self.url = url

    async def reload(self, **_: Any) -> None:
        self.reload_count += 1
        self.current_title = self.server_title

    def locator(self, sel: str) -> _Loc:
        return _Loc(self, sel)

    def on(self, event: str, handler: Any) -> None:
        if event == "response":
            self._handlers.append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        if event == "response" and handler in self._handlers:
            self._handlers.remove(handler)

    async def fire(self, reply: tuple[str, str] | None) -> None:
        if reply is None:
            return
        if self.persist_rename_on_enter and reply == self.on_enter:
            self.server_title = self.filled or ""
        for h in list(self._handlers):
            await h(_Response(*reply))
        if self.navigate_on_create:
            self.url = self.navigate_on_create


def _page_creating(title: str = "Sep 17 - 17:02", pid: str = PID) -> FakePage:
    page = FakePage()
    page.on_click[mc.NEW_PROJECT_BUTTON] = (
        _batch(mc.CREATE_PROJECT_RPC),
        _frame("jHPbke", [pid, [title]]),
    )
    page.navigate_on_create = f"https://flow.google.com/project/{pid}"
    return page


# --- create -----------------------------------------------------------------------------


async def test_create_reads_id_and_title_from_the_rpc_reply() -> None:
    page = _page_creating(title="Sep 17 - 17:02")
    page.on_enter = (_batch(mc.RENAME_PROJECT_RPC), _frame("o8DA4", ["my film"]))

    info = await mc.create_project(page, "my film")  # type: ignore[arg-type]

    assert info.project_id == PID
    assert info.title == "my film"
    assert page.gotos[0] == "https://flow.google.com/"
    assert page.clicks == [mc.NEW_PROJECT_BUTTON]
    assert page.filled == "my film"  # the requested title was applied by rename


async def test_create_skips_rename_when_the_reply_already_carries_the_title() -> None:
    page = _page_creating(title="my film")

    info = await mc.create_project(page, "my film")  # type: ignore[arg-type]

    assert info.title == "my film"
    assert page.filled is None


async def test_create_without_the_fab_is_drift_naming_the_anchor() -> None:
    page = _page_creating()
    page.present[mc.NEW_PROJECT_BUTTON] = False

    with pytest.raises(UiSelectorDriftError, match="new-project"):
        await mc.create_project(page, "x", timeout_s=0.1)  # type: ignore[arg-type]


async def test_create_with_no_reply_times_out() -> None:
    page = _page_creating()
    page.on_click[mc.NEW_PROJECT_BUTTON] = None

    with pytest.raises(TransportTimeoutError, match=mc.CREATE_PROJECT_RPC):
        await mc.create_project(page, "x", timeout_s=0.1)  # type: ignore[arg-type]


async def test_create_reply_without_a_project_id_is_a_wire_format_error() -> None:
    page = _page_creating(pid="not-a-uuid")

    with pytest.raises(WireFormatError, match=mc.CREATE_PROJECT_RPC):
        await mc.create_project(page, "x", timeout_s=0.1)  # type: ignore[arg-type]


async def test_create_ignores_other_rpcs_while_waiting() -> None:
    page = FakePage()
    page.on_click[mc.NEW_PROJECT_BUTTON] = (_batch("UpteDb"), _frame("UpteDb", [[[PID, ["old"]]]]))

    with pytest.raises(TransportTimeoutError):
        await mc.create_project(page, "x", timeout_s=0.1)  # type: ignore[arg-type]


# --- rename -----------------------------------------------------------------------------


async def test_unreadable_rename_reply_is_verified_from_reloaded_project() -> None:
    """The real browser sees HTTP 200 but response.text() raises after UI navigation."""
    page = FakePage(url=f"https://flow.google.com/project/{PID}")
    page.server_title = "old title"
    page.on_enter = (_batch(mc.RENAME_PROJECT_RPC), "__UNREADABLE__")
    page.persist_rename_on_enter = True

    await mc.rename_project(page, PID, "new title", timeout_s=0.02)  # type: ignore[arg-type]

    assert page.reload_count == 1
    assert page.server_title == "new title"


async def test_unreadable_rename_reply_without_persisted_title_still_fails() -> None:
    page = FakePage(url=f"https://flow.google.com/project/{PID}")
    page.server_title = "old title"
    page.on_enter = (_batch(mc.RENAME_PROJECT_RPC), "__UNREADABLE__")

    with pytest.raises(TransportTimeoutError, match=mc.RENAME_PROJECT_RPC):
        await mc.rename_project(page, PID, "new title", timeout_s=0.02)  # type: ignore[arg-type]

    assert page.reload_count == 1
    assert page.server_title == "old title"


async def test_rename_skips_rpc_when_project_title_is_already_correct() -> None:
    page = FakePage(url=f"https://flow.google.com/project/{PID}")
    page.server_title = "gflow-cli images"
    await mc.rename_project(page, PID, "gflow-cli images", timeout_s=0.02)  # type: ignore[arg-type]
    assert page.filled is None
    assert page.reload_count == 0


async def test_rename_navigates_to_the_project_and_confirms_the_echo() -> None:
    page = FakePage()
    page.on_enter = (_batch(mc.RENAME_PROJECT_RPC), _frame("o8DA4", ["new title"]))

    await mc.rename_project(page, PID, "new title")  # type: ignore[arg-type]

    assert page.gotos == [f"https://flow.google.com/project/{PID}"]
    assert page.filled == "new title"


async def test_rename_does_not_reload_a_project_page_already_open() -> None:
    page = FakePage(url=f"https://flow.google.com/project/{PID}")
    page.on_enter = (_batch(mc.RENAME_PROJECT_RPC), _frame("o8DA4", ["t"]))

    await mc.rename_project(page, PID, "t")  # type: ignore[arg-type]

    assert page.gotos == []


async def test_rename_echoing_another_title_is_a_wire_format_error() -> None:
    page = FakePage()
    page.on_enter = (_batch(mc.RENAME_PROJECT_RPC), _frame("o8DA4", ["something else"]))

    with pytest.raises(WireFormatError, match="something else"):
        await mc.rename_project(page, PID, "wanted", timeout_s=0.1)  # type: ignore[arg-type]


async def test_rename_without_the_title_input_is_drift() -> None:
    page = FakePage()
    page.present[mc.PROJECT_TITLE_INPUT] = False

    with pytest.raises(UiSelectorDriftError, match="title"):
        await mc.rename_project(page, PID, "t", timeout_s=0.1)  # type: ignore[arg-type]


def test_anchors_are_structural_not_labels() -> None:
    """AGENTS.md locale-invariance: no display text in these selectors."""
    assert "flow-projects-page" in mc.NEW_PROJECT_BUTTON
    assert "'add'" in mc.NEW_PROJECT_BUTTON or '"add"' in mc.NEW_PROJECT_BUTTON
    assert mc.PROJECT_TITLE_INPUT.startswith("flow-editable-text")
