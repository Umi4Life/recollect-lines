#!/usr/bin/env python3
"""Validate built wheel and sdist artifacts from dist/ (not a source checkout).

Installs each distribution into a disposable virtual environment and runs the
same offline entry-point smoke checks as clean_install_acceptance.py. Also
scans archive member names for packaging hygiene violations.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# Member paths that must never appear in public distributions.
DENY_SUBSTRINGS = (
    ".env",
    ".recollect",
    "credentials.json",
    "credentials.yaml",
    "credentials.yml",
    "secret",
    "sk-",
    "/mnt/",
    "/nas/",
    "field-test",
    "field_test",
    "providers.json",
    "__pycache__",
    ".pytest_cache",
)

# Wheel install layout: only package + dist-info metadata.
WHEEL_ALLOW_PREFIXES = ("recollect_lines/", "recollect_lines-", "recollect_lines-0")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, text=True, capture_output=True, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise SystemExit(1)


def _archive_members(archive: Path) -> list[str]:
    if archive.suffix == ".whl":
        with zipfile.ZipFile(archive) as zf:
            return zf.namelist()
    if archive.suffix == ".gz" and archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as tf:
            return [m.name for m in tf.getmembers() if m.isfile()]
    raise ValueError(f"unsupported archive: {archive}")


def _deny_matches(members: list[str]) -> list[str]:
    hits: list[str] = []
    for name in members:
        lowered = name.lower()
        for needle in DENY_SUBSTRINGS:
            if needle in lowered:
                hits.append(name)
                break
    return hits


def _wheel_layout_ok(members: list[str]) -> bool:
    for name in members:
        if name.endswith("/"):
            continue
        if any(name.startswith(prefix) for prefix in WHEEL_ALLOW_PREFIXES):
            continue
        return False
    return True


def inspect_artifacts(dist_dir: Path) -> tuple[Path, Path]:
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    check("exactly one wheel in dist/", len(wheels) == 1, str(wheels))
    check("exactly one sdist in dist/", len(sdists) == 1, str(sdists))
    wheel, sdist = wheels[0], sdists[0]
    check("wheel name matches recollect_lines", "recollect_lines" in wheel.name)
    check("sdist name matches project", sdist.name.startswith("recollect_lines-"))

    for archive in (wheel, sdist):
        members = _archive_members(archive)
        denied = _deny_matches(members)
        check(f"{archive.name} has no denied paths", not denied, ", ".join(denied[:5]))
        check(
            f"{archive.name} includes recollect_lines package",
            any("recollect_lines/" in m for m in members),
        )

    wheel_members = _archive_members(wheel)
    check("wheel layout is package + metadata only", _wheel_layout_ok(wheel_members))

    print(f"Artifact hygiene OK: {wheel.name}, {sdist.name}")
    return wheel, sdist


def _venv_paths(venv_dir: Path) -> tuple[Path, Path]:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe", venv_dir / "Scripts"
    return venv_dir / "bin" / "python", venv_dir / "bin"


def _install_and_smoke(label: str, install_cmd: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="recollect-dist-accept-") as tmp:
        tmp_path = Path(tmp)
        venv_dir = tmp_path / "venv"
        venv.create(venv_dir, with_pip=True)
        python, scripts = _venv_paths(venv_dir)

        run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
        run([str(python), "-m", "pip", "install", *install_cmd])

        env = {"PATH": f"{scripts}{os.pathsep}{os.environ.get('PATH', '')}"}

        help_result = run(["recollect-lines", "--help"], env=env)
        check(f"{label}: recollect-lines --help", "usage:" in help_result.stdout.lower())

        doctor_result = run(
            ["recollect-lines", "--home", str(tmp_path / "broker"), "doctor", "--json"],
            env=env,
        )
        check(
            f"{label}: doctor --json",
            '"doctor_schema_version"' in doctor_result.stdout,
        )
        check(f"{label}: doctor has no sk- tokens", "sk-" not in doctor_result.stdout)

        mcp_result = run(["recollect-mcp", "--help"], env=env)
        check(f"{label}: recollect-mcp --help", "usage:" in mcp_result.stdout.lower())

        legacy = shutil.which("recollect", path=env["PATH"])
        check(f"{label}: legacy recollect absent", legacy is None, f"found at {legacy}")


def main() -> int:
    dist_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DIST
    check("dist/ exists", dist_dir.is_dir(), str(dist_dir))

    wheel, sdist = inspect_artifacts(dist_dir)

    _install_and_smoke("wheel", [str(wheel)])
    _install_and_smoke("sdist", [str(sdist)])

    print(
        "Dist-artifact acceptance PASSED: wheel and sdist install cleanly; "
        "entry points work; archives pass hygiene checks."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
