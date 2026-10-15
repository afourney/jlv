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

from jlv import JlvApp
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
            self.assertEqual(self.uri("data:application/x-jlv-unknown," + payload).extension(), ".txt")
            guess.assert_not_called()

    def test_omitted_mime_sniffs_or_falls_back(self):
        self.assertEqual(self.uri("data:;base64," + base64.b64encode(PNG).decode()).extension(), ".png")
        self.assertEqual(self.uri("data:;charset=UTF-8,hello").extension(), ".txt")
        # Actual filetype TAR matcher needs byte 261, beyond the README's 261 bytes.
        tar = b"\0" * 257 + b"ustar" + b"\0" * 50
        self.assertEqual(self.uri("data:;base64," + base64.b64encode(tar).decode()).extension(), ".tar")

    def test_encoding_forms(self):
        for value, expected in [
            ("data:,hello%20world+!", b"hello world+!"),
            ("DATA:text/plain;charset=utf-8;BASE64,aGk%3D", b"hi"),
            ("data:application/octet-stream,%00%FF", b"\0\xff"),
            ("data:,", b""), ("data:;base64,", b""),
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
        for payload in [base64.b64encode(PNG + b"x" * 4_000_000).decode(),
                        base64.b64encode(PNG + b"x" * 4_000_000).decode().replace("A", "%41")]:
            uri = self.uri("data:;base64," + payload)
            with patch("jlv.data_uri.base64.b64decode", wraps=base64.b64decode) as decode:
                with patch("jlv.data_uri.filetype.guess", wraps=__import__("filetype").guess) as guess:
                    self.assertEqual(uri.extension(), ".png")
                    self.assertLessEqual(len(guess.call_args.args[0]), SIGNATURE_BYTES)
                self.assertLessEqual(len(decode.call_args.args[0]), ((SIGNATURE_BYTES + 2) // 3) * 4)
        uri = self.uri("data:," + "%41" * 2_000_000)
        self.assertEqual(len(uri.prefix()), SIGNATURE_BYTES)
        preview = NativePreview()
        preview.launcher = "test"
        with patch.object(DataURI, "decode", side_effect=AssertionError), patch.object(DataURI, "prefix", side_effect=AssertionError):
            self.assertTrue(preview.available(uri.value))

    def test_platform_availability(self):
        for platform, command in [("linux", "xdg-open"), ("darwin", "open")]:
            with patch("jlv.data_uri.sys.platform", platform), patch("jlv.data_uri.shutil.which", return_value="/bin/" + command) as which:
                self.assertEqual(native_launcher(), "/bin/" + command)
                which.assert_called_once_with(command)
            with patch("jlv.data_uri.sys.platform", platform), patch("jlv.data_uri.shutil.which", return_value=None):
                self.assertIsNone(native_launcher())
        with patch("jlv.data_uri.sys.platform", "win32"), patch("jlv.data_uri.os.startfile", Mock(), create=True):
            self.assertEqual(native_launcher(), "startfile")
        with patch("jlv.data_uri.sys.platform", "win32"), patch("jlv.data_uri.os.startfile", None, create=True):
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


class PreviewAppTests(unittest.IsolatedAsyncioTestCase):
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
                with patch.object(DataURI, "decode", slow_decode), patch("jlv.data_uri.subprocess.Popen") as spawn:
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
                        self.assertEqual(app.screen.query_one("#inspector").border_subtitle, "Press > to open natively")
                        await pilot.press(">")
                        await pilot.pause()
                        self.assertEqual(bells, [1, 1])
                        temp = app._native_preview.directory
                        await pilot.press("escape")
                        app._native_preview.launcher = None
                        app._update_hint()
                        self.assertFalse(app.query_one("#hint-rack").display)
                    self.assertFalse(Path(temp).exists())
