"""Measure CLI startup and first Down key via a PTY, without recording file contents."""

from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import importlib.util
import json
import os
import pty
import platform
import resource
import select
import statistics
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--implementation", type=Path)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--navigation-samples", type=int, default=1)
    parser.add_argument("--wait-for-index", action="store_true")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--events-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.navigation_samples < 1:
        parser.error("--navigation-samples must be positive")
    if args.child:
        def notify(stage: str) -> None:
            os.write(args.events_fd, (json.dumps([stage, time.perf_counter()]) + "\n").encode())

        if args.implementation:
            spec = importlib.util.spec_from_file_location("terminal_implementation", args.implementation)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        else:
            import jlv as module
        notify("imported")

        async def exercise(pilot) -> None:
            app = pilot.app
            left = app.query_one("#lines-list")
            source = getattr(app, "source", None)

            async def rendered(index: int) -> None:
                while left.index != index or app._detail_idx != index:
                    await asyncio.sleep(0.001)
                while hasattr(app, "_detail_offset") and (
                    app._detail_offset != len(app._pretty_lines)
                    or len(app.query_one("#detail-list").children) != len(app._pretty_lines)
                ):
                    await asyncio.sleep(0.001)
                done = asyncio.get_running_loop().create_future()
                app.call_after_refresh(done.set_result, None)
                await done

            await rendered(0)
            notify("ready")
            for index in range(1, args.navigation_samples + 1):
                while source is not None and len(source) <= index and not getattr(source, "complete", True):
                    if source.error:
                        notify("index_failed")
                        return
                    await asyncio.sleep(0.001)
                if source is not None and len(source) <= index:
                    notify("too_few_records")
                    return
                while getattr(left, "row_count", index + 1) <= index:
                    await asyncio.sleep(0.001)
                notify(f"navigation_ready_{index}")
                await rendered(index)
                notify(f"down_{index}")
            if args.wait_for_index:
                if source is not None:
                    while not getattr(source, "complete", True):
                        if source.error:
                            notify("index_failed")
                            return
                        await asyncio.sleep(0.001)
                    while getattr(left, "row_count", len(source)) != len(source):
                        await asyncio.sleep(0.001)
                await rendered(args.navigation_samples)
                notify("indexed")
            if app.query("SearchBar"):
                notify("command_quit")
                notify("quit_ready")
                while app.focused is not app.query_one("SearchBar Input"):
                    await asyncio.sleep(0.001)
                notify("command_ready")
            else:
                notify("bare_quit")
                notify("quit_ready")

        original_run = module.JlvApp.run

        def measured_run(app, *positional, **keywords):
            notify("loaded")
            return original_run(app, *positional, **keywords, auto_pilot=exercise)

        module.JlvApp.run = measured_run
        sys.argv = ["jlv", str(args.file)]
        try:
            module.main()
        finally:
            os.write(args.events_fd, (json.dumps([
                "peak_rss_mib", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (
                    1024 * 1024 if sys.platform == "darwin" else 1024
                ),
            ]) + "\n").encode())
        return

    if args.output is None:
        parser.error("--output is required")
    master, slave = pty.openpty()
    events, writer = os.pipe()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    command = [
        sys.executable, __file__, str(args.file), "--child", "--events-fd", str(writer),
        "--navigation-samples", str(args.navigation_samples),
    ]
    if args.wait_for_index:
        command.append("--wait-for-index")
    if args.implementation:
        command.extend(["--implementation", str(args.implementation)])
    results = {
        "file": str(args.file), "bytes": args.file.stat().st_size,
        "python": platform.python_version(),
        "terminal": [120, 40], "timings_ms": {}, "stage": "startup",
    }
    invoked = time.perf_counter()
    process = subprocess.Popen(
        command, stdin=slave, stdout=slave, stderr=slave,
        env={**os.environ, "TERM": "xterm-256color"},
        pass_fds=(writer,),
    )
    os.close(slave)
    os.close(writer)
    pending = bytearray()
    stages = {}

    def drain_terminal() -> None:
        try:
            os.read(master, 65536)
        except OSError as error:
            if error.errno != errno.EIO:
                raise

    def wait_for(stage: str) -> None:
        deadline = time.perf_counter() + args.timeout
        while time.perf_counter() < deadline:
            for descriptor in select.select([master, events], [], [], 0.1)[0]:
                if descriptor == master:
                    drain_terminal()
                else:
                    pending.extend(os.read(events, 4096))
                    while b"\n" in pending:
                        line, _, remainder = pending.partition(b"\n")
                        pending[:] = remainder
                        name, timestamp = json.loads(line)
                        stages[name] = timestamp
            if stage in stages:
                return
            if "index_failed" in stages:
                raise RuntimeError("file indexing failed")
            if "too_few_records" in stages:
                raise RuntimeError("file has too few records for the requested navigation samples")
            if process.poll() is not None:
                raise RuntimeError(f"viewer exited with status {process.returncode}")
        raise TimeoutError(f"no {stage} event within {args.timeout:g} seconds")

    try:
        wait_for("ready")
        results["timings_ms"].update({
            "imports": (stages["imported"] - invoked) * 1000,
            "load": (stages["loaded"] - stages["imported"]) * 1000,
            "first_frame": (stages["ready"] - stages["loaded"]) * 1000,
            "startup": (stages["ready"] - invoked) * 1000,
        })
        results["stage"] = "navigation"
        durations = []
        for index in range(1, args.navigation_samples + 1):
            wait_for(f"navigation_ready_{index}")
            started = time.perf_counter()
            os.write(master, b"\x1b[B")
            wait_for(f"down_{index}")
            durations.append((stages[f"down_{index}"] - started) * 1000)
        results["timings_ms"].update({
            "first_down": durations[0],
            "down_median": statistics.median(durations),
            "down_max": max(durations),
        })
        if args.wait_for_index:
            results["stage"] = "indexing"
            wait_for("indexed")
            results["timings_ms"]["fully_indexed"] = (stages["indexed"] - invoked) * 1000
        results["stage"] = "quit"
        wait_for("quit_ready")
        if "bare_quit" in stages:
            os.write(master, b"q")
        else:
            wait_for("command_quit")
            os.write(master, b":")
            wait_for("command_ready")
            os.write(master, b"q\r")
        deadline = time.perf_counter() + 10
        while process.poll() is None:
            if time.perf_counter() >= deadline:
                raise TimeoutError("viewer did not exit after :q")
            if select.select([master], [], [], 0.1)[0]:
                drain_terminal()
        if process.returncode:
            raise RuntimeError(f"viewer exited with status {process.returncode}")
        wait_for("peak_rss_mib")
        results["status"] = "complete"
        results["stage"] = "complete"
    except (TimeoutError, RuntimeError, subprocess.TimeoutExpired) as error:
        results["status"] = "failed"
        results["error"] = str(error)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        os.close(master)
        os.close(events)
        if "peak_rss_mib" in stages:
            results["peak_rss_mib"] = stages["peak_rss_mib"]
        args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
    if results["status"] != "complete":
        sys.exit(1)


if __name__ == "__main__":
    main()
