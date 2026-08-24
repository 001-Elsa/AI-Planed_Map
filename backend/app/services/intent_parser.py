import json
import re
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from backend.app.core.config import Settings
from backend.app.core.exceptions import UpstreamError
from backend.app.schemas.ai_intent import (
    MAX_PLANNING_TASKS,
    PlanningIntent,
    PlanningPreferences,
    PlanningTask,
    TransportMode,
    UncertainConstraint,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


class IntentParser(Protocol):
    name: str

    async def parse(self, text: str) -> PlanningIntent: ...


class RuleBasedIntentParser:
    """Deterministic fallback for local development and graceful degradation."""

    name = "rule-based-v2"
    input_tokens = 0
    output_tokens = 0
    _action_split = re.compile(
        r"(?:[，,；;。\n]+|然后(?:去|到|是)?|接着(?:去|到|是)?|"
        r"最后(?:去|到|是)?|再(?:去|到|是)?|顺路(?:去|到|是)?|先(?:去|到|是)?)"
    )
    _ordered_markers = re.compile(r"(?:先|然后|接着|最后|依次|按顺序)")
    _list_marker = re.compile(r"(?:^|\n)\s*(?:\d{1,2}|[一二三四五六七八九十]+)[.、)）]\s*")

    def _split_actions(self, text: str) -> list[str]:
        normalized = self._list_marker.sub("\n", text.strip())
        pieces = [part.strip(" 。.!！\t") for part in self._action_split.split(normalized)]
        pieces = [part for part in pieces if part]
        if len(pieces) == 1:
            whitespace_parts = [part for part in re.split(r"\s+", pieces[0]) if part]
            # A whitespace-only list is common when users paste place names. Do not split
            # ordinary prose: every item must look like a compact POI name.
            if 2 <= len(whitespace_parts) <= MAX_PLANNING_TASKS and all(
                1 < len(part) <= 30
                and not re.search(r"(?:出发|分钟|小时|之前|以后|尽量|希望|需要)", part)
                for part in whitespace_parts
            ):
                pieces = whitespace_parts
        return pieces

    @staticmethod
    def _location_from_action(piece: str) -> str:
        location = piece.strip()
        generic_actions = {
            "吃饭": "餐厅",
            "吃东西": "餐厅",
            "买药": "药店",
            "买水果": "水果店",
            "买东西": "商场",
            "取快递": "快递",
            "取件": "快递",
            "购物": "商场",
            "喝咖啡": "咖啡",
        }
        if location in generic_actions:
            return generic_actions[location]
        location = re.sub(r"^(?:去|到|前往|前去|找|搜索|查找|游览|参观|逛)\s*", "", location)
        location = re.sub(
            r"^(?:(?:一家|一个|附近的|最近的|评分高的|口碑好的)\s*)+",
            "",
            location,
        )
        location = re.sub(
            r"(?:买|取|吃|喝|逛|参观|游览|办理)(?:东西|水果|晚饭|午饭|早餐|咖啡|业务|手续|街)?$",
            "",
            location,
        ).strip()
        return generic_actions.get(location, location)

    def _datetime(self, text: str, context: str = "") -> datetime | None:
        for chinese, number in {
            "十二": "12",
            "十一": "11",
            "十": "10",
            "九": "9",
            "八": "8",
            "七": "7",
            "六": "6",
            "五": "5",
            "四": "4",
            "三": "3",
            "两": "2",
            "二": "2",
            "一": "1",
        }.items():
            text = text.replace(chinese, number)
        match = re.search(r"(?:(明天|后天|今天).{0,8})?([0-2]?\d)(?:[:：点时]([0-5]?\d)?)", text)
        if not match:
            return None
        day_word, hour, minute = match.groups()
        now = datetime.now(SHANGHAI)
        day_delta = {"明天": 1, "后天": 2}.get(day_word, 0)
        value = now + timedelta(days=day_delta)
        parsed_hour = int(hour)
        if "下午" in context + text and parsed_hour < 12:
            parsed_hour += 12
        return value.replace(hour=parsed_hour, minute=int(minute or 0), second=0, microsecond=0)

    async def parse(self, text: str) -> PlanningIntent:
        mode = TransportMode.walking
        if any(word in text for word in ("开车", "驾车", "自驾")):
            mode = TransportMode.driving
        elif any(word in text for word in ("公交", "地铁", "轻轨", "公共交通", "坐车", "换乘")):
            mode = TransportMode.transit
        elif any(word in text for word in ("骑车", "骑行", "自行车")):
            mode = TransportMode.cycling

        preferences = PlanningPreferences(
            optimization_goal=(
                "shortest_distance"
                if any(word in text for word in ("路程最短", "距离最短", "少绕路"))
                else "shortest_time"
                if any(word in text for word in ("最快", "最短时间", "用时最少", "尽快"))
                else "balanced"
            ),
            minimize_distance=any(
                word in text for word in ("顺路", "路程最短", "距离最短", "少绕路")
            ),
            minimize_walking=any(word in text for word in ("少走路", "不想走路")),
            minimize_cost=any(word in text for word in ("便宜", "省钱", "费用低")),
            prefer_high_rating=any(word in text for word in ("评分高", "口碑好")),
            travel_style=(
                "relaxed"
                if any(word in text for word in ("轻松", "悠闲", "休闲", "慢节奏", "不赶"))
                else "intensive"
                if any(word in text for word in ("紧凑", "特种兵", "多安排", "多打卡"))
                else "balanced"
            ),
            avoid_hiking=any(
                word in text
                for word in (
                    "不想爬山",
                    "不要爬山",
                    "不爬山",
                    "避免爬山",
                    "不想登山",
                    "不要登山",
                    "避免登山",
                )
            ),
        )
        if preferences.travel_style == "relaxed":
            preferences.minimize_walking = True
        departure = None
        departure_match = re.search(
            r"((?:明天|后天|今天)?.{0,5}(?:上午|下午|晚上)?"
            r"(?:[一二两三四五六七八九十]{1,2}|\d{1,2})(?:[:：点时]\d{0,2})?)从",
            text,
        )
        if departure_match:
            departure = self._datetime(departure_match.group(1), text)

        origin = None
        origin_match = re.search(r"从([^，,；;\n]+?)(?:出发|开始)", text)
        if origin_match:
            origin = origin_match.group(1).strip()

        cleaned = re.sub(r"^.*?出发[，,\s]*", "", text)
        pieces = self._split_actions(cleaned)
        ignored = (
            "尽量少走路",
            "尽量省钱",
            "路程最短",
            "距离最短",
            "最短时间",
            "用时最少",
            "尽快",
            "不想爬山",
            "不要爬山",
            "希望轻松旅游",
            "希望悠闲旅游",
        )
        tasks: list[PlanningTask] = []
        for piece in pieces:
            if not piece or piece in ignored:
                continue
            # A trailing optimization clause is often joined to the last stop with
            # “并/而且/同时”, for example “最后去博物馆，并要求最短时间”.  Keep
            # those words in the preference model, but never send “并要求” to POI
            # search as if it were another place.  The look-ahead avoids damaging
            # legitimate POI names that happen to start with “并”.
            piece = re.sub(
                r"^(?:并且|并|且|而且|同时|另外|此外)(?=(?:要求|希望|需要|请|尽量|优先|最好|要))",
                "",
                piece,
            ).strip()
            if not piece or piece in ignored:
                continue
            piece = re.sub(
                r"^(?:\d{1,2}|[一二三四五六七八九十]+)[.、)）]\s*",
                "",
                piece,
            )
            piece = re.sub(
                r"^(?:(?:坐|乘|搭|换乘)?(?:公共交通|公交|地铁|轻轨|车)|坐车|乘车|换乘)(?:去|到)?\s*",
                "",
                piece,
            ).strip()
            piece = re.sub(r"^(?:去|到|是|前往|前去)\s*", "", piece).strip()
            piece = re.sub(
                r"(尽量少走路|尽量省钱|路程最短|不想爬山|不要爬山|希望轻松旅游)$",
                "",
                piece,
            ).strip()
            deadline = None
            deadline_match = re.search(
                r"((?:今天|明天|后天)?.{0,4}(?:[一二三四五六七八九十]{1,2}|\d{1,2})(?:[:：点时]\d{0,2})?)前",
                piece,
            )
            if deadline_match:
                deadline = self._datetime(deadline_match.group(1), text)
                if (
                    deadline
                    and departure
                    and not any(
                        word in deadline_match.group(1) for word in ("今天", "明天", "后天")
                    )
                ):
                    deadline = deadline.replace(
                        year=departure.year, month=departure.month, day=departure.day
                    )
            service = 0
            service_match = re.search(r"(?:停留|待|逛)(\d{1,3})分钟", piece)
            if service_match:
                service = int(service_match.group(1))
            location = re.sub(
                r"(?:在)?(?:今天|明天|后天)?(?:上午|下午|晚上)?\d{1,2}(?:[:：点时]\d{0,2})?前|"
                r"(?:停留|待|逛)\d{1,3}分钟|五点前|"
                r"(?:尽量少走路|尽量省钱|路程最短|距离最短|最短时间|用时最少|尽快|"
                r"不想爬山|不要爬山|希望轻松旅游|希望悠闲旅游)",
                "",
                piece,
            ).strip()
            location = self._location_from_action(location)
            if re.fullmatch(
                r"(?:并且|并|且|而且|同时|另外|此外)?(?:要求|希望|需要|要|请安排|帮我安排)",
                location,
            ):
                location = ""
            if location:
                tasks.append(
                    PlanningTask(
                        description=piece,
                        location_name=location,
                        service_duration_minutes=service,
                        deadline=deadline,
                    )
                )
        if not tasks:
            tasks = [PlanningTask(description=text, location_name=text)]
        intent = PlanningIntent(
            origin=origin,
            departure_time=departure,
            transport_mode=mode,
            tasks=tasks[:MAX_PLANNING_TASKS],
            preferences=preferences,
        )
        if self._ordered_markers.search(text):
            intent.constraints.hard.required_task_order = list(range(len(intent.tasks)))
        return intent


class OpenAICompatibleIntentParser:
    name = "openai-compatible"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self.input_tokens = 0
        self.output_tokens = 0

    async def parse(self, text: str) -> PlanningIntent:
        schema = PlanningIntent.model_json_schema()

        def make_strict(node):
            if isinstance(node, dict):
                properties = node.get("properties")
                if isinstance(properties, dict):
                    node["additionalProperties"] = False
                    node["required"] = list(properties)
                for value in node.values():
                    make_strict(value)
            elif isinstance(node, list):
                for value in node:
                    make_strict(value)

        make_strict(schema)
        payload = {
            "model": self.settings.llm_model,
            "max_tokens": self.settings.max_llm_output_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是出行需求解析器。只提取用户明确表达的信息，不猜测具体 POI。"
                        "硬截止时间写入 task.deadline，偏好写入 preferences。"
                        "所有日期使用 Asia/Shanghai 时区。"
                    ),
                },
                {"role": "user", "content": text},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "planning_intent", "strict": True, "schema": schema},
            },
        }
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        try:
            response = await self.client.post(
                f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            response_data = response.json()
            content = response_data["choices"][0]["message"]["content"]
            usage = response_data.get("usage") or {}
            self.input_tokens = int(usage.get("prompt_tokens") or 0)
            self.output_tokens = int(usage.get("completion_tokens") or 0)
            return PlanningIntent.model_validate(json.loads(content))
        except (
            httpx.HTTPError,
            KeyError,
            json.JSONDecodeError,
            ValidationError,
            TimeoutError,
        ) as exc:
            raise UpstreamError("模型输出未通过结构校验", {"reason": str(exc)}) from exc


class FallbackIntentParser:
    """Try the primary LLM parser, then degrade to deterministic rules."""

    name = "llm-with-rule-fallback"

    def __init__(self, primary: IntentParser, fallback: IntentParser | None = None) -> None:
        self.primary = primary
        self.fallback = fallback or RuleBasedIntentParser()
        self.input_tokens = 0
        self.output_tokens = 0
        self.last_parser = primary.name
        self.fallback_used = False
        self.fallback_reason: str | None = None

    async def parse(self, text: str) -> PlanningIntent:
        try:
            intent = await self.primary.parse(text)
            self.input_tokens = int(getattr(self.primary, "input_tokens", 0) or 0)
            self.output_tokens = int(getattr(self.primary, "output_tokens", 0) or 0)
            self.last_parser = self.primary.name
            self.fallback_used = False
            self.fallback_reason = None
            # Lower confidence signal: leave preferences intact but mark uncertain if sparse.
            if not intent.tasks:
                raise UpstreamError("模型未提取到任务")
            return intent
        except Exception as exc:  # noqa: BLE001 - any LLM fault triggers deterministic degrade
            self.fallback_used = True
            self.fallback_reason = str(exc)
            self.last_parser = self.fallback.name
            self.input_tokens = int(getattr(self.primary, "input_tokens", 0) or 0)
            self.output_tokens = int(getattr(self.primary, "output_tokens", 0) or 0)
            intent = await self.fallback.parse(text)
            # Degraded parse: push uncertainty so planner prefers clarification.
            intent.constraints.uncertain.append(
                UncertainConstraint(
                    field="intent_parser",
                    reason=f"LLM 解析失败，已降级到规则解析器：{self.fallback_reason}"[:300],
                    confidence=0.45,
                    safety_buffer_minutes=10,
                )
            )
            return intent


def build_intent_parser(settings: Settings, client: httpx.AsyncClient) -> IntentParser:
    if settings.llm_api_key:
        return FallbackIntentParser(OpenAICompatibleIntentParser(settings, client))
    return RuleBasedIntentParser()
