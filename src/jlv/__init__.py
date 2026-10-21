from __future__ import annotations

import contextlib
import json
import sys
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from rich.cells import cell_len
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.document._document import Selection as TextSelection
from textual.geometry import Offset, Region
from textual.message import Message
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option
from textual.worker import Worker

from jlv.data_uri import NativePreview, PreviewInfo
from jlv.rows import PanelKey, RowView
from jlv.scripts import Script, ScriptResult, Selection, config_path, execute, load_scripts
from jlv.search import SearchBar, text_location
from jlv.source import JsonlFile


def decode_json(text: str) -> object | None:
    """Decode a JSON-encoded string to an object, array, or string; otherwise None."""
    stripped = text.strip()
    # Skip parsing plain text: objects, arrays, and strings start and end with these.
    if len(stripped) < 2 or stripped[0] not in '{["' or stripped[-1] not in '}]"':
        return None
    try:
        decoded = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    # Scalars such as "42" or "null" are valid JSON but rarely encoded on purpose.
    return decoded if isinstance(decoded, (dict, list, str)) else None


def decode_nested(value: object) -> tuple[object, bool]:
    """Decode every JSON-encoded string within ``value`` by one layer; report any change."""
    if isinstance(value, str):
        decoded = decode_json(value)
        return (value, False) if decoded is None else (decoded, True)
    if isinstance(value, dict):
        items = {key: decode_nested(child) for key, child in value.items()}
        return {key: child for key, (child, _) in items.items()}, any(c for _, c in items.values())
    if isinstance(value, list):
        children = [decode_nested(child) for child in value]
        return [child for child, _ in children], any(c for _, c in children)
    return value, False


@dataclass
class PrettyDocument:
    lines: list[tuple[str, str | None]]
    strings: dict[int, str]
    width: int
    blocks: list[tuple[int, int]]
    value: object = None
    paths: dict[int, tuple[str | int, ...]] = field(default_factory=dict)
    _expandable: dict[int, bool] = field(default_factory=dict, repr=False, compare=False)

    def block_start(self, row: int, forward: bool) -> int:
        starts = [start for start, _ in self.blocks]
        if forward:
            index = bisect_right(starts, row)
            return starts[index] if index < len(starts) else row
        index = bisect_left(starts, row)
        return starts[index - 1] if index else row

    def matching_delimiter(self, row: int) -> int | None:
        for start, end in self.blocks:
            if row == start:
                return end
            if row == end:
                return start
        return None

    @classmethod
    def from_value(cls, value: object) -> PrettyDocument:
        lines: list[tuple[str, str | None]] = []
        strings: dict[int, str] = {}
        paths: dict[int, tuple[str | int, ...]] = {}
        blocks: list[tuple[int, int]] = []

        def append(
            value: object,
            depth: int,
            prefix: str = "",
            suffix: str = "",
            path: tuple[str | int, ...] = (),
        ) -> None:
            indent = "  " * depth
            block_index = len(blocks)
            block_start = len(lines)
            if isinstance(value, (dict, list)):
                blocks.append((block_start, block_start))
            # Every row naming a value maps to its path; containers use both delimiter rows.
            paths[len(lines)] = path
            if isinstance(value, (dict, list)) and value:
                opening, closing = ("{", "}") if isinstance(value, dict) else ("[", "]")
                lines.append((indent + prefix + opening, None))
                entries = value.items() if isinstance(value, dict) else enumerate(value)
                for i, (key, child) in enumerate(entries):
                    child_prefix = (
                        json.dumps(key, ensure_ascii=False) + ": "
                        if isinstance(value, dict)
                        else ""
                    )
                    append(
                        child,
                        depth + 1,
                        child_prefix,
                        "," if i < len(value) - 1 else "",
                        (*path, key),
                    )
                paths[len(lines)] = path
                lines.append((indent + closing + suffix, None))
            else:
                kind = None
                if isinstance(value, str):
                    kind = "string"
                    strings[len(lines)] = value
                elif value is None:
                    kind = "null"
                elif isinstance(value, bool):
                    kind = "bool"
                elif isinstance(value, (int, float)):
                    kind = "number"
                lines.append(
                    (indent + prefix + json.dumps(value, ensure_ascii=False) + suffix, kind)
                )
            if isinstance(value, (dict, list)):
                blocks[block_index] = (block_start, len(lines) - 1)

        append(value, 0)
        return cls(
            lines,
            strings,
            max(
                len(line) if line.isascii() and "\x7f" not in line else cell_len(line)
                for line, _ in lines
            ),
            blocks,
            value,
            paths,
        )

    def expandable(self, row: int) -> bool:
        """Whether ``>`` can decode the string at ``row``; cached per row."""
        if row not in self.strings:
            return False
        if row not in self._expandable:
            self._expandable[row] = decode_json(self.strings[row]) is not None
        return self._expandable[row]

    def selected(self, row: int) -> tuple[tuple[str | int, ...], object] | None:
        """The path and value named by ``row``, or None for rows such as blank padding."""
        path = self.paths.get(row)
        if path is None:
            return None
        value = self.value
        for key in path:
            value = value[key]  # type: ignore[index]
        return path, value

    def decode_string(self, row: int) -> PrettyDocument | None:
        """Return a copy with the string at ``row`` replaced by its decoded JSON, if any."""
        if row not in self.strings:
            return None
        decoded = decode_json(self.strings[row])
        if decoded is None:
            return None

        def replace(value: object, path: tuple[str | int, ...]) -> object:
            if not path:
                return decoded
            copy = dict(value) if isinstance(value, dict) else list(value)  # type: ignore[arg-type]
            copy[path[0]] = replace(value[path[0]], path[1:])  # type: ignore[index]
            return copy

        return PrettyDocument.from_value(replace(self.value, self.paths[row]))


