# Claude Code 历史对话清洗工具 设计文档

> 日期：2026-04-16
> 状态：设计完成，待实施
> 前置依赖：无

## 1. 问题定义

Claude Code 的对话历史（`~/.claude/projects/` 下的 JSONL 文件）是宝贵的知识资产，但目前无法有效利用。原始数据约 900MB，其中 60-80% 是工具执行结果（tool_result），信息密度低。需要一个清洗工具将原始数据压缩为精华对话记录（约 6% 体积），为后续的嵌入检索和 WebUI 浏览打基础。

**成功标准**：
- 从 ~900MB 原始数据提取 ~50MB 精华对话
- 保留完整的人机对话内容和工具调用元数据
- 产出结构化 JSONL，可直接用于嵌入和 WebUI

## 2. 调研发现

### 2.1 现有生态工具

对 GitHub 上 11+ 个相关项目做了全面调研，发现生态分为两个阵营：

**查看器类**（渲染全部内容，不做清洗）：
- claude-code-history-viewer（974 stars, Tauri 桌面应用）— 功能最全的查看器，支持 7 种 AI 工具，但展示全量数据不做清洗
- claude-code-transcripts（1,436 stars, Simon Willison, Python）— JSONL 转分页 HTML，不剥离 tool_result
- cc-history-export（Go, 已停更）— 批量导出 JSON/Markdown/HTML

**记忆/搜索类**（索引摘要，但质量参差）：
- episodic-memory（已安装，Jesse Vincent）— 向量嵌入 + sqlite-vec 语义搜索
- Claudest/claude-memory（224 stars, Python）— FTS5 全文搜索

**分析类**：
- claude-session-analyzer（Python）— 行为量化分析（thinking 深度、自我纠正频率），一次性研究工具

**关键发现：没有任何项目做"剥离工具结果、只留精华对话"的清洗。** 这是一个明确的生态空白。

### 2.2 episodic-memory 深度分析

作为最接近的已有方案，对其做了源码级审查。发现多个根本性问题：

**架构缺陷**：
- 使用 all-MiniLM-L6-v2（2019 年老模型，384 维，512 token 上限），调研明确建议"新项目不要用"
- 嵌入前截断到 2000 字符，长对话语义大量丢失
- tool_result 完全不索引（代码有 TODO 未实现）、thinking blocks 也丢弃
- 相似度计算有 bug：L2 距离被当作 cosine distance 处理（#55），搜索排名不可靠

**工程质量问题**：
- 6 个月 81 个 issues，15 个补丁修平台崩溃
- #61：同步过程吃掉 14GB 内存（每次摘要 spawn 完整 Claude Code 子进程）
- #66：备份死循环吞掉 158GB 磁盘
- native dependencies（better-sqlite3, onnxruntime-node）持续导致跨平台兼容问题
- 501 MB 插件体积（496 MB node_modules）

**结论**：不值得扩展或依赖，应独立构建。

### 2.3 JSONL 数据格式分析

对实际会话数据做了定量分析（10 个 500KB-10MB 的 session 样本）：

**消息类型分布**：
| type | 占比 | 内容 |
|------|------|------|
| tool_result | 60-83% | 工具执行输出（Bash stdout、文件内容、搜索结果）|
| tool_use | 12-23% | 工具调用（name + input）|
| thinking | 9% | Claude 内部推理 |
| text | 3-6% | 人机对话正文 |

**tool_use input 大小分布**（跨工具类型）：
| 工具 | 中位大小 | 最大 | 原因 |
|------|---------|------|------|
| Write | 3.9 KB | 40 KB | 包含完整文件内容 |
| Edit | 635 B | 18 KB | 包含 old_string + new_string |
| Bash | 200 B | 5 KB | 命令字符串 |
| Agent | 1.5 KB | 6.6 KB | subagent prompt |
| Read | 105 B | 202 B | 仅文件路径 |
| Grep | 155 B | 310 B | pattern + path |

**数据规模**：
- 660 个主会话 + 3,261 个 agent 子会话
- 总计约 898 MB
- 单个会话最大 76 MB
- 清洗后预估 ~50 MB（原始的 ~6%）

### 2.4 嵌入 & 检索架构

**向量数据库**：sqlite-vec（单文件、无服务器、SQL 查询）或 LanceDB（内置 BM25 混合搜索）。对 <100K 文档的个人工具，两者都够用。

