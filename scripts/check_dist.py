"""Validate built distributions and smoke-test an isolated wheel installation."""

import configparser
import email
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
# Every module in the package source, so new or removed modules need no edits here.
PACKAGE_FILES = sorted(
    path.relative_to(ROOT / "src").as_posix()
    for path in (ROOT / "src" / "jlv").iterdir()
    if path.is_file() and path.suffix == ".py"
)


def main() -> None:
    """Check package contents, metadata, and the CLI from the built wheel."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    wheels = list((ROOT / "dist").glob("*.whl"))
    sdists = list((ROOT / "dist").glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1, "Expected one wheel and one source distribution"
    wheel_path = wheels[0]
    assert wheel_path.name.endswith("-py3-none-any.whl"), "Expected a pure Python wheel"

    with ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
        for required in PACKAGE_FILES:
            assert required in names, f"Missing from wheel: {required}"
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(wheel.read(metadata_name))
        assert metadata["Name"] == project["name"], "Incorrect distribution name"
        assert metadata["Version"] == project["version"], "Incorrect distribution version"
        assert metadata["Requires-Python"] == project["requires-python"]
        entry_name = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        entries = configparser.ConfigParser()
        entries.read_string(wheel.read(entry_name).decode())
        assert dict(entries["console_scripts"]) == project["scripts"], "Incorrect CLI entry points"

    with tarfile.open(sdists[0], "r:gz") as sdist:
        relative_names = {"/".join(PurePosixPath(name).parts[1:]) for name in sdist.getnames()}
        for required in ("pyproject.toml", "README.md", *(f"src/{name}" for name in PACKAGE_FILES)):
            assert required in relative_names, f"Missing from source distribution: {required}"
        metadata_file = sdist.extractfile(
            f"{wheel_path.name.split('-')[0]}-{project['version']}/PKG-INFO"
        )
        assert metadata_file is not None, "Missing source distribution metadata"
        metadata = email.message_from_bytes(metadata_file.read())
        assert metadata["Name"] == project["name"], "Incorrect source distribution name"
        assert metadata["Version"] == project["version"], "Incorrect source distribution version"

    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to check the installed wheel")
    with TemporaryDirectory(prefix="jlv-wheel-check-") as tmp:
        environment = Path(tmp) / "venv"
        subprocess.run([uv, "venv", "--python", sys.executable, str(environment)], check=True)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            [uv, "pip", "install", "--python", str(python), str(wheel_path)],
            check=True,
            timeout=120,
        )
        # Isolated mode and an external working directory prevent an editable
        # checkout from concealing missing files in the actual wheel.
        subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                (
                    "import importlib.metadata, pathlib, sys, jlv; "
                    "assert importlib.metadata.version(sys.argv[1]) == sys.argv[2]; "
                    "assert pathlib.Path(jlv.__file__).is_relative_to(pathlib.Path(sys.prefix))"
                ),
                project["name"],
                project["version"],
            ],
            cwd=tmp,
            check=True,
            timeout=30,
        )
        # The viewer itself needs a terminal; check the installed entry point's usage path.
        command = environment / ("Scripts/jlv.exe" if os.name == "nt" else "bin/jlv")
        result = subprocess.run([str(command)], cwd=tmp, capture_output=True, text=True, timeout=30)
        assert result.returncode == 1, result
        assert result.stderr.strip() == "Usage: jlv <file.jsonl>", result
        assert "Traceback" not in result.stderr, result
    print("Validated distributions; isolated wheel import and CLI checks passed.")


if __name__ == "__main__":
    main()
