"""Knowledge base for retrieval-augmented answers (RAG).

Chunks come from three places, so a fact has one source of truth: Markdown documents under ``data/knowledge``, the
live ``BudgetPolicy`` (numbers are generated from the code, never typed into a document) and the rule packs.

Retrieval is BM25 over word tokens. Permission filtering happens *before* ranking, so a chunk the asker may not see can
never reach the model, a cache entry or a trace. An embedding retriever can replace ``search`` behind the same interface.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from eventmate.agents.budget import BudgetPolicy
from eventmate.assistant.identity import Perm, Principal
from eventmate.planning.rules import load_packs
from eventmate.schemas import _Model

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "data" / "knowledge"
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an the of to in on for and or is are be it its this that with as at by from what how do does can i my me you your".split())
_FRONT = re.compile(r"\A---\npermission: (kb:[a-z]+)\n---\n", re.S)
K1, B = 1.4, 0.75


class Chunk(_Model):
    id: str
    title: str
    text: str
    source: str
    permission: str = Perm.KB_READ.value


class Hit(_Model):
    chunk: Chunk
    score: float


def _tokens(text: str) -> List[str]:
    out = []
    for w in _WORD.findall(text.lower()):
        if w in _STOP:
            continue
        out.append(w[:-1] if len(w) > 3 and w.endswith("s") else w)  # crude plural folding
    return out


class KnowledgeBase:
    def __init__(self, chunks: Sequence[Chunk]):
        if len({c.id for c in chunks}) != len(chunks):
            raise ValueError("duplicate chunk ids")
        self.chunks: Tuple[Chunk, ...] = tuple(chunks)
        self._tf = [Counter(_tokens(c.title + " " + c.text)) for c in self.chunks]
        self._len = [sum(tf.values()) or 1 for tf in self._tf]
        self._avg = sum(self._len) / max(1, len(self._len))
        df: Counter = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(self.chunks)
        self._idf: Dict[str, float] = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.min_score = 0.0  # set from policy: weak matches are no match (the Dify score threshold)
        self.version = hashlib.sha256("\n".join(f"{c.id}|{c.permission}|{c.text}" for c in self.chunks).encode()).hexdigest()[:16]

    def get(self, chunk_id: str) -> Optional[Chunk]:
        return next((c for c in self.chunks if c.id == chunk_id), None)

    def search(self, query: str, principal: Principal, k: int = 4) -> List[Hit]:
        """Top chunks the principal may read. Chunks outside their permissions are never scored."""
        allowed = principal.perms()
        q = _tokens(query)
        hits = []
        for i, c in enumerate(self.chunks):
            if c.permission not in {p.value for p in allowed}:
                continue
            tf, norm = self._tf[i], K1 * (1 - B + B * self._len[i] / self._avg)
            score = sum(self._idf.get(t, 0.0) * tf[t] * (K1 + 1) / (tf[t] + norm) for t in q if t in tf)
            if score > 0 and score >= self.min_score:
                hits.append(Hit(chunk=c, score=round(score, 4)))
        return sorted(hits, key=lambda h: (-h.score, h.chunk.id))[:k]


def _doc_chunks(directory: Path) -> List[Chunk]:
    chunks: List[Chunk] = []
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        m = _FRONT.match(text)
        if not m:
            raise ValueError(f"{path.name}: front matter 'permission: kb:...' is required")
        perm, body = m.group(1), text[m.end():]
        for n, part in enumerate(re.split(r"\n(?=## )", body)):
            lines = part.strip().splitlines()
            if not lines:
                continue
            title = lines[0].lstrip("# ").strip()
            chunks.append(Chunk(id=f"{path.stem}-{n}", title=title, text=" ".join(lines[1:]).strip() or title, source=f"doc:{path.name}", permission=perm))
    return chunks


def _policy_chunks(policy: BudgetPolicy) -> List[Chunk]:
    split = ", ".join(f"{k} {v / 100:g} percent" for k, v in sorted(policy.weights_bp.items()))
    text = (
        f"The budget is split across services by weight: {split}. "
        f"{policy.contingency_bp / 100:g} percent of the total budget is held back as contingency before the split. "
        f"The budget reality check calls a budget comfortable when the cheapest eligible vendors use at most {policy.comfortable_bp / 100:g} percent of the budget left after contingency."
    )
    return [Chunk(id="policy-budget", title="Budget split and contingency", text=text, source="code:BudgetPolicy", permission=Perm.KB_READ.value)]


def _pack_chunks() -> List[Chunk]:
    out = []
    for p in load_packs():
        status = "reviewed by " + (p.reviewed_by or "?") if p.reviewed else "an unreviewed draft"
        items = " ".join(f"{i.title}: {i.detail}" for i in p.items)
        out.append(Chunk(id=p.id, title=f"{p.diet.value.replace('_', ' ')} dietary checklist (v{p.version}, {status})", text=items, source=f"pack:{p.id}@v{p.version}", permission=Perm.KB_READ.value))
    return out


def build_default(policy: Optional[BudgetPolicy] = None, directory: Optional[Path] = None) -> KnowledgeBase:
    return KnowledgeBase(_doc_chunks(directory or KNOWLEDGE_DIR) + _policy_chunks(policy or BudgetPolicy()) + _pack_chunks())
