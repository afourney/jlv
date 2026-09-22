from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event

import regex
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, Static


@dataclass(frozen=True)
class SearchOptions:
    query: str
    regular_expression: bool = False
    match_case: bool = False

    def compile(self) -> regex.Pattern[str]:
        expression = self.query if self.regular_expression else regex.escape(self.query)
        flags = regex.VERSION0 | (0 if self.match_case else regex.IGNORECASE)
        return regex.compile(expression, flags)


@dataclass(frozen=True)
class SearchHit:
    record: int
    start: int
    end: int
    text: str
    wrapped: bool = False


def find_match(
    text_at: Callable[[int], str],
    count: int,
    options: SearchOptions,
    origin: tuple[int, int],
    previous: SearchHit | None,
    backwards: bool,
    cancelled: Event,
    timeout: float = 0.1,
) -> SearchHit | None:
    """Search records independently, retaining only the next result."""
    pattern = options.compile()
    record, offset = origin
    if previous is not None:
        record = previous.record
        offset = previous.start if backwards else previous.end + (previous.start == previous.end)
    step = -1 if backwards else 1
    for distance in range(count + 1):
        if cancelled.is_set():
            return None
        index = (record + step * distance) % count
        text = text_at(index)
        if cancelled.is_set():
            return None
        wrapped = record + step * distance not in range(count) or distance == count
        lower, upper = 0, len(text) + 1
        if distance == 0:
            if backwards:
                upper = offset
            else:
                lower = offset
        elif distance == count:
            if backwards:
                lower = offset
            else:
                upper = offset
        found: tuple[int, int] | None = None
        try:
            if backwards:
                # Forward iteration preserves the same non-overlapping matches
                # in both directions, including anchors and lookarounds.
                for match in pattern.finditer(text, timeout=timeout, concurrent=True):
                    if cancelled.is_set():
                        return None
                    if match.start() >= upper:
                        break
                    if match.start() >= lower:
                        found = match.span()
            elif lower <= len(text):
                match = pattern.search(text, pos=lower, timeout=timeout, concurrent=True)
                if match is not None and match.start() < upper:
                    found = match.span()
        except TimeoutError as error:
            raise TimeoutError(
                f"Search timed out in record {index + 1}; simplify the expression."
            ) from error
        if found is not None:
            return SearchHit(index, *found, text, wrapped)
    return None


def text_location(text: str, offset: int) -> tuple[int, int]:
    """Convert a character offset to a (line, column), splitting only on LF."""
    row = text.count("\n", 0, offset)
    return row, offset - (text.rfind("\n", 0, offset) + 1)


