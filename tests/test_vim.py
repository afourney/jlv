from __future__ import annotations

import asyncio
import itertools
import json
import tempfile
import unittest
from pathlib import Path

from rich.cells import cell_len
from textual.widgets import Input, Static

from jlv import JlvApp, PrettyDocument, StringModal, StringView
from jlv.rows import RowView
from jlv.search import SearchBar
from jlv.source import JsonlFile


class BlockTests(unittest.TestCase):
    def test_nested_and_empty_blocks_are_structural(self):
        value = {"a": [{"b": 1}, {}, []], "text": "{ not a block }", "c": {"d": 2}}
        document = PrettyDocument.from_value(value)
        self.assertEqual(
            "\n".join(line for line, _ in document.lines),
            json.dumps(value, indent=2, ensure_ascii=False),
        )
        starts = [start for start, _ in document.blocks]
        self.assertEqual(starts, [0, 1, 2, 5, 6, 9])
        self.assertEqual(document.block_start(3, forward=False), 2)
        self.assertEqual(document.block_start(4, forward=False), 2)
        self.assertEqual(document.block_start(2, forward=False), 1)
        self.assertEqual(document.block_start(8, forward=False), 6)
        self.assertEqual(document.block_start(5, forward=False), 2)
        self.assertEqual(document.block_start(1, forward=True), 2)
        self.assertEqual(document.block_start(4, forward=True), 5)
        self.assertEqual(document.block_start(9, forward=True), 9)
        self.assertEqual(document.block_start(0, forward=False), 0)
        for start, end in document.blocks:
            self.assertEqual(document.matching_delimiter(start), end)
            self.assertEqual(document.matching_delimiter(end), start)
        for row in (3, 8, 10):
            self.assertIsNone(document.matching_delimiter(row))
        for previous, following in itertools.pairwise(starts):
            self.assertEqual(document.block_start(previous, forward=True), following)
            self.assertEqual(document.block_start(following, forward=False), previous)

    def test_scalar_has_no_blocks(self):
        document = PrettyDocument.from_value("a { or [ in a string")
        self.assertEqual(document.blocks, [])
        self.assertEqual(document.block_start(0, forward=True), 0)
        self.assertEqual(document.block_start(0, forward=False), 0)
        self.assertIsNone(document.matching_delimiter(0))


class VimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "data.jsonl"
        value = {
            "a": [{"value": "needle"}, {"value": "needle"}],
            "short": "short",
            "medium": "界" * 75,
            "long": "x" * 2000,
            "string": "\n".join(f"Line {i}: needle" for i in range(1, 101)),
        }
        self.values = [value, {"result": "needle"}, *({"id": i} for i in range(2, 80))]
        path.write_text("".join(json.dumps(row) + "\n" for row in self.values))
        self.source = JsonlFile(path)
        self.addCleanup(self.source.close)
        self.app = JlvApp(self.source)

    async def settled(self, pilot):
        bar = self.app.screen.query_one(SearchBar)
        async with asyncio.timeout(5):
            while bar.searching:
                await asyncio.sleep(0.005)
        await pilot.pause()
        return bar

    async def command(self, pilot, text):
        await pilot.press(":")
        bar = self.app.screen.query_one(SearchBar)
        self.assertTrue(bar.command_mode)
        self.assertEqual(bar.query_one(Input).value, ":")
        await pilot.press(*text, "enter")
        await pilot.pause()
        return bar

    async def test_record_navigation_and_scoped_jump(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            left = self.app.query_one("#lines-list", RowView)
            await pilot.press("j", "j", "k")
            self.assertEqual(left.index, 1)
            self.assertEqual(self.app._detail_idx, 1)
            await pilot.press("G")
            self.assertEqual(left.index, len(self.values) - 1)
            self.assertGreater(left.scroll_y, 0)
            await pilot.press("g")
            self.assertEqual(left.index, len(self.values) - 1)
            await pilot.press("g")
            self.assertEqual(left.index, 0)
            await pilot.press("G", "g", "escape", "g")
            self.assertEqual(left.index, len(self.values) - 1)
            await pilot.press("g")
            self.assertEqual(left.index, 0)
            bar = await self.command(pilot, "50")
            self.assertEqual(left.index, 49)
            self.assertEqual(self.app._detail_idx, 49)
            self.assertFalse(bar.display)
            self.assertIs(self.app.focused, left)
            self.assertLess(left.index - left.scroll_y, left.scrollable_content_region.height)

    async def test_gg_prefix_is_cancelled_by_other_input_and_focus_changes(self):
        async with self.app.run_test() as pilot:
            left = self.app.query_one("#lines-list", RowView)
            await pilot.press("G", "g", "k", "g")
            self.assertEqual(left.index, len(self.values) - 2)
            await pilot.press("tab", "shift+tab", "g")
            self.assertEqual(left.index, len(self.values) - 2)
            await pilot.press("g")
            self.assertEqual(left.index, 0)
            await pilot.press("G", "g", "up", "g")
            self.assertEqual(left.index, len(self.values) - 2)
            await pilot.press("g")
            self.assertEqual(left.index, 0)

    async def test_right_panel_line_jump_and_blocks(self):
        async with self.app.run_test() as pilot:
            left = self.app.query_one("#lines-list", RowView)
            right = self.app.query_one("#detail-list", RowView)
            await pilot.press("tab", "j", "j", "j")
            self.assertEqual(right.index, 3)
            await pilot.press("[")
            self.assertEqual(right.index, 2)
            await pilot.press("[")
            self.assertEqual(right.index, 1)
            await pilot.press("]")
            self.assertEqual(right.index, 2)
            await pilot.press("]")
            self.assertEqual(right.index, 5)
            await pilot.press("G")
            self.assertEqual(right.index, right.row_count - 1)
            await pilot.press("g", "g")
            self.assertEqual(right.index, 0)
            bar = await self.command(pilot, "4")
            self.assertEqual(right.index, 3)
            self.assertEqual(left.index, 0)
            self.assertFalse(bar.display)
            self.assertIs(self.app.focused, right)

    async def test_block_navigation_reveals_delimiters_horizontally(self):
        path = Path(self.directory.name) / "blocks.jsonl"
        value = {
            "界" * 80: {"array" * 40: [1], "empty" * 40: {}},
            "long": "x" * 2000,
        }
        path.write_text(json.dumps(value) + "\n")
        with JsonlFile(path) as source:
            app = JlvApp(source)
            async with app.run_test(size=(80, 24)) as pilot:
                right = app.query_one("#detail-list", RowView)
                right.focus()
                for key, row in [("]", 1), ("]", 2), ("]", 5), ("[", 2), ("[", 1), ("[", 0)]:
                    right.scroll_to(x=right.max_scroll_x, animate=False)
                    await pilot.press(key)
                    self.assertEqual(right.index, row)
                    line = app._pretty_lines[row][0]
                    width = right.scrollable_content_region.width
                    self.assertEqual(right.scroll_x, max(0, cell_len(line) - width))
                    delimiter = max(line.rfind("{"), line.rfind("["))
                    x = cell_len(line[:delimiter])
                    self.assertGreaterEqual(x, right.scroll_x)
                    self.assertLess(x, right.scroll_x + width)
                right.go_to_line(3)
                await pilot.press("0", "[")
                self.assertEqual(right.index, 2)
                self.assertGreater(right.scroll_x, 0)

    async def test_percent_matches_structural_delimiters_and_reveals_them(self):
        path = Path(self.directory.name) / "matching.jsonl"
        path.write_text(
            json.dumps(
                {
                    "界" * 80 + "[{": [{"text": "} ] {"}, {}, []],
                    "text": "[" + "x" * 1000 + "]",
                }
            )
            + "\n"
        )
        with JsonlFile(path) as source:
            app = JlvApp(source)
            async with app.run_test(size=(80, 24)) as pilot:
                right = app.query_one("#detail-list", RowView)
                right.focus()
                for start, end in app._document.blocks:
                    right.go_to_line(start)
                    for target in (end, start):
                        right.scroll_to(x=right.max_scroll_x, animate=False)
                        await pilot.press("%")
                        self.assertEqual(right.index, target)
                        line_width = cell_len(app._pretty_lines[target][0])
                        self.assertEqual(
                            right.scroll_x,
                            max(0, line_width - right.scrollable_content_region.width),
                        )
                        self.assertLess(
                            right.index - right.scroll_y, right.scrollable_content_region.height
                        )
                for row in app._document.strings:
                    right.go_to_line(row)
                    right.scroll_to(x=10, animate=False)
                    await pilot.press("%")
                    self.assertEqual(right.index, row)
                    self.assertEqual(right.scroll_x, 10)

    async def test_horizontal_navigation_uses_current_line_not_document_width(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            right = self.app.query_one("#detail-list", RowView)
            right.focus()
            medium = next(
                i for i, (text, _) in enumerate(self.app._pretty_lines) if '"medium":' in text
            )
            right.go_to_line(medium)
            await pilot.pause()
            await pilot.press("$")
            width = cell_len(self.app._pretty_lines[medium][0])
            expected = max(0, width - right.scrollable_content_region.width)
            self.assertEqual(right.scroll_x, expected)
            self.assertLess(right.scroll_x, right.max_scroll_x)
            await pilot.press("h")
            self.assertEqual(right.scroll_x, expected - 3)
            await pilot.press("l")
            self.assertEqual(right.scroll_x, expected)
            await pilot.press("0")
            self.assertEqual(right.scroll_x, 0)
            right.go_to_line(medium - 1)
            await pilot.press("$")
            self.assertEqual(right.scroll_x, 0)

    async def test_search_enter_restores_panel_focus_for_n_and_N(self):
        async with self.app.run_test() as pilot:
            left = self.app.query_one("#lines-list", RowView)
            await pilot.press("/", *"needle", "enter")
            bar = await self.settled(pilot)
            first = bar.hit
            self.assertTrue(bar.display)
            self.assertIs(self.app.focused, left)
            await pilot.press("n")
            await self.settled(pilot)
            self.assertGreater(bar.hit.start, first.start)
            await pilot.press("N")
            await self.settled(pilot)
            self.assertEqual(bar.hit.start, first.start)
            await pilot.press("?")
            self.assertIs(self.app.focused, bar.query_one(Input))
            self.assertEqual(bar.query_one(Input).value, "needle")
            await pilot.press("enter")
            await self.settled(pilot)
            self.assertIs(self.app.focused, left)
            self.assertGreater(bar.hit.start, first.start)
            await pilot.press("escape")
            self.assertIsNone(self.app.query_one("#detail-list", RowView).find_selection)
            await pilot.press("n")
            await self.settled(pilot)
            self.assertTrue(bar.display)
            self.assertIs(self.app.focused, left)

    async def test_search_from_right_returns_focus_and_commands_keep_scope(self):
        async with self.app.run_test() as pilot:
            right = self.app.query_one("#detail-list", RowView)
            left = self.app.query_one("#lines-list", RowView)
            await pilot.press("tab", "/", *"needle", "enter")
            bar = await self.settled(pilot)
            self.assertIs(self.app.focused, right)
            self.assertIsNotNone(right.find_selection)
            await self.command(pilot, "2")
            self.assertEqual(right.index, 1)
            self.assertEqual(left.index, 0)
            self.assertIsNone(right.find_selection)
            self.assertEqual(bar.query_one(Input).value, "needle")
            await pilot.press("n")
            await self.settled(pilot)
            self.assertIs(self.app.focused, right)
            self.assertEqual(bar.hit.record, 0)
            self.assertEqual(bar.hit.start, bar.hit.text.index("needle"))
            await pilot.press(":", "q", "ctrl+f")
            self.assertFalse(bar.command_mode)
            self.assertEqual(bar.query_one(Input).value, "needle")
            self.assertFalse(self.app._exit)

    async def test_panel_enter_after_search_inspects_current_right_line(self):
        async with self.app.run_test() as pilot:
            left = self.app.query_one("#lines-list", RowView)
            right = self.app.query_one("#detail-list", RowView)
            main_screen = self.app.screen
            for panel in (left, right):
                panel.focus()
                await pilot.press("/", *"needle", "enter")
                bar = await self.settled(pilot)
                self.assertIs(self.app.focused, panel)
                self.assertIs(self.app.screen, main_screen)
                self.assertTrue(bar.display)
                # Inspect the current line, even after moving away from the hit.
                row = next(
                    row for row, value in self.app._document.strings.items() if value == "short"
                )
                right.go_to_line(row)
                await pilot.pause()
                saved_hit = bar.hit
                await pilot.press("enter")
                self.assertFalse(bar.display)
                self.assertIsNone(right.find_selection)
                self.assertIsInstance(self.app.screen, StringModal)
                inspector = self.app.screen.query_one(StringView)
                # Loading is deferred until after the first inspector refresh.
                async with asyncio.timeout(5):
                    while inspector._pending_value is not None:
                        await asyncio.sleep(0.005)
                self.assertEqual(inspector.text, "short")
                self.assertEqual(bar.hit, saved_hit)
                self.assertEqual(bar.query_one(Input).value, "needle")
                await pilot.press("escape")
                self.assertIs(self.app.screen, main_screen)
                self.assertIs(self.app.focused, right)
                right.go_to_line(0)

    async def test_panel_enter_after_non_string_search_only_focuses_right(self):
        async with self.app.run_test() as pilot:
            left = self.app.query_one("#lines-list", RowView)
            right = self.app.query_one("#detail-list", RowView)
            main_screen = self.app.screen
            for panel in (left, right):
                panel.focus()
                await pilot.press("/", *'"a"', "enter")
                bar = await self.settled(pilot)
                self.assertIs(self.app.focused, panel)
                self.assertNotIn(right.index, self.app._document.strings)
                saved_hit = bar.hit
                await pilot.press("enter")
                self.assertFalse(bar.display)
                self.assertIsNone(right.find_selection)
                self.assertIs(self.app.screen, main_screen)
                self.assertIs(self.app.focused, right)
                self.assertEqual(bar.hit, saved_hit)
                await pilot.press("f3")
                await self.settled(pilot)
                self.assertTrue(bar.display)

    async def test_search_openers_remember_independent_regex_settings(self):
        async with self.app.run_test() as pilot:
            main_bar = self.app.query_one(SearchBar)
            for inspector in (False, True):
                if inspector:
                    self.app.push_screen(StringModal("needle needle"))
                    await pilot.pause()
                await pilot.press("ctrl+f")
                bar = self.app.screen.query_one(SearchBar)
                self.assertFalse(bar.regular_expression)
                await pilot.press("escape")
                await pilot.press("/", *"nee.le", "enter")
                await self.settled(pilot)
                self.assertTrue(bar.regular_expression)
                self.assertIsNotNone(bar.hit)
                first = bar.hit
                await pilot.press("escape", "?")
                self.assertTrue(bar.regular_expression)
                self.assertEqual(bar.hit, first)
                await pilot.press("escape", "ctrl+f")
                self.assertFalse(bar.regular_expression)
                self.assertIsNone(bar.hit)
                self.assertEqual(bar.query_one(Input).value, "nee.le")
                await pilot.press("alt+r", "escape", "/", "alt+r")
                self.assertFalse(bar.regular_expression)
                await pilot.press("escape", "?")
                self.assertFalse(bar.regular_expression)
                await pilot.press("escape", "/")
                self.assertFalse(bar.regular_expression)
                await pilot.press("escape", "ctrl+f")
                self.assertTrue(bar.regular_expression)
                await pilot.press("enter")
                await self.settled(pilot)
                self.assertIsNotNone(bar.hit)
                for key in ("f3", "n", "N"):
                    await pilot.press("escape", key)
                    await self.settled(pilot)
                    self.assertTrue(bar.regular_expression)
                    self.assertIsNotNone(bar.hit)
                await pilot.press("escape", "/", *"needle", "enter")
                await self.settled(pilot)
                await pilot.press("escape", "n")
                await self.settled(pilot)
                self.assertFalse(bar.regular_expression)
                await pilot.press("escape")
                if inspector:
                    await pilot.press("ctrl+f")
                    self.assertTrue(bar.regular_expression)
                    self.assertFalse(main_bar.regular_expression)

    async def test_navigation_keys_are_literal_in_search_input(self):
        async with self.app.run_test() as pilot:
            await pilot.press("/")
            text = "hjklggGnN0$:q/??[]%"
            await pilot.press(*text)
            bar = self.app.query_one(SearchBar)
            self.assertEqual(bar.query_one(Input).value, text)
            self.assertFalse(self.app._exit)
            self.assertFalse(bar.command_mode)
            self.assertEqual(self.app.query_one("#lines-list", RowView).index, 0)

    async def test_command_cancel_preserves_query_and_search_cursor(self):
        async with self.app.run_test() as pilot:
            await pilot.press("/", *"needle", "enter")
            bar = await self.settled(pilot)
            first = bar.hit
            await pilot.press(":", "9", "escape")
            self.assertFalse(bar.display)
            self.assertFalse(bar.command_mode)
            self.assertEqual(bar.query_one(Input).value, "needle")
            self.assertEqual(bar.hit, first)
            await pilot.press("n")
            await self.settled(pilot)
            self.assertEqual(bar.hit.record, 0)
            self.assertGreater(bar.hit.start, first.start)

    async def test_command_errors_remain_visible_and_do_not_move(self):
        async with self.app.run_test() as pilot:
            left = self.app.query_one("#lines-list", RowView)
            for text in ["0", "999", "-1", "x", "9" * 5000]:
                bar = await self.command(pilot, text) if len(text) < 100 else None
                if bar is None:
                    await pilot.press(":")
                    bar = self.app.query_one(SearchBar)
                    bar.query_one(Input).value = ":" + text
                    await pilot.pause()
                    await pilot.press("enter")
                self.assertTrue(bar.display)
                self.assertIs(self.app.focused, bar.query_one(Input))
                self.assertEqual(left.index, 0)
                self.assertRegex(
                    str(bar.query_one(Static).render()), "Unknown command|Line must be"
                )
                await pilot.press("escape")

    async def test_bare_q_does_not_quit_from_either_main_panel(self):
        async with self.app.run_test() as pilot:
            await pilot.press("q")
            self.assertFalse(self.app._exit)
            await pilot.press("tab", "q")
            self.assertFalse(self.app._exit)

    async def test_colon_q_quits_from_main_but_only_closes_inspector(self):
        async with self.app.run_test() as pilot:
            await self.command(pilot, "q")
            self.assertTrue(self.app._exit)
        app = JlvApp(self.source)
        async with app.run_test() as pilot:
            main_screen = app.screen
            main_bar = app.query_one(SearchBar)
            main_bar.query_one(Input).value = "saved query"
            app.push_screen(StringModal("text"))
            await pilot.pause()
            await pilot.press(":", "q", "enter")
            self.assertFalse(app._exit)
            self.assertIs(app.screen, main_screen)
            self.assertEqual(main_bar.query_one(Input).value, "saved query")
            self.assertIsInstance(app.focused, RowView)
            await pilot.press(":", "q", "enter")
            self.assertTrue(app._exit)

    async def test_inspector_commands_use_logical_not_wrapped_lines(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            value = "x " * 1000 + "\nsecond\n" + "\n".join(f"line {i}" for i in range(3, 101))
            self.app.push_screen(StringModal(value))
            await pilot.pause()
            view = self.app.screen.query_one(StringView)
            bar = await self.command(pilot, "2")
            self.assertIs(self.app.focused, view)
            self.assertFalse(bar.display)
            self.assertEqual(view.selection.end, (1, 0))
            self.assertGreater(view.scroll_y, 10)
            await pilot.press("j", "j", "k")
            self.assertEqual(view.selection.end, (2, 0))
            await pilot.press("G")
            self.assertEqual(view.selection.end, (99, 0))
            await pilot.press("g", "g")
            self.assertEqual(view.selection.end, (0, 0))
            self.assertEqual(view.scroll_y, 0)
            await pilot.press("$")
            self.assertGreater(view.scroll_y, 10)
            await pilot.press("0")
            self.assertEqual(view.scroll_y, 0)
            await pilot.press("/", *"second", "enter")
            bar = await self.settled(pilot)
            self.assertIs(self.app.focused, view)
            self.assertEqual(view.selected_text, "second")
            await pilot.press("n")
            await self.settled(pilot)
            self.assertTrue(bar.hit.wrapped)
            await self.command(pilot, "3")
            self.assertEqual(view.selection.end, (2, 0))
            self.assertEqual(view.selected_text, "")
            self.assertFalse(bar.display)
            self.assertEqual(bar.query_one(Input).value, "second")
            self.assertEqual(self.app._detail_idx, 0)
            self.assertEqual(view.text, value)


if __name__ == "__main__":
    unittest.main()
