from __future__ import annotations

import json
import sys
from collections import OrderedDict
from dataclasses import dataclass

from rich.cells import cell_len
from textual.app import App, ComposeResult
from textual import events
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets import TextArea

from jlv.rows import RowView
from jlv.source import JsonlFile


@dataclass
class PrettyDocument:
    lines: list[tuple[str, str | None]]
    strings: dict[int, str]
    width: int

    @classmethod
    def from_value(cls, value: object) -> PrettyDocument:
        lines: list[tuple[str, str | None]] = []
        strings: dict[int, str] = {}

        def append(value: object, depth: int, prefix: str = "", suffix: str = "") -> None:
            indent = "  " * depth
            if isinstance(value, (dict, list)) and value:
                opening, closing = ("{", "}") if isinstance(value, dict) else ("[", "]")
                lines.append((indent + prefix + opening, None))
                entries = value.items() if isinstance(value, dict) else enumerate(value)
                for i, (key, child) in enumerate(entries):
                    child_prefix = json.dumps(key, ensure_ascii=False) + ": " if isinstance(value, dict) else ""
                    append(child, depth + 1, child_prefix, "," if i < len(value) - 1 else "")
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
                lines.append((indent + prefix + json.dumps(value, ensure_ascii=False) + suffix, kind))

        append(value, 0)
        return cls(
            lines, strings,
            max(
                len(line) if line.isascii() and "\x7f" not in line else cell_len(line)
                for line, _ in lines
            ),
        )


class StringView(TextArea):
    def __init__(self, value: str) -> None:
        self._pending_value: str | None = value
        super().__init__(
            read_only=True,
            show_cursor=False,
            highlight_cursor_line=False,
            soft_wrap=True,
            id="modal-container",
        )

    def on_resize(self, event: events.Resize) -> None:
        if self._pending_value is not None and self.content_size.width:
            # Wrap once at the actual viewport width, not at each startup size.
            value, self._pending_value = self._pending_value, None
            self.load_text(value)
            event.prevent_default()


class StringModal(ModalScreen[None]):
    """A read-only, wrapped document with viewport-based rendering."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q", "app.quit", "Quit"),
    ]
    DEFAULT_CSS = """
    StringModal {
        align: center middle;
    }
    StringModal #modal-container {
        width: 90%;
        height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
        overflow-y: scroll;
        scrollbar-size: 1 1;
    }
    """

    def __init__(self, value: str) -> None:
        super().__init__()
        self.value = value

    def compose(self) -> ComposeResult:
        yield StringView(self.value)

    def action_dismiss(self) -> None:
        self.app.pop_screen()


class JlvApp(App):
    """JSONL Log Viewer."""

    TITLE = "jlv"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("tab", "focus_next", "Switch Panel"),
        Binding("shift+tab", "focus_previous", "Switch Panel"),
    ]
    DEFAULT_CSS = """
    Horizontal {
        height: 1fr;
    }
    #lines-list {
        width: 2fr;
    }
    #detail-list {
        width: 3fr;
        border-left: solid $accent;
    }
    """

    def __init__(self, source: JsonlFile) -> None:
        super().__init__()
        self.source = source
        self._documents: OrderedDict[int, PrettyDocument] = OrderedDict()
        self._document: PrettyDocument | None = None
        self._detail_idx: int | None = None
        self._pretty_lines: list[tuple[str, str | None]] = []

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield RowView(id="lines-list")
            yield RowView(id="detail-list")

    def on_mount(self) -> None:
        left = self.query_one("#lines-list", RowView)
        left.set_rows(len(self.source), self.source.preview_width, self.source.preview)
        left.focus()
        self._update_detail(0)

    def on_row_view_highlighted(self, event: RowView.Highlighted) -> None:
        if event.view.id == "lines-list":
            # Ignore queued highlights superseded by newer input in the same frame.
            if event.index == event.view.index:
                self._update_detail(event.index)

    def on_row_view_selected(self, event: RowView.Selected) -> None:
        if event.view.id == "detail-list" and self._document is not None:
            value = self._document.strings.get(event.index)
            if value is not None:
                self.push_screen(StringModal(value))
                return
        self.bell()

    def _update_detail(self, index: int) -> None:
        if index == self._detail_idx:
            return
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
        source = JsonlFile(sys.argv[1])
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    with source:
        JlvApp(source).run()
