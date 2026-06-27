"""
本地嵌入模型 — ONNX Runtime + BGE-small-en-v1.5

使用 BAAI/bge-small-en-v1.5（384维）通过 ONNX Runtime 本地推理。
首次运行时自动查找/下载模型。不可用时降级为哈希伪向量以保证可用性。
"""

import os
import warnings
from pathlib import Path
from typing import Sequence

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore
    _HAS_NUMPY = False


class LocalEmbedder:
    """
    本地嵌入模型封装

    使用方法:
        embedder = LocalEmbedder()
        vectors = embedder.embed(["def hello(): ...", "class Foo: ..."])
        # vectors.shape -> (2, 384)
    """

    MODEL_ID = "BAAI/bge-small-en-v1.5"
    DIMENSION = 384
    MAX_SEQ_LENGTH = 512

    def __init__(self, cache_dir: str | None = None,
                 batch_size: int = 32):
        self.batch_size = batch_size
        self.cache_dir = cache_dir or ".code-kb/models"
        self._session = None       # onnxruntime.InferenceSession
        self._tokenizer = None     # transformers.AutoTokenizer
        self._checked = False

    # ──── public API ────

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """将文本列表转换为向量列表，返回 list[list[float]] 每行384维"""
        if not texts:
            return []

        self._ensure_loaded()

        all_vecs: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            batch_vecs = self._encode_batch(batch)
            all_vecs.extend(batch_vecs)
        return all_vecs

    def embed_single(self, text: str) -> list[float]:
        """嵌入单个文本"""
        result = self.embed([text])
        return result[0] if result else []

    def is_available(self) -> bool:
        """ONNX 模型是否可用"""
        self._ensure_loaded()
        return self._session is not None and self._tokenizer is not None

    # ──── lazy loading ────

    def _ensure_loaded(self):
        """惰性加载 ONNX session 和 tokenizer"""
        if self._checked:
            return
        self._checked = True

        if not _HAS_NUMPY:
            warnings.warn(
                "numpy is required for ONNX inference. "
                "Install: pip install numpy>=1.21  "
                "Falling back to hash-based pseudo-embeddings."
            )
            return

        model_path = self._find_model()
        if not model_path:
            warnings.warn(
                "BGE-small ONNX model not found. "
                "Run: python install.py  or  "
                "optimum-cli export onnx --model BAAI/bge-small-en-v1.5 bge-small-onnx/  "
                "Falling back to hash-based pseudo-embeddings."
            )
            return

        # 加载 tokenizer
        try:
            from transformers import AutoTokenizer
            self._tokenizer = AutoTokenizer.from_pretrained(
                str(Path(model_path).parent)
            )
        except Exception as e:
            warnings.warn(f"Failed to load tokenizer: {e}")
            return

        # 加载 ONNX session
        try:
            import onnxruntime as ort
            self._session = ort.InferenceSession(
                model_path,
                providers=["CPUExecutionProvider"],
            )
        except Exception as e:
            warnings.warn(f"Failed to load ONNX session: {e}")
            return

    def _find_model(self) -> str | None:
        """多路径查找 ONNX 模型文件，返回 model.onnx 的路径"""
        candidates = [
            # 1. install.py 缓存位置
            Path.home() / ".cache" / "codebase-skill" / "models" / "bge-small-en-v1.5" / "model.onnx",
            # 2. 项目本地缓存
            Path(self.cache_dir) / "bge-small-en-v1.5" / "model.onnx",
            # 3. optimum 导出目录
            Path("bge-small-onnx") / "model.onnx",
        ]

        # 4. HuggingFace 缓存
        hf_cache = os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
        hf_model = Path(hf_cache) / "hub" / "models--BAAI--bge-small-en-v1.5"
        if hf_model.exists():
            snapshots = hf_model / "snapshots"
            if snapshots.exists():
                for snapshot in sorted(snapshots.iterdir(), reverse=True):
                    for name in ["onnx/model.onnx", "model.onnx", "onnx/model_quantized.onnx"]:
                        p = snapshot / name
                        if p.exists():
                            candidates.append(p)

        for p in candidates:
            if p.exists():
                return str(p)

        return None

    # ──── encoding ────

    def _encode_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """编码一批文本"""
        if self._session is not None and self._tokenizer is not None:
            return self._onnx_encode(texts)
        else:
            return self._hash_encode(texts)

    def _onnx_encode(self, texts: Sequence[str]) -> list[list[float]]:
        """
        ONNX Runtime 真实推理

        流程：tokenize -> session.run -> CLS pooling -> L2 normalize

        BGE 模型官方实现使用 [CLS] token 的输出作为句子嵌入。
        """
        if not _HAS_NUMPY:
            warnings.warn("numpy not available, falling back to hash encoding")
            return self._hash_encode(texts)

        # Tokenize
        encoded = self._tokenizer(
            list(texts),
            padding=True,
            truncation=True,
            max_length=self.MAX_SEQ_LENGTH,
            return_tensors="np",
        )

        input_ids = encoded["input_ids"].astype(np.int64)
        attention_mask = encoded["attention_mask"].astype(np.int64)

        # ONNX 推理 — 获取 last_hidden_state
        ort_inputs = {
            self._session.get_inputs()[0].name: input_ids,
            self._session.get_inputs()[1].name: attention_mask,
        }
        ort_outputs = self._session.run(None, ort_inputs)
        last_hidden_state = ort_outputs[0]  # shape: (batch, seq_len, 384)

        # BGE 官方: 取 [CLS] token 输出作为句子嵌入
        sentence_embeddings = last_hidden_state[:, 0, :]

        # L2 normalize
        norms = np.linalg.norm(sentence_embeddings, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)  # 防止除零
        normalized = sentence_embeddings / norms

        return normalized.tolist()

    def _hash_encode(self, texts: Sequence[str]) -> list[list[float]]:
        """
        基于字符 n-gram 哈希的伪向量编码（降级方案）

        保证：相同文本 -> 相同向量，相似文本 -> 余弦相似度较高。
        作为 ONNX 模型不可用时的确定性降级方案。
        """
        import hashlib
        import math

        vectors: list[list[float]] = []

        for text in texts:
            vec = [0.0] * self.DIMENSION
            for n in range(2, 6):
                for i in range(len(text) - n + 1):
                    ngram = text[i:i + n]
                    h = hashlib.sha256(ngram.encode("utf-8")).hexdigest()
                    idx = int(h[:16], 16) % self.DIMENSION
                    val = (int(h[16:24], 16) / 0xFFFFFFFF) * 2.0 - 1.0
                    vec[idx] += val
            norm = math.sqrt(sum(v * v for v in vec))
            if norm > 0:
                vec = [v / norm for v in vec]
            vectors.append(vec)

        return vectors
