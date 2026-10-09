from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.cells import cell_len
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Input, Static, TextArea

from jlv import JlvApp, PrettyDocument, StringModal, main
from jlv.rows import RowView
from jlv.search import SearchBar
from jlv.source import JsonlFile


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test.jsonl"

    def source(self, content: bytes) -> JsonlFile:
        self.path.write_bytes(content)
        source = JsonlFile(self.path)
        self.addCleanup(source.close)
        return source

    def test_index_utf8_crlf_and_trailing_blanks(self):
        values = [{"a": "hello"}, ["日本語", "😀"], 42, None, ""]
        raw = "\r\n".join(json.dumps(v, ensure_ascii=False) for v in values) + "\r\n \r\n\t"
        source = self.source(raw.encode())
        self.assertEqual(len(source), len(values))
        for index, value in enumerate(values):
            self.assertEqual(source.value(index), value)
        self.assertTrue(source.preview(1).startswith("    2  "))
        with self.assertRaises(IndexError):
            source.raw(-1)
        with self.assertRaises(IndexError):
            source.raw(len(values))

    def test_validation(self):
        for content, error in [
            (b"", "file is empty"),
            (b" \n\t\n", "file is empty"),
            (b"{}\n\n{}\n", "line 2 is empty"),
            (b"\n{}\n", "line 1 is empty"),
            (b"{}\n{broken}\n", "line 2 is not valid JSON"),
            (b'{}\n"\\xff"\n'.replace(b"\\xff", b"\xff"), "line 2 is not valid UTF-8"),
        ]:
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaisesRegex(ValueError, error):
                    JsonlFile(self.path)

    def test_universal_newlines_and_trimmed_unicode_whitespace(self):
        source = self.source('\u00a0{"a":1}\u00a0\r[2]\r\ntrue\n\r'.encode())
        self.assertEqual(len(source), 3)
        self.assertEqual([source.value(i) for i in range(3)], [{"a": 1}, [2], True])

    def test_zero_width_ascii_control(self):
        source = self.source(b'{"text":"a\x7fb"}\n')
        self.assertEqual(source.preview_width, cell_len(source.preview(0)))
        document = PrettyDocument.from_value(source.value(0))
        self.assertEqual(document.width, max(cell_len(line) for line, _ in document.lines))

    def test_previews_are_truncated_but_values_are_not(self):
        value = {"text": "界" * 1500}
        source = self.source((json.dumps(value, ensure_ascii=False) + "\n").encode())
        preview = source.preview(0)
        self.assertTrue(preview.endswith("..."))
        self.assertEqual(len(preview), 1010)
        self.assertEqual(source.preview_width, cell_len(preview))
        self.assertEqual(source.value(0), value)
        self.assertIs(source.preview(0), preview)

    def test_preview_cache_is_bounded(self):
        source = self.source(b"{}\n" * 300)
        for i in range(len(source)):
            source.preview(i)
        self.assertEqual(len(source._previews), 256)
        self.assertNotIn(0, source._previews)
        source.close()
        with self.assertRaisesRegex(ValueError, "closed"):
            source.raw(0)

    def test_preview_width_matches_rich_for_mixed_unicode(self):
        values = [
            {"text": "ascii " * 200},
            {"text": "a" * 800 + "界" * 190},
            {"text": "a" * 900 + "e\u0301\u007f\u0085"},
            {"text": "a" * 900 + "👩\u200d💻 ☀\ufe0f"},
            {"text": "👩\u200d💻" * 400},
            {"text": "界" * 1000},
        ]
        for value in values:
            with self.subTest(value=str(value)[:40]):
                source = self.source((json.dumps(value, ensure_ascii=False) + "\n").encode())
                self.assertEqual(source.preview_width, cell_len(source.preview(0)))
        source = self.source(
            "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values).encode()
        )
        self.assertEqual(
            source.preview_width, max(cell_len(source.preview(i)) for i in range(len(source)))
        )

    def test_validation_preserves_python_json_values(self):
        records = [
            "NaN", "Infinity", "-Infinity", "1e400",
            str(2 ** 100), "9" * 400, '"\\ud800"',
            '{"duplicate":1,"duplicate":2}',
            "[" * 130 + "0" + "]" * 130,
        ]
        source = self.source(("\n".join(records) + "\n").encode())
        for index, text in enumerate(records):
            self.assertEqual(json.dumps(source.value(index)), json.dumps(json.loads(text)))

    def test_validation_checks_beyond_the_preview(self):
        for suffix, error in [
            (b'"} garbage', "not valid JSON"),
            (b'\xff"}', "not valid UTF-8"),
            (b'\\q"}', "not valid JSON"),
        ]:
            with self.subTest(suffix=suffix):
                self.path.write_bytes(b'{}\n{"text":"' + b"x" * 10_000 + suffix + b"\n")
                with self.assertRaisesRegex(ValueError, f"line 2 is {error}"):
                    JsonlFile(self.path)

    def test_preview_prefix_handles_utf8_boundaries_and_padding(self):
        records = [
            json.dumps(text, ensure_ascii=False)
            for text in (
                "界" * 2000, "😀" * 2000, "x" * 3999 + "😀", "x" * 999 + " ",
                " " * 10_000, "界" + " " * 10_000,
            )
        ]
        records.extend([" " * 5000 + '{"padded":true}' + " " * 5000, '"short"' + " " * 5000])
        for text in records:
            with self.subTest(length=len(text)):
                source = self.source((text + "\n").encode())
                expected = text.strip()
                if len(expected) > 1000:
                    expected = expected[:1000] + "..."
                self.assertEqual(source.preview(0), "    1  " + expected)
                self.assertEqual(source.preview_width, cell_len(source.preview(0)))
                self.assertEqual(source.value(0), json.loads(text))

    def test_validation_preserves_python_depth_and_integer_limits(self):
        records = [
            b"[" * depth + b"0" + b"]" * depth
            for depth in (sys.getrecursionlimit() + 10, 10_000)
        ]
        if limit := sys.get_int_max_str_digits():
            records.append(b"9" * (limit + 1))
        for record in records:
            with self.subTest(length=len(record)):
                self.path.write_bytes(b"{}\n" + record + b"\n")
                try:
                    json.loads(record)
                except (ValueError, RecursionError):
                    with self.assertRaisesRegex(ValueError, "line 2 is not valid JSON"):
                        with JsonlFile(self.path):
                            pass
                else:
                    with JsonlFile(self.path) as source:
                        self.assertEqual(len(source), 2)

    def test_background_index_and_viewport_reads_have_independent_cursors(self):
        values = [{"id": index, "body": "x" * 1200} for index in range(2500)]
        self.path.write_text("".join(json.dumps(value) + "\n" for value in values))
        with JsonlFile(self.path, eager=False) as source:
            self.assertGreater(len(source), 0)
            self.assertLess(len(source), len(values))
            self.assertFalse(source.complete)
            source.start_indexing()
            for index in range(500):
                row = index % len(source)
                self.assertEqual(source.value(row), values[row])
            source._thread.join(timeout=5)
            self.assertFalse(source._thread.is_alive())
            self.assertIsNone(source.error)
            self.assertTrue(source.complete)
            self.assertEqual(len(source), len(values))
            self.assertEqual(source.value(len(values) - 1), values[-1])

    def test_background_errors_preserve_valid_records(self):
        for tail, error in [
            (b"{broken}\n", "line 101 is not valid JSON"),
            (b'"\xff"\n', "line 101 is not valid UTF-8"),
            (b"\n{}\n", "line 101 is empty"),
        ]:
            with self.subTest(error=error):
                self.path.write_bytes(b"{}\n" * 100 + tail)
                with JsonlFile(self.path, eager=False) as source:
                    self.assertFalse(source.complete)
                    self.assertIsNone(source.error)
                    source.start_indexing()
                    source._thread.join(timeout=5)
                    self.assertFalse(source._thread.is_alive())
                    self.assertIn(error, source.error)
                    self.assertFalse(source.complete)
                    self.assertEqual(len(source), 100)
                    self.assertEqual(source.value(99), {})

    def test_closing_cancels_and_joins_background_indexing(self):
        self.path.write_bytes(b'{"id":1}\n' * 5000)
        source = JsonlFile(self.path, eager=False)
        self.addCleanup(source.close)
        from jlv.source import orjson
        decode = orjson.loads

        def slow_decode(raw):
            time.sleep(0.001)
            return decode(raw)

        with patch("jlv.source.orjson.loads", side_effect=slow_decode):
            source.start_indexing()
            source.close()
        self.assertFalse(source._thread.is_alive())
        self.assertFalse(source.complete)
        self.assertLess(len(source), 5000)
        with self.assertRaisesRegex(ValueError, "closed"):
            source.raw(0)

    @unittest.skipUnless(Path("/proc/self/fd").exists(), "requires Linux file descriptors")
    def test_non_seekable_input(self):
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        with os.fdopen(writer, "wb") as stream:
            stream.write(b'{"message":"from a pipe"}\n')
        with JsonlFile(f"/proc/self/fd/{reader}") as source:
            self.assertEqual(source.value(0), {"message": "from a pipe"})


