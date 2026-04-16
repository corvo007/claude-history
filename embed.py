"""Embedding pipeline for Claude Code conversation history.

Reads cleaned turns from history.db, embeds with BGE-M3 (GPU) or
jina-v2-base-zh (CPU), stores dense vectors in sqlite-vec + full text
in FTS5 for hybrid search.
"""

import argparse
import os
import sqlite3
import struct
import sys
import time
from pathlib import Path

import numpy as np

# Add NVIDIA DLL paths before importing onnxruntime
_venv = Path(__file__).parent / ".venv" / "Lib" / "site-packages" / "nvidia"
for _sub in ["cudnn/bin", "cublas/bin", "cuda_runtime/bin", "cuda_nvrtc/bin"]:
    _dll_dir = _venv / _sub
    if _dll_dir.is_dir():
        os.environ["PATH"] = str(_dll_dir) + os.pathsep + os.environ.get("PATH", "")

import onnxruntime as ort
from huggingface_hub import hf_hub_download
from tokenizers import Tokenizer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# GPU model: BGE-M3 FP16 via ONNX (1024 dim)
GPU_MODEL_REPO = "hotchpotch/vespa-onnx-BAAI-bge-m3-only-dense"
GPU_MODEL_FILE = "BAAI-bge-m3_fp16.onnx"
GPU_TOKENIZER_FILE = "tokenizer.json"
GPU_EMBEDDING_DIM = 1024

# CPU model: jina-v2-base-zh via fastembed (768 dim)
CPU_MODEL_NAME = "jinaai/jina-embeddings-v2-base-zh"
CPU_EMBEDDING_DIM = 768

MAX_TOKENS = 512
BATCH_SIZE = 32

# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def _has_cuda() -> bool:
    providers = ort.get_available_providers()
    if "CUDAExecutionProvider" not in providers:
        return False
    # Also check cuDNN is loadable by trying a dummy session
    try:
        # If CUDA provider is listed and DLLs are in PATH, this is enough
        return True
    except Exception:
        return False


class GpuEmbedder:
    """BGE-M3 FP16 ONNX embedder for GPU."""

    dim = GPU_EMBEDDING_DIM

    def __init__(self):
        print("Loading BGE-M3 FP16 (GPU)...")
        t0 = time.time()
        tokenizer_path = hf_hub_download(GPU_MODEL_REPO, GPU_TOKENIZER_FILE)
        model_path = hf_hub_download(GPU_MODEL_REPO, GPU_MODEL_FILE)

        self.session = ort.InferenceSession(
            model_path,
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        actual = self.session.get_providers()
        if "CUDAExecutionProvider" not in actual:
            raise RuntimeError("CUDA provider not available after loading model")

        self.tokenizer = Tokenizer.from_file(tokenizer_path)
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        self.tokenizer.enable_truncation(max_length=MAX_TOKENS)
        print(f"  Providers: {actual}")
        print(f"  Loaded in {time.time() - t0:.1f}s")

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        encoded = self.tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids)

        outputs = self.session.run(None, {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        })

        token_embeddings = outputs[0]
        mask = attention_mask[..., np.newaxis].astype(np.float32)
        embeddings = (token_embeddings * mask).sum(axis=1) / mask.sum(axis=1)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        embeddings = embeddings / np.maximum(norms, 1e-12)
        return embeddings.astype(np.float32)

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed_batch([text])[0]


class CpuEmbedder:
    """jina-v2-base-zh via fastembed for CPU."""

    dim = CPU_EMBEDDING_DIM

    def __init__(self):
        from fastembed import TextEmbedding
        print("Loading jina-v2-base-zh (CPU)...")
        t0 = time.time()
        self.model = TextEmbedding(CPU_MODEL_NAME)
        print(f"  Loaded in {time.time() - t0:.1f}s")

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        return np.array(list(self.model.embed(texts)), dtype=np.float32)

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed_batch([text])[0]


def create_embedder(force_cpu: bool = False):
    """Create the best available embedder."""
    if not force_cpu and _has_cuda():
        try:
            return GpuEmbedder()
        except Exception as e:
            print(f"  GPU failed ({e}), falling back to CPU")
    return CpuEmbedder()


# Convenience alias used by search.py
Embedder = None  # Set after parsing args


# ---------------------------------------------------------------------------
# Vector serialization for sqlite-vec
# ---------------------------------------------------------------------------

def serialize_f32(vec: np.ndarray) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


# ---------------------------------------------------------------------------
# Database schema extension
# ---------------------------------------------------------------------------

def make_schema_sql(dim: int) -> str:
    return f"""\
CREATE VIRTUAL TABLE IF NOT EXISTS vec_turns USING vec0(
    turn_id INTEGER,
    embedding float[{dim}]
);

CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    user_text,
    assistant_text,
    content='turns',
    content_rowid='id',
    tokenize='unicode61'
);
"""

