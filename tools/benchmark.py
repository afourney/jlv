"""End-to-end headless benchmark; use stress-test.jsonl for comparable results."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import platform
import resource
import statistics
import sys
import time
from pathlib import Path

import textual
from textual.events import Key, MouseScrollDown
from textual.pilot import Pilot

import jlv


async def benchmark(path: Path, output: Path, samples: int) -> None:
    module_path = Path(jlv.__file__)
    source_files = (
        sorted(module_path.parent.glob("*.py")) if hasattr(jlv, "__path__") else [module_path]
    )
    results = {
        "file": str(path),
        "bytes": path.stat().st_size,
        "python": platform.python_version(),
        "textual": textual.__version__,
        "terminal": [120, 40],
        "samples": samples,
        "implementation_sha256": {
            file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in source_files
        },
        "timings": {},
    }

    def report(name, values):
        results["timings"][name] = {
            "samples": len(values),
            "median_ms": statistics.median(values) * 1000,
            "p95_ms": sorted(values)[max(0, int(len(values) * 0.95 + 0.999) - 1)] * 1000,
            "max_ms": max(values) * 1000,
        }
        results["peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        output.write_text(json.dumps(results, indent=2) + "\n")
        print(name, results["timings"][name], flush=True)

    # Choose demanding records independently of the implementation under test.
    largest = complex_record = inspector_record = 0
    largest_size = most_lines = longest_string = 0
    record_count = 0
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for i, line in enumerate(stream):
            record_count += 1
            digest.update(line)
            if len(line) > largest_size:
                largest, largest_size = i, len(line)
            pretty_lines = 0
            pending = [json.loads(line)]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    pretty_lines += 2 if value else 1
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pretty_lines += 2 if value else 1
                    pending.extend(value)
                else:
                    pretty_lines += 1
                    if isinstance(value, str) and len(value) > longest_string:
                        inspector_record, longest_string = i, len(value)
            if pretty_lines > most_lines:
                complex_record, most_lines = i, pretty_lines
    if record_count <= samples or most_lines <= samples + 1:
        raise ValueError("benchmark needs more records and preview lines than navigation samples")
    results["records"] = record_count
    results["sha256"] = digest.hexdigest()
    results["selected_records_zero_based"] = {
        "largest": largest,
        "complex": complex_record,
        "inspector": inspector_record,
    }
    results["longest_string_characters"] = longest_string
    results["most_pretty_lines"] = most_lines

    started = time.perf_counter()
    source = None
    if hasattr(jlv, "JsonlFile"):
        source = jlv.JsonlFile(path)
        app = jlv.JlvApp(source)
    else:
        with path.open(encoding="utf-8") as stream:
            lines = stream.readlines()
        while lines and not lines[-1].strip():
            lines.pop()
        parsed = [json.loads(line) for line in lines]
        app = jlv.JlvApp(lines, parsed)
    report("load", [time.perf_counter() - started])

    try:

        async def exercise(pilot: Pilot) -> None:
            async def frame():
                done = asyncio.get_running_loop().create_future()
                app.call_after_refresh(done.set_result, None)
                await asyncio.wait_for(done, 120)

            async def until(predicate):
                async with asyncio.timeout(180):
                    while not predicate():
                        await asyncio.sleep(0.001)

            async def detail_ready():
                await until(
                    lambda: getattr(app, "_detail_idx", None) == app.query_one("#lines-list").index
                )
                await until(
                    lambda: (
                        not hasattr(app, "_detail_offset")
                        or (
                            app._detail_offset == len(app._pretty_lines)
                            and len(app.query_one("#detail-list").children)
                            == len(app._pretty_lines)
                        )
                    )
                )
                await frame()

            await detail_ready()
            report("startup", [time.perf_counter() - started])
            left = app.query_one("#lines-list")
            right = app.query_one("#detail-list")

            async def measure(name, action, wait=frame, count=samples):
                durations = []
                for _ in range(count):
                    start = time.perf_counter()
                    action()
                    await wait()
                    durations.append(time.perf_counter() - start)
                report(name, durations)

            await measure("frame_overhead", lambda: None)
            left.focus()
            await frame()

            async def down(view, preview=False):
                expected = view.index + 1
                app.post_message(Key("down", None))
                await until(lambda: view.index == expected)
                if preview:
                    await detail_ready()
                else:
                    await frame()

            for name, row in [("left_down", 0), ("left_down_at_end", record_count - samples - 1)]:
                left.index = row
                await detail_ready()
                durations = []
                for _ in range(samples):
                    start = time.perf_counter()
                    await down(left, preview=True)
                    durations.append(time.perf_counter() - start)
                report(name, durations)
            for name, row in [("largest_preview", largest), ("complex_preview", complex_record)]:
                durations = []
                for _ in range(min(samples, 20)):
                    left.index = 0
                    await detail_ready()
                    if hasattr(app, "_documents"):
                        app._documents.pop(row, None)
                    start = time.perf_counter()
                    left.index = row
                    await detail_ready()
                    durations.append(time.perf_counter() - start)
                report(name, durations)
            right.focus()
            await frame()
            for name, row in [
                ("right_down", 0),
                ("right_down_at_end", len(app._pretty_lines) - samples - 2),
            ]:
                right.index = row
                await frame()
                durations = []
                for _ in range(samples):
                    start = time.perf_counter()
                    await down(right)
                    durations.append(time.perf_counter() - start)
                report(name, durations)

            left.index = inspector_record
            await detail_ready()
            string_row = max(
                (i for i, (line, kind) in enumerate(app._pretty_lines) if kind == "string"),
                key=lambda i: len(app._pretty_lines[i][0]),
            )
            right.index = string_row
            right.focus()
            await until(lambda: app.focused is right)
            await frame()
            assert right.index == string_row

            async def inspector_ready():
                await until(lambda: isinstance(app.screen, jlv.StringModal))
                await until(lambda: bool(app.screen.query("#modal-container")))
                if hasattr(jlv, "StringView"):
                    inspector = app.screen.query_one("#modal-container")
                    await until(
                        lambda: (
                            inspector.text == app.screen.value
                            and inspector.content_size.width > 0
                            and inspector.wrapped_document._width == inspector.wrap_width
                        )
                    )
                await frame()

            open_durations = []
            close_durations = []
            for _ in range(min(samples, 20)):
                start = time.perf_counter()
                app.post_message(Key("enter", None))
                await inspector_ready()
                open_durations.append(time.perf_counter() - start)
                start = time.perf_counter()
                app.post_message(Key("escape", None))
                await until(lambda: not isinstance(app.screen, jlv.StringModal))
                await frame()
                close_durations.append(time.perf_counter() - start)
                await until(lambda: app.focused is right)
            report("inspector_open", open_durations)
            report("inspector_close", close_durations)
            app.post_message(Key("enter", None))
            await inspector_ready()
            assert isinstance(app.screen, jlv.StringModal)
            inspector = app.screen.query_one("#modal-container")
            assert app.screen.value and len(app.screen.value) == longest_string
            await measure("inspector_scroll", lambda: inspector.scroll_relative(y=3, animate=False))
            inspector.scroll_to(y=inspector.max_scroll_y, animate=False)
            await frame()
            await measure(
                "inspector_scroll_at_end", lambda: inspector.scroll_relative(y=-3, animate=False)
            )
            wheel_durations = []
            for _ in range(samples):
                if inspector.scroll_y >= inspector.max_scroll_y:
                    inspector.scroll_to(y=0, animate=False)
                    await frame()
                previous = inspector.scroll_y
                start = time.perf_counter()
                inspector.post_message(
                    MouseScrollDown(inspector, 5, 5, 0, 1, 0, False, False, False)
                )
                await until(lambda previous=previous: inspector.scroll_y > previous)
                await frame()
                wheel_durations.append(time.perf_counter() - start)
            report("inspector_wheel", wheel_durations)
            app.post_message(Key("escape", None))
            await until(lambda: not isinstance(app.screen, jlv.StringModal))
            await frame()
            app.exit()

        await app.run_async(headless=True, size=(120, 40), auto_pilot=exercise)
        if app._exception is not None:
            raise app._exception
        results["status"] = "complete"
        output.write_text(json.dumps(results, indent=2) + "\n")
    except Exception as error:
        results["status"] = "failed"
        results["error"] = f"{type(error).__name__}: {error}"
        output.write_text(json.dumps(results, indent=2) + "\n")
        raise
    finally:
        if source is not None:
            source.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument(
        "--implementation", type=Path, help="An exported legacy __init__.py to benchmark"
    )
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    if args.implementation:
        spec = importlib.util.spec_from_file_location(
            "benchmark_implementation", args.implementation
        )
        assert spec is not None and spec.loader is not None
        jlv = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = jlv
        spec.loader.exec_module(jlv)
    asyncio.run(benchmark(args.file, args.output, args.samples))
