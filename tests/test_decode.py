from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jlv import JlvApp, PrettyDocument, StringModal, StringView
from jlv.rows import RowView
from jlv.search import SearchBar
from jlv.source import JsonlFile

INNER = {"tool": "search", "args": {"query": "needle", "limit": 3}}


class DecodeDocumentTests(unittest.TestCase):
    def test_decodes_nested_string_without_changing_original(self):
        value = {"a": 1, "steps": [{"payload": json.dumps(INNER)}], "z": 2}
        document = PrettyDocument.from_value(value)
        row = next(row for row, path in document.paths.items() if path == ("steps", 0, "payload"))
        decoded = document.decode_string(row)
        self.assertIsNotNone(decoded)
        expected = {"a": 1, "steps": [{"payload": INNER}], "z": 2}
        self.assertEqual(decoded.value, expected)
        self.assertEqual(
            "\n".join(line for line, _ in decoded.lines),
            json.dumps(expected, indent=2, ensure_ascii=False),
        )
        self.assertEqual(list(decoded.value), ["a", "steps", "z"])
        self.assertEqual(value["steps"][0]["payload"], json.dumps(INNER))

    def test_rejects_plain_text_scalars_and_non_strings(self):
        value = {"text": "hello", "number": "42", "null": "null", "n": 5, "bad": "{oops"}
        document = PrettyDocument.from_value(value)
        for row in range(len(document.lines)):
            self.assertIsNone(document.decode_string(row), document.lines[row][0])

    def test_top_level_string_record_decodes(self):
        document = PrettyDocument.from_value(json.dumps([1, 2]))
        decoded = document.decode_string(0)
        self.assertEqual(decoded.value, [1, 2])

    def test_expandable_matches_decode(self):
        value = {
            "obj": json.dumps(INNER), "arr": " [1, 2] ", "str": json.dumps("x"), "plain": "hello",
            "number": "42", "bad": "{oops}", "n": 5, "quoted": '"', "empty": "",
        }
        document = PrettyDocument.from_value(value)
        expandable = {document.lines[row][0].split(":")[0].strip() for row in range(len(document.lines))
                      if document.expandable(row)}
        self.assertEqual(expandable, {'"obj"', '"arr"', '"str"'})
        for row in range(len(document.lines)):
            self.assertEqual(document.expandable(row), document.decode_string(row) is not None)


class DecodeAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "data.jsonl"
        triple = json.dumps(json.dumps(INNER))
        self.values = [
            {"id": 0, "note": "plain", "payload": triple},
            {"id": 1, "payload": json.dumps(INNER)},
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in self.values))
        self.source = JsonlFile(path)
        self.addCleanup(self.source.close)
        self.app = JlvApp(self.source)
        self.bells = 0

    def lines(self):
        return [line for line, _ in self.app._document.lines]

    async def focus_payload(self, pilot):
        await pilot.press("tab")
        right = self.app.query_one("#detail-list", RowView)
        row = next(i for i, line in enumerate(self.lines()) if '"payload"' in line)
        right.go_to_line(row)
        await pilot.pause()
        return right, row

    def count_bells(self):
        def bell():
            self.bells += 1
        self.app.bell = bell

    async def test_repeated_decode_and_undo(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            self.count_bells()
            right, row = await self.focus_payload(pilot)
            original = self.lines()
            await pilot.press(">")
            self.assertEqual(self.app._document.value["payload"], json.dumps(INNER))
            self.assertEqual(right.index, row)
            await pilot.press(">")
            self.assertEqual(self.app._document.value["payload"], INNER)
            self.assertEqual(right.index, row)
            self.assertEqual(self.lines()[row], '  "payload": {')
            self.assertEqual(self.bells, 0)
            await pilot.press(">")
            self.assertEqual(self.bells, 1)
            await pilot.press("<", "<")
            self.assertEqual(self.lines(), original)
            self.assertEqual(right.index, row)
            await pilot.press("<")
            self.assertEqual(self.bells, 2)

    async def test_plain_strings_and_left_panel_ring_bell(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            self.count_bells()
            await pilot.press(">")
            self.assertEqual(self.bells, 1)
            await pilot.press("tab")
            right = self.app.query_one("#detail-list", RowView)
            right.go_to_line(next(i for i, line in enumerate(self.lines()) if '"note"' in line))
            original = self.lines()
            await pilot.press(">")
            self.assertEqual(self.bells, 2)
            self.assertEqual(self.lines(), original)

    async def test_changing_records_resets_decoding(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            await self.focus_payload(pilot)
            original = self.lines()
            await pilot.press(">", ">", "shift+tab", "j", "k")
            self.assertEqual(self.app._decoded, [])
            self.assertEqual(self.lines(), original)

    async def test_inspector_shows_decoded_strings(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            right, row = await self.focus_payload(pilot)
            await pilot.press(">", ">")
            right.go_to_line(next(i for i, line in enumerate(self.lines()) if "needle" in line))
            await pilot.press("enter")
            self.assertIsInstance(self.app.screen, StringModal)
            self.assertEqual(self.app.screen.value, "needle")

    async def test_search_restores_original_record(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            await self.focus_payload(pilot)
            original = self.lines()
            await pilot.press(">", "/", *"plain", "enter")
            bar = self.app.screen.query_one(SearchBar)
            while bar.searching:
                await pilot.pause()
            await pilot.pause()
            self.assertEqual(self.app._decoded, [])
            self.assertEqual(self.lines(), original)
            right = self.app.query_one("#detail-list", RowView)
            self.assertIn("plain", original[right.find_selection.start.y])

    async def test_decode_hint_follows_selected_row_and_focus(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            rack = self.app.query_one("#hint-rack")
            right = self.app.query_one("#detail-list", RowView)
            self.assertFalse(rack.display)
            right.go_to_line(next(i for i, line in enumerate(self.lines()) if '"payload"' in line))
            await pilot.pause()
            # The JSON preview isn't focused yet.
            self.assertFalse(rack.display)
            right, row = await self.focus_payload(pilot)
            self.assertTrue(rack.display)
            await pilot.press("k")
            self.assertFalse(rack.display)
            await pilot.press("j")
            self.assertTrue(rack.display)
            await pilot.press("escape")
            self.assertFalse(rack.display)
            await pilot.press("tab")
            self.assertTrue(rack.display)
            await pilot.press("enter")
            self.assertFalse(self.app.screen_stack[0].query_one("#hint-rack").display)
            await pilot.press("escape")
            self.assertTrue(rack.display)
            # After decoding, the cursor is on a string that is encoded once more.
            await pilot.press(">")
            self.assertTrue(rack.display)
            await pilot.press(">")
            self.assertFalse(rack.display)
            await pilot.press("<")
            self.assertTrue(rack.display)

    async def test_hint_does_not_resize_panels(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            right = self.app.query_one("#detail-list", RowView)
            height = right.size.height
            await self.focus_payload(pilot)
            self.assertTrue(self.app.query_one("#hint-rack").display)
            self.assertEqual(right.size.height, height)


class StringRecordHintTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "data.jsonl"
        path.write_text(json.dumps(json.dumps({"a": "needle"})) + "\n")
        self.source = JsonlFile(path)
        self.addCleanup(self.source.close)
        self.app = JlvApp(self.source)

    async def test_search_restoring_row_zero_string_refreshes_hint(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            rack = self.app.query_one("#hint-rack")
            await pilot.press("enter")
            self.assertTrue(rack.display)
            await pilot.press("/", *"needle", "enter")
            bar = self.app.screen.query_one(SearchBar)
            while bar.searching:
                await pilot.pause()
            await pilot.pause()
            await pilot.press(">")
            self.assertFalse(rack.display)
            # The match is on row 0 of the restored record, where the cursor already is.
            await pilot.press("n")
            while bar.searching:
                await pilot.pause()
            await pilot.pause()
            self.assertEqual(self.app._decoded, [])
            self.assertEqual(self.app.query_one("#detail-list", RowView).index, 0)
            self.assertTrue(rack.display)

class EscapeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "data.jsonl"
        path.write_text(json.dumps({"text": "needle", "payload": json.dumps(INNER)}) + "\n")
        self.source = JsonlFile(path)
        self.addCleanup(self.source.close)
        self.app = JlvApp(self.source)

    async def test_escape_backs_out_one_level_at_a_time(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            left = self.app.query_one("#lines-list", RowView)
            right = self.app.query_one("#detail-list", RowView)
            await pilot.press("tab")
            right.go_to_line(1)
            await pilot.press("enter")
            self.assertIsInstance(self.app.screen, StringModal)
            await pilot.press("/")
            self.assertTrue(self.app.screen.query_one(SearchBar).display)
            await pilot.press("escape")
            self.assertIsInstance(self.app.screen, StringModal)
            self.assertFalse(self.app.screen.query_one(SearchBar).display)
            await pilot.press("escape")
            self.assertNotIsInstance(self.app.screen, StringModal)
            self.assertIs(self.app.focused, right)
            await pilot.press("/")
            await pilot.press("escape")
            self.assertFalse(self.app.query_one(SearchBar).display)
            self.assertIs(self.app.focused, right)
            await pilot.press("escape")
            self.assertIs(self.app.focused, left)
            await pilot.press("escape")
            self.assertIs(self.app.focused, left)
            self.assertTrue(self.app.is_running)

    async def test_escape_from_right_keeps_decoded_view(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            right = self.app.query_one("#detail-list", RowView)
            await pilot.press("tab")
            right.go_to_line(2)
            await pilot.press(">", "escape")
            self.assertIs(self.app.focused, self.app.query_one("#lines-list", RowView))
            self.assertEqual(self.app._document.value["payload"], INNER)
            await pilot.press("tab")
            self.assertEqual(right.index, 2)

    async def test_enter_drills_down_from_record_list(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            left = self.app.query_one("#lines-list", RowView)
            right = self.app.query_one("#detail-list", RowView)
            bells = []
            self.app.bell = lambda: bells.append(1)
            await pilot.click("#lines-list", offset=(2, 0))
            self.assertIs(self.app.focused, left)
            await pilot.press("enter")
            self.assertIs(self.app.focused, right)
            right.go_to_line(1)
            await pilot.press("enter")
            self.assertIsInstance(self.app.screen, StringModal)
            await pilot.press("escape", "escape")
            self.assertIs(self.app.focused, left)
            self.assertEqual(bells, [])


class InspectorDecodeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "data.jsonl"
        self.envelope = {
            "a": json.dumps(json.dumps({"x": 1})), "b": json.dumps([1, 2]), "c": "plain", "d": 5,
            "e": {"nested": json.dumps({"no": 1})},
        }
        record = {"payload": json.dumps(json.dumps(INNER)), "text": "plain", "envelope": json.dumps(self.envelope)}
        path.write_text(json.dumps(record) + "\n")
        self.source = JsonlFile(path)
        self.addCleanup(self.source.close)
        self.app = JlvApp(self.source)

    async def open_inspector(self, pilot, row):
        await pilot.press("tab")
        self.app.query_one("#detail-list", RowView).go_to_line(row)
        await pilot.press("enter")
        await pilot.pause()
        self.assertIsInstance(self.app.screen, StringModal)
        return self.app.screen

    async def test_decodes_layers_then_pretty_prints_and_undoes(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            bells = []
            self.app.bell = lambda: bells.append(1)
            modal = await self.open_inspector(pilot, 1)
            view = modal.query_one(StringView)
            original = view.text
            await pilot.press(">")
            self.assertEqual(view.text, json.dumps(INNER))
            await pilot.press(">")
            pretty = json.dumps(INNER, indent=2)
            self.assertEqual(view.text, pretty)
            self.assertEqual(bells, [])
            await pilot.press(">")
            self.assertEqual(bells, [1])
            self.assertEqual(view.text, pretty)
            await pilot.press("/", *"needle", "enter")
            bar = modal.query_one(SearchBar)
            while bar.searching:
                await pilot.pause()
            await pilot.pause()
            self.assertEqual(view.selected_text, "needle")
            await pilot.press("escape", "<", "<")
            self.assertEqual(view.text, original)
            await pilot.press("<")
            self.assertEqual(bells, [1, 1])

    async def test_plain_string_rings_bell(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            bells = []
            self.app.bell = lambda: bells.append(1)
            modal = await self.open_inspector(pilot, 2)
            await pilot.press(">")
            self.assertEqual(bells, [1])
            self.assertEqual(modal.query_one(StringView).text, "plain")


    async def test_repeated_decode_expands_strings_one_layer_at_a_time(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            bells = []
            self.app.bell = lambda: bells.append(1)
            modal = await self.open_inspector(pilot, 3)
            view = modal.query_one(StringView)
            states = [view.text]

            async def decode():
                await pilot.press(">")
                states.append(view.text)
                return json.loads(view.text)

            self.assertEqual(await decode(), self.envelope)
            expected = {
                **self.envelope, "a": json.dumps({"x": 1}), "b": [1, 2], "e": {"nested": {"no": 1}},
            }
            self.assertEqual(await decode(), expected)
            expected["a"] = {"x": 1}
            self.assertEqual(await decode(), expected)
            self.assertEqual(bells, [])
            await pilot.press(">")
            self.assertEqual(bells, [1])
            self.assertEqual(view.text, states[-1])
            for state in reversed(states[:-1]):
                await pilot.press("<")
                self.assertEqual(view.text, state)

    async def test_decoded_field_reveals_nested_encoded_array(self):
        chain = json.dumps({"body": json.dumps({"text": json.dumps(["a", {"c": json.dumps({"deep": True})}])})})
        async with self.app.run_test(size=(100, 30)) as pilot:
            await self.open_inspector(pilot, 2)
            modal = self.app.screen
            modal._show(chain, None)
            view = modal.query_one(StringView)
            for _ in range(4):
                await pilot.press(">")
            self.assertEqual(json.loads(view.text), {"body": {"text": ["a", {"c": {"deep": True}}]}})

    async def test_border_hint_tracks_what_can_be_decoded(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            modal = await self.open_inspector(pilot, 1)
            border = modal.query_one("#inspector")
            self.assertEqual(border.border_subtitle, "Press > to decode")
            await pilot.press(">")
            self.assertEqual(border.border_subtitle, "Press > to decode · < to undo")
            await pilot.press(">")
            self.assertEqual(border.border_subtitle, "Press < to undo")
            await pilot.press("<", "<")
            self.assertEqual(border.border_subtitle, "Press > to decode")
            await pilot.press("escape")
            self.app.query_one("#detail-list", RowView).go_to_line(2)
            await pilot.press("enter")
            self.assertEqual(self.app.screen.query_one("#inspector").border_subtitle, "")


class FocusHighlightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "data.jsonl"
        path.write_text(json.dumps({"a": 1}) + "\n")
        self.source = JsonlFile(path)
        self.addCleanup(self.source.close)
        self.app = JlvApp(self.source)

    def highlight(self, selector):
        """The row highlight's background, or None when it matches the panel's."""
        view = self.app.query_one(selector, RowView)
        color = view.get_component_rich_style("rowview--highlight").bgcolor
        return None if color == view.rich_style.bgcolor else color

    async def test_highlight_follows_focus(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            focused_left = self.highlight("#lines-list")
            self.assertIsNotNone(focused_left)
            self.assertIsNone(self.highlight("#detail-list"))
            await pilot.press("tab")
            dimmed_left = self.highlight("#lines-list")
            self.assertIsNotNone(dimmed_left)
            self.assertNotEqual(dimmed_left, focused_left)
            self.assertEqual(self.highlight("#detail-list"), focused_left)
            await pilot.press("escape")
            self.assertEqual(self.highlight("#lines-list"), focused_left)
            self.assertIsNone(self.highlight("#detail-list"))
