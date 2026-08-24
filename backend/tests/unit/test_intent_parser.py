import asyncio

from backend.app.schemas.ai_intent import TransportMode
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


def test_natural_connectors_do_not_become_part_of_place_names():
    text = "先去花园酒店\n然后是友谊商店\n盒马鲜生\n万国广场"
    intent = asyncio.run(RuleBasedIntentParser().parse(text))

    assert [task.location_name for task in intent.tasks] == [
        "花园酒店",
        "友谊商店",
        "盒马鲜生",
        "万国广场",
    ]
    assert intent.constraints.hard.required_task_order == [0, 1, 2, 3]


def test_numbered_and_whitespace_place_lists_scale_beyond_four_places():
    numbered = "\n".join(f"{index + 1}. 地点{index + 1}" for index in range(12))
    intent = asyncio.run(RuleBasedIntentParser().parse(numbered))
    assert len(intent.tasks) == 12
    assert intent.tasks[0].location_name == "地点1"
    assert intent.tasks[-1].location_name == "地点12"

    whitespace = asyncio.run(
        RuleBasedIntentParser().parse("花园酒店 友谊商店 盒马鲜生 万国广场 广州塔")
    )
    assert [task.location_name for task in whitespace.tasks] == [
        "花园酒店",
        "友谊商店",
        "盒马鲜生",
        "万国广场",
        "广州塔",
    ]


def test_actions_are_normalized_to_searchable_places_and_goal_is_explicit():
    intent = asyncio.run(
        RuleBasedIntentParser().parse("先取快递，然后买药，再去盒马鲜生买水果，要求最短时间")
    )
    assert [task.location_name for task in intent.tasks] == ["快递", "药店", "盒马鲜生"]
    assert intent.preferences.optimization_goal == "shortest_time"

    distance = asyncio.run(RuleBasedIntentParser().parse("广州塔，越秀公园，距离最短"))
    assert distance.preferences.optimization_goal == "shortest_distance"
    assert distance.preferences.minimize_distance

    category = asyncio.run(RuleBasedIntentParser().parse("找一家评分高的餐厅"))
    assert category.tasks[0].location_name == "餐厅"


def test_public_transit_words_select_mode_without_polluting_place_names():
    intent = asyncio.run(
        RuleBasedIntentParser().parse("坐地铁去广州塔，然后乘公交到陈家祠，再换乘去北京路步行街")
    )

    assert intent.transport_mode == TransportMode.transit
    assert [task.location_name for task in intent.tasks] == [
        "广州塔",
        "陈家祠",
        "北京路步行街",
    ]


def test_joined_optimization_clause_does_not_become_a_fake_place():
    intent = asyncio.run(
        RuleBasedIntentParser().parse(
            "坐公共交通去广州塔，然后乘公交到陈家祠，再换乘去北京路步行街，"
            "最后去广东省博物馆，尽量少走路并要求最短时间"
        )
    )

    assert intent.transport_mode == TransportMode.transit
    assert intent.preferences.minimize_walking
    assert intent.preferences.optimization_goal == "shortest_time"
    assert [task.location_name for task in intent.tasks] == [
        "广州塔",
        "陈家祠",
        "北京路步行街",
        "广东省博物馆",
    ]


def test_relaxed_trip_and_hiking_avoidance_are_typed_preferences():
    intent = asyncio.run(RuleBasedIntentParser().parse("不想爬山，希望轻松旅游。"))

    assert intent.preferences.avoid_hiking is True
    assert intent.preferences.travel_style == "relaxed"
    assert intent.preferences.minimize_walking is True

    intensive = asyncio.run(
        RuleBasedIntentParser().parse("不要爬山，但希望行程紧凑，多打卡。")
    )
    assert intensive.preferences.avoid_hiking is True
    assert intensive.preferences.travel_style == "intensive"
