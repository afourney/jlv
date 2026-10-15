from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jlv import JlvApp, NativePreviewWarning, StringModal, StringView
from jlv.data_uri import SIGNATURE_BYTES, DataURI, NativePreview, native_launcher
from jlv.rows import RowView
from jlv.source import JsonlFile

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


class DataURITests(unittest.TestCase):
    def uri(self, value):
        uri = DataURI.parse(value)
        self.assertIsNotNone(uri)
        return uri

    def test_mime_precedes_signature_and_unknown_falls_back(self):
        payload = base64.b64encode(PNG).decode()
        with patch("jlv.data_uri.filetype.guess") as guess:
            self.assertEqual(self.uri("data:text/plain;base64," + payload).extension(), ".txt")
            self.assertEqual(self.uri("data:image/jpeg;base64," + payload).extension(), ".jpg")
            self.assertEqual(
                self.uri("data:application/x-jlv-unknown," + payload).extension(), ".txt"
            )
            guess.assert_not_called()

    def test_omitted_mime_sniffs_or_falls_back(self):
        self.assertEqual(
            self.uri("data:;base64," + base64.b64encode(PNG).decode()).extension(), ".png"
        )
        self.assertEqual(self.uri("data:;charset=UTF-8,hello").extension(), ".txt")
        # Actual filetype TAR matcher needs byte 261, beyond the README's 261 bytes.
        tar = b"\0" * 257 + b"ustar" + b"\0" * 50
        self.assertEqual(
            self.uri("data:;base64," + base64.b64encode(tar).decode()).extension(), ".tar"
        )

    def test_encoding_forms(self):
        for value, expected in [
            ("data:,hello%20world+!", b"hello world+!"),
            ("DATA:text/plain;charset=utf-8;BASE64,aGk%3D", b"hi"),
            ("data:application/octet-stream,%00%FF", b"\0\xff"),
            ("data:,", b""),
            ("data:;base64,", b""),
        ]:
            self.assertEqual(self.uri(value).decode(), expected)

    def test_malformed(self):
        for value in ["data:bad,x", "data:;base64", "data:;base64;foo=bar,AA==", "data:;oops,x"]:
            self.assertIsNone(DataURI.parse(value))
        for payload in ["a", "!!!!", "AA=", "AA==extra", "%GG", "%", "é"]:
            with self.assertRaises(ValueError):
                self.uri("data:;base64," + payload).decode()
        with self.assertRaises(ValueError):
            self.uri("data:,%z0").decode()

    def test_multimegabyte_prefix_is_bounded(self):
        for payload in [
            base64.b64encode(PNG + b"x" * 4_000_000).decode(),
            base64.b64encode(PNG + b"x" * 4_000_000).decode().replace("A", "%41"),
        ]:
            uri = self.uri("data:;base64," + payload)
            with patch("jlv.data_uri.base64.b64decode", wraps=base64.b64decode) as decode:
                with patch(
                    "jlv.data_uri.filetype.guess", wraps=__import__("filetype").guess
                ) as guess:
                    self.assertEqual(uri.extension(), ".png")
                    self.assertLessEqual(len(guess.call_args.args[0]), SIGNATURE_BYTES)
                self.assertLessEqual(
                    len(decode.call_args.args[0]), ((SIGNATURE_BYTES + 2) // 3) * 4
                )
        uri = self.uri("data:," + "%41" * 2_000_000)
        self.assertEqual(len(uri.prefix()), SIGNATURE_BYTES)
        preview = NativePreview()
        preview.launcher = "test"
        with (
            patch.object(DataURI, "decode", side_effect=AssertionError),
            patch.object(DataURI, "prefix", side_effect=AssertionError),
        ):
            self.assertTrue(preview.available(uri.value))

    def test_platform_availability(self):
        for platform, command in [("linux", "xdg-open"), ("darwin", "open")]:
            with (
                patch("jlv.data_uri.sys.platform", platform),
                patch("jlv.data_uri.shutil.which", return_value="/bin/" + command) as which,
            ):
                self.assertEqual(native_launcher(), "/bin/" + command)
                which.assert_called_once_with(command)
            with (
                patch("jlv.data_uri.sys.platform", platform),
                patch("jlv.data_uri.shutil.which", return_value=None),
            ):
                self.assertIsNone(native_launcher())
        with (
            patch("jlv.data_uri.sys.platform", "win32"),
            patch("jlv.data_uri.os.startfile", Mock(), create=True),
        ):
            self.assertEqual(native_launcher(), "startfile")
        with (
            patch("jlv.data_uri.sys.platform", "win32"),
            patch("jlv.data_uri.os.startfile", None, create=True),
        ):
            self.assertIsNone(native_launcher())
        with patch("jlv.data_uri.sys.platform", "other"):
            self.assertIsNone(native_launcher())


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.preview = NativePreview()
        self.preview.launcher = "/bin/test-opener"
        self.addCleanup(self.preview.cleanup)
        self.failed = Mock()

    def test_launch_arguments_output_and_cleanup(self):
        for status in [0, 1]:
            child = Mock()
            child.wait.return_value = status
            with patch("jlv.data_uri.subprocess.Popen", return_value=child) as popen:
                self.preview._open("data:text/plain,hello%20world", self.failed)
            args, kwargs = popen.call_args
            self.assertEqual(args[0][0], "/bin/test-opener")
            self.assertEqual(Path(args[0][1]).read_bytes(), b"hello world")
            self.assertTrue(args[0][1].endswith(".txt"))
            self.assertNotIn("shell", kwargs)
            for stream in ["stdin", "stdout", "stderr"]:
                self.assertEqual(kwargs[stream], subprocess.DEVNULL)
            child.wait.assert_called_once_with(timeout=0.5)
        self.failed.assert_called_once()
        directory = self.preview.directory
        self.preview.cleanup()
        self.assertFalse(Path(directory).exists())
        self.preview._open("data:,again", self.failed)
        self.assertFalse(Path(directory).exists())

    def test_errors_report_failure(self):
        self.preview._open("data:;base64,invalid!", self.failed)
        with patch("jlv.data_uri.subprocess.Popen", side_effect=OSError):
            self.preview._open("data:,ok", self.failed)
        self.assertEqual(self.failed.call_count, 2)

    def test_windows_exception_and_locked_cleanup(self):
        self.preview.launcher = "startfile"
        with patch("jlv.data_uri.os.startfile", create=True) as start:
            self.preview._open("data:,ok", self.failed)
            start.assert_called_once()
            self.assertTrue(Path(start.call_args.args[0]).is_absolute())
        with patch("jlv.data_uri.os.startfile", side_effect=OSError, create=True):
            self.preview._open("data:,ok", self.failed)
        self.failed.assert_called_once()
        with patch("jlv.data_uri.shutil.rmtree") as remove:
            self.preview.cleanup()
            remove.assert_called_once_with(self.preview.directory, ignore_errors=True)

    def test_slow_launcher_reaped_without_failure_or_kill(self):
        reaped = threading.Event()
        child = Mock()

        def wait(timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("viewer", timeout)
            reaped.set()
            return 1

        child.wait.side_effect = wait
        with patch("jlv.data_uri.subprocess.Popen", return_value=child):
            self.preview._open("data:,ok", self.failed)
        self.assertTrue(reaped.wait(1))
        self.failed.assert_not_called()
        child.kill.assert_not_called()
        child.terminate.assert_not_called()

    def test_real_child_exit_and_reaping(self):
        # Synthetic launcher commands exercise the real process lifecycle on this host.
        real_popen = subprocess.Popen
        children = []

        def spawn(args, **kwargs):
            child = real_popen([sys.executable, "-c", "import sys; sys.exit(7)"], **kwargs)
            children.append(child)
            return child

        with patch("jlv.data_uri.subprocess.Popen", side_effect=spawn):
            self.preview._open("data:,test", self.failed)
        self.failed.assert_called_once()
        self.assertEqual(children[0].returncode, 7)


class PreviewPolicyTests(unittest.TestCase):
    def info(self, mime, payload):
        uri = DataURI.parse(f"data:{mime};base64," + base64.b64encode(payload).decode())
        return uri.preview_info()

    def test_real_media_signatures_and_plain_text(self):
        for mime, payload, extension in [
            ("image/png", PNG, ".png"),
            ("", PNG, ".png"),
            ("image/apng", PNG, ".png"),
            ("image/pjpeg", b"\xff\xd8\xff\xdb" + b"\0" * 32, ".jpg"),
            ("image/gif", b"GIF89a" + b"\0" * 32, ".gif"),
            ("audio/x-wav", b"RIFF" + b"\0" * 4 + b"WAVE" + b"\0" * 32, ".wav"),
            ("audio/ogg", b"OggS" + b"\0" * 32, ".ogg"),
            ("video/mp4", b"\0\0\0\x18ftypisom" + b"\0" * 20, ".mp4"),
            ("text/plain", "Hello, world! café".encode(), ".txt"),
            ("", b"hello", ".txt"),
            ("", b"", ".txt"),
        ]:
            with self.subTest(mime=mime, extension=extension):
                info = self.info(mime, payload)
                self.assertIsNone(info.warning)
                self.assertEqual(info.extension, extension)

    def test_active_unknown_and_mismatched_contents_require_consent(self):
        for mime, payload in [
            ("", b"MZ" + b"\0" * 32),
            ("application/hta", b"<script>example</script>"),
            ("text/html", b"<html>example</html>"),
            ("image/svg+xml", b"<svg></svg>"),
            ("application/pdf", b"%PDF-1.7"),
            ("application/postscript", b"%!PS-Adobe"),
            ("application/zip", b"PK\x03\x04" + b"\0" * 32),
            ("application/msword", b"example"),
            ("image/x-unknown", PNG),
            ("application/octet-stream", PNG),
            ("image/jpeg", PNG),
            ("image/png", b"MZ" + b"\0" * 32),
            ("text/plain", b"MZ" + b"\0" * 32),
            ("image/png", b"not an image"),
            ("", b"\0\0\0\0"),
            ("", b"\xf0"),
            ("text/plain", b"#!/bin/sh\nexample"),
            ("text/plain", b"[Desktop Entry]\nExec=example"),
            ("text/plain", b"<html>example</html>"),
            ("text/plain", b"@echo off\nexample"),
        ]:
            with self.subTest(mime=mime, payload=payload[:20]):
                self.assertIsNotNone(self.info(mime, payload).warning)

    def test_os_mime_overrides_cannot_change_passive_extensions(self):
        with patch("jlv.data_uri.mimetypes.guess_extension", return_value=".exe") as guess:
            info = self.info("image/png", PNG)
            self.assertEqual(info.extension, ".png")
            self.assertIsNone(info.warning)
            guess.assert_not_called()

    def test_inspection_bounds_and_utf8_boundary(self):
        uri = DataURI.parse(
            "data:image/png;base64," + base64.b64encode(PNG + b"x" * 4_000_000).decode()
        )
        with (
            patch.object(DataURI, "decode", side_effect=AssertionError),
            patch(
                "jlv.data_uri.filetype.guess",
                wraps=__import__("filetype").guess,
            ) as guess,
        ):
            self.assertIsNone(uri.preview_info().warning)
            self.assertLessEqual(len(guess.call_args.args[0]), SIGNATURE_BYTES)
        self.assertIsNone(
            self.info("text/plain", b"a" * (SIGNATURE_BYTES - 1) + "é".encode()).warning
        )

    def test_all_launchers_fail_closed_or_request_consent_before_decoding(self):
        payloads = [
            "data:;base64," + base64.b64encode(b"MZ" + b"\0" * 32).decode(),
            "data:application/hta,example",
        ]
        for launcher in ["startfile", "/bin/xdg-open", "/usr/bin/open"]:
            for payload in payloads:
                with self.subTest(launcher=launcher, payload=payload):
                    preview = NativePreview()
                    preview.launcher = launcher
                    self.addCleanup(preview.cleanup)
                    failed, confirm = Mock(), Mock()
                    with (
                        patch("jlv.data_uri.os.startfile", create=True) as start,
                        patch(
                            "jlv.data_uri.subprocess.Popen",
                        ) as spawn,
                        patch.object(DataURI, "decode", side_effect=AssertionError),
                    ):
                        preview._open(payload, failed)
                        failed.assert_called_once()
                        preview._open(payload, failed, confirm)
                        confirm.assert_called_once()
                        info, allow = confirm.call_args.args
                        self.assertIsNotNone(info.warning)
                        self.assertTrue(callable(allow))
                        start.assert_not_called()
                        spawn.assert_not_called()
                        self.assertIsNone(preview.directory)

    def test_approval_after_exit_does_not_decode_or_launch(self):
        preview = NativePreview()
        preview.launcher = "startfile"
        failed, confirm = Mock(), Mock()
        preview._open("data:application/hta,example", failed, confirm)
        info, allow = confirm.call_args.args
        self.assertIsNotNone(info.warning)
        preview.cleanup()
        finished = threading.Event()
        real_launch = preview._launch

        def launch(*args):
            try:
                real_launch(*args)
            finally:
                finished.set()

        with (
            patch.object(preview, "_launch", side_effect=launch),
            patch.object(
                DataURI,
                "decode",
                side_effect=AssertionError,
            ),
            patch("jlv.data_uri.os.startfile", create=True) as start,
        ):
            allow()
            self.assertTrue(finished.wait(1))
            start.assert_not_called()
            failed.assert_not_called()
            self.assertIsNone(preview.directory)


class PreviewAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_recursive_detector_failure_rings_once_without_prompt_or_launch(self):
        payload = bytes.fromhex("502a4d1800000000") * 1024
        for mime in ("", "text/plain"):
            with self.subTest(mime=mime), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "synthetic.jsonl"
                value = f"data:{mime};base64," + base64.b64encode(payload).decode()
                path.write_text(json.dumps(value) + "\n")
                with JsonlFile(path) as source:
                    app = JlvApp(source)
                    app._native_preview.launcher = "/test/open"
                    rang = asyncio.Event()
                    app.bell = Mock(side_effect=rang.set)
                    with (
                        patch.object(app, "_native_confirmation") as confirm,
                        patch.object(DataURI, "decode") as decode,
                        patch("jlv.data_uri.subprocess.Popen") as spawn,
                        patch("jlv.data_uri.os.startfile", create=True) as start,
                        patch("threading.excepthook") as uncaught,
                    ):
                        async with app.run_test(size=(100, 30)) as pilot:
                            await pilot.press("tab", ">")
                            await asyncio.wait_for(rang.wait(), timeout=2)
                            await pilot.pause()
                            app.bell.assert_called_once_with()
                            confirm.assert_not_called()
                            decode.assert_not_called()
                            spawn.assert_not_called()
                            start.assert_not_called()
                            uncaught.assert_not_called()
                            self.assertIsNone(app._native_preview.directory)

    async def test_decode_does_not_block_ui_or_recreate_files_after_exit(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def slow_decode(uri):
            entered.set()
            release.wait(5)
            finished.set()
            return b"hello"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            path.write_text(json.dumps("data:,hello") + "\n")
            with JsonlFile(path) as source:
                app = JlvApp(source)
                app._native_preview.launcher = "/test/open"
                with (
                    patch.object(DataURI, "decode", slow_decode),
                    patch("jlv.data_uri.subprocess.Popen") as spawn,
                ):
                    try:
                        async with app.run_test(size=(100, 30)) as pilot:
                            await pilot.press("tab", ">")
                            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                            await pilot.press("escape")
                            self.assertIs(app.focused, app.query_one("#lines-list", RowView))
                        self.assertTrue(app._native_preview._closed)
                    finally:
                        release.set()
                    self.assertTrue(await asyncio.to_thread(finished.wait, 1))
                    await asyncio.sleep(0.05)
                    spawn.assert_not_called()
                    self.assertIsNone(app._native_preview.directory)

    async def test_nonzero_exit_and_malformed_payload_ring_bell(self):
        for value in ["data:,hello", "data:;base64,bad!"]:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "synthetic.jsonl"
                path.write_text(json.dumps(value) + "\n")
                with JsonlFile(path) as source:
                    app = JlvApp(source)
                    app._native_preview.launcher = "/test/open"
                    bells = []
                    app.bell = lambda bells=bells: bells.append(1)
                    child = Mock()
                    child.wait.return_value = 3
                    with patch("jlv.data_uri.subprocess.Popen", return_value=child):
                        async with app.run_test(size=(100, 30)) as pilot:
                            await pilot.press("tab", ">")
                            await pilot.pause()
                            self.assertEqual(bells, [1])

    async def test_hint_shortcut_bell_modal_and_exit_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            path.write_text(json.dumps({"payload": "data:text/plain,hello"}) + "\n")
            with JsonlFile(path) as source:
                app = JlvApp(source)
                app._native_preview.launcher = "/test/open"
                bells = []
                app.bell = lambda bells=bells: bells.append(1)
                with patch("jlv.data_uri.subprocess.Popen", side_effect=OSError) as spawn:
                    async with app.run_test(size=(100, 30)) as pilot:
                        await pilot.press("tab")
                        app.query_one("#detail-list", RowView).go_to_line(1)
                        await pilot.pause()
                        self.assertTrue(app.query_one("#hint-rack").display)
                        spawn.assert_not_called()
                        await pilot.press(">")
                        await pilot.pause()
                        self.assertEqual(bells, [1])
                        self.assertEqual(app._decoded, [])
                        await pilot.press("enter")
                        self.assertEqual(
                            app.screen.query_one("#inspector").border_subtitle,
                            "Press > to open natively",
                        )
                        await pilot.press(">")
                        await pilot.pause()
                        self.assertEqual(bells, [1, 1])
                        temp = app._native_preview.directory
                        await pilot.press("escape")
                        app._native_preview.launcher = None
                        app._update_hint()
                        self.assertFalse(app.query_one("#hint-rack").display)
                    self.assertFalse(Path(temp).exists())


class WarningAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "synthetic.jsonl"
        self.payload = b"MZ" + b"\0" * 32
        value = "data:;base64," + base64.b64encode(self.payload).decode()
        path.write_text(json.dumps({"payload": value}) + "\n")
        self.source = JsonlFile(path)
        self.addCleanup(self.source.close)
        self.app = JlvApp(self.source)
        self.app._native_preview.launcher = "startfile"
        self.launched = threading.Event()
        start = patch(
            "jlv.data_uri.os.startfile", side_effect=lambda path: self.launched.set(), create=True
        )
        self.start = start.start()
        self.addCleanup(start.stop)

    async def focus_payload(self, pilot):
        await pilot.press("tab")
        right = self.app.query_one("#detail-list", RowView)
        right.go_to_line(1)
        await pilot.pause()
        return right

    async def warning(self, pilot):
        for _ in range(100):
            if isinstance(self.app.screen, NativePreviewWarning):
                return self.app.screen
            await pilot.pause(0.01)
        self.fail("No native-opening warning appeared")

    async def test_cancel_button_enter_and_escape_never_decode_or_launch(self):
        with patch.object(DataURI, "decode", side_effect=AssertionError):
            async with self.app.run_test(size=(100, 30)) as pilot:
                right = await self.focus_payload(pilot)
                for cancel in ["enter", "escape", "button"]:
                    with self.subTest(cancel=cancel):
                        await pilot.press(">")
                        warning = await self.warning(pilot)
                        self.assertEqual(self.app.focused.id, "native-cancel")
                        self.assertEqual(warning.info.extension, ".exe")
                        self.assertEqual(warning.info.detected_mime, "application/x-msdownload")
                        if cancel == "button":
                            await pilot.click("#native-cancel")
                        else:
                            await pilot.press(cancel)
                        self.assertIs(self.app.focused, right)
                        self.start.assert_not_called()
                        self.assertIsNone(self.app._native_preview.directory)
                        self.assertEqual(self.app._decoded, [])

    async def test_allow_launches_exact_payload_and_asks_again_next_time(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            right = await self.focus_payload(pilot)
            await pilot.press(">")
            await self.warning(pilot)
            self.start.assert_not_called()
            self.assertIsNone(self.app._native_preview.directory)
            await pilot.press("shift+tab")
            self.assertEqual(self.app.focused.id, "native-allow")
            await pilot.press("enter")
            self.assertTrue(await asyncio.to_thread(self.launched.wait, 1))
            self.start.assert_called_once()
            path = Path(self.start.call_args.args[0])
            self.assertEqual(path.suffix, ".exe")
            self.assertEqual(path.read_bytes(), self.payload)
            self.assertIs(self.app.focused, right)
            await pilot.press(">")
            await self.warning(pilot)
            await pilot.press("escape")
            self.start.assert_called_once()
        self.assertFalse(path.exists())

    async def test_string_inspector_warning_restores_focus_on_cancel_and_allow(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            await self.focus_payload(pilot)
            await pilot.press("enter")
            inspector = self.app.screen
            self.assertIsInstance(inspector, StringModal)
            await pilot.press(">")
            await self.warning(pilot)
            await pilot.press("escape")
            self.assertIs(self.app.screen, inspector)
            self.assertIsInstance(self.app.focused, StringView)
            self.start.assert_not_called()
            await pilot.press(">")
            await self.warning(pilot)
            await pilot.click("#native-allow")
            self.assertTrue(await asyncio.to_thread(self.launched.wait, 1))
            self.assertIs(self.app.screen, inspector)
            self.assertIsInstance(self.app.focused, StringView)
            self.assertEqual(inspector._decoded, [])

    async def test_exit_with_pending_warning_never_creates_file(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            await self.focus_payload(pilot)
            await pilot.press(">")
            await self.warning(pilot)
        self.assertTrue(self.app._native_preview._closed)
        self.assertIsNone(self.app._native_preview.directory)
        self.start.assert_not_called()
