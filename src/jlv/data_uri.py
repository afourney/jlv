"""Bounded data URI inspection and explicit, session-scoped native opening."""
from __future__ import annotations

import base64
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote_to_bytes

import filetype

# filetype 1.2 reads 8192 header bytes, despite its README's 261-byte claim.
# TAR alone checks byte 261 (262 bytes); ZIP-derived formats scan further.
# No footer is requested. Unknown or truncated signatures fall back to text.
SIGNATURE_BYTES = 8192
HEADER_LIMIT = 4096


def native_launcher() -> str | None:
    if sys.platform == "win32":
        return "startfile" if callable(getattr(os, "startfile", None)) else None
    if sys.platform == "darwin":
        return shutil.which("open")
    if sys.platform.startswith("linux"):
        return shutil.which("xdg-open")
    return None


def _unquote(text: str) -> bytes:
    if re.search(r"%(?![0-9a-fA-F]{2})", text):
        raise ValueError("Invalid percent escape")
    return unquote_to_bytes(text)


@dataclass(frozen=True)
class DataURI:
    value: str
    offset: int
    mime: str
    base64: bool

    @classmethod
    def parse(cls, value: str) -> DataURI | None:
        if value[:5].lower() != "data:":
            return None
        comma = value.find(",", 5, HEADER_LIMIT)
        if comma < 0:
            return None
        parts = value[5:comma].split(";")
        mime = parts.pop(0).lower()
        if mime and not re.fullmatch(r"[\w!#$&^.+-]+/[\w!#$&^.+-]+", mime):
            return None
        encoded = bool(parts and parts[-1].lower() == "base64")
        if encoded:
            parts.pop()
        if any("=" not in part or not part.split("=", 1)[0] for part in parts):
            return None
        return cls(value, comma + 1, mime, encoded)

    def prefix(self) -> bytes:
        # Bound both source scanning and allocations, including percent-encoded base64.
        count = ((SIGNATURE_BYTES + 2) // 3) * 4 if self.base64 else SIGNATURE_BYTES
        end = self.offset
        units = 0
        while end < len(self.value) and units < count:
            end += 3 if self.value[end] == "%" else 1
            units += 1
        raw = _unquote(self.value[self.offset:end])
        if self.base64:
            raw = base64.b64decode(raw, validate=True)
        return raw[:SIGNATURE_BYTES]

    def extension(self) -> str:
        if self.mime:
            return mimetypes.guess_extension(self.mime, strict=False) or ".txt"
        kind = filetype.guess(self.prefix())
        return "." + kind.extension if kind else ".txt"

    def decode(self) -> bytes:
        raw = _unquote(self.value[self.offset:])
        return base64.b64decode(raw, validate=True) if self.base64 else raw


class NativePreview:
    """Own files for one app; background work never owns the viewer lifetime."""

    def __init__(self) -> None:
        self.launcher = native_launcher()
        self.directory: str | None = None
        self._lock = threading.Lock()
        self._closed = False

    def available(self, value: str) -> bool:
        # Hint needs only the header, never a payload copy or decode.
        return self.launcher is not None and DataURI.parse(value) is not None

    def open(self, value: str, failed: Callable[[], None]) -> None:
        threading.Thread(target=self._open, args=(value, failed), daemon=True).start()

    def _open(self, value: str, failed: Callable[[], None]) -> None:
        try:
            uri = DataURI.parse(value)
            if uri is None or self.launcher is None:
                raise ValueError("No native preview")
            extension = uri.extension()
            payload = uri.decode()
            with self._lock:
                if self._closed:
                    return
                if self.directory is None:
                    self.directory = tempfile.mkdtemp(prefix="jlv-")
                with tempfile.NamedTemporaryFile(dir=self.directory, suffix=extension, delete=False) as f:
                    f.write(payload)
                    path = str(Path(f.name).absolute())
                if self.launcher == "startfile":
                    os.startfile(path)
                    return
                child = subprocess.Popen(
                    [self.launcher, path], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            try:
                if child.wait(timeout=0.5) != 0:
                    failed()
            except subprocess.TimeoutExpired:
                # Reap eventually without delaying the UI or app shutdown.
                threading.Thread(target=child.wait, daemon=True).start()
        except (OSError, ValueError):
            failed()

    def cleanup(self) -> None:
        with self._lock:
            self._closed = True
            if self.directory is not None:
                shutil.rmtree(self.directory, ignore_errors=True)
