"""User-configured scripts that run on the current selection.

Scripts are declared in ``scripts.toml`` in JLV's user configuration directory.
They are trusted local programs: JLV runs them with the user's permissions and
never discovers them from opened files.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import tomllib
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_path

INPUT_MODES = ("value", "envelope")
SELECTION_KINDS = ("string", "container", "record", "any")
JSON_MIME = "application/json"
TEXT_MIMES = ("text/plain", "text/markdown")
DEFAULT_TIMEOUT = 30.0


def config_path() -> Path:
    return user_config_path("jlv", appauthor=False) / "scripts.toml"


@dataclass(frozen=True)
class Script:
    name: str
    command: tuple[str, ...]
    input: str = "value"
    output: str | None = None
    applies_to: tuple[str, ...] = ("any",)
    match: re.Pattern[str] | None = None
    timeout: float = DEFAULT_TIMEOUT

    def applies(self, selection: Selection) -> bool:
        if "any" not in self.applies_to and not any(
            kind in self.applies_to for kind in selection.kinds
        ):
            return False
        return self.match is None or self.match.search(selection.text) is not None


def _expand(arg: str, base: Path) -> str:
    """Expand a leading ``~``, and resolve ``./`` and ``../`` against the config directory."""
    if arg.startswith("~"):
        expanded = os.path.expanduser(arg)
        # Normalize separators, e.g. C:\Users\me/x on Windows, once the home path is known.
        return str(Path(expanded)) if expanded != arg else arg
    if arg.startswith(("./", "../")) or (os.sep == "\\" and arg.startswith((".\\", "..\\"))):
        return str((base / arg).resolve())
    return arg


def _script(entry: object, base: Path) -> Script:
    if not isinstance(entry, dict):
        raise ValueError("must be a table")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("needs a non-empty 'name'")
    command = entry.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(arg, str) and arg for arg in command)
    ):
        raise ValueError(f"{name!r}: 'command' must be a non-empty list of strings")
    mode = entry.get("input", "value")
    if mode not in INPUT_MODES:
        raise ValueError(f"{name!r}: 'input' must be one of {', '.join(INPUT_MODES)}")
    output = entry.get("output")
    if output is not None and not isinstance(output, str):
        raise ValueError(f"{name!r}: 'output' must be a MIME type string")
    applies_to = entry.get("applies_to", "any")
    if isinstance(applies_to, str):
        applies_to = [applies_to]
    if (
        not isinstance(applies_to, list)
        or not applies_to
        or any(kind not in SELECTION_KINDS for kind in applies_to)
    ):
        raise ValueError(
            f"{name!r}: 'applies_to' must be one or more of {', '.join(SELECTION_KINDS)}"
        )
    pattern = entry.get("match")
    if pattern is not None and not isinstance(pattern, str):
        raise ValueError(f"{name!r}: 'match' must be a regular expression string")
    try:
        match = re.compile(pattern) if pattern is not None else None
    except re.error as error:
        raise ValueError(f"{name!r}: invalid 'match' pattern: {error}") from error
    timeout = entry.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError(f"{name!r}: 'timeout' must be a positive number of seconds")
    return Script(
        name,
        tuple(_expand(arg, base) for arg in command),
        mode,
        output.strip().lower() if output else None,
        tuple(applies_to),
        match,
        float(timeout),
    )


def load_scripts(path: Path) -> tuple[list[Script], list[str]]:
    """Return valid scripts and a readable error for each invalid entry."""
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except FileNotFoundError:
        return [], []
    except (OSError, tomllib.TOMLDecodeError) as error:
        return [], [f"{path}: {error}"]
    entries = data.get("script", [])
    if not isinstance(entries, list):
        return [], [f"{path}: 'script' must be an array of tables ([[script]])"]
    scripts, errors = [], []
    for number, entry in enumerate(entries, 1):
        try:
            scripts.append(_script(entry, path.parent))
        except ValueError as error:
            errors.append(f"script {number}: {error}")
    return scripts, errors


def json_pointer(path: tuple[str | int, ...]) -> str:
    return "".join("/" + str(key).replace("~", "~0").replace("/", "~1") for key in path)


@dataclass(frozen=True)
class Selection:
    """A snapshot of the displayed record and the value selected within it."""

    record: object
    path: tuple[str | int, ...]
    value: object
    file: str
    line: int

    @property
    def kinds(self) -> tuple[str, ...]:
        kinds = ("record",) if not self.path else ()
        if isinstance(self.value, str):
            return (*kinds, "string")
        if isinstance(self.value, (dict, list)):
            return (*kinds, "container")
        return kinds

    @property
    def text(self) -> str:
        """The value as sent in ``value`` mode: strings raw, everything else as JSON."""
        if isinstance(self.value, str):
            return self.value
        return json.dumps(self.value, ensure_ascii=False)

    def payload(self, mode: str) -> bytes:
        if mode == "value" and isinstance(self.value, str):
            try:
                return self.value.encode("utf-8")
            except UnicodeEncodeError as error:
                raise PayloadError(
                    "The selected string contains unpaired surrogates, which can't be sent as "
                    'UTF-8. Use input = "envelope" to receive it as escaped JSON.'
                ) from error
        if mode == "value":
            return _json_bytes(self.value)
        envelope = {
            "record": self.record,
            "value": self.value,
            "path": json_pointer(self.path),
            "source": {"file": self.file, "line": self.line},
        }
        return _json_bytes(envelope)


class PayloadError(ValueError):
    """The selection can't be written to the script's stdin."""


def _json_bytes(data: object) -> bytes:
    try:
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        # Unpaired surrogates have no UTF-8 form, but \u escapes keep them as valid JSON.
        return json.dumps(data).encode("ascii")


@dataclass(frozen=True)
class ScriptResult:
    title: str
    mime: str
    content: object
    error: bool = False
    stderr: str = ""


@dataclass(frozen=True)
class Completed:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


# How long to wait for a killed process tree to be reaped before abandoning its pipes.
KILL_GRACE = 2.0


async def run(script: Script, selection: Selection) -> Completed:
    """Run ``script`` without a shell; a timeout or cancellation kills its process tree."""
    # Serialize first: nothing may fail between launching the process and guarding it.
    payload = selection.payload(script.input)
    process = await _spawn(script, selection)
    tree = _ProcessTree(process)
    stdout, stderr = bytearray(), bytearray()
    # Feed stdin while draining both pipes, so large inputs can't deadlock, and keep
    # whatever was captured if the script times out.
    tasks = [
        asyncio.ensure_future(_feed(process.stdin, payload)),
        asyncio.ensure_future(_drain(process.stdout, stdout)),
        asyncio.ensure_future(_drain(process.stderr, stderr)),
    ]
    try:
        async with asyncio.timeout(script.timeout):
            await asyncio.gather(*tasks)
            await process.wait()
    except TimeoutError:
        await tree.kill(tasks)
        return Completed(None, bytes(stdout), bytes(stderr), timed_out=True)
    except BaseException:
        await tree.kill(tasks)
        raise
    finally:
        tree.close()
    return Completed(process.returncode, bytes(stdout), bytes(stderr))


async def _spawn(script: Script, selection: Selection) -> asyncio.subprocess.Process:
    executable = shutil.which(script.command[0]) or script.command[0]
    env = {**os.environ, "JLV_FILE": selection.file, "JLV_LINE": str(selection.line)}
    spawn = asyncio.ensure_future(
        asyncio.create_subprocess_exec(
            executable,
            *script.command[1:],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            # Its own process group, so descendants can be killed together.
            start_new_session=os.name == "posix",
        )
    )
    try:
        # Cancelling a launch midway makes asyncio wait for the pipes to close, which
        # descendants can hold open indefinitely. Let it finish, then kill what it started.
        return await asyncio.shield(spawn)
    except asyncio.CancelledError:
        done, _ = await asyncio.wait({spawn}, timeout=KILL_GRACE)
        if spawn in done and not spawn.cancelled() and spawn.exception() is None:
            tree = _ProcessTree(spawn.result())
            await tree.kill([])
            tree.close()
        raise


async def _feed(stream: asyncio.StreamWriter | None, data: bytes) -> None:
    if stream is None:
        return
    # A script may exit, or close stdin, without reading all of its input.
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        stream.write(data)
        await stream.drain()
        stream.close()


async def _drain(stream: asyncio.StreamReader | None, sink: bytearray) -> None:
    if stream is None:
        return
    while chunk := await stream.read(64 * 1024):
        sink += chunk


class _ProcessTree:
    """A script and its descendants, which can be killed even after the script exits.

    On POSIX the script leads its own process group. On Windows it is assigned to a job
    object; descendants join the job when they start, and it tracks them by membership
    rather than by parent, so they are found even once the script has exited. (A process
    started in the moment before the script joins its job is not tracked.)
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self._job = _WindowsJob.create(process.pid) if os.name == "nt" else None

    async def kill(self, tasks: list[asyncio.Future]) -> None:
        """Kill the whole tree, waiting a bounded time for cleanup."""
        for task in tasks:
            task.cancel()
        await self._kill_tree()
        process = self.process
        try:
            # Descendants that escaped the tree can hold the pipes open indefinitely.
            await asyncio.wait_for(asyncio.shield(process.wait()), KILL_GRACE)
        except TimeoutError:
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _kill_tree(self) -> None:
        if os.name == "posix":
            # The group outlives its leader while any descendant remains.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.process.pid, signal.SIGKILL)
        elif self._job is not None:
            self._job.terminate()
        elif self.process.returncode is None:
            # Without a job, taskkill /T can only follow the tree from a live parent.
            with contextlib.suppress(OSError):
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(self.process.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(killer.wait(), KILL_GRACE)
        with contextlib.suppress(ProcessLookupError):
            self.process.kill()

    def close(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None


class _WindowsJob:
    """A Windows job object holding one script's process tree."""

    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100

    def __init__(self, kernel32: object, handle: int) -> None:
        self._kernel32 = kernel32
        self._handle = handle

    @classmethod
    def create(cls, pid: int) -> _WindowsJob | None:
        """Put ``pid`` in a new job; None if Windows refuses, falling back to taskkill."""
        try:
            return cls._create(pid)
        except (OSError, AttributeError):
            return None

    @classmethod
    def _create(cls, pid: int) -> _WindowsJob | None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        process = kernel32.OpenProcess(cls.PROCESS_TERMINATE | cls.PROCESS_SET_QUOTA, False, pid)
        assigned = bool(process) and kernel32.AssignProcessToJobObject(job, process)
        if process:
            kernel32.CloseHandle(process)
        if not assigned:
            kernel32.CloseHandle(job)
            return None
        return cls(kernel32, job)

    def terminate(self) -> None:
        self._kernel32.TerminateJobObject(self._handle, 1)  # type: ignore[attr-defined]

    def close(self) -> None:
        # Closing the handle leaves any surviving processes running, as on POSIX.
        self._kernel32.CloseHandle(self._handle)  # type: ignore[attr-defined]


async def execute(script: Script, selection: Selection) -> ScriptResult:
    try:
        completed = await run(script, selection)
    except PayloadError as error:
        return ScriptResult(
            f"{script.name}: could not send the selection", "text/plain", str(error), error=True
        )
    except OSError as error:
        return ScriptResult(
            f"{script.name}: could not start",
            "text/plain",
            f"Could not run {script.command[0]!r}: {error}",
            error=True,
        )
    return parse_output(script, completed)


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _is_json(mime: str) -> bool:
    return mime == JSON_MIME or mime.endswith("+json")


def _failure(title: str, summary: str, stdout: str, stderr: str) -> ScriptResult:
    """A readable failure that keeps whatever the script wrote."""
    sections = [summary]
    if stderr.strip():
        sections.append("stderr:\n" + stderr.rstrip())
    if stdout.strip():
        sections.append("stdout:\n" + stdout.rstrip())
    return ScriptResult(title, "text/plain", "\n\n".join(sections), error=True)


def parse_output(script: Script, completed: Completed) -> ScriptResult:
    """Interpret a finished run as text, Markdown, JSON, or a readable failure."""
    stdout, stderr = _decode(completed.stdout), _decode(completed.stderr)
    if completed.timed_out:
        return _failure(
            f"{script.name}: timed out",
            f"The script did not finish within {script.timeout:g} seconds and was stopped.",
            stdout,
            stderr,
        )
    if completed.returncode != 0:
        return _failure(
            f"{script.name}: failed", f"Exit status: {completed.returncode}", stdout, stderr
        )

    title, mime = script.name, script.output or "text/plain"
    envelope = _envelope(stdout)
    if envelope is not None:
        if isinstance(envelope.get("title"), str):
            title = envelope["title"]
        if isinstance(envelope.get("mime"), str):
            mime = envelope["mime"].strip().lower()
        content = envelope["content"]
        # Envelope content is already parsed: any value, including a string, is the result.
        if _is_json(mime):
            return ScriptResult(title, JSON_MIME, content, stderr=stderr)
        if not isinstance(content, str):
            content = json.dumps(content, indent=2, ensure_ascii=False)
        return ScriptResult(title, _text_mime(mime), content, stderr=stderr)
    if _is_json(mime):
        try:
            return ScriptResult(title, JSON_MIME, json.loads(stdout), stderr=stderr)
        except json.JSONDecodeError as error:
            hint = (
                " The output contains more than one JSON value; wrap them in an array, "
                "for example with jq -s."
                if error.msg == "Extra data"
                else ""
            )
            return _failure(
                f"{script.name}: invalid JSON output",
                f"Expected exactly one JSON value: {error}.{hint}",
                stdout,
                stderr,
            )
    return ScriptResult(title, _text_mime(mime), stdout, stderr=stderr)


def _text_mime(mime: str) -> str:
    return mime if mime in TEXT_MIMES else "text/plain"


def _envelope(stdout: str) -> dict | None:
    """Only an object marked ``"$jlv": "result"`` with ``content`` is a response envelope."""
    if not stdout.lstrip().startswith("{"):
        return None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and data.get("$jlv") == "result" and "content" in data:
        return data
    return None
