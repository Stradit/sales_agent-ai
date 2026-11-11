"""Local, free embeddings via fastembed (ONNX, no torch). The model loads once
per process (~20 s the first time, then cached on disk by fastembed)."""

import threading
from typing import List

from apps.sales_copilot import settings

_model = None
_lock = threading.Lock()


def get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                try:
                    from fastembed import TextEmbedding
                    _model = TextEmbedding(settings.EMBED_MODEL)
                except Exception as e:
                    print(f"⚠️  [Copilot] FastEmbed embedding model could not be loaded ({e})")
                    return None
    return _model


def embed_documents(texts: List[str], batch_size: int = 64) -> List[List[float]]:
    model = get_model()
    if model is None:
        return [[0.0] * 384 for _ in texts]
    return [v.tolist() for v in model.passage_embed(texts, batch_size=batch_size)]


def embed_query(text: str) -> List[float]:
    model = get_model()
    if model is None:
        return [0.0] * 384
    return next(iter(model.query_embed([text]))).tolist()


def warm_up_in_background() -> None:
    """Load the model off the request path so the first chat isn't a 20 s wait."""
    threading.Thread(target=get_model, name="copilot-embed-warmup", daemon=True).start()
