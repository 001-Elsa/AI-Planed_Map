import json
from pathlib import Path
from typing import Any, Protocol


class KnowledgeProvider(Protocol):
    name: str

    async def attraction_brief(self, name: str) -> dict[str, Any] | None: ...


class CuratedKnowledgeProvider:
    name = "curated-attractions-v1"

    def __init__(self) -> None:
        path = Path(__file__).resolve().parents[1] / "knowledge" / "attractions.json"
        self.records = json.loads(path.read_text(encoding="utf-8"))

    async def attraction_brief(self, name: str) -> dict[str, Any] | None:
        normalized = name.strip().casefold()
        for record in self.records:
            if any(
                normalized in alias.casefold() or alias.casefold() in normalized
                for alias in record["names"]
            ):
                return {
                    **record,
                    "provider": self.name,
                    "grounded": True,
                }
        return None
