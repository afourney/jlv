from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from array import array
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO
from threading import Lock

from rich.cells import cell_len


class JsonlFile:
    """Validated, indexed JSONL without retaining every decoded record in memory."""

    def __init__(self, path: str | Path) -> None:
        self._file: BinaryIO = open(path, "rb", buffering=1024 * 1024)
        self._read_lock = Lock()
        self._previews: OrderedDict[int, str] = OrderedDict()
        self.offsets = array("Q", [0])
        self.preview_width = 0
        try:
            if not stat.S_ISREG(os.fstat(self._file.fileno()).st_mode):
                with self._file as stream:
                    self._file = tempfile.TemporaryFile("w+b")
                    shutil.copyfileobj(stream, self._file, length=1024 * 1024)
                self._file.seek(0)
            first_blank: int | None = None
            offset = 0
            decoder = json.JSONDecoder()
            for line_number, raw in enumerate(self._lines(), 1):
                offset += len(raw)
                try:
                    text = raw.decode("utf-8").strip()
                except UnicodeDecodeError as error:
                    raise ValueError(f"line {line_number} is not valid UTF-8: {error}") from error
                if not text:
                    if first_blank is None:
                        first_blank = line_number
                    continue
                if first_blank is not None:
                    raise ValueError(f"line {first_blank} is empty — not valid JSONL")
                try:
                    decoder.decode(text)
                except (ValueError, RecursionError) as error:
                    raise ValueError(f"line {line_number} is not valid JSON: {error}") from error
                self.offsets.append(offset)
                preview = self._preview_text(text, line_number)
                width = len(preview) if preview.isascii() and "\x7f" not in preview else cell_len(preview)
                self.preview_width = max(self.preview_width, width)
            if not len(self):
                raise ValueError("file is empty")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _preview_text(text: str, line_number: int) -> str:
        if len(text) > 1000:
            text = text[:1000] + "..."
        preview = f"{line_number:>5}  {text}"
        return preview.expandtabs(4) if "\t" in preview else preview

    def _lines(self) -> Iterator[bytes]:
        for raw in self._file:
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
        with self._read_lock:
            self._file.close()
        self._previews.clear()

    def __enter__(self) -> JsonlFile:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
