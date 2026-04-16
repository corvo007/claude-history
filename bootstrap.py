"""Bootstrap script: ensures venv and dependencies exist, then runs the target script.

Used by plugin hooks and MCP server to auto-install on first run.
Usage: python bootstrap.py <script.py> [args...]
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()
VENV_DIR = PROJECT_DIR / ".venv"
VENV_PYTHON = VENV_DIR / ("Scripts" if sys.platform == "win32" else "bin") / "python"


def ensure_venv():
    """Create venv and install deps if needed."""
    if VENV_PYTHON.exists():
        return

    # Create venv with uv
    subprocess.run(
        ["uv", "sync"],
        cwd=str(PROJECT_DIR),
        capture_output=True,
    )

    # Try GPU deps if nvidia-smi available
    try:
        r = subprocess.run(["nvidia-smi"], capture_output=True, timeout=10)
        if r.returncode == 0:
            subprocess.run(
                ["uv", "sync", "--extra", "gpu"],
                cwd=str(PROJECT_DIR),
                capture_output=True,
            )
    except Exception:
        pass


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
