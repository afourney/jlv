from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from jlv import (
    NO_DATA,
    HelpModal,
    JlvApp,
    PrettyDocument,
    ScriptPicker,
    ScriptRunning,
    StringModal,
    StringView,
)
from jlv.rows import RowView
from jlv.scripts import (
    _WINDOWS_LAUNCHER,
    KILL_GRACE,
    Completed,
    Script,
    ScriptResult,
    Selection,
    execute,
    json_pointer,
    load_scripts,
    parse_output,
    run,
)
from jlv.source import JsonlFile

HTML = "<h1>Hi</h1><p>Hello</p>"


def selection(value: object = HTML, path: tuple[str | int, ...] = ("body",)) -> Selection:
    return Selection({"id": 7, "body": HTML}, path, value, "/data.jsonl", 42)


def output(stdout: str, returncode: int = 0, stderr: str = "") -> Completed:
    return Completed(returncode, stdout.encode(), stderr.encode())


class ConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "scripts.toml"

    def test_missing_config_has_no_scripts_or_errors(self):
        self.assertEqual(load_scripts(self.path), ([], []))

    def test_loads_scripts_and_expands_paths(self):
        self.path.write_text(
            """
[[script]]
name = "HTML to Markdown"
command = ["markitdown", "-x", "html"]
output = "text/markdown"
applies_to = "string"
match = '^\\s*<'

[[script]]
name = "Validate"
command = ["python", "./validate.py", "~/schema.json", "../x", "plain/./rel"]
input = "envelope"
applies_to = ["record", "container"]
timeout = 5
"""
        )
        scripts, errors = load_scripts(self.path)
        self.assertEqual(errors, [])
        markdown, validate = scripts
        self.assertEqual(markdown.command, ("markitdown", "-x", "html"))
        self.assertEqual((markdown.input, markdown.output), ("value", "text/markdown"))
        self.assertEqual(markdown.applies_to, ("string",))
        self.assertEqual(validate.input, "envelope")
        self.assertEqual(validate.timeout, 5)
        self.assertEqual(validate.command[1], str((self.directory / "validate.py").resolve()))
        self.assertEqual(validate.command[2], str(Path.home() / "schema.json"))
        self.assertEqual(validate.command[3], str((self.directory.parent / "x").resolve()))
        self.assertEqual(validate.command[4], "plain/./rel")

    def test_invalid_entries_are_reported_and_valid_ones_kept(self):
        self.path.write_text(
            """
[[script]]
name = "ok"
command = ["cat"]

[[script]]
name = "shell string"
command = "cat | wc"

[[script]]
name = "bad input"
command = ["cat"]
input = "record"

[[script]]
name = "bad kind"
command = ["cat"]
applies_to = "number"

[[script]]
name = "bad regex"
command = ["cat"]
match = "("
"""
        )
        scripts, errors = load_scripts(self.path)
        self.assertEqual([script.name for script in scripts], ["ok"])
        self.assertEqual(len(errors), 4)
        self.assertIn("'command'", errors[0])
        self.assertIn("'input'", errors[1])
        self.assertIn("'applies_to'", errors[2])
        self.assertIn("'match'", errors[3])

    def test_invalid_toml_is_one_error(self):
        self.path.write_text("[[script]\n")
        scripts, errors = load_scripts(self.path)
        self.assertEqual(scripts, [])
        self.assertEqual(len(errors), 1)


class SelectionTests(unittest.TestCase):
    def test_json_pointer_escapes_keys(self):
        self.assertEqual(json_pointer(()), "")
        self.assertEqual(json_pointer(("a/b", 0, "c~d", "")), "/a~1b/0/c~0d/")

    def test_value_mode_sends_strings_raw_and_other_values_as_json(self):
        self.assertEqual(selection().payload("value"), HTML.encode())
        self.assertEqual(selection({"a": [1]}).payload("value"), b'{"a": [1]}')
        self.assertEqual(selection(None).payload("value"), b"null")

    def test_envelope_path_resolves_to_value(self):
        envelope = json.loads(selection().payload("envelope"))
        self.assertEqual(
            envelope,
            {
                "record": {"id": 7, "body": HTML},
                "value": HTML,
                "path": "/body",
                "source": {"file": "/data.jsonl", "line": 42},
            },
        )

    def test_applies_by_kind_and_match(self):
        strings = Script("s", ("cat",), applies_to=("string",))
        records = Script("r", ("cat",), applies_to=("record",))
        containers = Script("c", ("cat",), applies_to=("container",))
        html = Script("h", ("cat",), applies_to=("any",), match=re.compile("^<"))
        record = Selection({"a": 1}, (), {"a": 1}, "f", 1)
        self.assertTrue(strings.applies(selection()))
        self.assertFalse(strings.applies(record))
        self.assertTrue(records.applies(record))
        self.assertTrue(containers.applies(record))
        self.assertFalse(containers.applies(selection(5, ("n",))))
        self.assertTrue(html.applies(selection()))
        self.assertFalse(html.applies(selection("plain")))
        # Inspector contents stand alone as the whole record but keep their own type.
        standalone = Selection("<p>", (), "<p>", "f", 1)
        self.assertTrue(strings.applies(standalone))
        self.assertTrue(records.applies(standalone))
        self.assertFalse(containers.applies(standalone))


