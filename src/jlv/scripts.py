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
    arg = os.path.expanduser(arg)
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
        if mode == "value":
            return self.text.encode("utf-8")
        envelope = {
            "record": self.record,
            "value": self.value,
            "path": json_pointer(self.path),
            "source": {"file": self.file, "line": self.line},
        }
        return json.dumps(envelope, ensure_ascii=False).encode("utf-8")


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


async def run(script: Script, selection: Selection) -> Completed:
    """Run ``script`` without a shell; cancelling kills the process."""
    executable = shutil.which(script.command[0]) or script.command[0]
    env = {**os.environ, "JLV_FILE": selection.file, "JLV_LINE": str(selection.line)}
    process = await asyncio.create_subprocess_exec(
        executable,
        *script.command[1:],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        # communicate() writes stdin while draining both pipes, so large inputs can't deadlock.
        stdout, stderr = await asyncio.wait_for(
            process.communicate(selection.payload(script.input)), script.timeout
        )
    except TimeoutError:
        await _kill(process)
        return Completed(None, b"", b"", timed_out=True)
    except BaseException:
        await _kill(process)
        raise
    return Completed(process.returncode, stdout, stderr)


async def execute(script: Script, selection: Selection) -> ScriptResult:
    try:
        completed = await run(script, selection)
    except OSError as error:
        return ScriptResult(
            f"{script.name}: could not start",
            "text/plain",
            f"Could not run {script.command[0]!r}: {error}",
            error=True,
        )
    return parse_output(script, completed)


async def _kill(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    with contextlib.suppress(Exception):
        await asyncio.shield(process.wait())


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _is_json(mime: str) -> bool:
    return mime == JSON_MIME or mime.endswith("+json")


def parse_output(script: Script, completed: Completed) -> ScriptResult:
    """Interpret a finished run as text, Markdown, JSON, or a readable failure."""
    if completed.timed_out:
        return ScriptResult(
            f"{script.name}: timed out",
            "text/plain",
            f"The script did not finish within {script.timeout:g} seconds and was stopped.",
            error=True,
        )
    stdout, stderr = _decode(completed.stdout), _decode(completed.stderr)
    if completed.returncode != 0:
        sections = [f"Exit status: {completed.returncode}"]
        if stderr.strip():
            sections.append("stderr:\n" + stderr.rstrip())
        if stdout.strip():
            sections.append("stdout:\n" + stdout.rstrip())
        return ScriptResult(f"{script.name}: failed", "text/plain", "\n\n".join(sections), True)

    title, mime, content = script.name, script.output or "text/plain", stdout
    envelope = _envelope(stdout)
    if envelope is not None:
        content = envelope["content"]
        if isinstance(envelope.get("title"), str):
            title = envelope["title"]
        if isinstance(envelope.get("mime"), str):
            mime = envelope["mime"].strip().lower()
    if _is_json(mime):
        if not isinstance(content, str):
            return ScriptResult(title, JSON_MIME, content, stderr=stderr)
        try:
            return ScriptResult(title, JSON_MIME, json.loads(content), stderr=stderr)
        except json.JSONDecodeError as error:
            hint = (
                "\n\nThe output contains more than one JSON value. Wrap them in an array, "
                "for example with jq -s."
                if error.msg == "Extra data"
                else ""
            )
            return ScriptResult(
                f"{script.name}: invalid JSON output",
                "text/plain",
                f"Expected exactly one JSON value: {error}{hint}",
                error=True,
            )
    if not isinstance(content, str):
        content = json.dumps(content, indent=2, ensure_ascii=False)
    return ScriptResult(title, mime if mime in TEXT_MIMES else "text/plain", content, stderr=stderr)


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
