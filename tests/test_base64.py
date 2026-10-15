from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jlv import JlvApp, NativePreviewWarning, StringModal
from jlv.data_uri import BASE64_PROBE_CHARS, SIGNATURE_BYTES, NativePreview, PlainBase64
from jlv.rows import RowView
from jlv.source import JsonlFile

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


class PlainBase64Tests(unittest.TestCase):
    def test_padded_unpadded_and_wrapped_payloads(self):
        for payload in [PNG, b"Hello, base64 world!"]:
            encoded = base64.b64encode(payload).decode()
            for value in [
                encoded,
                encoded.rstrip("="),
                " \n"
                + "\r\n".join(encoded[index : index + 12] for index in range(0, len(encoded), 12))
                + "\t",
            ]:
                with self.subTest(value=value):
                    decoded = PlainBase64.parse(value)
                    self.assertIsNotNone(decoded)
                    self.assertEqual(decoded.decode(), payload)
                    self.assertEqual(decoded.prefix(), payload)

    def test_rejects_ordinary_text_json_hex_and_other_encodings(self):
        for value in [
            "",
            "hello",
            "ordinarywordwithoutspaces",
            "UPPERCASEWORDWITHOUTSPACES",
            "ordinary words with spaces",
            "0123456789abcdef" * 4,
            '"SGVsbG8="',
            '{"encoded": "SGVsbG8="}',
            '["SGVsbG8="]',
            "https://example.com/image.png",
            "data:image/png;base64,example",
            "YWJjZA%3D%3D",
            "YWJjZA===",
            "YWJjZA==!",
            "YWJj_ZA==",
            "é" * 32,
        ]:
            with self.subTest(value=value):
                self.assertIsNone(PlainBase64.parse(value))

    def test_short_padded_text_is_supported_but_short_words_are_ambiguous(self):
        self.assertEqual(PlainBase64.parse("SGVsbG8=").decode(), b"Hello")
        self.assertIsNone(PlainBase64.parse("TWFu"))
        self.assertIsNone(PlainBase64.parse("SGVsbG8"))

    def test_inferred_media_text_and_active_content_share_existing_policy(self):
        for payload, extension, warned in [
            (PNG, ".png", False),
            (b"Hello, base64 world!", ".txt", False),
            (b"MZ" + b"\0" * 32, ".exe", True),
            (b"<svg><script>example</script></svg>", ".txt", True),
            (b"#!/bin/sh\nexample", ".txt", True),
        ]:
            with self.subTest(extension=extension, warned=warned):
                encoded = PlainBase64.parse(base64.b64encode(payload).decode())
                info = encoded.preview_info()
                self.assertEqual(info.extension, extension)
                self.assertEqual(info.warning is not None, warned)

    def test_hint_and_decoded_prefix_are_bounded_for_large_strings(self):
        class TrackedString(str):
            largest_slice = 0

            def __getitem__(self, key):
                result = super().__getitem__(key)
                if isinstance(key, slice):
                    self.largest_slice = max(self.largest_slice, len(result))
                return result

        value = TrackedString(base64.b64encode(PNG + b"x" * 4_000_000).decode())
        preview = NativePreview()
        preview.launcher = "/test/open"
        with (
            patch.object(PlainBase64, "decode", side_effect=AssertionError),
            patch.object(
                PlainBase64,
                "prefix",
                side_effect=AssertionError,
            ),
        ):
            self.assertTrue(preview.available(value))
        self.assertLessEqual(value.largest_slice, BASE64_PROBE_CHARS)
        encoded = PlainBase64.parse(value)
        with patch("jlv.data_uri._detect_type", wraps=__import__("filetype").guess) as detect:
            self.assertEqual(encoded.preview_info().extension, ".png")
            self.assertLessEqual(len(detect.call_args.args[0]), SIGNATURE_BYTES)
        self.assertLessEqual(value.largest_slice, ((SIGNATURE_BYTES + 2) // 3) * 8)

    def test_malformed_middle_fails_full_validation_before_file_or_launch(self):
        value = base64.b64encode(PNG + b"x" * 30_000).decode()
        value = value[:20_000] + "!" + value[20_001:]
        preview = NativePreview()
        preview.launcher = "/test/open"
        self.addCleanup(preview.cleanup)
        failed, confirm = Mock(), Mock()
        self.assertTrue(preview.available(value))
        with patch("jlv.data_uri.subprocess.Popen") as spawn:
            preview._open(value, failed, confirm)
        failed.assert_called_once_with()
        confirm.assert_not_called()
        spawn.assert_not_called()
        self.assertIsNone(preview.directory)

    def test_detector_failure_fails_closed_for_plain_base64_too(self):
        value = base64.b64encode(bytes.fromhex("502a4d1800000000") * 1024).decode()
        preview = NativePreview()
        preview.launcher = "/test/open"
        failed, confirm = Mock(), Mock()
        with patch("jlv.data_uri.subprocess.Popen") as spawn:
            preview._open(value, failed, confirm)
        failed.assert_called_once_with()
        confirm.assert_not_called()
        spawn.assert_not_called()
        self.assertIsNone(preview.directory)


class PlainBase64AppTests(unittest.IsolatedAsyncioTestCase):
    def app(self, value):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "synthetic.jsonl"
        path.write_text(json.dumps({"payload": value}) + "\n")
        source = JsonlFile(path)
        self.addCleanup(source.close)
        app = JlvApp(source)
        app._native_preview.launcher = "/test/open"
        return app

    async def select(self, app, pilot):
        await pilot.press("tab")
        app.query_one("#detail-list", RowView).go_to_line(1)
        await pilot.pause()

    async def until(self, pilot, condition):
        for _ in range(100):
            if condition():
                return
            await pilot.pause(0.01)
        self.fail("Preview operation did not complete")

    async def test_media_opens_from_both_panels_and_cleans_up(self):
        app = self.app(base64.b64encode(PNG).decode())
        child = Mock()
        child.wait.return_value = 0
        with patch("jlv.data_uri.subprocess.Popen", return_value=child) as spawn:
            async with app.run_test(size=(100, 30)) as pilot:
                await self.select(app, pilot)
                self.assertTrue(app.query_one("#hint-rack").display)
                spawn.assert_not_called()
                await pilot.press(">")
                await self.until(pilot, lambda: spawn.call_count == 1)
                first = Path(spawn.call_args.args[0][1])
                self.assertEqual(first.suffix, ".png")
                self.assertEqual(first.read_bytes(), PNG)
                await pilot.press("enter")
                self.assertIsInstance(app.screen, StringModal)
                self.assertEqual(
                    app.screen.query_one("#inspector").border_subtitle, "Press > to open natively"
                )
                await pilot.press(">")
                await self.until(pilot, lambda: spawn.call_count == 2)
                self.assertEqual(Path(spawn.call_args.args[0][1]).read_bytes(), PNG)
                self.assertEqual(app._decoded, [])
            self.assertFalse(first.exists())

    async def test_executable_requires_cancel_or_explicit_allow(self):
        payload = b"MZ" + b"\0" * 32
        app = self.app(base64.b64encode(payload).decode())
        child = Mock()
        child.wait.return_value = 0
        with patch("jlv.data_uri.subprocess.Popen", return_value=child) as spawn:
            async with app.run_test(size=(100, 30)) as pilot:
                await self.select(app, pilot)
                with patch.object(PlainBase64, "decode", side_effect=AssertionError):
                    await pilot.press(">")
                    await self.until(pilot, lambda: isinstance(app.screen, NativePreviewWarning))
                    self.assertEqual(app.focused.id, "native-cancel")
                    self.assertEqual(app.screen.info.extension, ".exe")
                    await pilot.press("enter")
                    spawn.assert_not_called()
                    self.assertIsNone(app._native_preview.directory)
                await pilot.press("enter", ">")
                await self.until(pilot, lambda: isinstance(app.screen, NativePreviewWarning))
                await pilot.click("#native-allow")
                await self.until(pilot, lambda: spawn.called)
                self.assertEqual(Path(spawn.call_args.args[0][1]).read_bytes(), payload)
                self.assertIsInstance(app.screen, StringModal)

    async def test_json_decode_then_base64_preview_then_undo(self):
        encoded = base64.b64encode(PNG).decode()
        wrapped = json.dumps(encoded)
        app = self.app(wrapped)
        child = Mock()
        child.wait.return_value = 0
        with patch("jlv.data_uri.subprocess.Popen", return_value=child) as spawn:
            async with app.run_test(size=(100, 30)) as pilot:
                await self.select(app, pilot)
                await pilot.press(">")
                self.assertEqual(app._document.strings[1], encoded)
                spawn.assert_not_called()
                await pilot.press(">")
                await self.until(pilot, lambda: spawn.called)
                self.assertEqual(Path(spawn.call_args.args[0][1]).read_bytes(), PNG)
                await pilot.press("<")
                self.assertEqual(app._document.strings[1], wrapped)
                spawn.assert_called_once()
