import asyncio

from backend.app.services.intent_parser import RuleBasedIntentParser


def test_chinese_request_extracts_preferences_and_deadline():
    text = "明天下午两点从学校出发，先取快递，再找评分高的蛋糕店，五点前到医院，尽量少走路"
    intent = asyncio.run(RuleBasedIntentParser().parse(text))
    assert intent.origin == "学校"
    assert intent.departure_time is not None
    assert intent.departure_time.hour == 14
    assert intent.preferences.minimize_walking
    assert intent.preferences.prefer_high_rating
    assert any(task.deadline and task.deadline.hour == 17 for task in intent.tasks)