class SearchBar(Vertical):
    DEFAULT_CSS = """
    SearchBar {
        height: auto;
        background: $surface;
    }
    SearchBar Input { width: 1fr; height: 3; }
    SearchBar Static { height: auto; padding: 0 1; }
    """

    class Found(Message):
        def __init__(self, bar: SearchBar, hit: SearchHit) -> None:
            self.bar, self.hit = bar, hit
            super().__init__()

    class Cleared(Message):
        pass

    class Submitted(Message):
        pass

    class Command(Message):
        def __init__(self, bar: SearchBar, text: str) -> None:
            self.bar, self.text = bar, text
            super().__init__()

    def __init__(
        self,
        count: int,
        text_at: Callable[[int], str],
        origin: Callable[[], tuple[int, int]],
        scope: str,
    ) -> None:
        super().__init__(id="find-bar")
        self.count, self.text_at, self.origin = count, text_at, origin
        self.scope = scope
        self.regular_expression = False
        self.match_case = False
        self.command_mode = False
        self._saved_query = ""
        self._saved_status = ""
        self.command_scope = ""
        self._status = "Enter/F3/n: next | Shift+F3/N: previous | Esc: close"
        self.hit: SearchHit | None = None
        self.applied_origin: tuple[int, int] | None = None
        self._request_origin: tuple[int, int] | None = None
        self._cancelled = Event()
        self._search_task: asyncio.Task[None] | None = None
        self.display = False

    def compose(self) -> ComposeResult:
        yield Input(placeholder=f"Find in {self.scope}", id="find-query")
        yield Static(self._status_text(), id="find-status", markup=False)

    def _status_text(self) -> str:
        if self.command_mode:
            return f"{self.command_scope}: :number to jump | :q to close | Esc: cancel\n{self._status}"
        regex_state = "on" if self.regular_expression else "off"
        case_state = "on" if self.match_case else "off"
        return (
            f"Alt+R: Regex {regex_state} | Alt+C: Match case {case_state}\n"
            f"{self._status}"
        )

    def status(self, text: str) -> None:
        self._status = text
        self.query_one("#find-status", Static).update(self._status_text())

    def open(self) -> None:
        self.restore_search()
        self.display = True
        self.query_one(Input).focus()

    def open_command(self, scope: str) -> None:
        self.cancel()
        field = self.query_one(Input)
        if not self.command_mode:
            self._saved_query = field.value
            self._saved_status = self._status
        self.command_mode = True
        self.command_scope = scope
        with self.prevent(Input.Changed):
            field.value = ":"
        field.placeholder = ":number or :q"
        field.select_on_focus = False
        self.status("")
        self.display = True
        field.focus()
        field.cursor_position = len(field.value)

    def restore_search(self) -> None:
        if self.command_mode:
            self.command_mode = False
            field = self.query_one(Input)
            field.select_on_focus = True
            with self.prevent(Input.Changed):
                field.value = self._saved_query
            field.placeholder = f"Find in {self.scope}"
            self.status(self._saved_status)

    def cancel(self) -> None:
        self._cancelled.set()
        if self._search_task is not None:
            self._search_task.cancel()
            self._search_task = None

    @property
    def searching(self) -> bool:
        return self._search_task is not None and not self._search_task.done()

    def accepts(self, hit: SearchHit) -> bool:
        return (
            self.hit is hit
            and not self._cancelled.is_set()
            and self.origin() == self._request_origin
        )

    def close(self) -> None:
        if self.searching:
            self.status("Search cancelled")
        self.cancel()
        self.restore_search()
        self.display = False
        self.post_message(self.Cleared())

    def on_unmount(self) -> None:
        self.cancel()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.command_mode:
            self.status("")
        else:
            self.reset()
        event.stop()

    def toggle_regex(self) -> None:
        self.regular_expression = not self.regular_expression
        self.reset()

    def toggle_case(self) -> None:
        self.match_case = not self.match_case
        self.reset()

    def reset(self) -> None:
        self.cancel()
        self.hit = None
        self.applied_origin = None
        self.status("Enter/F3/n: next | Shift+F3/N: previous | Esc: close")
        self.post_message(self.Cleared())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self.command_mode:
            self.post_message(self.Command(self, event.value))
        elif self.find():
            self.post_message(self.Submitted())
        event.stop()

    def find(self, backwards: bool = False) -> bool:
        self.restore_search()
        options = SearchOptions(
            self.query_one(Input).value,
            self.regular_expression,
            self.match_case,
        )
        self.display = True
        if not options.query:
            self.open()
            self.status("Enter text to find.")
            return False
        self.cancel()
        try:
            options.compile()
        except regex.error as error:
            self.status(f"Invalid regular expression: {error}")
            return False
        origin = self.origin()
        self._request_origin = origin
        previous = self.hit if origin == self.applied_origin else None
        cancelled = self._cancelled = Event()
        self.status("Searching...")

        async def search() -> None:
            try:
                hit = await asyncio.to_thread(
                    find_match, self.text_at, self.count, options,
                    origin, previous, backwards, cancelled,
                )
            except asyncio.CancelledError:
                return
            except (OSError, ValueError, RecursionError, TimeoutError, regex.error) as error:
                if not cancelled.is_set() and self.is_mounted:
                    self.status(str(error))
                return
            if cancelled.is_set() or not self.is_mounted:
                return
            if hit is None:
                self.hit = None
                self.post_message(self.Cleared())
                self.status("No match")
                return
            self.hit = hit
            self.status("Wrapped" if hit.wrapped else "Match")
            self.post_message(self.Found(self, hit))

        self._search_task = asyncio.create_task(search())
        return True
