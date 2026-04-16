"""Bootstrap script: ensures venv and dependencies exist, then runs the target script.

Used by plugin hooks and MCP server to auto-install on first run.
Usage: python bootstrap.py <script.py> [args...]
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()
VENV_DIR = PROJECT_DIR / ".venv"
VENV_PYTHON = VENV_DIR / ("Scripts" if sys.platform == "win32" else "bin") / "python"
VENV_PIP = VENV_DIR / ("Scripts" if sys.platform == "win32" else "bin") / "pip"


def _has_command(name: str) -> bool:
    return shutil.which(name) is not None


def _ensure_uv():
    """Install uv if not present."""
    if _has_command("uv"):
        return
    print("Installing uv...", file=sys.stderr)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "uv"],
        capture_output=True,
    )


def _has_gpu() -> bool:
    try:
        r = subprocess.run(["nvidia-smi"], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def ensure_venv():
    """Create venv and install deps if needed."""
    if VENV_PYTHON.exists():
        return

    _ensure_uv()

    gpu = _has_gpu()
    cmd = ["uv", "sync"]
    if gpu:
        cmd += ["--extra", "gpu"]

    subprocess.run(cmd, cwd=str(PROJECT_DIR), capture_output=True)


def main():
    if len(sys.argv) < 2:
        print("Usage: python bootstrap.py <script.py> [args...]", file=sys.stderr)
        sys.exit(1)

    ensure_venv()

    # Run target script with venv python
    target = sys.argv[1:]
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON)] + target)


if __name__ == "__main__":
    main()
