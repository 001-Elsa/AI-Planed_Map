import asyncio

from backend.app.core.exceptions import UpstreamError
from backend.app.services.intent_parser import FallbackIntentParser, RuleBasedIntentParser


class BoomParser:
    name = "boom"

    async def parse(self, text: str):
        raise UpstreamError("timeout", {"reason": "boom"})


class BadJsonParser:
    name = "bad-json"
    input_tokens = 10
    output_tokens = 0

    async def parse(self, text: str):
        raise ValueError("strict json failed")


def test_fallback_parser_switches_to_rules_and_lowers_confidence():
    async def scenario():
        parser = FallbackIntentParser(BoomParser())
        intent = await parser.parse("明天下午两点从学校出发，五点前到医院")
        assert parser.fallback_used is True
        assert parser.last_parser == "rule-based-v2"
        assert intent.tasks
        assert intent.constraints.uncertain
        assert intent.constraints.uncertain[0].confidence == 0.45

        ok = FallbackIntentParser(RuleBasedIntentParser())
        intent2 = await ok.parse("从家出发去公园")
        assert ok.fallback_used is False
        assert intent2.tasks

        bad = FallbackIntentParser(BadJsonParser())
        intent3 = await bad.parse("从家出发去超市")
        assert bad.fallback_used is True
        assert intent3.constraints.uncertain

    asyncio.run(scenario())
