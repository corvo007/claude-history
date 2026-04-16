"""MCP server for Claude Code conversation history search.

Replaces episodic-memory with a lightweight server backed by history.db.
Exposes search, read, and list tools over stdio.
Auto-indexes new conversations on startup.
"""

import logging
import os
import sqlite3
import struct
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
from fastmcp import FastMCP

logger = logging.getLogger("claude-history")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get(
    "CLAUDE_HISTORY_DB",
    str(Path(__file__).parent / "history.db"),
)

mcp = FastMCP(
    "claude-history",
    instructions=(
        "Search and read the user's past Claude Code conversations. "
        "Use 'search' to find relevant past discussions by topic. "
        "Use 'read' to read the full conversation of a specific session. "
        "Use 'list_sessions' to browse available sessions."
    ),
)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    # Load sqlite-vec if available
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception:
        pass
    return conn


def _has_vectors(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='vec_turns'"
    ).fetchone()[0] > 0


_embedder = None

def _get_embedder():
    """Lazy-load embedder on first search that needs it."""
    global _embedder
    if _embedder is None:
        # Add NVIDIA DLL paths
        venv = Path(__file__).parent / ".venv" / "Lib" / "site-packages" / "nvidia"
        for sub in ["cudnn/bin", "cublas/bin", "cuda_runtime/bin", "cuda_nvrtc/bin"]:
            dll_dir = venv / sub
            if dll_dir.is_dir():
                os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")

        from embed import create_embedder
        _embedder = create_embedder()
    return _embedder


def _serialize_f32(vec: np.ndarray) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search(
    query: str,
    mode: str = "hybrid",
    limit: int = 10,
    project: str | None = None,
) -> str:
    """Search past Claude Code conversations.

    Args:
        query: Search query (Chinese or English)
        mode: "hybrid" (semantic + keyword, default), "fts" (keyword only, fast), or "dense" (semantic only)
        limit: Max results (default 10)
        project: Filter by project name substring
    """
    conn = _get_conn()
    has_vec = _has_vectors(conn)

    results: list[tuple[int, float]] = []

    if mode == "fts" or (mode != "dense" and not has_vec):
        # FTS5 keyword search
        safe_query = '"' + query.replace('"', '""') + '"'
        try:
            rows = conn.execute(
                "SELECT rowid, rank FROM turns_fts WHERE turns_fts MATCH ? ORDER BY rank LIMIT ?",
                (safe_query, limit),
            ).fetchall()
        except Exception:
            terms = query.split()
            parts = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms if t)
            rows = conn.execute(
                "SELECT rowid, rank FROM turns_fts WHERE turns_fts MATCH ? ORDER BY rank LIMIT ?",
                (parts, limit),
            ).fetchall() if parts else []
        results = [(tid, -rank) for tid, rank in rows]

    elif mode == "dense" and has_vec:
        embedder = _get_embedder()
        query_vec = _serialize_f32(embedder.embed_one(query))
        rows = conn.execute(
            "SELECT turn_id, distance FROM vec_turns WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (query_vec, limit),
        ).fetchall()
        max_dist = max(d for _, d in rows) if rows else 1.0
        results = [(tid, 1 - dist / max_dist) for tid, dist in rows]

    else:  # hybrid
        embedder = _get_embedder()
        query_vec = _serialize_f32(embedder.embed_one(query))

        dense_rows = conn.execute(
            "SELECT turn_id, distance FROM vec_turns WHERE embedding MATCH ? ORDER BY distance LIMIT 30",
            (query_vec,),
        ).fetchall()

        safe_query = '"' + query.replace('"', '""') + '"'
        try:
            fts_rows = conn.execute(
                "SELECT rowid, rank FROM turns_fts WHERE turns_fts MATCH ? ORDER BY rank LIMIT 30",
                (safe_query,),
            ).fetchall()
        except Exception:
            terms = query.split()
            parts = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms if t)
            fts_rows = conn.execute(
                "SELECT rowid, rank FROM turns_fts WHERE turns_fts MATCH ? ORDER BY rank LIMIT 30",
                (parts,),
            ).fetchall() if parts else []

        # RRF fusion
        K = 60
        scores: dict[int, float] = {}
        for rank, (tid, _) in enumerate(dense_rows):
            scores[tid] = scores.get(tid, 0) + 0.6 / (K + rank)
        for rank, (tid, _) in enumerate(fts_rows):
            scores[tid] = scores.get(tid, 0) + 0.4 / (K + rank)
        results = sorted(scores.items(), key=lambda x: -x[1])[:limit]

    # Filter by project
    if project:
        filtered = []
        for tid, score in results:
            row = conn.execute(
                "SELECT s.project FROM turns t JOIN sessions s ON s.session_id=t.session_id WHERE t.id=?",
                (tid,),
            ).fetchone()
            if row and project.lower() in (row[0] or "").lower():
                filtered.append((tid, score))
        results = filtered

    # Format output
    if not results:
        conn.close()
        return "No results found."

    lines = []
    for i, (tid, score) in enumerate(results, 1):
        row = conn.execute("""
            SELECT t.user_text, t.assistant_text, t.timestamp,
                   s.project, t.session_id, t.turn_index
            FROM turns t JOIN sessions s ON s.session_id = t.session_id
            WHERE t.id = ?
        """, (tid,)).fetchone()
        if not row:
            continue

        user_text, asst_text, ts, proj, sid, turn_idx = row
        proj_name = Path(proj).name if proj else "?"
        date = ts[:10] if ts else "?"

        tools = conn.execute(
            "SELECT tool_name FROM tool_calls WHERE turn_id=?", (tid,)
        ).fetchall()
        tool_str = ", ".join(t[0] for t in tools) if tools else ""

        user_preview = (user_text or "")[:300]
        asst_preview = (asst_text or "")[:500]

        lines.append(f"### Result {i} — [{proj_name}] {date} (session {sid[:8]}… turn {turn_idx})")
        if tool_str:
            lines.append(f"**Tools:** {tool_str}")
        lines.append(f"**User:** {user_preview}")
        lines.append(f"**Assistant:** {asst_preview}")
        lines.append("")

    conn.close()
    return "\n".join(lines)


