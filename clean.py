"""Claude Code conversation history cleaner.

Reads raw JSONL session files from ~/.claude/projects/ and produces
a structured SQLite database with cleaned conversation data.
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

SKIP_TYPES = frozenset({
    "system", "progress", "queue-operation", "file-history-snapshot",
    "permission-mode", "last-prompt", "attachment", "agent-name",
    "custom-title",
})

# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    project      TEXT,
    start_time   TEXT,
    end_time     TEXT,
    version      TEXT,
    git_branch   TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL REFERENCES sessions(session_id),
    turn_index     INTEGER NOT NULL,
    timestamp      TEXT,
    user_text      TEXT,
    assistant_text TEXT,
    thinking       TEXT
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id       INTEGER NOT NULL REFERENCES turns(id),
    tool_name     TEXT,
    summary       TEXT,
    agent_prompt  TEXT,
    agent_result  TEXT,
    is_error      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_state (
    file_path  TEXT PRIMARY KEY,
    mtime      REAL,
    size       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_tool_calls_turn ON tool_calls(turn_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name ON tool_calls(tool_name);
"""

# ---------------------------------------------------------------------------
# Tool summary extraction
# ---------------------------------------------------------------------------

def summarize_tool(name: str, inp: dict) -> str:
    """Extract a concise summary from a tool_use input, per-tool rules."""
    match name:
        case "Edit":
            return inp.get("file_path", "")
        case "Write":
            return inp.get("file_path", "")
        case "Read":
            return inp.get("file_path", "")
        case "Bash":
            return inp.get("description") or inp.get("command", "")
        case "Grep":
            pattern = inp.get("pattern", "")
            path = inp.get("path", "")
            return f"{pattern} in {path}" if path else pattern
        case "Glob":
            return inp.get("pattern", "")
        case "WebFetch":
            return inp.get("url", "")
        case "WebSearch":
            return inp.get("query", "")
        case "ToolSearch":
            return inp.get("query", "")
        case "Skill":
            return inp.get("skill", "")
        case "Agent":
            return inp.get("description", "")
        case "TaskCreate" | "TaskUpdate":
            return inp.get("description") or inp.get("subject", "")
        case _:
            # MCP tools and others: just the name is enough
            if name.startswith("mcp__"):
                # mcp__chrome-devtools__click -> chrome-devtools/click
                parts = name.split("__")
                return "/".join(parts[1:]) if len(parts) > 1 else name
            return ""


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------