**嵌入模型**：nomic-embed-text v1.5/v2（本地运行）。支持 100+ 编程语言，2048 token 上下文，Matryoshka 降维。通过 Ollama HTTP API 调用，避免 native deps。

**分块策略**：Turn-level（user + assistant 一对为一个 chunk），附带 contextual prefix（项目名、日期、主题）。

**存储与嵌入解耦**：JSONL 是 source of truth，嵌入时通过模板渲染为纯文本。更换嵌入策略不需要重新清洗。

### 2.5 技术栈

用户项目技术栈扫描结果：
- 主力语言：TypeScript（ccusage, paper-qa-ui 等）+ Python（paper-search-mcp 等）
- 包管理：pnpm（TS）、uv（Python）
- Node.js v22.18.0, Python 3.8.10（系统）+ uv 管理 3.12+

## 3. 设计讨论过程

### 3.1 工具定位：和 episodic-memory 的关系

三个选项：A. 做 episodic-memory 上游（纯清洗器）；B. 独立全栈工具（清洗 + 嵌入 + 检索 + WebUI）；C. Fork/扩展 episodic-memory。

初始倾向 A（scope 最小），但对 episodic-memory 的深度审查改变了判断——其架构缺陷是根本性的（过时嵌入模型、2000 字符截断、搜索排名 bug、native deps 脆弱性），不值得依赖。

**结论：选 B，独立全栈工具，分 Phase 交付。** 每个 Phase 独立可用，不依赖 episodic-memory。Phase 2 完成后可卸载 episodic-memory。

### 3.2 清洗规则：tool_use input 的保留粒度

三个选项：A. 只保留工具名；B. 保留 name + description；C. 保留完整 input。

用户指出"agent 有时会执行大量 inline code"，数据验证了这个担忧：Write 工具的 input 中位 3.9KB、最大 40KB（包含完整文件内容），Edit 最大 18KB。选 C 会让 Write/Edit 的 input 大量膨胀清洗后数据。

**结论：按工具类型差异化处理。** Write/Edit 只留 file_path；Bash 留 command（短且信息量高）；Agent 保留完整 prompt；Read/Grep/Glob 留关键参数。tool_use 体积从 ~2.7MB 压缩至 ~50-100KB（>95% 压缩比）。

### 3.3 Subagent 处理

用户明确需求："知道给 agent 派了什么活，agent 返回什么结果就行了。"

**结论**：
- 不处理 agent-*.jsonl 内部对话
- 主会话中 Agent 的 tool_use 保留完整 prompt
- Agent 的 tool_result 作为唯一例外，保留全文（因为是 subagent 的总结输出，信息密度高）
- 其他所有 tool_result 剥离内容，只留 tool_use_id + is_error 状态

### 3.4 输出格式

最初讨论了三个选项：A. 纯 JSONL；B. 纯 Markdown；C. JSONL + 可选 Markdown 渲染。用户指出 WebUI 是最终查看方式，不需要 Markdown 中间格式，初步结论为纯 JSONL。

随后用户提出"输出成数据库更结构化"，这是更优的方案：
- Phase 2 已经需要 sqlite-vec，直接写同一个 SQLite 文件避免迁移
- WebUI 直接 SQL 查询，比遍历 JSONL 高效
- 单文件可移植，和 JSONL 一样好备份
- 天然支持索引、关联查询（如"查某项目所有用了 Agent 的 turn"）

**结论：输出为单个 SQLite 数据库 `history.db`。** 三张表：sessions（元数据）、turns（对话轮次）、tool_calls（工具调用）。Phase 2 在同一文件加 vec_turns 向量表。全生命周期一个文件。

### 3.5 技术栈选择

用户质疑"应该不复杂，也没有强类型的需求吧"。确实——核心逻辑就是逐行读 JSONL → 按规则过滤 → 写出清洗后 JSONL，本质是数据清洗管线。TypeScript 的 build 配置（tsconfig、模块解析）反而是负担。

**结论：Python + uv，单文件起步。** Phase 2 嵌入直接 sentence-transformers 或 Ollama HTTP，无缝衔接。后续 WebUI 在同一 Python 项目加一层 FastAPI/Hono 即可。

## 4. 设计方案

### 4.1 整体架构

