"""
嵌入模型模块。

复用现有 ModelBinding 系统——用户在设置页绑定「嵌入」功能位的模型，
本模块通过 endpoint/service.py:resolve() 自动获取配置。

支持:
  - local: sentence-transformers (离线, 无 Key 时自动启用)
  - 云端: 任意 OpenAI 兼容嵌入 API (通过设置页配置)
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

DEFAULT_LOCAL_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class Embedder:
    """文本嵌入器。优先云端, 不可用时回退本地。"""

    def __init__(
        self,
        *,
        local_model: str | None = None,
    ):
        self._local_model = local_model or DEFAULT_LOCAL_MODEL
        self._model: Any = None   # SentenceTransformer 实例, 或 "openai"
        self._dim: int | None = None
        self._load_attempted = False
        self._resolved_backend: str = ""

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._ensure_loaded()
        return self._dim or 384

    @staticmethod
    def is_local_available() -> bool:
        try:
            import sentence_transformers  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def is_available() -> bool:
        return Embedder.is_local_available()

    async def resolve(self, db) -> None:
        """从数据库解析嵌入模型配置。必须在首次 embed() 前调用。"""
        if self._load_attempted:
            return
        self._load_attempted = True

        try:
            from app.modules.endpoint import service as ps
            model = await ps.resolve(db, purpose="embedding")
            self._model = "openai"
            self._dim = model.context_window  # 嵌入模型的 context_window 存维度
            self._openai_config = {
                "base_url": model.base_url,
                "api_key": model.api_key,
                "model": model.model_id,
            }
            self._resolved_backend = "cloud"
            log.info("embedder_resolved", backend="cloud", model=model.model_id, dim=self._dim)
            return
        except Exception:
            log.debug("embedder_cloud_unavailable", reason="no embedding model bound")

        # 回退本地
        if self.is_local_available():
            self._load_local()
        else:
            raise RuntimeError("无可用嵌入模型。安装 sentence-transformers 或在设置页绑定嵌入模型。")

    def _ensure_loaded(self):
        if self._model is not None:
            return
        if self._load_attempted:
            raise RuntimeError("模型加载已失败")
        self._load_attempted = True
        self._load_local()

    def _load_local(self):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as err:
            raise ImportError("sentence-transformers 未安装: uv sync --extra memory") from err

        log.info("loading_local_embedder", model=self._local_model)
        try:
            self._model = SentenceTransformer(self._local_model, local_files_only=True)
        except Exception:
            self._model = SentenceTransformer(self._local_model, local_files_only=False)

        self._dim = self._model.get_sentence_embedding_dimension()
        self._resolved_backend = "local"
        log.info("embedder_ready", backend="local", dim=self._dim)

    def embed(self, text: str) -> list[float]:
        self._ensure_loaded()

        if self._model == "openai":
            return self._embed_cloud(text)
        return self._model.encode(text, normalize_embeddings=True).tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self._ensure_loaded()

        if self._model == "openai":
            return [self._embed_cloud(t) for t in texts]
        return self._model.encode(texts, normalize_embeddings=True).tolist()

    def _embed_cloud(self, text: str) -> list[float]:
        import httpx

        cfg = self._openai_config
        resp = httpx.post(
            f"{cfg['base_url']}/embeddings",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            json={"model": cfg["model"], "input": text[:8000]},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
