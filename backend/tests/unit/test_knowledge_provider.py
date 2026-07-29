import asyncio

from backend.app.clients.knowledge_client import CuratedKnowledgeProvider


def test_attraction_brief_is_grounded_and_unknown_is_not_hallucinated():
    provider = CuratedKnowledgeProvider()
    known = asyncio.run(provider.attraction_brief("拙政园"))
    assert known is not None
    assert known["grounded"] is True
    assert known["source_url"].startswith("https://ylj.suzhou.gov.cn/")
    assert known["citations"]
    hits = asyncio.run(provider.search("拙政园 廊桥", top_k=2))
    assert hits
    assert hits[0]["citation"]["doc_id"]
    assert asyncio.run(provider.attraction_brief("不存在的火星景点")) is None