```
Phase 1: CLI 清洗器
  ~/.claude/projects/**/*.jsonl → history.db (SQLite)

Phase 2: 嵌入管线
  history.db → nomic-embed (via Ollama) → history.db (加 vec_turns 表)

Phase 3: WebUI
  前端 → Python HTTP API → history.db
```

所有阶段共用同一个 SQLite 文件，Phase 2 只是往里加一张向量虚拟表。

### 4.2 Phase 1：清洗器

**输入**：`~/.claude/projects/` 下的所有 `*.jsonl`（排除 `agent-*.jsonl`）

**输出**：单个 SQLite 数据库文件 `history.db`，schema 如下：

```sql
-- 会话元数据
CREATE TABLE sessions (
  session_id    TEXT PRIMARY KEY,
  project       TEXT,
  start_time    TEXT,
  end_time      TEXT,
  version       TEXT,
  git_branch    TEXT
);

-- 对话轮次（核心）
CREATE TABLE turns (
  id            INTEGER PRIMARY KEY,
  session_id    TEXT REFERENCES sessions(session_id),
  turn_index    INTEGER,
  timestamp     TEXT,
  user_text     TEXT,
  assistant_text TEXT,
  thinking      TEXT
);

-- 工具调用（一个 turn 可能有多个）
CREATE TABLE tool_calls (
  id            INTEGER PRIMARY KEY,
  turn_id       INTEGER REFERENCES turns(id),
  tool_name     TEXT,
  summary       TEXT,
  agent_prompt  TEXT,    -- 仅 Agent 工具
  agent_result  TEXT,    -- 仅 Agent 工具
  is_error      BOOLEAN DEFAULT FALSE
);

-- Phase 2 加入向量搜索
-- CREATE VIRTUAL TABLE vec_turns USING vec0(
--   turn_id INTEGER,
--   embedding FLOAT[768]
-- );
```

索引：
- `sessions(project)` — 按项目筛选
- `turns(session_id, turn_index)` — 按会话排序
- `tool_calls(turn_id)` — 按轮次查工具
- `tool_calls(tool_name)` — 按工具类型筛选

**清洗规则**：

保留的 JSONL 行 type：
- `user`（role=user, content 含 text blocks）→ 提取纯文本
- `assistant`（role=assistant, content 含 text/thinking blocks）→ 提取文本和推理
- `assistant`（role=assistant, content 含 tool_use blocks）→ 按工具类型提取摘要

剥离的 JSONL 行 type：
- `system`, `progress`, `queue-operation`, `file-history-snapshot`, `permission-mode`, `last-prompt`

tool_use 按工具差异化处理：
| 工具 | 提取字段 | 输出 summary |
|------|---------|-------------|
| Edit | input.file_path | `"src/app.ts"` |
| Write | input.file_path | `"src/new-file.ts"` |
| Bash | input.description ?? input.command | `"Show recent commits"` 或 `"git log --oneline"` |
| Agent | input.description, input.prompt, **对应的 tool_result 全文** | 完整保留 prompt + result |
| Read | input.file_path | `"src/config.ts"` |
| Grep | input.pattern + input.path | `"TODO in src/"` |
| Glob | input.pattern | `"**/*.ts"` |
| WebFetch | input.url | `"https://..."` |
| WebSearch | input.query | `"sqlite-vec benchmarks"` |
| ToolSearch | input.query | `"select:WebFetch"` |
| Skill | input.skill | `"commit"` |
| MCP tools | tool name | `"chrome-devtools/click"` |
| TaskCreate/Update | input.description ?? 省略 | 任务描述 |
| 其他 | tool name only | 工具名 |

tool_result 处理：
- Agent 的 tool_result → **保留全文**
- 其他工具的 tool_result → 只保留 `{"tool_use_id": "...", "is_error": false}`

Turn 边界逻辑：
- 用户发一条 text 消息开始新 turn
- 后续 assistant 消息（text + thinking + tool_use）+ 对应 tool_result 都归入同一 turn
- 直到下一条用户 text 消息

**CLI 接口**：

```bash
# 清洗所有会话
python clean.py

# 清洗指定项目
python clean.py --project "D:\onedrive\codelab\claude-history"

# 清洗指定会话
python clean.py --session abc-123-def

# 指定输出数据库路径
python clean.py --db ./history.db

# 增量模式（只处理新增/修改的文件）
python clean.py --incremental
```

