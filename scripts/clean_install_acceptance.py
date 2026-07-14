#!/usr/bin/env python3
"""Offline clean-install acceptance for Phase 7A.

Builds a fresh virtual environment, installs this package from local artifacts
(no network, no PYTHONPATH=src shortcut), and proves console entry points.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="recollect-clean-install-") as tmp:
        tmp_path = Path(tmp)
        venv_dir = tmp_path / "venv"
        venv.create(venv_dir, with_pip=True)
        if sys.platform == "win32":
            python = venv_dir / "Scripts" / "python.exe"
            scripts = venv_dir / "Scripts"
        else:
            python = venv_dir / "bin" / "python"
            scripts = venv_dir / "bin"

        print("Bootstrapping build tooling into fresh venv...")
        run([str(python), "-m", "pip", "install", "setuptools>=68", "wheel"])
        print("Building and installing package wheel (offline package install)...")
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()
        run([
            str(python), "-m", "pip", "wheel", str(ROOT),
            "-w", str(wheel_dir), "--no-deps", "--no-build-isolation",
        ])
        run([
            str(python), "-m", "pip", "install",
            "--no-index", f"--find-links={wheel_dir}", "recollect-lines",
        ])

        env = {"PATH": f"{scripts}{os.pathsep}{os.environ.get('PATH', '')}"}

        help_result = run(["recollect-lines", "--help"], env=env)
        check("recollect-lines --help succeeds", "usage:" in help_result.stdout.lower())

        doctor_result = run(
            ["recollect-lines", "--home", str(tmp_path / "broker"), "doctor", "--json"],
            env=env,
        )
        check("recollect-lines doctor --json succeeds", '"doctor_schema_version"' in doctor_result.stdout)
        check("doctor JSON has no raw secret material", "sk-" not in doctor_result.stdout)

        mcp_result = run(["recollect-mcp", "--help"], env=env)
        check("recollect-mcp --help succeeds", "usage:" in mcp_result.stdout.lower())

        legacy = shutil.which("recollect", path=env["PATH"])
        check("recollect console script is absent", legacy is None, f"found at {legacy}")

    print(
        "Clean-install acceptance PASSED: fresh venv install exposes "
        "recollect-lines and recollect-mcp; legacy recollect is absent."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
