from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from array import array
from collections import OrderedDict
from collections.abc import Generator, Iterator
from codecs import utf_8_decode
from itertools import islice
from pathlib import Path
from threading import Event, Lock, Thread
from time import sleep
from typing import BinaryIO

import orjson
from rich.cells import cell_len


_NON_ASCII = re.compile(r"[^\x20-\x7e]+")


def _has_many_containers(raw: bytes) -> bool:
    # Conservatively use Python's own depth limits, without walking parsed objects.
    count = 0
    for marker in (b"[", b"{"):
        offset = raw.find(marker)
        while offset >= 0:
            count += 1
            if count >= 128:
                return True
            offset = raw.find(marker, offset + 1)
    return False


class JsonlFile:
    """Incrementally validated JSONL with bounded previews and a byte-offset index."""

    def __init__(self, path: str | Path, *, eager: bool = True) -> None:
        self._file: BinaryIO = open(path, "rb", buffering=1024 * 1024)
        self._read_lock = Lock()
        self._cancelled = Event()
        self._thread: Thread | None = None
        self._records: Generator[None, None, None] | None = None
        self._previews: OrderedDict[int, str] = OrderedDict()
        self.offsets = array("Q", [0])
        self.preview_width = 0
        self.scanned_bytes = 0
        self.complete = False
        self.error: str | None = None
        try:
            if not stat.S_ISREG(os.fstat(self._file.fileno()).st_mode):
                with self._file as stream:
                    self._file = tempfile.TemporaryFile("w+b")
                    shutil.copyfileobj(stream, self._file, length=1024 * 1024)
                self._file.seek(0)
            self.file_size = os.fstat(self._file.fileno()).st_size
            self._records = self._index_records()
            if eager:
                for _ in self._records:
                    pass
            else:
                for _ in islice(self._records, 64):
                    if self.scanned_bytes >= 256 * 1024:
                        break
        except BaseException:
            self.close()
            raise

    def _index_records(self) -> Generator[None, None, None]:
        first_blank: int | None = None
        offset = 0
        decoder = json.JSONDecoder()
        for line_number, raw in enumerate(self._lines(), 1):
            if self._cancelled.is_set():
                return
            offset += len(raw)
            self.scanned_bytes = offset
            validated = False
            if not _has_many_containers(raw):
                try:
                    orjson.loads(raw)
                    validated = True
                except orjson.JSONDecodeError:
                    # Python handles extensions and supplies the original diagnostics.
                    validated = False
            try:
                if validated:
                    trimmed = raw.strip()
                    prefix = trimmed[:1001]
                    if prefix.isascii():
                        # An ordinary ASCII preview cannot widen an already wider panel.
                        if (
                            first_blank is None and b"\t" not in prefix
                            and self.preview_width >= max(5, len(str(line_number))) + 1005
                        ):
                            self.offsets.append(offset)
                            yield
                            continue
                        text = prefix.decode("utf-8")
                    else:
                        # Enough bytes for 1,001 characters; ignore a cut final codepoint.
                        text = utf_8_decode(trimmed[:4004], "strict", False)[0]
                else:
                    text = raw.decode("utf-8").strip()
            except UnicodeDecodeError as error:
                raise ValueError(f"line {line_number} is not valid UTF-8: {error}") from error
            if not text:
                if first_blank is None:
                    first_blank = line_number
                continue
            if first_blank is not None:
                raise ValueError(f"line {first_blank} is empty — not valid JSONL")
            if not validated:
                try:
                    decoder.decode(text)
                except (ValueError, RecursionError) as error:
                    raise ValueError(f"line {line_number} is not valid JSON: {error}") from error
            preview = self._preview_text(text, line_number)
            width = len(preview)
            if not preview.isascii() or "\x7f" in preview:
                # Each non-ASCII character occupies at most two cells.
                extra = width - len(preview.encode("ascii", "ignore"))
                if width + extra > self.preview_width:
                    if "\u200d" in preview or "\ufe0f" in preview:
                        width = cell_len(preview)
                    else:
                        width += sum(cell_len(run) - len(run) for run in _NON_ASCII.findall(preview))
                else:
                    width = 0
            self.preview_width = max(self.preview_width, width)
            self.offsets.append(offset)
            yield
        if not self._cancelled.is_set():
            if not len(self):
                raise ValueError("file is empty")
            self.complete = True

    def start_indexing(self) -> None:
        if self.complete or self.error is not None or self._thread is not None or self._cancelled.is_set():
            return
        self._thread = Thread(target=self._finish_indexing, name="jlv-index", daemon=True)
        self._thread.start()

    def _finish_indexing(self) -> None:
        assert self._records is not None
        try:
            for _ in self._records:
                if len(self) % 256 == 0:
                    sleep(0)
        except (OSError, ValueError) as error:
            self.error = str(error)
        finally:
            self._records.close()

    @staticmethod
    def _preview_text(text: str, line_number: int) -> str:
        if len(text) > 1000:
            text = text[:1000] + "..."
        preview = f"{line_number:>5}  {text}"
        return preview.expandtabs(4) if "\t" in preview else preview

    def _lines(self) -> Iterator[bytes]:
        offset = 0
        while not self._cancelled.is_set():
            # Viewport reads may seek between batches; the scanner owns its own cursor.
            with self._read_lock:
                self._file.seek(offset)
                lines = self._file.readlines(1024 * 1024)
            if not lines:
                return
            for raw in lines:
                offset += len(raw)
                if b"\r" in raw:
                    yield from raw.splitlines(keepends=True)
                else:
                    yield raw

    def __len__(self) -> int:
        return len(self.offsets) - 1

    def raw(self, index: int) -> str:
        if not 0 <= index < len(self):
            raise IndexError(index)
        start, end = self.offsets[index:index + 2]
        with self._read_lock:
            if self._file.closed:
                raise ValueError("file is closed")
            self._file.seek(start)
            raw = self._file.read(end - start)
        if len(raw) != end - start:
            raise ValueError("file was truncated while viewing; reopen it")
        return raw.decode("utf-8")

    def value(self, index: int) -> object:
        return json.loads(self.raw(index).strip())

    def preview(self, index: int) -> str:
        if index in self._previews:
            self._previews.move_to_end(index)
            return self._previews[index]
        text = self._preview_text(self.raw(index).strip(), index + 1)
        self._previews[index] = text
        if len(self._previews) > 256:
            self._previews.popitem(last=False)
        return text

    def close(self) -> None:
        self._cancelled.set()
        if self._thread is not None:
            self._thread.join()
        if self._records is not None:
            self._records.close()
        with self._read_lock:
            self._file.close()
        self._previews.clear()

    def __enter__(self) -> JsonlFile:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