class StringView(TextArea):
    DEFAULT_CSS = """
    StringView > .text-area--selection {
        background: #ffff00;
        color: #000000;
        text-style: bold;
    }
    """

    def __init__(self, value: str) -> None:
        self._pending_value: str | None = value
        self._zero_match: tuple[int, int] | None = None
        super().__init__(
            read_only=True,
            show_cursor=False,
            highlight_cursor_line=False,
            soft_wrap=True,
            id="modal-container",
            placeholder="Loading...",
        )

    def on_resize(self, event: events.Resize) -> None:
        if self._pending_value is not None and self.content_size.width:
            # Paint the placeholder before wrapping, which can be slow for huge strings.
            self.call_after_refresh(self._load_pending)
            event.prevent_default()

    def _load_pending(self) -> None:
        if self._pending_value is None:
            return
        # Wrap once at the actual viewport width, not at each startup size.
        value, self._pending_value = self._pending_value, None
        self.placeholder = ""
        self.load_text(value)

    def set_text(self, value: str) -> None:
        if self._pending_value is not None:
            self._pending_value = value
            return
        self._zero_match = None
        self.load_text(value)
        self.scroll_to(x=0, y=0, animate=False)

    def show_match(self, text: str, start: int, end: int) -> None:
        first = text_location(text, start)
        last = text_location(text, end)
        self._zero_match = first if start == end else None
        self._line_cache.clear()
        self.selection = TextSelection(first, last)
        # Cursorless, read-only TextAreas don't scroll their selection into view.
        position = self.wrapped_document.location_to_offset(first)
        last_position = self.wrapped_document.location_to_offset(last)
        height = min(last_position.y - position.y + 1, self.scrollable_content_region.height)
        self.scroll_to_region(
            Region(position.x, position.y, 1, max(1, height)), animate=False, force=True
        )
        self.refresh()

    def clear_match(self) -> None:
        self._zero_match = None
        self._line_cache.clear()
        self.selection = TextSelection.cursor(self.selection.end)
        self.refresh()

    @property
    def row_count(self) -> int:
        return self.document.line_count

    def on_key(self, event: events.Key) -> None:
        self.post_message(PanelKey(self, event))

    def go_to_line(self, index: int) -> None:
        index = max(0, min(index, self.row_count - 1))
        self.clear_match()
        self.selection = TextSelection.cursor((index, 0))
        offset = self.wrapped_document.location_to_offset((index, 0))
        self.scroll_to(x=0, y=offset.y, animate=False)

    def move_line(self, delta: int) -> None:
        self.go_to_line(self.selection.end[0] + delta)

    def line_edge(self, end: bool) -> None:
        row = self.selection.end[0]
        column = len(self.document.get_line(row)) if end else 0
        # Preserve the logical line while revealing its first/last wrapped section.
        offset = self.wrapped_document.location_to_offset((row, column))
        self.scroll_to_region(Region(offset.x, offset.y, 1, 1), animate=False, force=True)

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        if self._zero_match is not None and self._zero_match[0] == line_index:
            column = self._zero_match[1]
            if column == len(line):
                line.append(" ")
            line.stylize("#000000 on #ffff00", column, column + 1)
        return line


# The inspector shows a plain string, rather than formatted JSON of a parsed value.
NO_DATA = object()


