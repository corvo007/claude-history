# claude-history Design Document

> Date: 2026-04-16
> Status: Implemented (Phase 1 & 2 complete)
> Prerequisites: None

## 1. Problem Definition

Claude Code conversation history (`~/.claude/projects/*.jsonl`) is a valuable knowledge asset, but currently unusable at scale. Raw data is ~900MB for 660 sessions, with 60-80% being low-density tool execution output (tool_result). A cleaning tool is needed to compress raw data into essential dialogue (~6% of original volume), enabling downstream embedding, retrieval, and a future WebUI.

**Success criteria:**
- Extract ~50MB of essential dialogue from ~900MB raw data
- Preserve complete human-AI conversation content and tool call metadata
- Output a structured SQLite database usable for embedding and WebUI

## 2. Research Findings

### 2.1 Existing Ecosystem

Surveyed 11+ GitHub projects. The ecosystem splits into two non-overlapping camps:

**Viewers** (render everything, no cleaning):
- claude-code-history-viewer (974 stars, Tauri desktop app) — most feature-rich viewer, supports 7 AI tools, but displays all data without cleaning
- claude-code-transcripts (1,436 stars, Simon Willison, Python) — JSONL to paginated HTML, does not strip tool_result
- cc-history-export (Go, abandoned) — batch export to JSON/Markdown/HTML

**Memory/Search** (index summaries, inconsistent quality):
- episodic-memory (Jesse Vincent) — vector embeddings + sqlite-vec semantic search
- Claudest/claude-memory (224 stars, Python) — FTS5 full-text search

**Analysis:**
- claude-session-analyzer (Python) — behavioral quantification (thinking depth, self-correction frequency), one-off research tool

**Key finding: No project performs "strip tool output, keep essential dialogue" cleaning.** This is a clear ecosystem gap.

### 2.2 episodic-memory Deep Analysis

As the closest existing solution, conducted source-level audit. Found fundamental issues:

