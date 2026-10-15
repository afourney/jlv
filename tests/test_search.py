from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import regex
from textual.widgets import Input, Static

from jlv import JlvApp, StringModal, StringView
from jlv.rows import RowView
from jlv.search import SearchBar, SearchOptions, find_match, text_location
from jlv.source import JsonlFile


class SearchEngineTests(unittest.TestCase):
    def find(
        self,
        texts,
        query,
        *,
        origin=(0, 0),
        previous=None,
        backwards=False,
        regex_mode=False,
        case=False,
    ):
        return find_match(
            texts.__getitem__,
            len(texts),
            SearchOptions(query, regex_mode, case),
            origin,
            previous,
            backwards,
            threading.Event(),
        )

    def test_next_previous_and_wrap(self):
        texts = ["foo foo", "nothing", "FOO"]
        first = self.find(texts, "foo")
        second = self.find(texts, "foo", previous=first)
        third = self.find(texts, "foo", previous=second)
        self.assertEqual((first.record, first.start), (0, 0))
        self.assertEqual((second.record, second.start), (0, 4))
        self.assertEqual((third.record, third.start), (2, 0))
        wrapped = self.find(texts, "foo", previous=third)
        self.assertEqual((wrapped.record, wrapped.start, wrapped.wrapped), (0, 0, True))
        previous = self.find(texts, "foo", previous=wrapped, backwards=True)
        self.assertEqual((previous.record, previous.start, previous.wrapped), (2, 0, True))
        previous = self.find(texts, "foo", previous=third, backwards=True)
        self.assertEqual((previous.record, previous.start), (0, 4))

    def test_literals_case_and_unicode_offsets(self):
        text = "界😀 a.b A.B axb"
        hit = self.find([text], "a.b")
        self.assertEqual(text[hit.start : hit.end], "a.b")
        self.assertEqual(hit.start, 3)
        hit = self.find([text], "A.B", case=True)
        self.assertEqual(text[hit.start : hit.end], "A.B")
        self.assertIsNone(self.find(["ABC"], "abc", case=True))

    def test_no_cross_record_matches_even_with_dotall(self):
        for query, regex_mode in [("end\nstart", False), (r"(?s)end.*start", True)]:
            self.assertIsNone(self.find(["end", "start"], query, regex_mode=regex_mode))
            hit = self.find(["end\nstart"], query, regex_mode=regex_mode)
            self.assertEqual(hit.text[hit.start : hit.end], "end\nstart")

    def test_anchors_lookbehind_and_record_context(self):
        texts = ["foo foo", "foo"]
        first = self.find(texts, r"^foo", regex_mode=True)
        second = self.find(texts, r"^foo", regex_mode=True, previous=first)
        self.assertEqual(second.record, 1)
        hit = self.find(["foo bar"], r"(?<=foo )bar", origin=(0, 4), regex_mode=True)
        self.assertEqual((hit.start, hit.end), (4, 7))
        hit = self.find(["first\nsecond"], r"(?m)^second$", regex_mode=True)
        self.assertEqual((hit.start, hit.end), (6, 12))

    def test_zero_width_progress_in_both_directions(self):
        texts = ["ab", ""]
        hit = None
        positions = []
        for _ in range(5):
            hit = self.find(texts, r"(?=)", previous=hit, regex_mode=True)
            positions.append((hit.record, hit.start))
        self.assertEqual(positions, [(0, 0), (0, 1), (0, 2), (1, 0), (0, 0)])
        previous = self.find(texts, r"(?=)", previous=hit, backwards=True, regex_mode=True)
        self.assertEqual((previous.record, previous.start), (1, 0))

    def test_no_match_and_invalid_pattern(self):
        self.assertIsNone(self.find(["abc", ""], "missing"))
        with self.assertRaises(regex.error):
            self.find(["abc"], "[", regex_mode=True)

    def test_cancellation_stops_before_loading_more_records(self):
        event = threading.Event()
        visits = []

        def text_at(index):
            visits.append(index)
            event.set()
            return "not found"

        hit = find_match(text_at, 10000, SearchOptions("missing"), (0, 0), None, False, event)
        self.assertIsNone(hit)
        self.assertEqual(visits, [0])

    def test_regex_timeout_is_reported(self):
        with self.assertRaisesRegex(TimeoutError, "timed out in record 1"):
            find_match(
                lambda index: "a" * 100_000 + "!",
                1,
                SearchOptions(r"(a+)+$", True),
                (0, 0),
                None,
                False,
                threading.Event(),
                timeout=0.005,
            )

    def test_line_offsets_do_not_split_unicode_separators(self):
        self.assertEqual(text_location("界\u2028x\nlast", 5), (1, 1))


class SearchInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "find.jsonl"
        self.values = [
            {"text": "needle needle", "literal": "a.b"},
            {"text": "x" * 1500 + " NEEDLE", "decoded": "first\nsecond\t界"},
            {"text": "last needle", "array": ["aaa", "bbb"]},
        ]
        path.write_text("".join(json.dumps(value) + "\n" for value in self.values))
        self.source = JsonlFile(path)
        self.addCleanup(self.source.close)
        self.app = JlvApp(self.source)

    async def settled(self, bar, pilot):
        async with asyncio.timeout(5):
            while bar.searching:
                await asyncio.sleep(0.005)
        await pilot.pause()

    async def query(self, pilot, text, *, regex_mode=False, case=False):
        await pilot.press("ctrl+f")
        bar = self.app.screen.query_one(SearchBar)
        bar.query_one(Input).value = text
        if bar.regular_expression != regex_mode:
            await pilot.press("alt+r")
        if bar.match_case != case:
            await pilot.press("alt+c")
        await pilot.pause()
        await pilot.press("enter")
        await self.settled(bar, pilot)
        return bar

    async def test_shared_search_navigation_and_long_line_highlight(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            bar = await self.query(pilot, "needle")
            left = self.app.query_one("#lines-list", RowView)
            right = self.app.query_one("#detail-list", RowView)
            self.assertEqual(left.index, 0)
            first = bar.hit
            self.assertIsNotNone(right.find_selection)
            await pilot.press("f3")
            await self.settled(bar, pilot)
            self.assertEqual(left.index, 0)
            self.assertGreater(bar.hit.start, first.start)
            await pilot.press("f3")
            await self.settled(bar, pilot)
            self.assertEqual(left.index, 1)
            self.assertEqual(self.app._detail_idx, 1)
            self.assertGreater(right.scroll_x, 1000)
            self.assertEqual(bar.hit.text[bar.hit.start : bar.hit.end], "NEEDLE")
            self.assertIn("NEEDLE", right.render_line(right.index - int(right.scroll_y)).text)
            await pilot.press("shift+f3")
            await self.settled(bar, pilot)
            self.assertEqual(left.index, 0)
            await pilot.press("escape")
            self.assertFalse(bar.display)
            self.assertIsNone(right.find_selection)
            self.assertIs(self.app.focused, left)
            await pilot.press("f3")
            await self.settled(bar, pilot)
            self.assertEqual(left.index, 1)
            self.assertTrue(bar.display)
            self.assertIsNotNone(right.find_selection)
            await pilot.press("f15")
            await self.settled(bar, pilot)
            self.assertEqual(left.index, 0)

    async def test_query_changes_cancel_and_show_errors(self):
        async with self.app.run_test() as pilot:
            bar = await self.query(pilot, "needle")
            bar = await self.query(pilot, "[", regex_mode=True)
            self.assertIn("Invalid regular expression", str(bar.query_one(Static).render()))
            self.assertIsNone(self.app.query_one("#detail-list", RowView).find_selection)
            bar = await self.query(pilot, "this is not present")
            self.assertIn("No match", str(bar.query_one(Static).render()))
            self.assertIsNone(bar.hit)
            bar = await self.query(pilot, "NEEDLE", case=True)
            self.assertEqual(bar.hit.record, 1)

    async def test_multiline_regex_stays_within_record(self):
        async with self.app.run_test() as pilot:
            bar = await self.query(pilot, r'(?s)"text".*?"literal"', regex_mode=True)
            self.assertEqual(bar.hit.record, 0)
            selected = self.app.query_one("#detail-list", RowView).find_selection
            self.assertLess(selected.start.y, selected.end.y)
            bar = await self.query(pilot, r"(?s)needle.*NEEDLE", regex_mode=True, case=True)
            self.assertIsNone(bar.hit)

    async def test_inspector_has_independent_decoded_search(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            main_bar = await self.query(pilot, "needle")
            saved = main_bar.hit
            await pilot.press("escape")
            left = self.app.query_one("#lines-list", RowView)
            left.index = 1
            await pilot.pause()
            right = self.app.query_one("#detail-list", RowView)
            right.index = next(
                row
                for row, value in self.app._document.strings.items()
                if value.startswith("first")
            )
            right.focus()
            await pilot.press("enter")
            self.assertIsInstance(self.app.screen, StringModal)
            bar = await self.query(pilot, r"first\nsecond\t界", regex_mode=True)
            self.assertIsNot(bar, main_bar)
            self.assertEqual(bar.hit.text[bar.hit.start : bar.hit.end], "first\nsecond\t界")
            inspector = self.app.screen.query_one(StringView)
            self.assertEqual(inspector.selected_text, "first\nsecond\t界")
            self.assertEqual(inspector.text, "first\nsecond\t界")
            await pilot.press("f3")
            await self.settled(bar, pilot)
            self.assertTrue(bar.hit.wrapped)
            bar = await self.query(pilot, "needle")
            self.assertIsNone(bar.hit)
            self.assertEqual(main_bar.hit, saved)
            self.assertEqual(main_bar.query_one(Input).value, "needle")
            await pilot.press("escape")
            self.assertIsInstance(self.app.screen, StringModal)
            self.assertIs(self.app.focused, inspector)
            await pilot.press("escape")
            self.assertNotIsInstance(self.app.screen, StringModal)
            self.assertEqual(main_bar.hit, saved)

    async def test_inspector_wrapping_and_zero_width_matches(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            self.app.push_screen(StringModal("界 " * 5000 + "\nEND"))
            await pilot.pause()
            bar = await self.query(pilot, "END")
            view = self.app.screen.query_one(StringView)
            self.assertGreater(view.scroll_y, 0)
            self.assertEqual(view.selected_text, "END")
            await pilot.resize_terminal(100, 30)
            bar = await self.query(pilot, r"$", regex_mode=True)
            self.assertEqual(bar.hit.start, len(bar.hit.text))
            self.assertIsNotNone(view._zero_match)
            self.assertTrue(view.get_line(1).spans)
            await pilot.press("f3")
            await self.settled(bar, pilot)
            self.assertTrue(bar.hit.wrapped)

    async def test_inspector_close_clears_highlight_but_preserves_search_position(self):
        async with self.app.run_test() as pilot:
            self.app.push_screen(StringModal("needle then needle"))
            await pilot.pause()
            bar = await self.query(pilot, "needle")
            view = self.app.screen.query_one(StringView)
            first = bar.hit
            self.assertEqual(first.start, 0)
            await pilot.press("escape")
            self.assertFalse(bar.display)
            self.assertEqual(view.selected_text, "")
            self.assertIsNone(view._zero_match)
            self.assertEqual(bar.hit, first)
            await pilot.press("f3")
            await self.settled(bar, pilot)
            self.assertTrue(bar.display)
            self.assertEqual(bar.hit.start, 12)
            self.assertEqual(view.selected_text, "needle")
            await pilot.press("escape", "f15")
            await self.settled(bar, pilot)
            self.assertEqual(bar.hit.start, 0)
            self.assertEqual(view.selected_text, "needle")

    async def test_closing_zero_width_search_clears_marker(self):
        async with self.app.run_test() as pilot:
            self.app.push_screen(StringModal("abc"))
            await pilot.pause()
            bar = await self.query(pilot, "$", regex_mode=True)
            view = self.app.screen.query_one(StringView)
            self.assertIsNotNone(view._zero_match)
            await pilot.press("escape")
            self.assertIsNone(view._zero_match)
            await pilot.press("f3")
            await self.settled(bar, pilot)
            self.assertIsNotNone(view._zero_match)
            self.assertTrue(bar.hit.wrapped)

    async def test_cancel_discards_background_result(self):
        async with self.app.run_test() as pilot:
            await pilot.press("ctrl+f")
            bar = self.app.screen.query_one(SearchBar)
            release = threading.Event()
            entered = threading.Event()
            self.addCleanup(release.set)

            def slow_text(index):
                entered.set()
                release.wait(timeout=3)
                return "late match"

            bar.text_at = slow_text
            bar.query_one(Input).value = "match"
            await pilot.pause()
            bar.find()
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            await pilot.press("escape")
            release.set()
            await pilot.pause()
            self.assertFalse(bar.display)
            self.assertIsNone(bar.hit)
            self.assertEqual(self.app._detail_idx, 0)

    async def test_quit_character_can_be_typed_in_query(self):
        async with self.app.run_test() as pilot:
            await pilot.press("ctrl+f", "q")
            self.assertFalse(self.app._exit)
            self.assertEqual(self.app.screen.query_one(Input).value, "q")

    async def test_search_bindings_do_not_interfere_with_command_palette(self):
        async with self.app.run_test() as pilot:
            main_screen = self.app.screen
            await pilot.press("ctrl+p")
            self.assertIsNot(self.app.screen, main_screen)
            await pilot.press("escape")
            self.assertIs(self.app.screen, main_screen)

    async def test_compact_bar_keyboard_toggles_and_bright_highlight(self):
        async with self.app.run_test(size=(60, 24)) as pilot:
            bar = await self.query(pilot, "needle")
            self.assertEqual(bar.query_one(Input).region.width, bar.region.width)
            self.assertGreater(bar.query_one(Static).region.height, 1)
            self.assertLessEqual(bar.region.bottom, 24)
            left = self.app.query_one("#lines-list", RowView)
            right = self.app.query_one("#detail-list", RowView)
            self.assertIs(self.app.focused, left)
            await pilot.press("ctrl+f")
            self.assertFalse(left.has_focus)
            selected_style = left.get_component_rich_style("rowview--highlight")
            self.assertNotEqual(selected_style.bgcolor, left.rich_style.bgcolor)
            strip = right.render_line(right.index - int(right.scroll_y))
            self.assertTrue(
                any(
                    segment.style
                    and segment.style.bgcolor
                    and segment.style.bgcolor.get_truecolor() == (255, 255, 0)
                    for segment in strip
                )
            )
            await pilot.press("alt+r", "alt+c")
            self.assertTrue(bar.regular_expression)
            self.assertTrue(bar.match_case)
            status = str(bar.query_one(Static).render())
            self.assertIn("Regex on", status)
            self.assertIn("Match case on", status)


@unittest.skipUnless(
    os.environ.get("JLV_STRESS_FILE"), "set JLV_STRESS_FILE for full fixture coverage"
)
class StressSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_file_search_and_cancellation_remain_responsive(self):
        with JsonlFile(os.environ["JLV_STRESS_FILE"]) as source:
            app = JlvApp(source)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("ctrl+f")
                bar = app.query_one(SearchBar)
                bar.query_one(Input).value = '"id": 29999,'
                await pilot.pause()
                start = time.perf_counter()
                bar.find()
                delays = []
                async with asyncio.timeout(60):
                    while bar.searching:
                        tick = time.perf_counter()
                        await asyncio.sleep(0.01)
                        delays.append(time.perf_counter() - tick)
                await pilot.pause()
                self.assertEqual(bar.hit.record, 29998)
                left = app.query_one("#lines-list", RowView)
                self.assertEqual(left.index, 29998)
                self.assertLess(left.index - left.scroll_y, left.scrollable_content_region.height)
                self.assertEqual(app._detail_idx, 29998)
                self.assertLess(max(delays), 0.5)
                print(
                    f"\nStress search: {time.perf_counter() - start:.3f}s to record 29999; "
                    f"max event-loop interval {max(delays) * 1000:.1f}ms"
                )
                bar.query_one(Input).value = "not-present-123456789"
                await pilot.pause()
                bar.find()
                await asyncio.sleep(0.02)
                start = time.perf_counter()
                await pilot.press("escape")
                self.assertFalse(bar.searching)
                self.assertFalse(bar.display)
                self.assertLess(time.perf_counter() - start, 0.5)
                self.assertEqual(left.index, 29998)


if __name__ == "__main__":
    unittest.main()
