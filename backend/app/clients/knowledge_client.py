import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Protocol

TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9]+")


def tokenize(text: str) -> list[str]:
    return [token.casefold() for token in TOKEN_RE.findall(text or "")]


class KnowledgeProvider(Protocol):
    name: str

    async def attraction_brief(self, name: str) -> dict[str, Any] | None: ...
    async def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]: ...


class CuratedKnowledgeProvider:
    """Curated JSON knowledge with lightweight local RAG (chunk + TF-IDF retrieve)."""

    name = "curated-attractions-rag-v1"

    def __init__(self) -> None:
        path = Path(__file__).resolve().parents[1] / "knowledge" / "attractions.json"
        self.records = json.loads(path.read_text(encoding="utf-8"))
        self.chunks = self._build_chunks(self.records)
        self._df = Counter()
        for chunk in self.chunks:
            for term in set(chunk["tokens"]):
                self._df[term] += 1
        self._n_docs = max(1, len(self.chunks))

    @staticmethod
    def _build_chunks(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        for record in records:
            base_names = " ".join(record.get("names") or [])
            sections = [
                ("summary", record.get("summary") or record.get("brief") or ""),
                ("history", record.get("history") or ""),
                ("tips", " ".join(record.get("tips") or [])),
            ]
            for section, text in sections:
                text = str(text).strip()
                if not text:
                    continue
                # Rough character-window chunking for Chinese prose.
                window = 180
                for start in range(0, len(text), window):
                    piece = text[start : start + window]
                    content = f"{base_names}\n{piece}"
                    chunks.append(
                        {
                            "doc_id": record.get("id") or base_names,
                            "names": record.get("names") or [],
                            "section": section,
                            "content": piece,
                            "source_url": record.get("source_url") or record.get("source"),
                            "updated_at": record.get("updated_at"),
                            "version": record.get("version") or "1",
                            "tokens": tokenize(content),
                            "record": record,
                        }
                    )
        return chunks

    def _tfidf(self, tokens: list[str]) -> dict[str, float]:
        counts = Counter(tokens)
        total = max(1, len(tokens))
        vector: dict[str, float] = {}
        for term, count in counts.items():
            tf = count / total
            idf = math.log((1 + self._n_docs) / (1 + self._df.get(term, 0))) + 1.0
            vector[term] = tf * idf
        return vector

    @staticmethod
    def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        dot = sum(value * b.get(key, 0.0) for key, value in a.items())
        norm_a = math.sqrt(sum(value * value for value in a.values()))
        norm_b = math.sqrt(sum(value * value for value in b.values()))
        if norm_a <= 1e-12 or norm_b <= 1e-12:
            return 0.0
        return dot / (norm_a * norm_b)

    async def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        query_vec = self._tfidf(tokenize(query))
        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk in self.chunks:
            score = self._cosine(query_vec, self._tfidf(chunk["tokens"]))
            # Alias boost for exact attraction names.
            if any(alias.casefold() in query.casefold() for alias in chunk["names"]):
                score += 0.25
            if score > 0.05:
                scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        results = []
        for score, chunk in scored[:top_k]:
            results.append(
                {
                    "score": round(score, 4),
                    "doc_id": chunk["doc_id"],
                    "section": chunk["section"],
                    "content": chunk["content"],
                    "source_url": chunk["source_url"],
                    "updated_at": chunk["updated_at"],
                    "version": chunk["version"],
                    "citation": {
                        "doc_id": chunk["doc_id"],
                        "section": chunk["section"],
                        "source_url": chunk["source_url"],
                    },
                }
            )
        return results

    async def attraction_brief(self, name: str) -> dict[str, Any] | None:
        normalized = name.strip().casefold()
        for record in self.records:
            if any(
                normalized in alias.casefold() or alias.casefold() in normalized
                for alias in record["names"]
            ):
                hits = await self.search(name, top_k=3)
                return {
                    **record,
                    "provider": self.name,
                    "grounded": True,
                    "retrieval": hits,
                    "citations": [hit["citation"] for hit in hits],
                }
        # Refuse hallucination: only return grounded retrieval for known docs.
        hits = await self.search(name, top_k=3)
        if not hits or hits[0]["score"] < 0.35:
            return None
        top = hits[0]
        record = next(
            (item for item in self.records if (item.get("id") or "") == top["doc_id"]),
            None,
        )
        if record is None:
            return None
        return {
            **record,
            "provider": self.name,
            "grounded": True,
            "retrieval": hits,
            "citations": [hit["citation"] for hit in hits],
        }