class StringModal(ModalScreen[None]):
    """A read-only, wrapped document with viewport-based rendering."""

    class Closed(Message):
        """Posted to the app: closing restores focus without focus events."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]
    DEFAULT_CSS = """
    StringModal {
        align: center middle;
    }
    StringModal #inspector {
        width: 90%;
        height: 90%;
        border: thick $accent;
        border-subtitle-color: $text-muted;
        background: $surface;
        padding: 1 2;
    }
    StringModal #modal-container {
        width: 1fr;
        height: 1fr;
        border: none;
        overflow-y: scroll;
        scrollbar-size: 1 1;
    }
    """

    def __init__(
        self,
        value: str,
        title: str | None = None,
        *,
        origin: tuple[object, tuple[str | int, ...]] | None = None,
        data: object = NO_DATA,
    ) -> None:
        super().__init__()
        self.value = value
        self._border_title = title
        # The record and path this string came from, if it is shown unchanged from a record.
        self.origin = origin
        # The parsed JSON value shown as formatted JSON, or NO_DATA, and earlier states.
        self._data = data
        self._decoded: list[tuple[str, object]] = []
        # What the next decode would show, if anything; computed for the border hint.
        self._next: tuple[str, object] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="inspector") as inspector:
            # Set before mounting, so the title is present as soon as the screen is.
            if self._border_title:
                inspector.border_title = self._border_title
            yield StringView(self.value)
            yield SearchBar(1, self._search_text, self._search_origin, "this string")

    def on_mount(self) -> None:
        # Decoding huge strings is slow; let the loading placeholder paint first.
        self.call_after_refresh(self._update_hint)

    def on_unmount(self) -> None:
        self.app.post_message(self.Closed())

    def _update_hint(self) -> None:
        if self._data is NO_DATA:
            decoded = decode_json(self.value)
            if decoded is None:
                self._next = None
            elif isinstance(decoded, str):
                self._next = decoded, NO_DATA
            else:
                self._next = json.dumps(decoded, indent=2, ensure_ascii=False), decoded
        else:
            data, changed = decode_nested(self._data)
            self._next = (json.dumps(data, indent=2, ensure_ascii=False), data) if changed else None
        hints = []
        if self._data is NO_DATA and self.app._native_preview.available(self.value):
            hints.append("Press > to open natively")
        elif self._next is not None:
            hints.append("Press > to decode")
        if self._decoded:
            hints.append("< to undo" if hints else "Press < to undo")
        self.query_one("#inspector").border_subtitle = " · ".join(hints)

    def _search_text(self, index: int) -> str:
        return self.value.replace("\r\n", "\n").replace("\r", "\n")

    def _search_origin(self) -> tuple[int, int]:
        view = self.query_one(StringView)
        row, column = view.selection.end
        lines = self._search_text(0).split("\n")
        return 0, sum(len(line) + 1 for line in lines[:row]) + column

    def on_search_bar_found(self, event: SearchBar.Found) -> None:
        if not event.bar.accepts(event.hit):
            return
        view = self.query_one(StringView)
        view.show_match(event.hit.text, event.hit.start, event.hit.end)
        event.bar.applied_origin = self._search_origin()
        event.stop()

    def on_search_bar_cleared(self, event: SearchBar.Cleared) -> None:
        view = self.query_one(StringView)
        view.clear_match()
        event.stop()

    def decode(self) -> bool:
        """Decode the string, or once it is formatted JSON, every encoded string within it."""
        if self._next is None:
            return False
        self._decoded.append((self.value, self._data))
        self._show(*self._next)
        return True

    def undo_decode(self) -> bool:
        if not self._decoded:
            return False
        self._show(*self._decoded.pop())
        return True

    def _show(self, value: str, data: object) -> None:
        self.value = value
        self._data = data
        self.query_one(SearchBar).cancel()
        self.query_one(StringView).set_text(value)
        self._update_hint()

    def action_dismiss(self) -> None:
        bar = self.query_one(SearchBar)
        if bar.display:
            bar.close()
            self.query_one(StringView).focus()
        else:
            self.app.pop_screen()


class NativePreviewWarning(ModalScreen[bool]):
    """Obtain consent for one file; Enter and Escape cancel by default."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    NativePreviewWarning {
        align: center middle;
    }
    NativePreviewWarning #native-warning {
        width: 76;
        max-width: 90%;
        height: auto;
        max-height: 90%;
        overflow-y: auto;
        border: thick $warning;
        padding: 1 2;
    }
    NativePreviewWarning Static {
        height: auto;
        margin-bottom: 1;
    }
    NativePreviewWarning #native-warning-title {
        text-style: bold;
    }
    NativePreviewWarning Horizontal {
        height: auto;
        align-horizontal: right;
    }
    NativePreviewWarning Button {
        margin-left: 1;
    }
    """

    def __init__(self, info: PreviewInfo) -> None:
        super().__init__()
        self.info = info

    def compose(self) -> ComposeResult:
        with Vertical(id="native-warning"):
            yield Static("Allow opening this file?", id="native-warning-title")
            yield Static(
                "Opening this file may run code or invoke an unexpected application. "
                "Only allow files from a source you trust.",
                markup=False,
            )
            yield Static(
                f"Declared type: {self.info.declared_mime or 'not specified'}\n"
                f"Detected type: {self.info.detected_mime or 'unknown'}\n"
                f"File extension: {self.info.extension}\n\n{self.info.warning}",
                markup=False,
            )
            with Horizontal():
                yield Button("Allow", variant="warning", id="native-allow")
                yield Button("Cancel", variant="primary", id="native-cancel")

    def on_mount(self) -> None:
        self.query_one("#native-cancel", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "native-allow")


class ScriptPicker(ModalScreen[Script | None]):
    """Choose a configured script; typing filters by name."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("down", "move(1)", show=False, priority=True),
        Binding("up", "move(-1)", show=False, priority=True),
    ]
    DEFAULT_CSS = """
    ScriptPicker {
        align: center middle;
    }
    ScriptPicker #script-picker {
        width: 60;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 0 1;
    }
    ScriptPicker OptionList {
        height: auto;
        max-height: 20;
        border: none;
    }
    """

    def __init__(self, scripts: list[Script]) -> None:
        super().__init__()
        self.scripts = scripts
        self._shown = scripts

    def compose(self) -> ComposeResult:
        with Vertical(id="script-picker"):
            yield Input(placeholder="Filter scripts", id="script-filter")
            yield OptionList(*self._options(), id="script-options")

    def on_mount(self) -> None:
        self.query_one("#script-picker").border_title = "Run script"
        self.query_one(OptionList).highlighted = 0

    def _options(self) -> list[Option]:
        return [Option(script.name) for script in self._shown]

    def on_input_changed(self, event: Input.Changed) -> None:
        text = event.value.casefold()
        self._shown = [script for script in self.scripts if text in script.name.casefold()]
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(self._options())
        options.highlighted = 0 if self._shown else None

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        index = self.query_one(OptionList).highlighted
        if index is not None:
            self.dismiss(self._shown[index])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self._shown[event.option_index])

    def action_move(self, delta: int) -> None:
        options = self.query_one(OptionList)
        if self._shown:
            current = options.highlighted or 0
            options.highlighted = max(0, min(current + delta, len(self._shown) - 1))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ScriptRunning(ModalScreen[None]):
    """Shown while a script runs; Escape stops it."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    ScriptRunning {
        align: center middle;
    }
    ScriptRunning Static {
        width: auto;
        max-width: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self.script_name = name
        self.worker: Worker | None = None

    def compose(self) -> ComposeResult:
        yield Static(
            f"Running {self.script_name}...\nPress Escape to cancel.",
            markup=False,
        )

    def action_cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
        self.dismiss()


SHORTCUTS = [
    ("Up / Down, k / j", "Select the previous / next record or line"),
    ("Page Up / Page Down", "Move by a page"),
    ("Home / End, gg / G", "Move to the first / last row"),
    ("Tab / Shift+Tab", "Switch panels"),
    ("Ctrl+B, z", "Collapse / expand the record list"),
    ("Left / Right, h / l", "Scroll horizontally where available"),
    ("0 / $, Ctrl+A / Ctrl+E", "Reveal the start / end of the current line"),
    ("[ / ]", "Navigate object/array block starts in the JSON preview"),
    ("%", "Jump between matching object/array delimiters in the JSON preview"),
    ("Enter", "Drill down: from the record list into the JSON preview, then into a string"),
    ("> / <", "Decode a JSON-encoded string or open encoded data natively / undo"),
    ("|", "Run a custom script on the selected value or record"),
    ("Ctrl+F, /, ?", "Open search"),
    ("F3, n / Shift+F3, N", "Find next / previous match"),
    ("Alt+R / Alt+C", "Toggle Regex / Match case while search is open"),
    (":number then Enter", "Jump to a 1-based line in the originating panel"),
    (":q then Enter", "Close the string inspector, or quit from a main panel"),
    ("Ctrl+C", "Copy selected text"),
    ("F1", "Show this help"),
    ("Escape", "Back out one level: close search, the inspector, then the JSON preview"),
]


class HelpModal(ModalScreen[None]):
    """List the keyboard shortcuts."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape,q", "dismiss", "Close"),
    ]
    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
    }
    HelpModal #help {
        width: 110;
        max-width: 90%;
        height: auto;
        max-height: 90%;
        border: thick $accent;
        border-subtitle-color: $text-muted;
        background: $surface;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold", no_wrap=True)
        table.add_column()
        for keys, effect in SHORTCUTS:
            table.add_row(keys, effect)
        with VerticalScroll(id="help"):
            yield Static(table)

    def on_mount(self) -> None:
        container = self.query_one("#help")
        container.border_title = "Keyboard shortcuts"
        container.border_subtitle = "Escape to close"
        container.focus()


