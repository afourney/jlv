from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable

from rich.cells import cell_len
from rich.segment import Segment
from textual import events
from textual.binding import Binding
from textual.geometry import Offset, Region, Size
from textual.message import Message
from textual.reactive import reactive
from textual.scroll_view import ScrollView
from textual.selection import Selection
from textual.strip import Strip


class RowView(ScrollView, can_focus=True):
    """Fixed-height selectable rows, rendered only for the visible viewport."""

    BINDINGS = [
        Binding("up", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("pageup", "cursor_page_up", show=False),
        Binding("pagedown", "cursor_page_down", show=False),
        Binding("home", "cursor_home", show=False),
        Binding("end", "cursor_end", show=False),
        Binding("enter", "select", show=False),
    ]

    DEFAULT_CSS = """
    RowView {
        height: 1fr;
        scrollbar-size: 1 1;
    }
    RowView > .rowview--highlight {
        background: $boost;
    }
    RowView:focus > .rowview--highlight {
        background: $accent;
        color: $text;
    }
    """
    COMPONENT_CLASSES = {"rowview--highlight"}
    index: reactive[int] = reactive(0, init=False)

    class Highlighted(Message):
        def __init__(self, view: RowView, index: int) -> None:
            self.view = view
            self.index = index
            super().__init__()

    class Selected(Message):
        def __init__(self, view: RowView, index: int) -> None:
            self.view = view
            self.index = index
            super().__init__()

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id)
        self.row_count = 0
        self._line: Callable[[int], str] = lambda index: ""
        self._unicode_cache: OrderedDict[tuple[int, int, int], tuple[Strip, int]] = OrderedDict()

    def set_rows(self, count: int, width: int, line: Callable[[int], str]) -> None:
        if self.is_mounted and self.text_selection is not None:
            self.screen.selections = {
                widget: selection for widget, selection in self.screen.selections.items()
                if widget is not self
            }
        self.row_count = count
        self._line = line
        self._unicode_cache.clear()
        self.virtual_size = Size(width, count)
        self.index = 0
        self.scroll_to(x=0, y=0, animate=False, force=True)
        self.refresh()

    def validate_index(self, index: int) -> int:
        return max(0, min(index, self.row_count - 1))

    def watch_index(self, old: int, new: int) -> None:
        self.refresh_line(old)
        self.refresh_line(new)
        self.scroll_to_region(
            Region(0, new, 1, 1), animate=False, x_axis=False, force=True
        )
        self.post_message(self.Highlighted(self, new))

    def render_line(self, y: int) -> Strip:
        x, offset = self.scroll_offset
        row = y + offset
        width = self.size.width
        style = self.rich_style
        if row >= self.row_count:
            return Strip.blank(width, style)
        if row == self.index:
            style += self.get_component_rich_style("rowview--highlight")
        text = self._line(row)
        single_cell = text.isascii() and "\x7f" not in text
        # Avoid constructing/cropping a giant Rich segment for long ASCII strings.
        if single_cell:
            visible = text[x:x + width].ljust(width)
            strip = Strip([Segment(visible)], width)
            character_offset = x
        else:
            key = (row, x, width)
            if key not in self._unicode_cache:
                strip = Strip([Segment(text)], cell_len(text))
                _, remainder = Segment(text).split_cells(x)
                character_offset = len(text) - len(remainder.text)
                self._unicode_cache[key] = (
                    strip.crop_extend(x, x + width, None), character_offset
                )
                if len(self._unicode_cache) > 256:
                    self._unicode_cache.popitem(last=False)
            self._unicode_cache.move_to_end(key)
            strip, character_offset = self._unicode_cache[key]
        strip = strip.apply_style(style)
        if self.text_selection is not None:
            span = self.text_selection.get_span(row)
            if span is not None:
                start, end = span
                start = min(start, len(text)) if single_cell else cell_len(text[:start])
                end = len(text) if end == -1 else end
                end = min(end, len(text)) if single_cell else cell_len(text[:end])
                start, end = max(0, start - x), min(width, end - x)
                if start < end:
                    before, selected, after = strip.divide([start, end, width])
                    strip = Strip.join([
                        before,
                        selected.apply_style(self.screen.get_component_rich_style("screen--selection")),
                        after,
                    ])
        return strip.apply_offsets(character_offset, row)

    def get_selection(self, selection: Selection) -> tuple[str, str]:
        start = selection.start or Offset(0, 0)
        last = min(
            self.row_count - 1,
            selection.end.y if selection.end is not None else self.row_count - 1,
        )
        if last < start.y:
            return "", "\n"
        lines = [self._line(row) for row in range(start.y, last + 1)]
        end_x = selection.end.x if selection.end is not None else len(lines[-1])
        relative = Selection(Offset(start.x, 0), Offset(end_x, len(lines) - 1))
        return relative.extract("\n".join(lines)), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        self.refresh()

    def on_focus(self) -> None:
        self.refresh_line(self.index)

    def on_blur(self) -> None:
        self.refresh_line(self.index)

    def on_click(self, event: events.Click) -> None:
        if event.widget is not self:
            return
        offset = event.get_content_offset(self)
        if offset is None:
            return
        row = offset.y + self.scroll_offset.y
        if 0 <= row < self.row_count:
            self.focus()
            self.index = row
            self.post_message(self.Selected(self, row))
            event.stop()

    def action_cursor_up(self) -> None:
        self.index -= 1

    def action_cursor_down(self) -> None:
        self.index += 1

    def action_cursor_page_up(self) -> None:
        self.index -= max(1, self.scrollable_content_region.height)

    def action_cursor_page_down(self) -> None:
        self.index += max(1, self.scrollable_content_region.height)

    def action_cursor_home(self) -> None:
        self.index = 0

    def action_cursor_end(self) -> None:
        self.index = self.row_count - 1

    def action_select(self) -> None:
        if self.row_count:
            self.post_message(self.Selected(self, self.index))
