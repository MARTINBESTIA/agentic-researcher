"""Lokálne embeddingy (Ollama / llama-server s --embedding) na zoskupovanie podobných nálezov.

Použitie:
- pri zaraďovaní tvrdenia vyberú hypotézy, ktoré model dostane ako kandidátov,
- v správe upozornia na hypotézy, ktoré sú možno rovnaký problém (zlúčiš ich príkazom merge).

Vektory sa ukladajú do SQLite a prepočítajú sa len pri zmene textu. Ak embedding model
nebeží, systém prejde na textovú podobnosť (rapidfuzz) a pokračuje.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Sequence

import numpy as np
from openai import OpenAI

from .config import EmbeddingsConfig, LLMConfig
from .db import DB

log = logging.getLogger(__name__)

# Qwen3-Embedding dáva lepšie výsledky s inštrukciou; rovnaká predpona na oboch stranách
# zvýšila podobnosť rovnakých problémov z ~0,6 na ~0,75 (merané na slovenských vetách).
INSTRUCTION = (
    "Instruct: Given a business problem statement, retrieve statements describing the same problem "
    "of the same group\nQuery: "
)


def hypothesis_text(h) -> str:
    return f"{h['title']} (segment: {h['segment'] or '?'}). {h['description'] or ''}".strip()


def claim_text(c) -> str:
    return f"{c['problem']} (segment: {c['affected_party'] or '?'})"


class Embedder:
    def __init__(self, cfg: EmbeddingsConfig, llm_cfg: LLMConfig, db: DB):
        self.cfg, self.db = cfg, db
        self.model = cfg.model
        self.client = OpenAI(
            base_url=cfg.base_url or llm_cfg.base_url, api_key=llm_cfg.api_key, timeout=120, max_retries=1
        )

    def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        """Normalizované vektory (riadky), takže kosínusová podobnosť je skalárny súčin."""
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        resp = self.client.embeddings.create(model=self.model, input=[INSTRUCTION + t for t in texts])
        matrix = np.array([d.embedding for d in sorted(resp.data, key=lambda d: d.index)], dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.where(norms == 0, 1, norms)

    def vectors(self, kind: str, items: Sequence[tuple[int, str]]) -> np.ndarray:
        """Vektory pre (id, text) s cache v databáze."""
        out: list[np.ndarray | None] = []
        missing: list[tuple[int, int, str, str]] = []
        for pos, (ref_id, text) in enumerate(items):
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
            blob = self.db.get_embedding(kind, ref_id, self.model, digest)
            if blob is None:
                out.append(None)
                missing.append((pos, ref_id, text, digest))
            else:
                out.append(np.frombuffer(blob, dtype=np.float32))
        for start in range(0, len(missing), 32):
            batch = missing[start : start + 32]
            matrix = self.embed_texts([text for _, _, text, _ in batch])
            for (pos, ref_id, _, digest), vec in zip(batch, matrix):
                self.db.put_embedding(kind, ref_id, self.model, digest, vec.astype(np.float32).tobytes())
                out[pos] = vec
        return np.vstack(out) if out else np.zeros((0, 0), dtype=np.float32)

    def hypothesis_matrix(self, hypotheses: Sequence) -> np.ndarray:
        return self.vectors("hypothesis", [(h["id"], hypothesis_text(h)) for h in hypotheses])

    def claim_vector(self, claim) -> np.ndarray:
        return self.vectors("claim", [(claim["id"], claim_text(claim))])[0]

    def ping(self) -> int:
        return int(self.embed_texts(["test"]).shape[1])


def rank_hypotheses(embedder: Embedder, claim, hypotheses: Sequence, k: int = 8) -> list:
    """Najpodobnejšie hypotézy k tvrdeniu podľa embeddingov."""
    if not hypotheses:
        return []
    sims = embedder.hypothesis_matrix(hypotheses) @ embedder.claim_vector(claim)
    order = np.argsort(-sims)[:k]
    return [hypotheses[i] for i in order]


def similar_pairs(embedder: Embedder, hypotheses: Sequence, threshold: float) -> list[tuple[int, int, float]]:
    """Dvojice hypotéz (id, id, podobnosť), ktoré sú možno rovnaký problém."""
    if len(hypotheses) < 2:
        return []
    matrix = embedder.hypothesis_matrix(hypotheses)
    sims = matrix @ matrix.T
    pairs = []
    for i in range(len(hypotheses)):
        for j in range(i + 1, len(hypotheses)):
            if sims[i, j] >= threshold:
                pairs.append((hypotheses[i]["id"], hypotheses[j]["id"], float(sims[i, j])))
    return sorted(pairs, key=lambda p: -p[2])
