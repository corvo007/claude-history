"""Hybrid search over cleaned Claude Code conversation history.

Combines dense vector similarity (sqlite-vec + BGE-M3) with
FTS5 keyword matching for best retrieval quality.
"""

import argparse
import os
import sqlite3
import struct
import sys
import time
from pathlib import Path

# Fix Windows terminal encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

# ---------------------------------------------------------------------------
# Vector deserialization
# ---------------------------------------------------------------------------

EMBEDDING_DIM = 1024


def serialize_f32(vec: np.ndarray) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


# ---------------------------------------------------------------------------
# Search functions
# ---------------------------------------------------------------------------

def search_dense(conn: sqlite3.Connection, query_vec: bytes, limit: int = 20) -> list[tuple]:
    """Vector similarity search via sqlite-vec. Returns [(turn_id, distance), ...]."""
    return conn.execute("""
        SELECT turn_id, distance
        FROM vec_turns
        WHERE embedding MATCH ?
        ORDER BY distance
        LIMIT ?
    """, (query_vec, limit)).fetchall()


def search_fts(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[tuple]:
    """FTS5 keyword search. Returns [(turn_id, rank), ...]."""
    # Wrap in quotes to treat as phrase, escape internal quotes
    safe_query = '"' + query.replace('"', '""') + '"'
    try:
        return conn.execute("""
            SELECT rowid, rank
            FROM turns_fts
            WHERE turns_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (safe_query, limit)).fetchall()
    except Exception:
        # Fallback: split into individual quoted terms
        terms = query.split()
        if not terms:
            return []
        parts = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        return conn.execute("""
            SELECT rowid, rank
            FROM turns_fts
            WHERE turns_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (parts, limit)).fetchall()


def hybrid_search(
    conn: sqlite3.Connection,
    query_vec: bytes,
    query_text: str,
    limit: int = 10,
    dense_weight: float = 0.6,
    fts_weight: float = 0.4,
    dense_k: int = 30,
    fts_k: int = 30,
) -> list[tuple]:
    """Hybrid search combining dense and FTS5 results via RRF (Reciprocal Rank Fusion).

    Returns [(turn_id, score), ...] sorted by score descending.
    """
    # Get results from both channels
    dense_results = search_dense(conn, query_vec, dense_k)
    fts_results = search_fts(conn, query_text, fts_k)

    # RRF scoring: score = weight * 1/(k+rank)
    K = 60  # RRF constant
    scores: dict[int, float] = {}

    for rank, (turn_id, _distance) in enumerate(dense_results):
        scores[turn_id] = scores.get(turn_id, 0) + dense_weight / (K + rank)

    for rank, (turn_id, _fts_rank) in enumerate(fts_results):
        scores[turn_id] = scores.get(turn_id, 0) + fts_weight / (K + rank)

    # Sort by score descending
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return ranked[:limit]


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

def format_results(conn: sqlite3.Connection, results: list[tuple], verbose: bool = False) -> str:
    """Format search results for display."""
    if not results:
        return "No results found."

    lines = []
    for i, (turn_id, score) in enumerate(results, 1):
        row = conn.execute("""
            SELECT t.user_text, t.assistant_text, t.timestamp,
                   s.project, t.session_id, t.turn_index
            FROM turns t
            JOIN sessions s ON s.session_id = t.session_id
            WHERE t.id = ?
        """, (turn_id,)).fetchone()

        if not row:
            continue

        user_text, assistant_text, timestamp, project, session_id, turn_index = row
        project_name = Path(project).name if project else "?"
        date = timestamp[:10] if timestamp else "?"

        # Tool calls for this turn
        tools = conn.execute(
            "SELECT tool_name FROM tool_calls WHERE turn_id=?", (turn_id,)
        ).fetchall()
        tool_names = ", ".join(t[0] for t in tools) if tools else ""

        lines.append(f"{'─' * 60}")
        lines.append(f"#{i}  score={score:.4f}  [{project_name}] {date}  session={session_id[:8]}... turn={turn_index}")
        if tool_names:
            lines.append(f"  Tools: {tool_names}")

        # User text (truncated)
        user_preview = (user_text or "")[:200]
        if len(user_text or "") > 200:
            user_preview += "..."
        lines.append(f"  User: {user_preview}")

        # Assistant text (truncated)
        if verbose:
            asst_preview = (assistant_text or "")[:500]
            if len(assistant_text or "") > 500:
                asst_preview += "..."
        else:
            asst_preview = (assistant_text or "")[:150]
            if len(assistant_text or "") > 150:
                asst_preview += "..."
        lines.append(f"  Assistant: {asst_preview}")

    lines.append(f"{'─' * 60}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def read_session(conn: sqlite3.Connection, session_id: str, offset: int = 0, limit: int = 20, verbose: bool = False) -> str:
    """Read turns from a specific session."""
    # Find matching session (support substring match)
    sessions = conn.execute(
        "SELECT session_id, project, start_time, end_time FROM sessions WHERE session_id LIKE ?",
        (f"%{session_id}%",),
    ).fetchall()

    if not sessions:
        return f"No session found matching '{session_id}'"
    if len(sessions) > 1:
        lines = [f"Multiple sessions match '{session_id}':"]
        for sid, proj, start, _ in sessions:
            proj_name = Path(proj).name if proj else "?"
            lines.append(f"  {sid}  [{proj_name}]  {start or '?'}")
        return "\n".join(lines)

    sid, project, start_time, end_time = sessions[0]
    project_name = Path(project).name if project else "?"

    total_turns = conn.execute(
        "SELECT COUNT(*) FROM turns WHERE session_id=?", (sid,)
    ).fetchone()[0]

    turns = conn.execute("""
        SELECT t.id, t.turn_index, t.timestamp, t.user_text, t.assistant_text, t.thinking
        FROM turns t
        WHERE t.session_id = ?
        ORDER BY t.turn_index
        LIMIT ? OFFSET ?
    """, (sid, limit, offset)).fetchall()

    lines = []
    lines.append(f"Session: {sid}")
    lines.append(f"Project: {project_name}  ({project})")
    lines.append(f"Time: {start_time or '?'} → {end_time or '?'}")
    lines.append(f"Turns: {total_turns} total, showing {offset}-{offset + len(turns) - 1}")
    lines.append(f"{'═' * 60}")

    for turn_id, turn_idx, ts, user_text, asst_text, thinking in turns:
        tools = conn.execute(
            "SELECT tool_name, summary, agent_prompt, agent_result FROM tool_calls WHERE turn_id=?",
            (turn_id,),
        ).fetchall()

        date_str = ts[:19].replace("T", " ") if ts else ""
        lines.append(f"\n{'─' * 60}")
        lines.append(f"Turn {turn_idx}  [{date_str}]")

        if user_text:
            lines.append(f"\n  User: {user_text}")

        if thinking:
            preview = thinking if verbose else thinking[:300]
            if not verbose and len(thinking) > 300:
                preview += f"... ({len(thinking)} chars)"
            lines.append(f"\n  Thinking: {preview}")

        if asst_text:
            preview = asst_text if verbose else asst_text[:500]
            if not verbose and len(asst_text) > 500:
                preview += f"... ({len(asst_text)} chars)"
            lines.append(f"\n  Assistant: {preview}")

        if tools:
            lines.append(f"\n  Tools ({len(tools)}):")
            for tn, ts_, ap, ar in tools:
                lines.append(f"    [{tn}] {ts_ or ''}")
                if ap:
                    ap_preview = ap[:200] + "..." if len(ap) > 200 else ap
                    lines.append(f"      Prompt: {ap_preview}")
                if ar:
                    ar_preview = ar[:200] + "..." if len(ar) > 200 else ar
                    lines.append(f"      Result: {ar_preview}")

    lines.append(f"\n{'═' * 60}")
    if offset + limit < total_turns:
        lines.append(f"More turns available. Use --offset {offset + limit} to continue.")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Search or read Claude Code conversation history."
    )
    sub = parser.add_subparsers(dest="command", help="Command")

    # --- search subcommand ---
    p_search = sub.add_parser("search", help="Search conversations")
    p_search.add_argument("query", nargs="+", help="Search query")
    p_search.add_argument(
        "--limit", "-n", type=int, default=10,
        help="Number of results (default: 10)",
    )
    p_search.add_argument(
        "--mode", choices=["hybrid", "dense", "fts"], default="hybrid",
        help="Search mode (default: hybrid)",
    )
    p_search.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show longer result previews",
    )
    p_search.add_argument(
        "--project", type=str, default=None,
        help="Filter by project name substring",
    )

    # --- read subcommand ---
    p_read = sub.add_parser("read", help="Read a specific session")
    p_read.add_argument("session", help="Session ID (or substring)")
    p_read.add_argument(
        "--offset", type=int, default=0,
        help="Start from this turn index (default: 0)",
    )
    p_read.add_argument(
        "--limit", "-n", type=int, default=20,
        help="Number of turns to show (default: 20)",
    )
    p_read.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show full text without truncation",
    )

    # --- list subcommand ---
    p_list = sub.add_parser("list", help="List sessions")
    p_list.add_argument(
        "--project", type=str, default=None,
        help="Filter by project name substring",
    )
    p_list.add_argument(
        "--limit", "-n", type=int, default=20,
        help="Number of sessions to show (default: 20)",
    )

    # Common args
    parser.add_argument(
        "--db", type=Path, default=Path("history.db"),
        help="Database path (default: ./history.db)",
    )
    args = parser.parse_args()

    # Backward compat: no subcommand = search
    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if not args.db.exists():
        print(f"Error: database not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(args.db))

    if args.command == "list":
        query = "SELECT s.session_id, s.project, s.start_time, COUNT(t.id) as turns FROM sessions s LEFT JOIN turns t ON s.session_id = t.session_id"
        params = []
        if args.project:
            query += " WHERE s.project LIKE ?"
            params.append(f"%{args.project}%")
        query += " GROUP BY s.session_id ORDER BY s.start_time DESC LIMIT ?"
        params.append(args.limit)

        rows = conn.execute(query, params).fetchall()
        for sid, proj, start, turns in rows:
            proj_name = Path(proj).name if proj else "?"
            date = start[:10] if start else "?"
            print(f"  {sid[:12]}...  {turns:4d} turns  [{proj_name:20s}]  {date}")
        print(f"\n{len(rows)} sessions")

    elif args.command == "read":
        print(read_session(conn, args.session, args.offset, args.limit, args.verbose))

    elif args.command == "search":
        query = " ".join(args.query)

        # Check if vec_turns exists
        has_vec = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='vec_turns'"
        ).fetchone()[0]

        if args.mode in ("hybrid", "dense") and has_vec:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)

        t0 = time.time()

        if args.mode == "fts" or not has_vec:
            if not has_vec and args.mode != "fts":
                print("Warning: no embeddings found, falling back to FTS search.", file=sys.stderr)
            fts_results = search_fts(conn, query, args.limit)
            results = [(tid, -rank) for tid, rank in fts_results]

        elif args.mode == "dense":
            from embed import create_embedder
            embedder = create_embedder()
            query_vec = serialize_f32(embedder.embed_one(query))
            dense_results = search_dense(conn, query_vec, args.limit)
            max_dist = max(d for _, d in dense_results) if dense_results else 1.0
            results = [(tid, 1 - dist / max_dist) for tid, dist in dense_results]

        else:  # hybrid
            from embed import create_embedder
            embedder = create_embedder()
            query_vec = serialize_f32(embedder.embed_one(query))
            results = hybrid_search(conn, query_vec, query, args.limit)

        elapsed = time.time() - t0

        if args.project:
            filtered = []
            for turn_id, score in results:
                project = conn.execute("""
                    SELECT s.project FROM turns t
                    JOIN sessions s ON s.session_id = t.session_id
                    WHERE t.id = ?
                """, (turn_id,)).fetchone()
                if project and args.project.lower() in (project[0] or "").lower():
                    filtered.append((turn_id, score))
            results = filtered

        print(f'Search: "{query}" ({args.mode} mode, {elapsed:.2f}s)\n')
        print(format_results(conn, results, args.verbose))
        print(f"\n{len(results)} results")

    conn.close()


if __name__ == "__main__":
    main()
