"""Bounded encoded-string inspection and session-scoped native opening."""

from __future__ import annotations

import base64
import codecs
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
BASE64_PROBE_CHARS = 4096
BASE64_WHITESPACE = str.maketrans("", "", " \t\r\n")

# Explicit MIME/extension pairs, never image/* or an OS-configured extension.
# These formats normally display data; this is a policy, not a viewer sandbox.
PASSIVE_FORMATS = (
    (".jpg", ("image/jpeg", "image/pjpeg")),
    (".png", ("image/png", "image/apng", "image/vnd.mozilla.apng")),
    (".gif", ("image/gif",)),
    (".webp", ("image/webp",)),
    (".bmp", ("image/bmp", "image/x-ms-bmp")),
    (".tif", ("image/tiff",)),
    (".ico", ("image/x-icon", "image/vnd.microsoft.icon")),
    (".avif", ("image/avif",)),
    (".heic", ("image/heic", "image/heif")),
    (".jpx", ("image/jpx", "image/jp2")),
    (".jxr", ("image/vnd.ms-photo",)),
    (".mp3", ("audio/mpeg", "audio/mp3")),
    (".wav", ("audio/wav", "audio/wave", "audio/x-wav", "audio/vnd.wave")),
    (".flac", ("audio/flac", "audio/x-flac")),
    (".ogg", ("audio/ogg", "video/ogg", "application/ogg", "audio/opus")),
    (".aac", ("audio/aac", "audio/x-aac")),
    (".m4a", ("audio/mp4", "audio/x-m4a")),
    (".aiff", ("audio/aiff", "audio/x-aiff")),
    (".amr", ("audio/amr",)),
    (".mid", ("audio/midi", "audio/x-midi")),
    (".mp4", ("video/mp4",)),
    (".m4v", ("video/x-m4v",)),
    (".mov", ("video/quicktime",)),
    (".avi", ("video/x-msvideo", "video/avi")),
    (".mkv", ("video/x-matroska",)),
    (".mpeg", ("video/mpeg",)),
    (".webm", ("video/webm",)),
    (".wmv", ("video/x-ms-wmv",)),
    (".flv", ("video/x-flv",)),
    (".3gp", ("video/3gpp",)),
    (".txt", ("text/plain",)),
)
PASSIVE_MIME_EXTENSIONS = {
    mime: extension for extension, aliases in PASSIVE_FORMATS for mime in aliases
}


def _plain_text(header: bytes) -> bool:
    try:
        # A bounded prefix may end partway through a valid UTF-8 character.
        text = codecs.getincrementaldecoder("utf-8")().decode(
            header,
            final=len(header) < SIGNATURE_BYTES,
        )
    except UnicodeError:
        return False
    return not any(ord(char) < 32 and char not in "\t\n\r\f" for char in text) and not any(
        marker in text.lower() for marker in ("<", "#!", "[desktop entry]", "@echo")
    )


@dataclass(frozen=True)
class PreviewInfo:
    declared_mime: str
    detected_mime: str
    extension: str
    warning: str | None


ConfirmPreview = Callable[[PreviewInfo, Callable[[], None]], None]


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


def _detect_type(header: bytes):
    try:
        return filetype.guess(header)
    except Exception as error:
        # Third-party matchers may fail on unusual headers. Use the existing
        # preview-error path; a detector failure must never authorize opening.
        raise ValueError("Unable to detect encoded file type") from error


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
        raw = _unquote(self.value[self.offset : end])
        if self.base64:
            raw = base64.b64decode(raw, validate=True)
        return raw[:SIGNATURE_BYTES]

    def extension(self) -> str:
        if self.mime:
            return (
                PASSIVE_MIME_EXTENSIONS.get(self.mime)
                or mimetypes.guess_extension(self.mime, strict=False)
                or ".txt"
            )
        kind = _detect_type(self.prefix())
        return "." + kind.extension if kind else ".txt"

    def preview_info(self) -> PreviewInfo:
        header = self.prefix()
        kind = _detect_type(header)
        detected = kind.mime if kind else "text/plain" if _plain_text(header) else ""
        mime = self.mime or detected
        if self.mime:
            extension = self.extension()
        else:
            extension = "." + kind.extension if kind else ".txt"
        # Fixed extensions also prevent local MIME registrations mapping a
        # normally passive type to an executable suffix.
        extension = PASSIVE_MIME_EXTENSIONS.get(mime, extension)
        warning = None
        if mime not in PASSIVE_MIME_EXTENSIONS:
            warning = "This file type is not on the list of formats that open without a warning."
        elif not detected:
            warning = "The contents could not be verified as the declared file type."
        elif PASSIVE_MIME_EXTENSIONS.get(detected) != extension:
            warning = "The detected contents do not match the declared file type."
        return PreviewInfo(self.mime, detected, extension, warning)

    def decode(self) -> bytes:
        raw = _unquote(self.value[self.offset :])
        return base64.b64decode(raw, validate=True) if self.base64 else raw