class NativePreviewConfirmationRequested(Message):
    def __init__(self, info: PreviewInfo, allow: Callable[[], None]) -> None:
        super().__init__()
        self.info = info
        self.allow = allow


class JlvApp(App):
    """JSONL Log Viewer."""

    TITLE = "jlv"
    BINDINGS = [
        Binding("tab", "focus_next", "Switch Panel"),
        Binding("shift+tab", "focus_previous", "Switch Panel"),
        Binding("ctrl+f", "find", "Find", priority=True),
        Binding("f3", "find_next", "Find next", priority=True),
        Binding("shift+f3", "find_previous", "Find previous", priority=True),
        Binding("f15", "find_previous", "Find previous", priority=True, show=False),
        Binding("alt+r", "toggle_regex", "Regex", priority=True, show=False),
        Binding("alt+c", "toggle_case", "Match case", priority=True, show=False),
        Binding("escape", "close_search", "Close search", priority=True),
        Binding("enter", "inspect_search_line", show=False, priority=True),
        Binding("f1", "help", "Help", priority=True),
        Binding("ctrl+b", "toggle_records", "Collapse records"),
    ]
    DEFAULT_CSS = """
    Horizontal {
        height: 1fr;
    }
    #panels {
        layers: default hint;
    }
    /* Its own layer, so docking it doesn't resize the panels underneath. */
    #hint-rack {
        layer: hint;
        dock: bottom;
        width: 1fr;
        height: auto;
        align: right bottom;
        display: none;
    }
    #decode-hint {
        width: auto;
        margin: 0 2 1 0;
        padding: 0 1;
        /* Match Textual's information toasts, such as the decode notification. */
        background: $panel-lighten-1;
        border-left: outer $success;
    }
    #lines-list {
        width: 2fr;
    }
    #detail-list {
        width: 3fr;
        border-left: solid $accent;
    }
    /* The record list always shows its selection, dimmed when unfocused. The
       JSON preview only shows its cursor while it is being navigated. */
    #lines-list > .rowview--highlight {
        background: $accent 35%;
        text-style: none;
    }
    #detail-list > .rowview--highlight {
        background: transparent;
        text-style: none;
    }
    #lines-list:focus > .rowview--highlight, #detail-list:focus > .rowview--highlight {
        background: $accent;
        text-style: bold;
    }
    #index-status {
        height: auto;
        max-height: 3;
        color: $text-muted;
    }
    """

    def __init__(self, source: JsonlFile, scripts_path: Path | None = None) -> None:
        super().__init__()
        self.source = source
        self.scripts_path = scripts_path or config_path()
        self._native_preview = NativePreview()
        self._documents: OrderedDict[int, PrettyDocument] = OrderedDict()
        self._document: PrettyDocument | None = None
        # Documents and rows replaced by decoding JSON strings in the current record.
        self._decoded: list[tuple[PrettyDocument, int]] = []
        self._detail_idx: int | None = None
        self._pretty_lines: list[tuple[str, str | None]] = []
        self._find_focus: RowView | None = None
        self._input_target: RowView | StringView | None = None
        self._pending_g: RowView | StringView | None = None
        self._index_timer: Timer | None = None
        self._records_collapsed = False
        # Once toggled, the record list keeps its size even if it turns out to hold one record.
        self._records_toggled = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="panels"):
            yield RowView(id="lines-list")
            yield RowView(id="detail-list")
            with Container(id="hint-rack"):
                yield Static("Press > to decode", id="decode-hint", markup=False)
        status = Static("", id="index-status", markup=False)
        status.display = not self.source.complete
        yield status
        yield SearchBar(
            lambda: len(self.source),
            self._search_text,
            self._search_origin,
            "JSONL records",
            unavailable=self._index_unavailable,
        )

    def _index_unavailable(self) -> str | None:
        if self.source.error is not None:
            return f"Indexing stopped: {self.source.error}"
        if not self.source.complete:
            return (
                f"Still indexing ({len(self.source):,} records). Try again when indexing completes."
            )
        return None

    def _search_text(self, index: int) -> str:
        return json.dumps(self.source.value(index), indent=2, ensure_ascii=False)

    def _search_origin(self) -> tuple[int, int]:
        row = self.query_one("#detail-list", RowView).index
        lines = self._pretty_lines
        if self._decoded:
            # Search the original record from the outermost decoded string.
            document, row = self._decoded[0]
            lines = document.lines
        offset = sum(len(line) + 1 for line, _ in lines[:row])
        return self.query_one("#lines-list", RowView).index, offset

    def action_find(self) -> None:
        self._remember_input_target()
        self.screen.query_one(SearchBar).open(vim=False)

    def _remember_input_target(self) -> None:
        if isinstance(self.focused, (RowView, StringView)):
            self._input_target = self.focused
            if isinstance(self.focused, RowView):
                self._find_focus = self.focused

    def _return_to_panel(self) -> None:
        target = self._input_target
        if target is not None and target.is_mounted and target.screen is self.screen:
            target.focus()
        elif isinstance(self.screen, StringModal):
            self.screen.query_one(StringView).focus()
        else:
            (self._find_focus or self.query_one("#detail-list", RowView)).focus()

    def on_search_bar_submitted(self, event: SearchBar.Submitted) -> None:
        self._return_to_panel()
        event.stop()

    def on_search_bar_command(self, event: SearchBar.Command) -> None:
        event.stop()
        command = event.text.strip()
        if command == ":q":
            if isinstance(self.screen, StringModal):
                event.bar.cancel()
                self.pop_screen()
            else:
                self.exit()
            return
        target = self._input_target
        if target is None or not target.is_mounted or target.screen is not self.screen:
            event.bar.status("Focus a viewing panel before opening a command.")
            return
        if (
            target.id == "lines-list"
            and self.source.complete
            and target.row_count != len(self.source)
        ):
            self._refresh_index()
        number = command[1:] if command.startswith(":") else ""
        if not number.isascii() or not number.isdecimal():
            event.bar.status("Unknown command. Use :number or :q.")
            return
        # Avoid conversion errors for arbitrarily long pasted numbers.
        if len(number) > 12 or not 1 <= int(number) <= target.row_count:
            if (
                target.id == "lines-list"
                and (len(number) > 12 or int(number) > target.row_count)
                and (reason := self._index_unavailable())
            ):
                event.bar.status(reason)
                return
            event.bar.status(f"Line must be between 1 and {target.row_count}.")
            return
        target.go_to_line(int(number) - 1)
        event.bar.close()
        self._return_to_panel()

    def on_descendant_blur(self, event: events.DescendantBlur) -> None:
        self._pending_g = None
        self._update_hint()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        self._update_hint()

    def on_string_modal_closed(self, event: StringModal.Closed) -> None:
        self._update_hint()

    def _update_hint(self) -> None:
        """Show the decode hint only while the JSON preview's selected string can be decoded."""
        if not self.screen_stack:
            return
        base = self.screen_stack[0]
        right = base.query_one("#detail-list", RowView)
        native = self._document is not None and self._native_preview.available(
            self._document.strings.get(right.index, "")
        )
        base.query_one("#decode-hint", Static).update(
            "Press > to open natively" if native else "Press > to decode"
        )
        base.query_one("#hint-rack").display = (
            self.focused is right
            and self._document is not None
            and (native or self._document.expandable(right.index))
        )

    def on_panel_key(self, event: PanelKey) -> None:
        panel = event.panel
        if panel is not self.focused or not isinstance(panel, (RowView, StringView)):
            return
        key = event.character
        previous_g, self._pending_g = self._pending_g, None
        if key in {"j", "k", "G", "0", "$", "[", "]", "%", ">", "<"} or (
            key == "g" and previous_g is panel
        ):
            bar = self.screen.query_one(SearchBar)
            if bar.searching:
                bar.cancel()
                bar.status("Search cancelled")
        if key == "g":
            if previous_g is panel:
                panel.go_to_line(0)
            else:
                self._pending_g = panel
        elif key == "G":
            if isinstance(panel, RowView) and not panel.end_available:
                panel.action_cursor_end()
            else:
                panel.go_to_line(panel.row_count - 1)
        elif key in {"j", "k"}:
            delta = 1 if key == "j" else -1
            if isinstance(panel, RowView):
                panel.go_to_line(panel.index + delta)
            else:
                panel.move_line(delta)
        elif key in {"h", "l"}:
            panel.scroll_relative(x=-3 if key == "h" else 3, animate=False)
        elif key in {"0", "$"}:
            panel.line_edge(key == "$")
        elif key == "z" and isinstance(panel, RowView):
            self.action_toggle_records()
        elif key in {"/", "?"}:
            self._remember_input_target()
            self.screen.query_one(SearchBar).open(vim=True)
        elif key == ":":
            self._remember_input_target()
            scope = (
                "String line"
                if isinstance(panel, StringView)
                else "JSONL record"
                if panel.id == "lines-list"
                else "Formatted JSON line"
            )
            self.screen.query_one(SearchBar).open_command(scope)
        elif key in {"n", "N"}:
            self._remember_input_target()
            self.screen.query_one(SearchBar).find(backwards=key == "N")
        elif key == "|":
            self._pick_script(panel)
        elif key in {">", "<"}:
            value = ""
            if isinstance(panel, StringView) and isinstance(self.screen, StringModal):
                if self.screen._data is NO_DATA:
                    value = self.screen.value
            elif panel.id == "detail-list" and self._document is not None:
                value = self._document.strings.get(panel.index, "")
            if key == ">" and self._native_preview.available(value):
                self._native_preview.open(value, self._native_failure, self._native_confirmation)
                return
            if isinstance(panel, StringView) and isinstance(self.screen, StringModal):
                changed = self.screen.decode() if key == ">" else self.screen.undo_decode()
            elif panel.id == "detail-list":
                changed = self._decode_string(panel) if key == ">" else self._undo_decode(panel)
            else:
                changed = False
            if not changed:
                self.bell()
        elif (
            key in {"[", "]", "%"}
            and isinstance(panel, RowView)
            and panel.id == "detail-list"
            and self._document is not None
        ):
            if key == "%":
                target = self._document.matching_delimiter(panel.index)
                if target is not None:
                    panel.go_to_line(target)
                    panel.line_edge(True)
            else:
                panel.go_to_line(self._document.block_start(panel.index, forward=key == "]"))
                if any(start == panel.index for start, _ in self._document.blocks):
                    panel.line_edge(True)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "help":
            # Script completion dismisses the top screen, so keep its dialog on top.
            return not isinstance(self.screen, ScriptRunning)
        if action == "toggle_records":
            return self.screen is self.screen_stack[0]
        if action == "inspect_search_line":
            return (
                isinstance(self.focused, RowView)
                and bool(self.screen.query(SearchBar))
                and self.screen.query_one(SearchBar).display
            )
        if action in {
            "find",
            "find_next",
            "find_previous",
            "close_search",
            "toggle_regex",
            "toggle_case",
        }:
            return bool(self.screen.query(SearchBar))
        return super().check_action(action, parameters)

    def action_toggle_records(self) -> None:
        self._records_toggled = True
        self._records_collapsed = not self._records_collapsed
        self._size_records()

    def _collapse_single_record(self) -> None:
        """A file with one record, such as a formatted JSON file, needs no record list."""
        if self.source.complete and len(self.source) == 1 and not self._records_toggled:
            self._records_collapsed = True
            self._size_records()

    def _record_preview(self, index: int) -> str:
        if self._records_collapsed:
            return f"{index + 1:>5}"
        return self.source.preview(index)

    def _record_width(self) -> int:
        if self._records_collapsed:
            return max(5, len(str(len(self.source))))
        return self.source.preview_width

    def _size_records(self) -> None:
        left = self.screen_stack[0].query_one("#lines-list", RowView)
        left.extend_rows(len(self.source), self._record_width())
        if self._records_collapsed:
            # Keep just the right-aligned record numbers, plus the scrollbar.
            left.styles.width = self._record_width() + 1
            left.scroll_to(x=0, animate=False)
        else:
            left.styles.width = None

    def action_help(self) -> None:
        if isinstance(self.screen, HelpModal):
            self.screen.dismiss()
        else:
            self.push_screen(HelpModal())

    def action_find_next(self) -> None:
        self._remember_input_target()
        self.screen.query_one(SearchBar).find()

    def action_find_previous(self) -> None:
        self._remember_input_target()
        self.screen.query_one(SearchBar).find(backwards=True)

    def action_toggle_regex(self) -> None:
        bar = self.screen.query_one(SearchBar)
        if bar.display and not bar.command_mode:
            bar.toggle_regex()

    def action_toggle_case(self) -> None:
        bar = self.screen.query_one(SearchBar)
        if bar.display and not bar.command_mode:
            bar.toggle_case()

    def action_close_search(self) -> None:
        self._pending_g = None
        if isinstance(self.screen, StringModal):
            self.screen.action_dismiss()
        else:
            bar = self.query_one(SearchBar)
            if bar.display:
                bar.close()
                (self._find_focus or self.query_one("#detail-list", RowView)).focus()
            elif self.focused is self.query_one("#detail-list", RowView):
                # Back out from the record's details to the record list.
                self.query_one("#lines-list", RowView).focus()

    def action_inspect_search_line(self) -> None:
        self._pending_g = None
        self.screen.query_one(SearchBar).close()
        right = self.query_one("#detail-list", RowView)
        right.focus()
        self._remember_input_target()
        self._inspect_string(right.index)

    def on_search_bar_found(self, event: SearchBar.Found) -> None:
        if not event.bar.accepts(event.hit):
            return
        hit = event.hit
        if self.source.complete:
            self._refresh_index()
        if self._decoded:
            # Matches are located in the original record's formatted text.
            self._detail_idx = None
        self.query_one("#lines-list", RowView).index = hit.record
        self._update_detail(hit.record)
        start_row, start_column = text_location(hit.text, hit.start)
        end_row, end_column = text_location(hit.text, hit.end)
        self.query_one("#detail-list", RowView).show_match(
            Offset(start_column, start_row), Offset(end_column, end_row)
        )
        event.bar.applied_origin = self._search_origin()
        event.bar.status(f"{'Wrapped - ' if hit.wrapped else ''}Record {hit.record + 1}")
        # The document may have been restored without the cursor row changing.
        self._update_hint()

    def on_search_bar_cleared(self, event: SearchBar.Cleared) -> None:
        view = self.query_one("#detail-list", RowView)
        view.find_selection = None
        view.refresh()

    def on_mount(self) -> None:
        left = self.query_one("#lines-list", RowView)
        left.set_rows(len(self.source), self._record_width(), self._record_preview)
        left.end_available = self.source.complete
        left.focus()
        self._update_detail(0)
        self._collapse_single_record()
        if not self.source.complete:
            self._index_timer = self.set_interval(0.1, self._refresh_index)
            self._refresh_index()
            self.call_after_refresh(self.source.start_indexing)

    def _refresh_index(self) -> None:
        screen = self.screen_stack[0]
        left = screen.query_one("#lines-list", RowView)
        complete, error = self.source.complete, self.source.error
        count, width = len(self.source), self._record_width()
        if left.row_count != count or left.virtual_size.width != width:
            left.extend_rows(count, width)
            if self._records_collapsed:
                self._size_records()
        left.end_available = complete
        status = screen.query_one("#index-status", Static)
        if error is not None:
            status.update(f"Indexing stopped: {error}")
            status.styles.color = self.get_css_variables()["error"]
        elif complete:
            status.display = False
            self._collapse_single_record()
        else:
            percent = self.source.scanned_bytes * 100 / max(1, self.source.file_size)
            status.update(f"Indexing: {count:,} records ({percent:.0f}%)")
        if (complete or error is not None) and self._index_timer is not None:
            self._index_timer.stop()

    def on_row_view_end_requested(self, event: RowView.EndRequested) -> None:
        if reason := self._index_unavailable():
            self.notify(reason, title="End of file unavailable", severity="warning")
        else:
            left = self.screen_stack[0].query_one("#lines-list", RowView)
            left.extend_rows(len(self.source), self._record_width())
            left.end_available = True
            left.action_cursor_end()
        event.stop()

    def on_row_view_highlighted(self, event: RowView.Highlighted) -> None:
        bar = self.query_one(SearchBar)
        if bar.searching:
            bar.cancel()
            bar.status("Search cancelled")
        if event.view.id == "lines-list":
            # Ignore queued highlights superseded by newer input in the same frame.
            if event.index == event.view.index:
                self._update_detail(event.index)
        else:
            self._update_hint()

    def on_row_view_selected(self, event: RowView.Selected) -> None:
        if event.view.id == "lines-list":
            if not event.click:
                # Drill down from the record list into the record's details.
                self.query_one("#detail-list", RowView).focus()
            return
        if self._inspect_string(event.index):
            return
        self.bell()

    def _inspect_string(self, index: int) -> bool:
        if self._document is not None:
            value = self._document.strings.get(index)
            if value is not None:
                origin = self._document.value, self._document.paths[index]
                self.push_screen(StringModal(value, origin=origin))
                self.query_one(SearchBar).cancel()
                return True
        return False

    def _selection(self, panel: RowView | StringView) -> Selection | None:
        """Snapshot the displayed record and the selected value, or the inspector's contents."""
        if self._document is None or self._detail_idx is None:
            return None
        line = self._detail_idx + 1
        if isinstance(panel, StringView):
            modal = self.screen
            if not isinstance(modal, StringModal):
                return None
            if modal.origin is not None and not modal._decoded:
                record, path = modal.origin
                return Selection(record, path, modal.value, self.source.path, line)
            # Decoded text and script results stand alone: they are the whole record.
            value = modal.value if modal._data is NO_DATA else modal._data
            return Selection(value, (), value, self.source.path, line)
        if panel.id == "lines-list":
            selected = (), self._document.value
        else:
            selected = self._document.selected(panel.index)
            if selected is None:
                return None
        path, value = selected
        return Selection(self._document.value, path, value, self.source.path, line)

    def _pick_script(self, panel: RowView | StringView) -> None:
        selection = self._selection(panel)
        if selection is None:
            self.bell()
            return
        scripts, errors = load_scripts(self.scripts_path)
        for error in errors:
            self.notify(error, title="Script configuration error", severity="error")
        available = [script for script in scripts if script.applies(selection)]
        if not available:
            if not scripts and not errors:
                self.notify(
                    f"Add scripts to {self.scripts_path}", title="No scripts configured", timeout=5
                )
            elif scripts:
                self.notify("No configured script applies to this selection.", title="Scripts")
            return
        self.query_one(SearchBar).cancel()

        def chosen(script: Script | None) -> None:
            if script is not None:
                self._run_script(script, selection)

        self.push_screen(ScriptPicker(available), chosen)

    def _run_script(self, script: Script, selection: Selection) -> None:
        inspector = isinstance(self.screen, StringModal)
        running = ScriptRunning(script.name)
        self.push_screen(running)

        async def work() -> None:
            try:
                result = await execute(script, selection)
            except Exception as error:  # noqa: BLE001 - never leave the running dialog open
                result = ScriptResult(
                    f"{script.name}: failed", "text/plain", f"{type(error).__name__}: {error}", True
                )
            if running.is_attached:
                running.dismiss()
                self._show_script_result(result, inspector)

        running.worker = self.run_worker(work(), group="scripts", exit_on_error=False)

    def _show_script_result(self, result: ScriptResult, inspector: bool) -> None:
        """Show text in a new inspector; JSON replaces the record, or opens over an inspector."""
        if result.stderr.strip() and not result.error:
            stderr = result.stderr.strip()
            self.notify(
                stderr if len(stderr) <= 500 else stderr[:500] + "...",
                title=f"{result.title}: stderr",
                severity="warning",
                markup=False,
            )
        if result.mime != "application/json" or result.error:
            self.push_screen(StringModal(str(result.content), title=result.title))
            return
        if inspector:
            content = result.content
            if isinstance(content, str):
                # A JSON string is shown, and sent onward, as the string itself.
                self.push_screen(StringModal(content, title=result.title))
            else:
                text = json.dumps(content, indent=2, ensure_ascii=False)
                self.push_screen(StringModal(text, title=result.title, data=content))
            return
        right = self.query_one("#detail-list", RowView)
        if self._document is not None:
            # A JSON result replaces the displayed record, like a decode; < restores it.
            self._decoded.append((self._document, right.index))
        self._show_document(PrettyDocument.from_value(result.content), 0)
        right.scroll_to(x=0, y=0, animate=False, force=True)
        right.focus()
        self.notify(
            "Press < to return; changing records resets.",
            title=f"Showing {result.title}",
            timeout=3,
        )

    def _native_failure(self) -> None:
        with contextlib.suppress(RuntimeError):  # The app has already exited.
            self.call_from_thread(self.bell)

    def _native_confirmation(self, info: PreviewInfo, allow: Callable[[], None]) -> None:
        self.post_message(NativePreviewConfirmationRequested(info, allow))

    def on_native_preview_confirmation_requested(
        self, event: NativePreviewConfirmationRequested
    ) -> None:
        if isinstance(self.screen, NativePreviewWarning):
            return

        def confirmed(allowed: bool | None) -> None:
            if allowed:
                event.allow()

        self.push_screen(NativePreviewWarning(event.info), confirmed)

    def on_unmount(self) -> None:
        self._native_preview.cleanup()

    def _decode_string(self, view: RowView) -> bool:
        if self._document is None:
            return False
        document = self._document.decode_string(view.index)
        if document is None:
            return False
        self._decoded.append((self._document, view.index))
        self._show_document(document, view.index)
        layers = len(self._decoded)
        self.notify(
            f"Decoded {layers} layer{'s' if layers > 1 else ''}. "
            "Press < to undo; changing records resets.",
            title="JSON string decoded",
            timeout=3,
        )
        return True

    def _undo_decode(self, view: RowView) -> bool:
        if not self._decoded:
            return False
        document, row = self._decoded.pop()
        self._show_document(document, row)
        return True

    def _show_document(self, document: PrettyDocument, row: int) -> None:
        """Replace the current record's view, keeping the cursor and scroll position."""
        view = self.query_one("#detail-list", RowView)
        x, y = view.scroll_x, view.scroll_y
        self._document = document
        self._pretty_lines = document.lines
        view.set_rows(len(document.lines), document.width, lambda row: document.lines[row][0])
        view.index = row
        view.scroll_to(x=x, y=y, animate=False, force=True)
        self._update_hint()

    def _update_detail(self, index: int) -> None:
        if index == self._detail_idx:
            return
        self._decoded.clear()
        if index in self._documents:
            document = self._documents[index]
            self._documents.move_to_end(index)
        else:
            document = PrettyDocument.from_value(self.source.value(index))
            self._documents[index] = document
            if len(self._documents) > 8:
                self._documents.popitem(last=False)
        self._document = document
        self._detail_idx = index
        self._pretty_lines = document.lines
        self.query_one("#detail-list", RowView).set_rows(
            len(document.lines), document.width, lambda row: document.lines[row][0]
        )


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: jlv <file.jsonl>", file=sys.stderr)
        sys.exit(1)
    try:
        source = JsonlFile(sys.argv[1], eager=False)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    with source:
        JlvApp(source).run()
    if source.error is not None:
        print(f"Error: {source.error}", file=sys.stderr)
        sys.exit(1)
