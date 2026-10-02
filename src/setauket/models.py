"""Lazy local inference; the service never downloads a model on a request."""

import hashlib
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_REPO = "Qdrant/bge-small-en-v1.5-onnx-Q"
EMBED_REVISION = "aa8f8b060edb00e03bfdd08813a2949946c8ba55"
EMBED_SHA256 = "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431"
TEXT_REPO = "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
TEXT_FILE = "qwen2.5-0.5b-instruct-q8_0.gguf"
TEXT_REVISION = "9217f5db79a29953eb74d5343926648285ec7e67"
TEXT_SHA256 = "ca59ca7f13d0e15a8cfa77bd17e65d24f6844b554a7b6c12e07a5f89ff76844e"


class LocalModels:
    def __init__(self, model_dir: Path):
        self.model_dir = Path(model_dir)
        self._embedder = None

    def install(self):
        from fastembed import TextEmbedding
        from huggingface_hub import hf_hub_download

        self.model_dir.mkdir(parents=True, exist_ok=True)
        from huggingface_hub import snapshot_download

        snapshot_download(repo_id=EMBED_REPO, revision=EMBED_REVISION,
                          local_dir=self.embedding_path,
                          allow_patterns=["config.json", "model_optimized.onnx", "ort_config.json",
                                          "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json",
                                          "vocab.txt"])
        self._verify_embedding()
        model = TextEmbedding(model_name=EMBED_MODEL, specific_model_path=str(self.embedding_path),
                              local_files_only=True)
        list(model.embed(["Model installation check"]))
        path = hf_hub_download(repo_id=TEXT_REPO, filename=TEXT_FILE, revision=TEXT_REVISION,
                               local_dir=self.model_dir / "text")
        with open(path, "rb") as model_file:
            digest = hashlib.file_digest(model_file, "sha256").hexdigest()
        if digest != TEXT_SHA256:
            Path(path).unlink(missing_ok=True)
            raise ValueError("Downloaded summarizer checksum does not match the pinned model")

    @property
    def text_path(self):
        return self.model_dir / "text" / TEXT_FILE

    @property
    def embedding_path(self):
        return self.model_dir / "embeddings" / EMBED_REVISION

    def _verify_embedding(self):
        path = self.embedding_path / "model_optimized.onnx"
        if not path.exists():
            raise FileNotFoundError("Embeddings missing; run 'setauket setup'")
        with path.open("rb") as model_file:
            if hashlib.file_digest(model_file, "sha256").hexdigest() != EMBED_SHA256:
                raise ValueError("Embedding model checksum does not match the pinned revision")

    def embed(self, content: str, query: bool = False) -> list[float]:
        from fastembed import TextEmbedding

        if self._embedder is None:
            self._verify_embedding()
            self._embedder = TextEmbedding(model_name=EMBED_MODEL,
                                           specific_model_path=str(self.embedding_path), local_files_only=True)
        if query:
            result = self._embedder.query_embed(content)
        else:
            result = self._embedder.embed([content])
        return next(iter(result)).tolist()

    def summarize(self, turns: list[dict]) -> str:
        if not self.text_path.exists():
            raise FileNotFoundError("Text model missing; run 'setauket setup'")
        if shutil.which("llama-cli") is None:
            raise FileNotFoundError("llama-cli missing; install llama.cpp with Homebrew")
        instructions = ("Summarize only facts explicitly stated in the transcript. Separate user requests "
                        "from completed actions. Include dates, filenames, decisions, participants and unresolved "
                        "questions only if explicitly present; omit categories with no evidence. Never invent "
                        "details or infer a participant's role from an agent ID. Do not reproduce secrets. "
                        "Write concise plain sentences without headings or examples.")
        partial = []
        batch, size = [], 0
        for turn in turns:
            when = (datetime.fromtimestamp(turn["created_at"], UTC).isoformat()
                    if turn.get("created_at") is not None else "unknown")
            # Long tool outputs must be represented in full rather than silently truncated.
            for offset in range(0, len(turn["content"]), 12000):
                item = f"{turn['role']} (agent {turn['agent_id']}, time {when}): {turn['content'][offset:offset + 12000]}"
                if batch and size + len(item) > 14000:
                    partial.append(self._complete(instructions, "\n".join(batch)))
                    batch, size = [], 0
                batch.append(item)
                size += len(item)
        if batch:
            partial.append(self._complete(instructions, "\n".join(batch)))
        while len(partial) > 1:
            partial = [self._complete(instructions + " Combine these summaries without losing dates or attribution.",
                                      "\n".join(partial[i:i + 4])) for i in range(0, len(partial), 4)]
        return partial[0] if partial else "No content was submitted for this session."

    def _complete(self, instructions, content):
        # Private files avoid leaking conversation content through process arguments.
        with tempfile.TemporaryDirectory(prefix="setauket-summary-") as directory:
            prompt = Path(directory) / "prompt.txt"
            output = Path(directory) / "output.txt"
            prompt.write_text(content)
            command = ["llama-cli", "-m", str(self.text_path), "-c", "8192",
                       "-ngl", "99" if sys.platform == "darwin" else "0", "-n", "800",
                       "--reasoning", "off", "--no-display-prompt", "--no-warmup",
                       "--no-show-timings", "--simple-io", "-st", "-sys", instructions,
                       "-f", str(prompt), "-o", str(output)]
            result = subprocess.run(command, capture_output=True, timeout=300, check=False)
            if result.returncode != 0:
                raise RuntimeError(f"llama-cli failed with status {result.returncode}")
            raw = output.read_text()
            prefix = f"User:\n{content}\n\nAssistant:\n"
            if not raw.startswith(prefix):
                raise ValueError("Unexpected summarizer output format")
            text = raw[len(prefix):].strip()
            if text.startswith("<think>") and "</think>" in text:
                text = text.split("</think>", 1)[1].strip()
            if not text:
                raise ValueError("Summarizer returned an empty response")
            return text

    def unload_text(self):
        pass
