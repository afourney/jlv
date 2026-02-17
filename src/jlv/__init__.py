from __future__ import annotations

import json
import sys

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Static, ListView, ListItem, Header, Footer
from rich.text import Text


class JsonLineItem(ListItem):
    """A single line from the JSONL file shown in the left panel."""

    def __init__(self, line_text: str, line_number: int) -> None:
        super().__init__()
        self.line_text = line_text
        self.line_number = line_number

    def compose(self) -> ComposeResult:
        text = self.line_text.strip()
        if len(text) > 1000:
            text = text[:1000] + "..."
        display = f"{self.line_number:>5}  {text}"
        yield Static(display, markup=False)


class PrettyJsonLine(ListItem):
    """A single line of the pretty-printed JSON shown in the right panel."""

    def __init__(self, text_line: str, json_path_type: str | None) -> None:
        super().__init__()
        self.json_path_type = json_path_type
        self.text_line = text_line

    def compose(self) -> ComposeResult:
        yield Static(self.text_line, markup=False)


class StringModal(ModalScreen[None]):
    """Full-screen modal to display a decoded string value."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
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
        overflow-y: auto;
        overflow-x: auto;
    }
    """

    def __init__(self, value: str) -> None:
        super().__init__()
        self.value = value

    def compose(self) -> ComposeResult:
        yield Static(self.value, id="modal-container", markup=False)

    def action_dismiss(self) -> None:
        self.app.pop_screen()


def _classify_pretty_lines(obj: object) -> list[tuple[str, str | None]]:
    """Pretty-print a JSON object and tag each line with the type of the value
    that appears on that line (if it is a leaf), or None otherwise.

    Returns a list of (line_text, type_or_none) tuples.
    """
    pretty = json.dumps(obj, indent=2, ensure_ascii=False)
    lines = pretty.split("\n")
    result: list[tuple[str, str | None]] = []

    for line in lines:
        stripped = line.strip().rstrip(",")
        # Determine if this line contains a leaf value
        value_part: str | None = None
        if ":" in stripped:
            # key: value line
            _, _, rhs = stripped.partition(":")
            value_part = rhs.strip().rstrip(",")
        elif stripped and stripped not in ("{", "}", "[", "]", "{}", "[]"):
            # bare value in an array
            value_part = stripped

        path_type: str | None = None
        if value_part is not None:
            if value_part.startswith('"'):
                path_type = "string"
            elif value_part in ("true", "false"):
                path_type = "bool"
            elif value_part == "null":
                path_type = "null"
            else:
                try:
                    float(value_part)
                    path_type = "number"
                except ValueError:
                    pass

        result.append((line, path_type))

    return result


def _extract_string_value(line: str) -> str | None:
    """Given a pretty-printed JSON line, extract the raw string value if present."""
    stripped = line.strip().rstrip(",")
    value_part: str | None = None
    if ":" in stripped:
        _, _, rhs = stripped.partition(":")
        value_part = rhs.strip().rstrip(",")
    else:
        value_part = stripped

    if value_part and value_part.startswith('"') and value_part.endswith('"'):
        try:
            return json.loads(value_part)
        except json.JSONDecodeError:
            return None
    return None


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
    #left-panel {
        width: 2fr;
    }
    #right-panel {
        width: 3fr;
        border-left: solid $accent;
    }
    ListView {
        height: 1fr;
    }
    ListView {
        overflow-x: auto;
    }
    ListView > ListItem {
        height: 1;
        width: auto;
    }
    ListView > ListItem > Static {
        width: auto;
    }
    """

    def __init__(self, lines: list[str], parsed: list[object]) -> None:
        super().__init__()
        self.jsonl_lines = lines
        self.parsed_objects = parsed
        self._pretty_lines: list[tuple[str, str | None]] = []

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="left-panel"):
                items = [JsonLineItem(line, i + 1) for i, line in enumerate(self.jsonl_lines)]
                yield ListView(*items, id="lines-list")
            with Vertical(id="right-panel"):
                yield ListView(id="detail-list")

    def on_mount(self) -> None:
        lines_list = self.query_one("#lines-list", ListView)
        lines_list.focus()
        if self.parsed_objects:
            self._update_detail(0)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "lines-list" and event.item is not None:
            assert isinstance(event.item, JsonLineItem)
            idx = event.item.line_number - 1
            self._update_detail(idx)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "detail-list" and event.item is not None:
            assert isinstance(event.item, PrettyJsonLine)
            if event.item.json_path_type == "string":
                value = _extract_string_value(event.item.text_line)
                if value is not None:
                    self.push_screen(StringModal(value))
                    return
        # Not a string attribute — ring the bell
        self.bell()

    DETAIL_BATCH_SIZE = 100

    def _update_detail(self, idx: int) -> None:
        detail = self.query_one("#detail-list", ListView)
        detail.clear()
        obj = self.parsed_objects[idx]
        self._pretty_lines = _classify_pretty_lines(obj)
        self._detail_idx = idx
        self._detail_offset = 0
        self._load_detail_batch()

    def _load_detail_batch(self) -> None:
        detail = self.query_one("#detail-list", ListView)
        end = min(self._detail_offset + self.DETAIL_BATCH_SIZE, len(self._pretty_lines))
        batch = [
            PrettyJsonLine(text_line, path_type)
            for text_line, path_type in self._pretty_lines[self._detail_offset:end]
        ]
        if batch:
            detail.extend(batch)
        self._detail_offset = end
        if self._detail_offset < len(self._pretty_lines):
            self.set_timer(0.01, self._load_detail_batch)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: jlv <file.jsonl>", file=sys.stderr)
        sys.exit(1)

    filepath = sys.argv[1]

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
    except OSError as e:
        print(f"Error reading file: {e}", file=sys.stderr)
        sys.exit(1)

    # Strip empty trailing lines
    while raw_lines and raw_lines[-1].strip() == "":
        raw_lines.pop()

    if not raw_lines:
        print("Error: file is empty", file=sys.stderr)
        sys.exit(1)

    parsed: list[object] = []
    for i, line in enumerate(raw_lines, 1):
        stripped = line.strip()
        if not stripped:
            print(f"Error: line {i} is empty — not valid JSONL", file=sys.stderr)
            sys.exit(1)
        try:
            parsed.append(json.loads(stripped))
        except json.JSONDecodeError as e:
            print(f"Error: line {i} is not valid JSON: {e}", file=sys.stderr)
            sys.exit(1)

    app = JlvApp(raw_lines, parsed)
    app.run()