class OutputTests(unittest.TestCase):
    script = Script("Tool", ("tool",))

    def test_plain_text_and_configured_mime(self):
        result = parse_output(self.script, output("Hello"))
        self.assertEqual(
            (result.title, result.mime, result.content), ("Tool", "text/plain", "Hello")
        )
        markdown = Script("Tool", ("tool",), output="text/markdown")
        self.assertEqual(parse_output(markdown, output("# Hi")).mime, "text/markdown")
        other = Script("Tool", ("tool",), output="text/html")
        self.assertEqual(parse_output(other, output("<p>")).mime, "text/plain")

    def test_only_marked_envelopes_are_unwrapped(self):
        envelope = {"$jlv": "result", "title": "Schema", "mime": "text/markdown", "content": "ok"}
        result = parse_output(self.script, output(json.dumps(envelope)))
        self.assertEqual(
            (result.title, result.mime, result.content), ("Schema", "text/markdown", "ok")
        )
        unmarked = json.dumps({"content": "x"})
        self.assertEqual(parse_output(self.script, output(unmarked)).content, unmarked)

    def test_json_output_must_be_one_value(self):
        script = Script("jq", ("jq",), output="application/json")
        self.assertEqual(parse_output(script, output('{"a": 1}\n')).content, {"a": 1})
        multiple = parse_output(script, output("1\n2\n"))
        self.assertTrue(multiple.error)
        self.assertIn("jq -s", multiple.content)
        self.assertTrue(parse_output(script, output("{nope")).error)

    def test_envelope_can_wrap_json_data_resembling_an_envelope(self):
        inner = {"$jlv": "result", "content": "not unwrapped twice"}
        envelope = {"$jlv": "result", "mime": "application/json", "content": inner}
        result = parse_output(self.script, output(json.dumps(envelope)))
        self.assertEqual((result.mime, result.content), ("application/json", inner))

    def test_failures_include_status_and_stderr(self):
        result = parse_output(self.script, output("partial", 2, "boom"))
        self.assertTrue(result.error)
        self.assertIn("Exit status: 2", result.content)
        self.assertIn("boom", result.content)
        self.assertIn("partial", result.content)
        timed_out = parse_output(self.script, Completed(None, b"", b"", timed_out=True))
        self.assertTrue(timed_out.error)

    def test_envelope_content_is_not_parsed_twice(self):
        for content in ["hello", "42", None, 42]:
            envelope = {"$jlv": "result", "mime": "application/json", "content": content}
            result = parse_output(self.script, output(json.dumps(envelope)))
            self.assertFalse(result.error)
            self.assertEqual(result.content, content)

    def test_parse_and_timeout_failures_keep_output(self):
        script = Script("Tool", ("tool",), output="application/json")
        invalid = parse_output(script, output("not json", stderr="helpful diagnostic"))
        self.assertTrue(invalid.error)
        self.assertIn("not json", invalid.content)
        self.assertIn("helpful diagnostic", invalid.content)
        timed_out = parse_output(script, Completed(None, b"halfway", b"slow", timed_out=True))
        self.assertIn("halfway", timed_out.content)
        self.assertIn("slow", timed_out.content)


# Starts a descendant that inherits stdout/stderr and outlives the script, then records
# the descendant's pid in the file named by the first argument. With a second argument,
# the script exits at once, leaving the descendant holding its pipes.
TREE = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "sys.stderr.write('warming up\\n'); sys.stderr.flush()\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "time.sleep(0 if len(sys.argv) > 2 else 30)\n"
)


def running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # An orphan killed by its group may briefly remain a zombie before init reaps it.
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().split()[2] == "Z")


class RunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.pid_file = Path(directory.name) / "pid"
        self.script = Script("tree", (sys.executable, "-c", TREE, str(self.pid_file)))

    def assert_gone(self, pid: int) -> None:
        deadline = time.monotonic() + 3
        while running(pid):
            if time.monotonic() > deadline:
                self.fail(f"descendant {pid} is still running")
            time.sleep(0.05)

    async def wait_for_pid(self) -> int:
        deadline = time.monotonic() + 10
        while not (self.pid_file.exists() and self.pid_file.read_text()):
            if time.monotonic() > deadline:
                self.fail("the script did not start its descendant")
            await asyncio.sleep(0.05)
        return int(self.pid_file.read_text())

    async def test_timeout_kills_descendants_and_keeps_output(self):
        script = Script(self.script.name, self.script.command, timeout=3.0)
        start = time.monotonic()
        completed = await run(script, selection())
        self.assertLess(time.monotonic() - start, 6)
        self.assertTrue(completed.timed_out)
        self.assertIn(b"warming up", completed.stderr)
        self.assert_gone(int(self.pid_file.read_text()))

    async def test_cancelling_kills_descendants(self):
        task = asyncio.ensure_future(run(self.script, selection()))
        pid = await self.wait_for_pid()
        start = time.monotonic()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertLess(time.monotonic() - start, 3)
        self.assert_gone(pid)

    async def test_timeout_kills_descendants_after_the_script_exits(self):
        command = (*self.script.command, "exit")
        completed = await run(Script("tree", command, timeout=3.0), selection())
        # The script exited, but its descendant kept stdout open until the timeout.
        self.assertTrue(completed.timed_out)
        self.assert_gone(int(self.pid_file.read_text()))

    async def test_cancelling_during_launch_kills_what_started(self):
        await self.cancel_during_launch(0.5)

    async def test_cancelling_launch_slower_than_cleanup_grace(self):
        await self.cancel_during_launch(KILL_GRACE + 0.3)

    async def test_cancelling_launch_again_still_cleans_up(self):
        await self.cancel_during_launch(0.5, cancel_twice=True)

    async def cancel_during_launch(self, delay: float, *, cancel_twice: bool = False):
        launched = asyncio.Event()
        real = asyncio.create_subprocess_exec
        processes = []

        async def slow_launch(*args, **kwargs):
            # The process exists, but the launch hasn't returned it yet.
            process = await real(*args, **kwargs)
            processes.append(process)
            launched.set()
            await asyncio.sleep(delay)
            return process

        with unittest.mock.patch("asyncio.create_subprocess_exec", slow_launch):
            task = asyncio.ensure_future(run(self.script, selection()))
            await launched.wait()
            if os.name == "nt":
                # The launcher cannot start the script until its job has been assigned.
                self.assertFalse(self.pid_file.exists())
                pid = processes[0].pid
            else:
                pid = await self.wait_for_pid()
            start = time.monotonic()
            task.cancel()
            if cancel_twice:
                await asyncio.sleep(0.05)
                task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertLess(time.monotonic() - start, max(3, delay + KILL_GRACE))
        self.assert_gone(processes[0].pid)
        self.assert_gone(pid)
        if os.name == "nt":
            self.assertFalse(self.pid_file.exists())

    async def test_gated_launcher_preserves_stdin_arguments_and_status(self):
        # Exercise the gate protocol on every platform, including raw bytes that would
        # be lost if the launcher buffered stdin or translated Windows newlines.
        body = (
            "import json, sys\n"
            "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
            "print(json.dumps(sys.argv[1:]), file=sys.stderr)\n"
            "sys.exit(7)\n"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-S",
            "-c",
            _WINDOWS_LAUNCHER,
            sys.executable,
            "-c",
            body,
            "a b",
            'a"b',
            "café",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = b"\0\r\n\x1a" + bytes(range(256)) * 1024
        stdout, stderr = await asyncio.wait_for(process.communicate(b"\0" + payload), 10)
        self.assertEqual(stdout, payload)
        self.assertEqual(process.returncode, 7)
        self.assertEqual(json.loads(stderr), ["a b", 'a"b', "café"])

    @unittest.skipUnless(os.name == "nt", "Windows job assignment")
    async def test_failed_job_assignment_never_starts_script(self):
        marker = self.pid_file.with_name("launched")
        command = (sys.executable, "-c", f"open({str(marker)!r}, 'w').close()")
        with unittest.mock.patch("jlv.scripts._WindowsJob.create", return_value=None):
            result = await execute(Script("touch", command), selection())
        self.assertTrue(result.error)
        self.assertIn("Windows job", result.content)
        self.assertFalse(marker.exists())

    async def test_unencodable_value_fails_before_launch(self):
        marker = self.pid_file.with_name("launched")
        command = (sys.executable, "-c", f"open({str(marker)!r}, 'w').close()")
        result = await execute(Script("touch", command), selection("bad \ud800"))
        self.assertTrue(result.error)
        self.assertIn("surrogates", result.content)
        self.assertFalse(marker.exists())
        # Envelopes escape the same value as valid JSON instead.
        echo = (sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())")
        result = await execute(Script("echo", echo, input="envelope"), selection("bad \ud800"))
        self.assertFalse(result.error)
        self.assertEqual(json.loads(result.content)["value"], "bad \ud800")

    async def test_large_input_is_not_deadlocked(self):
        script = Script(
            "cat", (sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())")
        )
        value = "x" * 5_000_000
        completed = await run(script, selection(value))
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(len(completed.stdout), len(value))


class DocumentPathTests(unittest.TestCase):
    def test_every_value_row_has_a_path_and_decode_ignores_non_strings(self):
        value = {"n": 1, "b": True, "z": None, "e": {}, "a": [], "o": {"s": "[1]"}}
        document = PrettyDocument.from_value(value)
        self.assertEqual(set(document.paths), set(range(len(document.lines))))
        for row in range(len(document.lines)):
            path, selected = document.selected(row)
            current = value
            for key in path:
                current = current[key]
            self.assertEqual(selected, current)
            self.assertEqual(document.decode_string(row) is not None, document.expandable(row))
        closing = len(document.lines) - 2
        self.assertEqual(document.lines[closing][0].strip(), "}")
        self.assertEqual(document.selected(closing), (("o",), {"s": "[1]"}))


def python_script(directory: Path, name: str, body: str) -> str:
    (directory / name).write_text(body)
    return f'[sys.executable, "./{name}"]'


class ScriptAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        path = self.directory / "data.jsonl"
        self.values = [
            {"id": 0, "body": HTML, "n": 3},
            {"id": 1, "body": "plain", "payload": json.dumps({"h": HTML})},
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in self.values))
        self.source = JsonlFile(path)
        self.addCleanup(self.source.close)
        self.config = self.directory / "scripts.toml"
        self.app = JlvApp(self.source, scripts_path=self.config)

    def configure(self, *entries: str) -> None:
        self.config.write_text(
            "\n".join(entries).replace("sys.executable", json.dumps(sys.executable))
        )

    def lines(self):
        return [line for line, _ in self.app._document.lines]

    async def wait_for(self, pilot, predicate, timeout=10.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > deadline:
                self.fail("timed out waiting for condition")
            await pilot.pause(0.05)

    async def select(self, pilot, key: str):
        await pilot.press("tab")
        right = self.app.query_one("#detail-list", RowView)
        right.go_to_line(next(i for i, line in enumerate(self.lines()) if f'"{key}"' in line))
        await pilot.pause()
        return right

    async def test_value_script_shows_text_in_inspector(self):
        upper = python_script(
            self.directory,
            "upper.py",
            "import os, sys\n"
            "data = sys.stdin.read()\n"
            "print(os.environ['JLV_LINE'], data.upper(), end='')\n",
        )
        self.configure(
            f'[[script]]\nname = "Upper"\ncommand = {upper}\napplies_to = "string"\n'
            'output = "text/markdown"\n',
            f'[[script]]\nname = "Never"\ncommand = {upper}\napplies_to = "record"\n',
        )
        async with self.app.run_test(size=(100, 30)) as pilot:
            await self.select(pilot, "body")
            await pilot.press("|")
            picker = self.app.screen
            self.assertIsInstance(picker, ScriptPicker)
            self.assertEqual([script.name for script in picker.scripts], ["Upper"])
            await pilot.press("enter")
            await self.wait_for(pilot, lambda: isinstance(self.app.screen, StringModal))
            modal = self.app.screen
            self.assertEqual(modal.value, "1 " + HTML.upper())
            self.assertEqual(modal.query_one("#inspector").border_title, "Upper")
            self.assertIsInstance(self.app.focused, StringView)

    async def test_envelope_json_result_replaces_document_and_undoes(self):
        echo = python_script(
            self.directory,
            "echo.py",
            "import json, sys\n"
            "env = json.load(sys.stdin)\n"
            "print(json.dumps({'path': env['path'], 'value': env['value'],"
            " 'source': env['source']['line']}))\n",
        )
        self.configure(
            f'[[script]]\nname = "Echo"\ncommand = {echo}\ninput = "envelope"\n'
            'output = "application/json"\n'
        )
        async with self.app.run_test(size=(100, 30)) as pilot:
            original = self.lines()
            right = await self.select(pilot, "n")
            await pilot.press("|", "enter")
            await self.wait_for(pilot, lambda: self.app._document.value != self.values[0])
            self.assertEqual(self.app._document.value, {"path": "/n", "value": 3, "source": 1})
            self.assertIs(self.app.focused, right)
            self.assertEqual(right.index, 0)
            # Chained: the result is now the record sent to the next script.
            await pilot.press("|", "enter")
            await self.wait_for(pilot, lambda: self.app._document.value.get("path") == "")
            self.assertEqual(self.app._document.value["value"]["path"], "/n")
            await pilot.press("<", "<")
            self.assertEqual(self.lines(), original)

    async def test_failure_and_filtering(self):
        fail = python_script(
            self.directory, "fail.py", "import sys\nsys.stderr.write('bad input')\nsys.exit(3)\n"
        )
        self.configure(
            f'[[script]]\nname = "Alpha"\ncommand = {fail}\n',
            f'[[script]]\nname = "Beta"\ncommand = {fail}\n',
        )
        async with self.app.run_test(size=(100, 30)) as pilot:
            await self.select(pilot, "id")
            await pilot.press("|", "b")
            options = self.app.screen.query_one("#script-options")
            self.assertEqual(options.option_count, 1)
            await pilot.press("enter")
            await self.wait_for(pilot, lambda: isinstance(self.app.screen, StringModal))
            self.assertEqual(self.app.screen.query_one("#inspector").border_title, "Beta: failed")
            self.assertIn("Exit status: 3", self.app.screen.value)
            self.assertIn("bad input", self.app.screen.value)

    async def check_help_during_script(self, mime: str, content: object):
        self.configure('[[script]]\nname = "Example"\ncommand = ["unused"]\n')
        finish = asyncio.Event()

        async def complete(script, selection):
            await finish.wait()
            return ScriptResult("Example", mime, content, False)

        with unittest.mock.patch("jlv.execute", side_effect=complete):
            async with self.app.run_test(size=(100, 30)) as pilot:
                base = self.app.screen
                await pilot.press("|", "enter")
                running = self.app.screen
                self.assertIsInstance(running, ScriptRunning)
                await pilot.press("f1")
                self.assertIs(self.app.screen, running)

                finish.set()
                await self.wait_for(pilot, lambda: running.worker.is_finished)
                self.assertNotIn(running, self.app.screen_stack)
                if mime == "application/json":
                    self.assertIs(self.app.screen, base)
                    self.assertEqual(self.app._document.value, content)
                else:
                    self.assertIsInstance(self.app.screen, StringModal)
                    self.assertEqual(self.app.screen.value, content)

                # Help becomes available again on either kind of result.
                result_screen = self.app.screen
                await pilot.press("f1")
                self.assertIsInstance(self.app.screen, HelpModal)
                await pilot.press("escape")
                self.assertIs(self.app.screen, result_screen)
                if mime != "application/json":
                    await pilot.press("escape")
                    self.assertIs(self.app.screen, base)

    async def test_help_during_script_with_json_result(self):
        await self.check_help_during_script("application/json", {"result": 2})

    async def test_help_during_script_with_text_result(self):
        await self.check_help_during_script("text/plain", "done")

    async def test_escape_cancels_running_script(self):
        pid_file = self.directory / "pid"
        slow = python_script(
            self.directory,
            "slow.py",
            f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n",
        )
        self.configure(f'[[script]]\nname = "Slow"\ncommand = {slow}\n')
        async with self.app.run_test(size=(100, 30)) as pilot:
            await pilot.press("|", "enter")
            await self.wait_for(pilot, lambda: isinstance(self.app.screen, ScriptRunning))
            await self.wait_for(pilot, lambda: pid_file.exists() and pid_file.read_text())
            worker = self.app.screen.worker
            await pilot.press("f1")
            self.assertIsInstance(self.app.screen, ScriptRunning)
            await pilot.press("escape")
            await self.wait_for(pilot, lambda: worker.is_finished)
            self.assertNotIsInstance(self.app.screen, (ScriptRunning, StringModal))
        if os.name == "posix":
            # Cancelling waits for the killed process, so it no longer exists.
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)

    def configure_describe(self) -> None:
        describe = python_script(
            self.directory,
            "describe.py",
            "import json, sys\n"
            "env = json.load(sys.stdin)\n"
            "print(json.dumps({'path': env['path'], 'value': env['value'],"
            " 'record': env['record'], 'line': env['source']['line']}))\n",
        )
        self.configure(
            f'[[script]]\nname = "Text"\ncommand = {describe}\ninput = "envelope"\n',
            f'[[script]]\nname = "Json"\ncommand = {describe}\ninput = "envelope"\n'
            'output = "application/json"\n',
        )

    async def run_in_inspector(self, pilot, name: str) -> StringModal:
        before = self.app.screen
        await pilot.press("|", *name.lower(), "enter")
        await self.wait_for(
            pilot,
            lambda: isinstance(self.app.screen, StringModal) and self.app.screen is not before,
        )
        return self.app.screen

    async def test_inspector_runs_on_its_string_and_chains_results(self):
        self.configure_describe()
        async with self.app.run_test(size=(100, 30)) as pilot:
            await self.select(pilot, "body")
            await pilot.press("enter")
            inspector = self.app.screen
            self.assertIsInstance(inspector, StringModal)
            # An unchanged string keeps its place in the record.
            result = await self.run_in_inspector(pilot, "Text")
            sent = json.loads(result.value)
            self.assertEqual(sent["path"], "/body")
            self.assertEqual(sent["value"], HTML)
            self.assertEqual(sent["record"], self.values[0])
            self.assertEqual(sent["line"], 1)
            # A script result stands alone as the next script's record.
            chained = await self.run_in_inspector(pilot, "Text")
            sent = json.loads(chained.value)
            self.assertEqual(sent["path"], "")
            self.assertEqual(sent["value"], result.value)
            self.assertEqual(sent["record"], result.value)
            await pilot.press("escape", "escape")
            self.assertIs(self.app.screen, inspector)

    async def test_inspector_sends_decoded_value_and_shows_json_results(self):
        self.configure_describe()
        async with self.app.run_test(size=(100, 30)) as pilot:
            await pilot.press("j")
            await self.select(pilot, "payload")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press(">")
            self.assertEqual(self.app.screen._data, {"h": HTML})
            result = await self.run_in_inspector(pilot, "Json")
            expected = {"path": "", "value": {"h": HTML}, "record": {"h": HTML}, "line": 2}
            self.assertEqual(result._data, expected)
            self.assertEqual(json.loads(result.value), expected)
            # The main view is unchanged underneath the inspectors.
            self.assertEqual(self.app._document.value, self.values[1])

    async def test_inspector_keeps_json_scalar_types_when_chaining(self):
        echo = python_script(
            self.directory, "echo.py", "import sys\nsys.stdout.write(sys.stdin.read())\n"
        )
        emit = python_script(self.directory, "emit.py", "import sys\nprint(sys.argv[1])\n")
        emit = emit[:-1] + ", {!r}]"
        self.configure(
            f'[[script]]\nname = "Echo"\ncommand = {echo}\ninput = "envelope"\n',
            *(
                f'[[script]]\nname = "{name}"\ncommand = {emit.format(text)}\n'
                'output = "application/json"\n'
                for name, text in [("Num", "42"), ("Null", "null"), ("Str", '"hello"')]
            ),
        )
        async with self.app.run_test(size=(100, 30)) as pilot:
            await self.select(pilot, "body")
            await pilot.press("enter")
            await pilot.pause()
            for name, expected in [("num", 42), ("null", None), ("str", "hello")]:
                result = await self.run_in_inspector(pilot, name)
                if isinstance(expected, str):
                    # A JSON string is shown as the string itself, without quotes.
                    self.assertEqual((result.value, result._data), (expected, NO_DATA))
                else:
                    self.assertEqual(result._data, expected)
                sent = json.loads((await self.run_in_inspector(pilot, "echo")).value)
                self.assertEqual(
                    (sent["record"], sent["value"], sent["path"]), (expected, expected, "")
                )
                await pilot.press("escape", "escape")

    async def test_unconfigured_picker_notifies(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            await pilot.press("|")
            await pilot.pause()
            self.assertNotIsInstance(self.app.screen, ScriptPicker)
            self.assertTrue(
                any("No scripts configured" in n.title for n in self.app._notifications)
            )


if __name__ == "__main__":
    unittest.main()