**默认行为**：
- 源目录：`~/.claude/projects/`
- 输出：项目根目录下 `./history.db`
- 增量处理：数据库中记录已处理的 session_id + 源文件 mtime，跳过未变更的文件

### 4.3 Phase 2：嵌入管线（前瞻设计）

- 从 `turns` 表按行分块
- 每个 turn 用模板渲染为嵌入文本：`[{project}] [{date}]\nUser: {user}\nAssistant: {assistant}\nTools: {tool_names}`
- nomic-embed-text via Ollama HTTP API 生成向量
- 在同一个 `history.db` 中创建 `vec_turns` 虚拟表（sqlite-vec）
- 无需额外文件，所有数据在一个 SQLite 里

### 4.4 Phase 3：WebUI（前瞻设计）

- Python HTTP API（FastAPI 或类似）
- 前端：浏览 session 列表 → 查看清洗后对话 → 语义搜索
- 数据源：同一个 `history.db`

## 5. 实施路线图

### Phase 1：CLI 清洗器
- **scope**：单个 Python 文件（`clean.py`），实现完整清洗管线，输出 `history.db`
- **估计规模**：~300-500 行 Python
- **验证标准**：对实际 900MB 数据完成清洗，数据库 <50MB，人工抽查 5 个 session 确认信息完整性

### Phase 2：嵌入管线
- **scope**：嵌入脚本，在同一个 `history.db` 中添加 `vec_turns` 向量表
- **前置**：安装 Ollama + nomic-embed-text
- **验证标准**：语义搜索"上次调试 React router 的问题"能找到相关对话

### Phase 3：WebUI
- **scope**：Python HTTP API + 前端界面，读写同一个 `history.db`
- **验证标准**：浏览器中可浏览和搜索所有历史对话

## 6. 开放问题

1. **compacted messages**：Claude Code 在上下文接近限制时会压缩历史消息，压缩后的格式是否需要特殊处理？需要实际遇到时再看
2. **增量更新策略**：活跃 session 的 JSONL 文件会持续追加，增量清洗是重新处理整个文件还是只处理新增行？（可在数据库中记录 session_id + 源文件 mtime + 已处理的 byte offset）
3. **嵌入模型更新**：nomic-embed-text 如果出新版本，已有向量需要全量重建。是否需要版本化向量存储？
4. **多机同步**：如果在多台机器上使用 Claude Code，清洗后的数据如何合并？（session_id 是 UUID，天然去重）

## 7. 信息来源

**竞品/产品**：
- [claude-code-history-viewer](https://github.com/jhlee0409/claude-code-history-viewer) — 974 stars, Tauri 桌面查看器
- [claude-code-transcripts](https://github.com/simonw/claude-code-transcripts) — 1,436 stars, Simon Willison, JSONL→HTML
- [episodic-memory](https://github.com/obra/episodic-memory) — Jesse Vincent, 向量嵌入搜索插件（81 issues, 多个根本性缺陷）
- [Claudest](https://github.com/gupsammy/Claudest) — 224 stars, FTS5 记忆插件
- [claude-session-analyzer](https://github.com/lucemia/claude-session-analyzer) — 行为量化分析
- [universal-session-viewer](https://github.com/tad-hq/universal-session-viewer) — Electron 查看器
- [cc-history-export](https://github.com/eternnoir/cc-history-export) — Go 批量导出（已停更）

**技术文档**：
- nomic-embed-text — 本地嵌入模型，2048 token context, Matryoshka 降维
- sqlite-vec — SQLite 向量搜索扩展，单文件部署
- LanceDB — 嵌入式向量数据库，内置 BM25 混合搜索

**代码库**：
- `~/.claude/projects/` — Claude Code session JSONL 存储位置
- `~/.claude/history.jsonl` — 全局命令历史（非对话内容，仅 slash commands）
- episodic-memory 源码（`~/.claude/plugins/cache/superpowers-marketplace/episodic-memory/1.0.15/src/`）— 作为反面教材审查

**用户领域知识**：
- 用户确认 thinking blocks 保留
- 用户确认 subagent 只需 prompt + result，不需内部对话
- 用户指出"没有强类型需求"，引导技术栈从 TypeScript 转向 Python
- 用户指出 WebUI 是最终查看方式，不需要 Markdown 中间格式