def extract_text_from_content(content) -> str:
    """Extract plain text from a message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def extract_thinking_from_content(content) -> str:
    """Extract thinking blocks from assistant content."""
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "thinking":
            parts.append(block.get("thinking", ""))
    return "\n".join(parts)


def extract_tool_uses(content) -> list[dict]:
    """Extract tool_use blocks from assistant content."""
    if not isinstance(content, list):
        return []
    tools = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tools.append({
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input": block.get("input", {}),
            })
    return tools


def extract_tool_results(content) -> dict[str, dict]:
    """Extract tool_result blocks, keyed by tool_use_id."""
    if not isinstance(content, list):
        return {}
    results = {}
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            tool_use_id = block.get("tool_use_id", "")
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                # Extract text from structured content
                text_parts = []
                for sub in result_content:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        text_parts.append(sub.get("text", ""))
                result_content = "\n".join(text_parts)
            is_error = block.get("is_error", False)
            results[tool_use_id] = {
                "content": result_content,
                "is_error": is_error,
            }
    return results


def has_user_text(content) -> bool:
    """Check if a user message has actual text (not just tool_results)."""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                if block.get("text", "").strip():
                    return True
    return False


def parse_session(lines: list[dict]) -> dict:
    """Parse a session's JSONL lines into structured data.

    Returns {meta: {...}, turns: [{user_text, assistant_text, thinking, tools, timestamp}, ...]}
    """
    meta = {
        "session_id": None,
        "project": None,
        "start_time": None,
        "end_time": None,
        "version": None,
        "git_branch": None,
    }
    turns: list[dict] = []
    current_turn = None

    # Pending agent tool_uses: id -> {name, input}
    pending_agent_tools: dict[str, dict] = {}

    for obj in lines:
        msg_type = obj.get("type", "")

        if msg_type in SKIP_TYPES:
            continue

        # Extract session metadata from any message that has it
        if not meta["session_id"] and obj.get("sessionId"):
            meta["session_id"] = obj.get("sessionId")
        if not meta["project"] and obj.get("cwd"):
            meta["project"] = obj.get("cwd")
        if not meta["version"] and obj.get("version"):
            meta["version"] = obj.get("version")
        if not meta["git_branch"] and obj.get("gitBranch"):
            meta["git_branch"] = obj.get("gitBranch")

        timestamp = obj.get("timestamp")
        if timestamp:
            if not meta["start_time"]:
                meta["start_time"] = timestamp
            meta["end_time"] = timestamp

        if msg_type == "user":
            msg = obj.get("message", {})
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")

            # Always capture tool_results first (for Agent result correlation)
            if current_turn is not None:
                tool_results = extract_tool_results(content)
                for tool_use_id, result in tool_results.items():
                    if tool_use_id in pending_agent_tools:
                        pending_agent_tools.pop(tool_use_id)
                        for tc in current_turn["tools"]:
                            if tc.get("_tool_use_id") == tool_use_id:
                                tc["agent_result"] = result["content"]
                                tc["is_error"] = result["is_error"]
                                break

            # User message with tool_results only → part of ongoing turn
            if not has_user_text(content):
                continue

            # New turn starts
            if current_turn is not None:
                turns.append(current_turn)

            current_turn = {
                "timestamp": timestamp,
                "user_text": extract_text_from_content(content),
                "assistant_text": "",
                "thinking": "",
                "tools": [],
            }
            pending_agent_tools = {}

        elif msg_type == "assistant":
            if current_turn is None:
                # Assistant message before any user message — create implicit turn
                current_turn = {
                    "timestamp": timestamp,
                    "user_text": "",
                    "assistant_text": "",
                    "thinking": "",
                    "tools": [],
                }

            msg = obj.get("message", {})
            if not isinstance(msg, dict):
                continue
            content = msg.get("content", [])

            # Accumulate text
            text = extract_text_from_content(content)
            if text:
                if current_turn["assistant_text"]:
                    current_turn["assistant_text"] += "\n" + text
                else:
                    current_turn["assistant_text"] = text

            # Accumulate thinking
            thinking = extract_thinking_from_content(content)
            if thinking:
                if current_turn["thinking"]:
                    current_turn["thinking"] += "\n" + thinking
                else:
                    current_turn["thinking"] = thinking

            # Extract tool uses
            for tool in extract_tool_uses(content):
                tool_name = tool["name"]
                tool_input = tool["input"]
                tool_id = tool["id"]

                tc = {
                    "_tool_use_id": tool_id,
                    "tool_name": tool_name,
                    "summary": summarize_tool(tool_name, tool_input),
                    "agent_prompt": None,
                    "agent_result": None,
                    "is_error": False,
                }

                if tool_name == "Agent":
                    tc["agent_prompt"] = tool_input.get("prompt", "")
                    pending_agent_tools[tool_id] = tool

                current_turn["tools"].append(tc)

    # Don't forget the last turn
    if current_turn is not None:
        turns.append(current_turn)

    return {"meta": meta, "turns": turns}


# ---------------------------------------------------------------------------
# SQLite output
# ---------------------------------------------------------------------------

def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    return conn


def write_session(conn: sqlite3.Connection, data: dict):
    meta = data["meta"]
    session_id = meta["session_id"]
    if not session_id:
        return

    # Upsert session
    conn.execute(
        """INSERT INTO sessions (session_id, project, start_time, end_time, version, git_branch)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               end_time=excluded.end_time,
               version=excluded.version""",
        (session_id, meta["project"], meta["start_time"], meta["end_time"],
         meta["version"], meta["git_branch"]),
    )

    # Delete existing turns for this session (for re-processing)
    existing_turn_ids = [
        row[0] for row in
        conn.execute("SELECT id FROM turns WHERE session_id=?", (session_id,)).fetchall()
    ]
    if existing_turn_ids:
        placeholders = ",".join("?" * len(existing_turn_ids))
        conn.execute(f"DELETE FROM tool_calls WHERE turn_id IN ({placeholders})", existing_turn_ids)
        has_vec = conn.execute("SELECT 1 FROM sqlite_master WHERE name='vec_turns'").fetchone()
        if has_vec:
            conn.execute(f"DELETE FROM vec_turns WHERE turn_id IN ({placeholders})", existing_turn_ids)
        has_fts = conn.execute("SELECT 1 FROM sqlite_master WHERE name='turns_fts'").fetchone()
        if has_fts:
            conn.execute("INSERT INTO turns_fts(turns_fts) VALUES('rebuild')")
        conn.execute("DELETE FROM turns WHERE session_id=?", (session_id,))

    # Insert turns
    for i, turn in enumerate(data["turns"]):
        cursor = conn.execute(
            """INSERT INTO turns (session_id, turn_index, timestamp, user_text, assistant_text, thinking)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, i, turn["timestamp"], turn["user_text"],
             turn["assistant_text"], turn["thinking"] or None),
        )
        turn_id = cursor.lastrowid

        for tc in turn["tools"]:
            conn.execute(
                """INSERT INTO tool_calls (turn_id, tool_name, summary, agent_prompt, agent_result, is_error)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (turn_id, tc["tool_name"], tc["summary"],
                 tc.get("agent_prompt"), tc.get("agent_result"),
                 1 if tc.get("is_error") else 0),
            )


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def discover_sessions(
    source_dir: Path,
    project_filter: str | None = None,
    session_filter: str | None = None,
) -> list[Path]:
    """Find all session JSONL files, excluding agent files."""
    files = []
    for project_dir in sorted(source_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        if project_filter and project_filter not in str(project_dir):
            continue
        for f in sorted(project_dir.glob("*.jsonl")):
            if f.name.startswith("agent-"):
                continue
            if session_filter and session_filter not in f.stem:
                continue
            files.append(f)
    return files


def get_file_stat(fpath: Path) -> tuple[float, int]:
    """Return (mtime, size) for a file, captured once to avoid races."""
    stat = fpath.stat()
    return stat.st_mtime, stat.st_size


def should_process(conn: sqlite3.Connection, fpath: Path, mtime: float, size: int) -> bool:
    """Check if file needs reprocessing based on mtime/size."""
    row = conn.execute(
        "SELECT mtime, size FROM sync_state WHERE file_path=?",
        (str(fpath),),
    ).fetchone()
    if row and row[0] == mtime and row[1] == size:
        return False
    return True


def mark_processed(conn: sqlite3.Connection, fpath: Path, mtime: float, size: int):
    conn.execute(
        """INSERT INTO sync_state (file_path, mtime, size)
           VALUES (?, ?, ?)
           ON CONFLICT(file_path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size""",
        (str(fpath), mtime, size),
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_file(fpath: Path) -> dict | None:
    """Read and parse a single JSONL session file."""
    lines = []
    with open(fpath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not lines:
        return None
    return parse_session(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Clean Claude Code conversation history into a structured SQLite database."
    )
    parser.add_argument(
        "--source", type=Path, default=CLAUDE_PROJECTS_DIR,
        help=f"Source directory (default: {CLAUDE_PROJECTS_DIR})",
    )
    parser.add_argument(
        "--db", type=Path, default=Path("history.db"),
        help="Output database path (default: ./history.db)",
    )
    parser.add_argument(
        "--project", type=str, default=None,
        help="Filter by project path substring",
    )
    parser.add_argument(
        "--session", type=str, default=None,
        help="Filter by session ID substring",
    )
    parser.add_argument(
        "--incremental", action="store_true",
        help="Skip files that haven't changed since last run",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print progress details",
    )
    args = parser.parse_args()

    if not args.source.is_dir():
        print(f"Error: source directory not found: {args.source}", file=sys.stderr)
        sys.exit(1)

    # Discover files
    files = discover_sessions(args.source, args.project, args.session)
    print(f"Found {len(files)} session files")

    # Init database
    conn = init_db(args.db)

    processed = 0
    skipped = 0
    total_turns = 0
    total_tools = 0

    errors = 0

    for fpath in files:
        mtime, size = get_file_stat(fpath)

        if args.incremental and not should_process(conn, fpath, mtime, size):
            skipped += 1
            continue

        if args.verbose:
            print(f"  Processing: {fpath.name} ({size:,} bytes)")

        try:
            data = process_file(fpath)
            if data and data["meta"]["session_id"]:
                write_session(conn, data)
                mark_processed(conn, fpath, mtime, size)
                conn.commit()

                n_turns = len(data["turns"])
                n_tools = sum(len(t["tools"]) for t in data["turns"])
                total_turns += n_turns
                total_tools += n_tools
                processed += 1

                if args.verbose:
                    print(f"    → {n_turns} turns, {n_tools} tool calls")
        except Exception as e:
            conn.rollback()
            errors += 1
            print(f"  Error processing {fpath.name}: {e}", file=sys.stderr)

    # Print summary
    db_size = args.db.stat().st_size
    print(f"\nDone!")
    print(f"  Processed: {processed} sessions ({skipped} skipped, {errors} errors)")
    print(f"  Turns: {total_turns}")
    print(f"  Tool calls: {total_tools}")
    print(f"  Database: {args.db} ({db_size:,} bytes / {db_size/1024/1024:.1f} MB)")

    conn.close()


if __name__ == "__main__":
    main()