# ---------------------------------------------------------------------------
# Rendering turns to embedding text
# ---------------------------------------------------------------------------

def render_turn_text(row: tuple) -> str:
    turn_id, session_id, user_text, assistant_text, thinking, project, timestamp, tools = row

    parts = []
    project_name = Path(project).name if project else "unknown"
    date = timestamp[:10] if timestamp else ""
    parts.append(f"[{project_name}] [{date}]")

    if user_text:
        parts.append(f"User: {user_text}")
    if assistant_text:
        text = assistant_text[:2000] if len(assistant_text) > 2000 else assistant_text
        parts.append(f"Assistant: {text}")
    if tools:
        parts.append(f"Tools: {tools}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def get_unembedded_turns(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute("""
        SELECT t.id, t.session_id, t.user_text, t.assistant_text, t.thinking,
               s.project, t.timestamp,
               GROUP_CONCAT(tc.tool_name, ', ') as tools
        FROM turns t
        JOIN sessions s ON s.session_id = t.session_id
        LEFT JOIN tool_calls tc ON tc.turn_id = t.id
        WHERE t.id NOT IN (SELECT turn_id FROM vec_turns)
        GROUP BY t.id
        ORDER BY t.id
    """).fetchall()


def init_vec(conn: sqlite3.Connection, dim: int):
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    # Check if vec_turns exists with a different dimension
    existing = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='vec_turns'"
    ).fetchone()[0]
    if existing:
        # Verify dimension matches
        vec_count = conn.execute("SELECT COUNT(*) FROM vec_turns").fetchone()[0]
        if vec_count > 0:
            # Read one vector to check dim
            row = conn.execute("SELECT embedding FROM vec_turns LIMIT 1").fetchone()
            if row:
                existing_dim = len(row[0]) // 4  # float32 = 4 bytes
                if existing_dim != dim:
                    print(f"  Warning: existing vectors are {existing_dim}d, new model is {dim}d")
                    print(f"  Dropping old vectors and re-embedding...")
                    conn.execute("DROP TABLE vec_turns")
                    conn.commit()

    conn.executescript(make_schema_sql(dim))


def populate_fts(conn: sqlite3.Connection):
    fts_count = conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0]
    turns_count = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]

    if fts_count < turns_count:
        conn.execute("INSERT INTO turns_fts(turns_fts) VALUES('rebuild')")
        conn.commit()
        fts_count = conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0]
        print(f"  FTS5 index: {fts_count} turns indexed")


def main():
    parser = argparse.ArgumentParser(
        description="Embed conversation turns for hybrid search."
    )
    parser.add_argument(
        "--db", type=Path, default=Path("history.db"),
        help="Database path (default: ./history.db)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=BATCH_SIZE,
        help=f"Embedding batch size (default: {BATCH_SIZE})",
    )
    parser.add_argument(
        "--cpu", action="store_true",
        help="Force CPU model (jina-v2-base-zh) even if GPU is available",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"Error: database not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    # Create embedder
    embedder = create_embedder(force_cpu=args.cpu)

    conn = sqlite3.connect(str(args.db))
    conn.execute("PRAGMA journal_mode=WAL")

    # Initialize vector extension and tables (with correct dimension)
    init_vec(conn, embedder.dim)

    # Populate FTS5 index
    print("Building FTS5 index...")
    populate_fts(conn)

    # Get turns needing embedding
    turns = get_unembedded_turns(conn)
    if not turns:
        print("All turns already embedded.")
        conn.close()
        return

    print(f"Embedding {len(turns)} turns (dim={embedder.dim})...")

    total = len(turns)
    t0 = time.time()

    for i in range(0, total, args.batch_size):
        batch = turns[i:i + args.batch_size]
        texts = [render_turn_text(row) for row in batch]
        turn_ids = [row[0] for row in batch]

        embeddings = embedder.embed_batch(texts)

        for turn_id, embedding in zip(turn_ids, embeddings):
            conn.execute(
                "INSERT INTO vec_turns(turn_id, embedding) VALUES (?, ?)",
                (turn_id, serialize_f32(embedding)),
            )

        conn.commit()

        elapsed = time.time() - t0
        done = min(i + args.batch_size, total)
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        print(f"  {done}/{total} ({done*100/total:.0f}%) - {rate:.0f} turns/s - ETA {eta:.0f}s")

    elapsed = time.time() - t0
    vec_count = conn.execute("SELECT COUNT(*) FROM vec_turns").fetchone()[0]
    print(f"\nDone! {vec_count} turns embedded in {elapsed:.1f}s ({total/elapsed:.0f} turns/s)")

    db_size = args.db.stat().st_size
    print(f"Database: {args.db} ({db_size:,} bytes / {db_size/1024/1024:.1f} MB)")

    conn.close()


if __name__ == "__main__":
    main()