class DocumentTests(unittest.TestCase):
    def test_formatting_matches_json_dumps(self):
        values = [
            {},
            [],
            None,
            True,
            False,
            42,
            -1.25,
            float("nan"),
            "a: sentence, with \"quotes\"\n\tand unicode 界 😀",
            {"key:with:colons": "correct: value", "quoted\"key": ["a:b", ""]},
            {"a": [1, {"nested": [[], {}, None, True, "long" * 1000]}]},
        ]
        for value in values:
            with self.subTest(value=repr(value)[:80]):
                document = PrettyDocument.from_value(value)
                self.assertEqual(
                    "\n".join(line for line, _ in document.lines),
                    json.dumps(value, indent=2, ensure_ascii=False),
                )
                for index, string in document.strings.items():
                    self.assertEqual(document.lines[index][1], "string")
                    self.assertIn(json.dumps(string, ensure_ascii=False), document.lines[index][0])

    def test_string_values_come_from_structure(self):
        document = PrettyDocument.from_value({"key:with:colons": ["url:with:colons", "", "a,b,"]})
        self.assertEqual(list(document.strings.values()), ["url:with:colons", "", "a,b,"])


class CliTests(unittest.TestCase):
    def test_background_validation_error_returns_failure(self):
        def run(app):
            app.source.start_indexing()
            app.source._thread.join(timeout=5)
            self.assertFalse(app.source._thread.is_alive())

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid-tail.jsonl"
            path.write_bytes(b"{}\n" * 100 + b"{broken}\n")
            with (
                patch("sys.argv", ["jlv", str(path)]),
                patch.object(JlvApp, "run", autospec=True, side_effect=run),
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
                self.assertRaises(SystemExit) as exited,
            ):
                main()
            self.assertEqual(exited.exception.code, 1)
            self.assertIn("Error: line 101 is not valid JSON", stderr.getvalue())


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "test.jsonl"
        self.values = [
            {"id": i, "odd:key": "A short sentence.", "array": list(range(100)),
             "body": ("A paragraph with a colon: and quoted \"text\". " * 30 + "\n\n") * 50}
            for i in range(150)
        ]
        path.write_text("".join(json.dumps(value) + "\n" for value in self.values))
        self.source = JsonlFile(path)
        self.addCleanup(self.source.close)
        self.app = JlvApp(self.source)

    async def test_keyboard_navigation_and_complete_preview(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            left = self.app.query_one("#lines-list", RowView)
            right = self.app.query_one("#detail-list", RowView)
            await pilot.press("down", "down")
            self.assertEqual(left.index, 2)
            self.assertEqual(self.app._detail_idx, 2)
            self.assertEqual(right.row_count, len(self.app._pretty_lines))
            self.assertEqual(len(left.children), 0)
            self.assertEqual(len(right.children), 0)
            await pilot.press("end")
            self.assertEqual(left.index, len(self.values) - 1)
            self.assertGreater(left.scroll_y, 0)
            await pilot.press("down")
            self.assertEqual(left.index, len(self.values) - 1)
            await pilot.press("home", "up")
            self.assertEqual(left.index, 0)
            await pilot.press("pagedown")
            self.assertGreater(left.index, 1)
            await pilot.press("tab", "end")
            self.assertIs(self.app.focused, right)
            self.assertEqual(right.index, right.row_count - 1)
            self.assertGreater(right.scroll_y, 0)
            await pilot.press("home", "down")
            self.assertEqual(right.index, 1)
            await pilot.press("shift+tab")
            self.assertIs(self.app.focused, left)

    async def test_strings_open_exactly_and_inspector_is_read_only(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            right = self.app.query_one("#detail-list", RowView)
            document = self.app._document
            for index, value in document.strings.items():
                right.index = index
                right.focus()
                await pilot.pause()
                await pilot.press("enter")
                self.assertIsInstance(self.app.screen, StringModal)
                inspector = self.app.screen.query_one("#modal-container", TextArea)
                self.assertEqual(inspector.text, value)
                self.assertTrue(inspector.read_only)
                await pilot.press("x")
                self.assertEqual(inspector.text, value)
                if len(value) > 1000:
                    await pilot.press("end")
                    self.assertGreater(inspector.scroll_y, 0)
                    await pilot.resize_terminal(80, 24)
                    self.assertEqual(inspector.text, value)
                    await pilot.press("home")
                    self.assertEqual(inspector.scroll_y, 0)
                await pilot.press("escape")
                self.assertNotIsInstance(self.app.screen, StringModal)
                self.assertIs(self.app.focused, right)
                self.assertEqual(right.index, index)

    async def test_mouse_selection_after_scrolling(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            left = self.app.query_one("#lines-list", RowView)
            left.scroll_to(y=90, animate=False)
            await pilot.pause()
            await pilot.click("#lines-list", offset=(4, 4))
            self.assertEqual(left.index, 94)
            self.assertEqual(self.app._detail_idx, 94)
            await pilot.click("#detail-list", offset=(0, 2))
            self.assertNotIsInstance(self.app.screen, StringModal)
            await pilot.click("#detail-list", offset=(10, 2))
            self.assertIsInstance(self.app.screen, StringModal)
            self.assertEqual(self.app.screen.value, "A short sentence.")

    async def test_rapid_selection_and_bounded_cache(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            left = self.app.query_one("#lines-list", RowView)
            for i in range(100):
                left.index = i
            await pilot.pause()
            self.assertEqual(self.app._detail_idx, 99)
            self.assertLessEqual(len(self.app._documents), 8)
            self.assertEqual(
                "\n".join(line for line, _ in self.app._pretty_lines),
                json.dumps(self.values[99], ensure_ascii=False, indent=2),
            )
            for index in range(12):
                left.index = index
                await pilot.pause()
            self.assertEqual(len(self.app._documents), 8)
            self.assertNotIn(0, self.app._documents)

    async def test_horizontal_scrolling_and_unicode(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            right = self.app.query_one("#detail-list", RowView)
            document = PrettyDocument.from_value({"界": "😀" * 100 + "end"})
            right.set_rows(len(document.lines), document.width, lambda row: document.lines[row][0])
            right.focus()
            await pilot.pause()
            right.scroll_to(x=21, animate=False)
            await pilot.pause()
            strip = right.render_line(1)
            self.assertEqual(strip.cell_length, right.size.width)
            self.assertEqual(next(iter(strip)).style.meta["offset"], (14, 1))
            self.assertGreater(right.scroll_x, 0)
            await pilot.resize_terminal(100, 30)
            self.assertEqual(right.row_count, len(document.lines))

    async def test_selection_extracts_only_requested_rows(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            right = self.app.query_one("#detail-list", RowView)
            selection = Selection(Offset(2, 1), Offset(7, 2))
            expected = Selection(Offset(2, 0), Offset(7, 1)).extract(
                "\n".join(line for line, _ in self.app._pretty_lines[1:3])
            )
            self.assertEqual(right.get_selection(selection), (expected, "\n"))
            self.app.screen.selections = {right: selection}
            strip = right.render_line(1)
            self.assertEqual(strip.cell_length, right.size.width)
            left = self.app.query_one("#lines-list", RowView)
            self.app.screen.selections = {
                right: selection, left: Selection(Offset(0, 0), Offset(4, 0))
            }
            left.index = 1
            await pilot.pause()
            self.assertNotIn(right, self.app.screen.selections)
            self.assertIn(left, self.app.screen.selections)

    async def test_bare_q_does_not_quit_or_close_inspector(self):
        async with self.app.run_test() as pilot:
            right = self.app.query_one("#detail-list", RowView)
            right.index = next(iter(self.app._document.strings))
            right.focus()
            await pilot.pause()
            await pilot.press("enter", "q")
            self.assertFalse(self.app._exit)
            self.assertIsInstance(self.app.screen, StringModal)

    async def test_scalar_strings_and_non_string_selection(self):
        path = Path(self.directory.name) / "scalars.jsonl"
        values = ["", "url:with:colons", ["a:b", None, False], 123]
        path.write_text("".join(json.dumps(value) + "\n" for value in values))
        with JsonlFile(path) as source:
            app = JlvApp(source)
            async with app.run_test() as pilot:
                left = app.query_one("#lines-list", RowView)
                right = app.query_one("#detail-list", RowView)
                for index in range(3):
                    left.index = index
                    await pilot.pause()
                    row, text = next(iter(app._document.strings.items()))
                    right.index = row
                    right.focus()
                    await pilot.press("enter")
                    self.assertIsInstance(app.screen, StringModal)
                    self.assertEqual(app.screen.value, text)
                    self.assertEqual(app.screen.query_one(TextArea).text, text)
                    await pilot.press("escape")
                left.index = 3
                await pilot.pause()
                right.focus()
                await pilot.press("enter")
                self.assertNotIsInstance(app.screen, StringModal)

    async def test_background_progress_preserves_navigation_and_full_file_commands(self):
        path = Path(self.directory.name) / "background.jsonl"
        path.write_text("".join(
            json.dumps({"id": i, "text": "last needle" if i == 199 else "body"}) + "\n"
            for i in range(200)
        ))
        with JsonlFile(path, eager=False) as source:
            start_indexing = source.start_indexing
            app = JlvApp(source)
            with patch.object(source, "start_indexing"):
                async with app.run_test(size=(100, 30)) as pilot:
                    left = app.query_one("#lines-list", RowView)
                    bar = app.query_one(SearchBar)
                    status = app.query_one("#index-status", Static)
                    self.assertTrue(status.display)
                    await pilot.press("down", "down", "end", "G")
                    self.assertEqual(left.index, 2)
                    await pilot.press(":")
                    bar.query_one(Input).value = ":200"
                    await pilot.press("enter")
                    self.assertIn("Still indexing", bar._status)
                    self.assertEqual(left.index, 2)
                    await pilot.press("escape", "ctrl+f")
                    bar.query_one(Input).value = "last needle"
                    await pilot.press("enter")
                    self.assertIn("Still indexing", bar._status)
                    self.assertFalse(bar.searching)
                    await pilot.press("escape")
                    selection = Selection(Offset(1, 1), Offset(5, 2))
                    app.screen.selections = {left: selection}
                    left.scroll_to(y=20, animate=False)
                    await pilot.pause()
                    start_indexing()
                    async with asyncio.timeout(5):
                        while not source.complete:
                            await asyncio.sleep(0.01)
                    app._refresh_index()
                    await pilot.pause()
                    self.assertEqual(left.row_count, 200)
                    self.assertEqual(left.index, 2)
                    self.assertEqual(left.scroll_y, 20)
                    self.assertEqual(app.screen.selections[left], selection)
                    self.assertFalse(status.display)
                    await pilot.press("G")
                    self.assertEqual(left.index, 199)
                    await pilot.press("home", "ctrl+f", "enter")
                    async with asyncio.timeout(5):
                        while bar.searching:
                            await asyncio.sleep(0.01)
                    await pilot.pause()
                    self.assertEqual(left.index, 199)
                    self.assertEqual(bar.hit.record, 199)

    async def test_background_error_is_visible_even_with_inspector_open(self):
        path = Path(self.directory.name) / "invalid-tail.jsonl"
        path.write_bytes(b'{"text":"inspect me"}\n' * 100 + b"{broken}\n")
        with JsonlFile(path, eager=False) as source:
            start_indexing = source.start_indexing
            app = JlvApp(source)
            with patch.object(source, "start_indexing"):
                async with app.run_test() as pilot:
                    base = app.screen
                    right = app.query_one("#detail-list", RowView)
                    right.index = next(iter(app._document.strings))
                    right.focus()
                    await pilot.press("enter")
                    self.assertIsInstance(app.screen, StringModal)
                    start_indexing()
                    async with asyncio.timeout(5):
                        while source.error is None:
                            await asyncio.sleep(0.01)
                    app._refresh_index()
                    self.assertIsInstance(app.screen, StringModal)
                    self.assertEqual(app.screen.value, "inspect me")
                    status = base.query_one("#index-status", Static)
                    self.assertTrue(status.display)
                    self.assertIn("line 101 is not valid JSON", str(status.content))
                    await pilot.press("escape", "ctrl+f")
                    bar = app.query_one(SearchBar)
                    bar.query_one(Input).value = "missing"
                    await pilot.press("enter")
                    self.assertIn("Indexing stopped", bar._status)
                    self.assertFalse(bar.searching)

    async def test_full_file_commands_before_final_progress_refresh(self):
        path = Path(self.directory.name) / "completed.jsonl"
        path.write_bytes(b'{"text":"body"}\n' * 199 + b'{"text":"last needle"}\n')
        for command in (True, False):
            with self.subTest(command=command), JsonlFile(path, eager=False) as source:
                app = JlvApp(source)
                with patch.object(source, "start_indexing"):
                    async with app.run_test() as pilot:
                        app._index_timer.stop()
                        left = app.query_one("#lines-list", RowView)
                        bar = app.query_one(SearchBar)
                        source._finish_indexing()
                        self.assertTrue(source.complete)
                        self.assertEqual(left.row_count, 64)
                        await pilot.press(":" if command else "ctrl+f")
                        bar.query_one(Input).value = ":200" if command else "last needle"
                        await pilot.press("enter")
                        async with asyncio.timeout(5):
                            while bar.searching:
                                await asyncio.sleep(0.01)
                        await pilot.pause()
                        self.assertEqual(left.row_count, 200)
                        self.assertEqual(left.index, 199)
                        self.assertEqual(app._detail_idx, 199)


@unittest.skipUnless(os.environ.get("JLV_STRESS_FILE"), "set JLV_STRESS_FILE for full fixture coverage")
class StressFileTests(unittest.TestCase):
    def test_every_record_matches_original_formatting(self):
        path = Path(os.environ["JLV_STRESS_FILE"])
        self.assertEqual(path.stat().st_size, 300_000_000)
        with JsonlFile(path) as source:
            self.assertEqual(len(source), 30_000)
            for index in range(len(source)):
                value = source.value(index)
                document = PrettyDocument.from_value(value)
                self.assertEqual(
                    "\n".join(line for line, _ in document.lines),
                    json.dumps(value, indent=2, ensure_ascii=False),
                    f"record {index + 1}",
                )
                raw = source.raw(index).strip()
                preview = raw[:1000] + "..." if len(raw) > 1000 else raw
                self.assertEqual(source.preview(index), f"{index + 1:>5}  {preview}")


if __name__ == "__main__":
    unittest.main()