**Architectural flaws:**
- Uses all-MiniLM-L6-v2 (2019 model, 384d, 512 token limit) — research explicitly advises "do not use for new projects"
- Truncates text to 2000 characters before embedding — massive semantic loss on long conversations
- tool_result completely unindexed (code has TODO comment, never implemented); thinking blocks also dropped
- Similarity calculation bug: L2 distance treated as cosine distance (#55) — search rankings unreliable

**Engineering quality issues:**
- 81 issues in 6 months, 15 patch releases fixing platform crashes
- #61: Sync process consumes 14GB RAM (each summarization spawns full Claude Code subprocess)
- #66: Backup loop fills 158GB of disk space
- Native dependencies (better-sqlite3, onnxruntime-node) cause ongoing cross-platform compatibility issues
- 501 MB plugin footprint (496 MB node_modules)

**Conclusion:** Not worth extending or depending on. Build independently.

### 2.3 JSONL Data Format Analysis

Quantitative analysis on real session data (10 sessions, 500KB–10MB each):

**Message type distribution:**
| Type | Share | Content |
|------|-------|---------|
| tool_result | 60-83% | Tool execution output (Bash stdout, file contents, search results) |
| tool_use | 12-23% | Tool calls (name + input) |
| thinking | 9% | Claude's internal reasoning |
| text | 3-6% | Human-AI dialogue |

**tool_use input sizes by tool type:**
| Tool | Median | Max | Why |
|------|--------|-----|-----|
| Write | 3.9 KB | 40 KB | Contains entire file contents |
| Edit | 635 B | 18 KB | Contains old_string + new_string |
| Bash | 200 B | 5 KB | Command strings |
| Agent | 1.5 KB | 6.6 KB | Subagent prompts |
| Read | 105 B | 202 B | File paths only |
| Grep | 155 B | 310 B | Pattern + path |

**Data scale:**
- 660 main sessions + 3,261 agent sub-sessions
- Total ~898 MB
- Largest single session: 76 MB
- Estimated post-cleaning: ~50 MB (~6% of original)

### 2.4 Embedding & Retrieval Architecture

**Vector database:** sqlite-vec (single file, serverless, SQL queryable) or LanceDB (built-in BM25 hybrid search). Both sufficient for <100K documents in a personal tool.

**Embedding models (revised during implementation):**
- Initial plan: nomic-embed-text v1.5/v2 — later discovered **no Chinese support**
- Final selection: BGE-M3 FP16 for GPU (1024d, Chinese first-class, 8192 tokens), jina-v2-base-zh for CPU (768d, Chinese-English bilingual)

**Chunking strategy:** Turn-level (user + assistant pair as one chunk) with contextual prefix (project name, date, topic).

**Storage/embedding decoupling:** SQLite is source of truth; embedding text is rendered from DB via template. Changing embedding strategy doesn't require re-cleaning.

### 2.5 Tech Stack

User's project ecosystem scan:
- Primary languages: TypeScript (ccusage, paper-qa-ui, etc.) + Python (paper-search-mcp, etc.)
- Package managers: pnpm (TS), uv (Python)
- Runtime: Node.js v22.18.0, Python 3.8.10 (system) + uv-managed 3.12+

## 3. Design Discussion

### 3.1 Tool Positioning: Relationship to episodic-memory

Three options: A. Upstream cleaner for episodic-memory; B. Independent full-stack tool (clean + embed + search + WebUI); C. Fork/extend episodic-memory.

Initial leaning toward A (smallest scope), but deep audit of episodic-memory changed the assessment — its architectural flaws are fundamental (outdated embedding model, 2000-char truncation, broken search ranking, native dep fragility), making it not worth depending on.

**Conclusion: Option B — independent full-stack tool, phased delivery.** Each phase independently useful. episodic-memory can be uninstalled after Phase 2.

### 3.2 Cleaning Rules: tool_use Input Granularity

Three options: A. Keep tool name only; B. Keep name + description; C. Keep full input.

User pointed out "agents sometimes execute large amounts of inline code." Data confirmed: Write tool input median 3.9KB, max 40KB (entire file contents); Edit max 18KB. Option C would bloat cleaned data with code.

**Conclusion: Per-tool differentiated strategy.** Write/Edit keep file_path only; Bash keeps command (short, high information density); Agent keeps full prompt; Read/Grep/Glob keep key parameters. tool_use volume compressed from ~2.7MB to ~50-100KB (>95% compression).

### 3.3 Subagent Handling

User's explicit requirement: "Just need to know what task was dispatched to the agent and what result came back."

**Conclusion:**
- Do not process agent-*.jsonl internal conversations
- Main session's Agent tool_use: keep full prompt
- Agent's tool_result: **exception — keep full text** (subagent summary output, high information density)
- All other tool_results: strip content, keep only tool_use_id + is_error status

### 3.4 Output Format

Initially discussed three options: A. Pure JSONL; B. Pure Markdown; C. JSONL + optional Markdown rendering. User noted WebUI is the intended viewer, no need for Markdown intermediate format. Initial conclusion: pure JSONL.

User then proposed "output to database for more structure" — a superior approach:
- Phase 2 already needs sqlite-vec; writing to the same SQLite file avoids migration
- WebUI queries SQL directly, more efficient than iterating JSONL files
- Single file, portable, easy to back up
- Native support for indexes and joins (e.g., "find all turns in project X that used Agent")

**Conclusion: Output to a single SQLite database `history.db`.** Three tables: sessions (metadata), turns (conversation turns), tool_calls (tool usage). Phase 2 adds vec_turns vector table to the same file. One file for the entire lifecycle.

### 3.5 Tech Stack Selection

User questioned "this shouldn't be complex, no strong typing needs, right?" Correct — the core logic is read JSONL line by line → filter by rules → write to SQLite. Essentially a data cleaning pipeline. TypeScript's build configuration (tsconfig, module resolution) would be overhead.

**Conclusion: Python + uv, start with a single file.** Phase 2 embedding integrates seamlessly. Future WebUI adds a lightweight API layer in the same Python project.

## 4. Design

### 4.1 Overall Architecture

```
Phase 1: CLI Cleaner
  ~/.claude/projects/**/*.jsonl → history.db (SQLite)

Phase 2: Embedding Pipeline
  history.db → BGE-M3 FP16 (GPU) / jina-v2-base-zh (CPU) → history.db (+ vec_turns + turns_fts)

Phase 3: WebUI
  Frontend → Python HTTP API → history.db
```

All phases share a single SQLite file. Phase 2 adds vector and FTS virtual tables.

### 4.2 Phase 1: Cleaner

**Input:** All `*.jsonl` in `~/.claude/projects/` (excluding `agent-*.jsonl`)

**Output:** Single SQLite database `history.db` with schema:

```sql
CREATE TABLE sessions (
  session_id    TEXT PRIMARY KEY,
  project       TEXT,
  start_time    TEXT,
  end_time      TEXT,
  version       TEXT,
  git_branch    TEXT
);

CREATE TABLE turns (
  id            INTEGER PRIMARY KEY,
  session_id    TEXT REFERENCES sessions(session_id),
  turn_index    INTEGER,
  timestamp     TEXT,
  user_text     TEXT,
  assistant_text TEXT,
  thinking      TEXT
);

CREATE TABLE tool_calls (
  id            INTEGER PRIMARY KEY,
  turn_id       INTEGER REFERENCES turns(id),
  tool_name     TEXT,
  summary       TEXT,
  agent_prompt  TEXT,    -- Agent tool only
  agent_result  TEXT,    -- Agent tool only
  is_error      BOOLEAN DEFAULT FALSE
);
```

**Cleaning rules:**

Kept JSONL line types:
- `user` (role=user, content with text blocks) → extract plain text
- `assistant` (role=assistant, content with text/thinking blocks) → extract text and reasoning
- `assistant` (role=assistant, content with tool_use blocks) → extract per-tool summaries

Stripped JSONL line types:
- `system`, `progress`, `queue-operation`, `file-history-snapshot`, `permission-mode`, `last-prompt`

Per-tool summary extraction:
| Tool | Extracted field | Output summary |
|------|----------------|----------------|
| Edit | input.file_path | `"src/app.ts"` |
| Write | input.file_path | `"src/new-file.ts"` |
| Bash | input.description ?? input.command | `"Show recent commits"` or `"git log --oneline"` |
| Agent | input.description, input.prompt, **matching tool_result full text** | Full prompt + result preserved |
| Read | input.file_path | `"src/config.ts"` |
| Grep | input.pattern + input.path | `"TODO in src/"` |
| Glob | input.pattern | `"**/*.ts"` |
| WebFetch | input.url | `"https://..."` |
| WebSearch | input.query | `"sqlite-vec benchmarks"` |
| MCP tools | tool name | `"chrome-devtools/click"` |

Turn boundary logic:
- A user text message starts a new turn
- Subsequent assistant messages (text + thinking + tool_use) and corresponding tool_results belong to the same turn
- Until the next user text message

### 4.3 Phase 2: Embedding Pipeline

- Reads from `turns` table, one turn = one chunk
- Each turn rendered to embedding text via template: `[{project}] [{date}]\nUser: {user}\nAssistant: {assistant}\nTools: {tool_names}`
- GPU: BGE-M3 FP16 via ONNX (1024d, 67 turns/s on RTX 4070)
- CPU: jina-v2-base-zh via fastembed (768d, 68 turns/s)
- Dense vectors stored in `vec_turns` (sqlite-vec), full text indexed in `turns_fts` (FTS5)
- Hybrid search via Reciprocal Rank Fusion (RRF): `score = 0.6/(k+dense_rank) + 0.4/(k+fts_rank)`

### 4.4 Phase 3: WebUI (Planned)

- Python HTTP API (FastAPI or similar)
- Frontend: browse session list → view cleaned conversations → semantic search
- Data source: same `history.db`

## 5. Implementation Roadmap

### Phase 1: CLI Cleaner ✅
- **Scope:** Single Python file (`clean.py`), complete cleaning pipeline outputting `history.db`
- **Size:** ~300 lines of Python
- **Result:** 898MB → 28MB (3.1%), 658 sessions, 5,445 turns, 22,115 tool calls, zero orphans

### Phase 2: Embedding Pipeline ✅
- **Scope:** `embed.py` (embedding) + `search.py` (search CLI) + `mcp_server.py` (MCP server)
- **Result:** 5,445 turns embedded in 78s (GPU), hybrid search operational, MCP server with auto-indexing

### Phase 3: WebUI
- **Scope:** Python HTTP API + frontend
- **Validation:** Browse and search all conversations in browser

## 6. Open Questions

1. **Compacted messages:** Claude Code compresses history when nearing context limits. Does the compressed format need special handling? To be addressed when encountered.
2. **Incremental update strategy:** Active session JSONL files keep growing. Incremental cleaning tracks session_id + source file mtime in `sync_state` table.
3. **Embedding model updates:** If the model changes, existing vectors need full rebuild. The system auto-detects dimension mismatches and rebuilds.
4. **Multi-machine sync:** If using Claude Code on multiple machines, cleaned data merges naturally (session_id is UUID, deduplication is automatic).

## 7. Sources

**Competitors/Products:**
- [claude-code-history-viewer](https://github.com/jhlee0409/claude-code-history-viewer) — 974 stars, Tauri desktop viewer
- [claude-code-transcripts](https://github.com/simonw/claude-code-transcripts) — 1,436 stars, Simon Willison, JSONL→HTML
- [episodic-memory](https://github.com/obra/episodic-memory) — Jesse Vincent, vector embedding search plugin (81 issues, multiple fundamental flaws)
- [Claudest](https://github.com/gupsammy/Claudest) — 224 stars, FTS5 memory plugin
- [claude-session-analyzer](https://github.com/lucemia/claude-session-analyzer) — behavioral quantitative analysis
- [universal-session-viewer](https://github.com/tad-hq/universal-session-viewer) — Electron viewer
- [cc-history-export](https://github.com/eternnoir/cc-history-export) — Go batch export (abandoned)

**Technical documentation:**
- BGE-M3 — BERT encoder, 568M params, 1024d, 8192 token context, native multilingual
- jina-embeddings-v2-base-zh — 161M params, 768d, Chinese-English bilingual
- sqlite-vec — SQLite vector search extension, single-file deployment
- FTS5 — SQLite built-in full-text search

**Codebase:**
- `~/.claude/projects/` — Claude Code session JSONL storage location
- `~/.claude/history.jsonl` — Global command history (non-conversation, slash commands only)
- episodic-memory source (`~/.claude/plugins/cache/superpowers-marketplace/episodic-memory/1.0.15/src/`) — audited as counter-example