class PlainBase64(DataURI):
    """Likely standard base64 without a data URI header; MIME is inferred."""

    @classmethod
    def parse(cls, value: str) -> PlainBase64 | None:
        # Inspect bounded source slices only: hints never decode the payload or
        # scan an entire multi-megabyte string. Full validation happens on >.
        probe = value[:BASE64_PROBE_CHARS].translate(BASE64_WHITESPACE)
        tail = value[-128:].translate(BASE64_WHITESPACE)
        if len(probe) < 8 or (len(probe) < 16 and not tail.endswith("=")):
            return None
        if not all(re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", part) for part in (probe, tail)):
            return None
        if re.fullmatch(r"[0-9a-fA-F]+", probe):
            return None  # Hex IDs and hashes are too ambiguous.
        if not any(char in "+/=" for char in probe + tail) and not (
            re.search(r"[A-Z]", probe) and re.search(r"[a-z]", probe)
        ):
            return None  # Ordinary single-case words are too ambiguous.
        return cls(value, 0, "", True)

    @staticmethod
    def _decode(text: str) -> bytes:
        raw = text.translate(BASE64_WHITESPACE).encode("ascii")
        return base64.b64decode(raw + b"=" * (-len(raw) % 4), validate=True)

    def prefix(self) -> bytes:
        count = ((SIGNATURE_BYTES + 2) // 3) * 4
        # Cap source scanning as well as decoded output, allowing normal line
        # wrapping without unbounded searches through whitespace.
        raw = self.value[: count * 2].translate(BASE64_WHITESPACE)[:count]
        if len(self.value) > count * 2:
            # A source-boundary cut may leave a partial quartet after removing
            # whitespace. Only decode complete groups from this partial sample;
            # decode() still validates the entire payload before opening.
            raw = raw[: len(raw) // 4 * 4]
        return self._decode(raw)[:SIGNATURE_BYTES]

    def decode(self) -> bytes:
        return self._decode(self.value)


class NativePreview:
    """Own files for one app; background work never owns the viewer lifetime."""

    def __init__(self) -> None:
        self.launcher = native_launcher()
        self.directory: str | None = None
        self._lock = threading.Lock()
        self._closed = False

    def available(self, value: str) -> bool:
        # Hints inspect bounded source slices, never the full payload.
        return self.launcher is not None and (
            DataURI.parse(value) is not None or PlainBase64.parse(value) is not None
        )

    def open(
        self, value: str, failed: Callable[[], None], confirm: ConfirmPreview | None = None
    ) -> None:
        threading.Thread(target=self._open, args=(value, failed, confirm), daemon=True).start()

    def _open(
        self, value: str, failed: Callable[[], None], confirm: ConfirmPreview | None = None
    ) -> None:
        try:
            with self._lock:
                if self._closed:
                    return
            uri = DataURI.parse(value) or PlainBase64.parse(value)
            if uri is None or self.launcher is None:
                raise ValueError("No native preview")
            info = uri.preview_info()
            if info.warning:
                if confirm is None:
                    failed()  # Fail closed when no UI can obtain consent.
                else:

                    def allow() -> None:
                        threading.Thread(
                            target=self._launch,
                            args=(uri, info.extension, failed),
                            daemon=True,
                        ).start()

                    confirm(info, allow)
                return
            self._launch(uri, info.extension, failed)
        except (OSError, ValueError):
            failed()

    def _launch(self, uri: DataURI, extension: str, failed: Callable[[], None]) -> None:
        try:
            with self._lock:
                if self._closed:
                    return
            payload = uri.decode()
            with self._lock:
                if self._closed:
                    return
                if self.directory is None:
                    self.directory = tempfile.mkdtemp(prefix="jlv-")
                with tempfile.NamedTemporaryFile(
                    dir=self.directory, suffix=extension, delete=False
                ) as f:
                    f.write(payload)
                    path = str(Path(f.name).absolute())
                if self.launcher == "startfile":
                    os.startfile(path)
                    return
                child = subprocess.Popen(
                    [self.launcher, path],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
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
