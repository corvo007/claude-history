"""One-click setup for claude-history.

Usage:
    uv run python setup.py          # Auto-detect GPU
    uv run python setup.py --cpu    # Force CPU only
    uv run python setup.py --skip-index  # Config only, skip initial indexing
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()
VENV_PYTHON = PROJECT_DIR / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / "python"
DB_PATH = PROJECT_DIR / "history.db"


def find_claude_settings() -> Path:
    """Find Claude Code settings.json."""
    if sys.platform == "win32":
        candidates = [
            Path.home() / ".claude" / "settings.json",
        ]
    else:
        candidates = [
            Path.home() / ".claude" / "settings.json",
            Path.home() / ".config" / "claude" / "settings.json",
        ]
    for p in candidates:
        if p.exists():
            return p
    # Default: create in ~/.claude/
    p = Path.home() / ".claude" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def check_gpu() -> bool:
    """Check if NVIDIA GPU is available."""
    try:
        result = subprocess.run(
            ["nvidia-smi"], capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def install_deps(use_gpu: bool):
    """Install Python dependencies via uv."""
    print("\n[1/4] Installing dependencies...")
    cmd = ["uv", "sync"]
    if use_gpu:
        cmd += ["--extra", "gpu"]
    result = subprocess.run(cmd, cwd=str(PROJECT_DIR))
    if result.returncode != 0:
        print("Error: dependency installation failed.", file=sys.stderr)
        sys.exit(1)
    print("  Done.")


def run_initial_index():
    """Run clean + embed to build the database."""
    python = str(VENV_PYTHON)

    print("\n[2/4] Cleaning conversation history...")
    result = subprocess.run(
        [python, str(PROJECT_DIR / "clean.py"), "--db", str(DB_PATH)],
        cwd=str(PROJECT_DIR),
    )
    if result.returncode != 0:
        print("Warning: cleaning had errors, continuing anyway.", file=sys.stderr)

    print("\n[3/4] Building search index (embedding + FTS)...")
    result = subprocess.run(
        [python, str(PROJECT_DIR / "embed.py"), "--db", str(DB_PATH)],
        cwd=str(PROJECT_DIR),
    )
    if result.returncode != 0:
        print("Warning: embedding had errors, continuing anyway.", file=sys.stderr)


def configure_mcp():
    """Add claude-history MCP server to Claude Code settings."""
    print("\n[4/4] Configuring MCP server...")

    settings_path = find_claude_settings()

    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    else:
        settings = {}

    # Add MCP server
    servers = settings.setdefault("mcpServers", {})
    servers["claude-history"] = {
        "command": str(VENV_PYTHON),
        "args": [str(PROJECT_DIR / "mcp_server.py")],
        "env": {
            "CLAUDE_HISTORY_DB": str(DB_PATH),
        },
    }


    settings_path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"  MCP server configured in {settings_path}")


def main():
    parser = argparse.ArgumentParser(description="Set up claude-history")
    parser.add_argument("--cpu", action="store_true", help="Force CPU mode (skip GPU detection)")
    parser.add_argument("--skip-index", action="store_true", help="Skip initial indexing")
    args = parser.parse_args()

    print("=" * 50)
    print("  claude-history setup")
    print("=" * 50)

    # Detect GPU
    if args.cpu:
        use_gpu = False
        print("\nMode: CPU (forced)")
    else:
        use_gpu = check_gpu()
        print(f"\nMode: {'GPU' if use_gpu else 'CPU'} (auto-detected)")

    # Install
    install_deps(use_gpu)

    # Index
    if args.skip_index:
        print("\n[2/4] Skipping initial index (--skip-index)")
        print("[3/4] Skipping initial index (--skip-index)")
    else:
        run_initial_index()

    # Configure
    configure_mcp()

    # Summary
    print("\n" + "=" * 50)
    print("  Setup complete!")
    print("=" * 50)
    print(f"""
  Database: {DB_PATH}
  MCP server: configured

  Restart Claude Code to activate.
  The server auto-indexes new conversations on each startup.

  Manual commands:
    uv run python clean.py --incremental   # Clean new sessions
    uv run python embed.py                 # Embed new turns
    uv run python search.py search "query" # Search from CLI
""")


if __name__ == "__main__":
    main()
