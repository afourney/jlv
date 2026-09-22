from __future__ import annotations

import json
import sys
from collections import OrderedDict
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from rich.cells import cell_len
from rich.text import Text
from textual.app import App, ComposeResult
from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.document._document import Selection as TextSelection
from textual.geometry import Offset, Region
from textual.screen import ModalScreen
from textual.widgets import TextArea

from jlv.rows import PanelKey, RowView
from jlv.source import JsonlFile
from jlv.search import SearchBar, text_location


@dataclass
class PrettyDocument:
    lines: list[tuple[str, str | None]]
    strings: dict[int, str]
    width: int
    blocks: list[tuple[int, int]]

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
        blocks: list[tuple[int, int]] = []

        def append(value: object, depth: int, prefix: str = "", suffix: str = "") -> None:
            indent = "  " * depth
            block_index = len(blocks)
            block_start = len(lines)
            if isinstance(value, (dict, list)):
                blocks.append((block_start, block_start))
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
            if isinstance(value, (dict, list)):
                blocks[block_index] = (block_start, len(lines) - 1)

        append(value, 0)
        return cls(
            lines, strings,
            max(
                len(line) if line.isascii() and "\x7f" not in line else cell_len(line)
                for line, _ in lines
            ),
            blocks,
        )


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
        )

    def on_resize(self, event: events.Resize) -> None:
        if self._pending_value is not None and self.content_size.width:
            # Wrap once at the actual viewport width, not at each startup size.
            value, self._pending_value = self._pending_value, None
            self.load_text(value)
            event.prevent_default()

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


class StringModal(ModalScreen[None]):
    """A read-only, wrapped document with viewport-based rendering."""

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

    def __init__(self, value: str) -> None:
        super().__init__()
        self.value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="inspector"):
            yield StringView(self.value)
            yield SearchBar(1, self._search_text, self._search_origin, "this string")

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

    def action_dismiss(self) -> None:
        bar = self.query_one(SearchBar)
        if bar.display:
            bar.close()
            self.query_one(StringView).focus()
        else:
            self.app.pop_screen()


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
        self._find_focus: RowView | None = None
        self._input_target: RowView | StringView | None = None
        self._pending_g: RowView | StringView | None = None

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield RowView(id="lines-list")
            yield RowView(id="detail-list")
        yield SearchBar(len(self.source), self._search_text, self._search_origin, "JSONL records")

    def _search_text(self, index: int) -> str:
        return json.dumps(self.source.value(index), indent=2, ensure_ascii=False)

    def _search_origin(self) -> tuple[int, int]:
        row = self.query_one("#detail-list", RowView).index
        offset = sum(len(line) + 1 for line, _ in self._pretty_lines[:row])
        return self.query_one("#lines-list", RowView).index, offset

    def action_find(self) -> None:
        self._remember_input_target()
        self.screen.query_one(SearchBar).open()

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
        number = command[1:] if command.startswith(":") else ""
        if not number.isascii() or not number.isdecimal():
            event.bar.status("Unknown command. Use :number or :q.")
            return
        # Avoid conversion errors for arbitrarily long pasted numbers.
        if len(number) > 12 or not 1 <= int(number) <= target.row_count:
            event.bar.status(f"Line must be between 1 and {target.row_count}.")
            return
        target.go_to_line(int(number) - 1)
        event.bar.close()
        self._return_to_panel()

    def on_descendant_blur(self, event: events.DescendantBlur) -> None:
        self._pending_g = None

    def on_panel_key(self, event: PanelKey) -> None:
        panel = event.panel
        if panel is not self.focused or not isinstance(panel, (RowView, StringView)):
            return
        key = event.character
        previous_g, self._pending_g = self._pending_g, None
        if key in {"j", "k", "G", "0", "$", "[", "]", "%"} or key == "g" and previous_g is panel:
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
        elif key in {"/", "?"}:
            self.action_find()
        elif key == ":":
            self._remember_input_target()
            scope = (
                "String line" if isinstance(panel, StringView)
                else "JSONL record" if panel.id == "lines-list"
                else "Formatted JSON line"
            )
            self.screen.query_one(SearchBar).open_command(scope)
        elif key in {"n", "N"}:
            self._remember_input_target()
            self.screen.query_one(SearchBar).find(backwards=key == "N")
        elif (
            key in {"[", "]", "%"} and isinstance(panel, RowView)
            and panel.id == "detail-list" and self._document is not None
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
        if action == "inspect_search_line":
            return (
                isinstance(self.focused, RowView)
                and bool(self.screen.query(SearchBar))
                and self.screen.query_one(SearchBar).display
            )
        if action in {
            "find", "find_next", "find_previous", "close_search", "toggle_regex", "toggle_case",
        }:
            return bool(self.screen.query(SearchBar))
        return super().check_action(action, parameters)

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
        self.query_one("#lines-list", RowView).index = hit.record
        self._update_detail(hit.record)
        start_row, start_column = text_location(hit.text, hit.start)
        end_row, end_column = text_location(hit.text, hit.end)
        self.query_one("#detail-list", RowView).show_match(
            Offset(start_column, start_row), Offset(end_column, end_row)
        )
        event.bar.applied_origin = self._search_origin()
        event.bar.status(f"{'Wrapped - ' if hit.wrapped else ''}Record {hit.record + 1}")

    def on_search_bar_cleared(self, event: SearchBar.Cleared) -> None:
        view = self.query_one("#detail-list", RowView)
        view.find_selection = None
        view.refresh()

    def on_mount(self) -> None:
        left = self.query_one("#lines-list", RowView)
        left.set_rows(len(self.source), self.source.preview_width, self.source.preview)
        left.focus()
        self._update_detail(0)

    def on_row_view_highlighted(self, event: RowView.Highlighted) -> None:
        bar = self.query_one(SearchBar)
        if bar.searching:
            bar.cancel()
            bar.status("Search cancelled")
        if event.view.id == "lines-list":
            # Ignore queued highlights superseded by newer input in the same frame.
            if event.index == event.view.index:
                self._update_detail(event.index)

    def on_row_view_selected(self, event: RowView.Selected) -> None:
        if event.view.id == "detail-list" and self._inspect_string(event.index):
            return
        self.bell()

    def _inspect_string(self, index: int) -> bool:
        if self._document is not None:
            value = self._document.strings.get(index)
            if value is not None:
                self.push_screen(StringModal(value))
                self.query_one(SearchBar).cancel()
                return True
        return False

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