@mcp.tool()
def read(
    session: str,
    offset: int = 0,
    limit: int = 20,
) -> str:
    """Read turns from a specific conversation session.

    Args:
        session: Session ID or substring to match
        offset: Start from this turn index (default 0)
        limit: Number of turns to read (default 20)
    """
    conn = _get_conn()

    sessions = conn.execute(
        "SELECT session_id, project, start_time, end_time FROM sessions WHERE session_id LIKE ?",
        (f"%{session}%",),
    ).fetchall()

    if not sessions:
        conn.close()
        return f"No session found matching '{session}'"

    if len(sessions) > 1:
        lines = [f"Multiple sessions match '{session}':"]
        for sid, proj, start, _ in sessions:
            proj_name = Path(proj).name if proj else "?"
            lines.append(f"- `{sid}` [{proj_name}] {start or '?'}")
        conn.close()
        return "\n".join(lines)

    sid, project, start_time, end_time = sessions[0]
    proj_name = Path(project).name if project else "?"

    total = conn.execute("SELECT COUNT(*) FROM turns WHERE session_id=?", (sid,)).fetchone()[0]

    turns = conn.execute("""
        SELECT t.id, t.turn_index, t.timestamp, t.user_text, t.assistant_text, t.thinking
        FROM turns t WHERE t.session_id = ?
        ORDER BY t.turn_index LIMIT ? OFFSET ?
    """, (sid, limit, offset)).fetchall()

    lines = []
    lines.append(f"**Session:** `{sid}`")
    lines.append(f"**Project:** {proj_name}")
    lines.append(f"**Time:** {start_time or '?'} → {end_time or '?'}")
    lines.append(f"**Turns:** {total} total, showing {offset}–{offset + len(turns) - 1}")
    lines.append("")

    for turn_id, turn_idx, ts, user_text, asst_text, thinking in turns:
        tools = conn.execute(
            "SELECT tool_name, summary, agent_prompt, agent_result FROM tool_calls WHERE turn_id=?",
            (turn_id,),
        ).fetchall()

        date_str = ts[:19].replace("T", " ") if ts else ""
        lines.append(f"---")
        lines.append(f"#### Turn {turn_idx} [{date_str}]")

        if user_text:
            lines.append(f"**User:** {user_text}")
        if thinking:
            lines.append(f"**Thinking:** {thinking[:500]}{'…' if len(thinking) > 500 else ''}")
        if asst_text:
            lines.append(f"**Assistant:** {asst_text}")
        if tools:
            tool_lines = []
            for tn, ts_, ap, ar in tools:
                tool_lines.append(f"- [{tn}] {ts_ or ''}")
                if ap:
                    tool_lines.append(f"  - Prompt: {ap[:300]}{'…' if len(ap) > 300 else ''}")
                if ar:
                    tool_lines.append(f"  - Result: {ar[:300]}{'…' if len(ar) > 300 else ''}")
            lines.append("**Tools:**\n" + "\n".join(tool_lines))
        lines.append("")

    if offset + limit < total:
        lines.append(f"*More turns available. Use offset={offset + limit} to continue.*")

    conn.close()
    return "\n".join(lines)


@mcp.tool()
def list_sessions(
    project: str | None = None,
    limit: int = 20,
) -> str:
    """List conversation sessions.

    Args:
        project: Filter by project name substring
        limit: Max sessions to list (default 20)
    """
    conn = _get_conn()

    query = """SELECT s.session_id, s.project, s.start_time, COUNT(t.id) as turns
               FROM sessions s LEFT JOIN turns t ON s.session_id = t.session_id"""
    params: list = []
    if project:
        query += " WHERE s.project LIKE ?"
        params.append(f"%{project}%")
    query += " GROUP BY s.session_id ORDER BY s.start_time DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        return "No sessions found."

    lines = ["| Session | Project | Date | Turns |", "|---------|---------|------|-------|"]
    for sid, proj, start, turns in rows:
        proj_name = Path(proj).name if proj else "?"
        date = start[:10] if start else "?"
        lines.append(f"| `{sid[:12]}…` | {proj_name} | {date} | {turns} |")

    lines.append(f"\n{len(rows)} sessions")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Background indexing
# ---------------------------------------------------------------------------

def _background_index():
    """Run clean + embed incrementally in the background."""
    project_dir = Path(__file__).parent
    python = sys.executable

    try:
        logger.info("Background indexing: cleaning new sessions...")
        subprocess.run(
            [python, str(project_dir / "clean.py"), "--incremental", "--db", DB_PATH],
            cwd=str(project_dir),
            capture_output=True,
            timeout=300,
        )

        logger.info("Background indexing: embedding new turns...")
        subprocess.run(
            [python, str(project_dir / "embed.py"), "--db", DB_PATH],
            cwd=str(project_dir),
            capture_output=True,
            timeout=600,
        )

        logger.info("Background indexing complete.")
    except Exception as e:
        logger.warning(f"Background indexing failed: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Start background indexing (non-blocking)
    t = threading.Thread(target=_background_index, daemon=True)
    t.start()

    mcp.run(transport="stdio")
