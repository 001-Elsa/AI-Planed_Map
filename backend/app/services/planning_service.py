import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.app.clients.amap_client import MapProvider
from backend.app.core.exceptions import AppError
from backend.app.schemas.ai_intent import (
    AIPlanRequest,
    AIPlanResult,
    PlannedStop,
    PlanningIntent,
    PoiCandidate,
)
from backend.app.services.intent_parser import IntentParser
from backend.app.services.route_optimizer import optimize_route


SHANGHAI = ZoneInfo("Asia/Shanghai")


def request_fingerprint(owner: str, request: AIPlanRequest, model: str) -> str:
    normalized = json.dumps(request.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(f"{owner}|{normalized}|{model}".encode()).hexdigest()


class PlanningService:
    def __init__(self, parser: IntentParser, map_provider: MapProvider) -> None:
        self.parser = parser
        self.map_provider = map_provider

    async def plan(self, request: AIPlanRequest) -> AIPlanResult:
        intent = await self.parser.parse(request.text)
        if request.departure_time:
            intent.departure_time = request.departure_time
        if request.transport_mode:
            intent.transport_mode = request.transport_mode
        for task in intent.tasks:
            if task.service_duration_minutes == 0:
                task.service_duration_minutes = request.default_service_duration_minutes

        if request.origin is None:
            return AIPlanResult(
                status="need_clarification",
                intent=intent,
                questions=[
                    {
                        "field": "origin",
                        "message": "请提供当前位置坐标，以便搜索真实地点并计算路线。",
                    }
                ],
            )

        candidates: list[list[PoiCandidate]] = []
        questions: list[dict] = []
        for index, task in enumerate(intent.tasks):
            keyword = task.location_name or task.category or task.description
            found = await self.map_provider.search_poi(keyword, request.origin, request.city)
            if not found:
                questions.append(
                    {"field": f"tasks.{index}.location", "message": f"没有找到与“{keyword}”匹配的地点"}
                )
                candidates.append([])
                continue
            if len(found) > 1 and found[0].distance_meters is not None and found[1].distance_meters is not None:
                close = abs(found[1].distance_meters - found[0].distance_meters) < 500
                vague = len(keyword) <= 6 and not any(char.isdigit() for char in keyword)
                if close and vague:
                    questions.append(
                        {
                            "field": f"tasks.{index}.location",
                            "message": f"检测到多个“{keyword}”候选地点，请确认。",
                            "candidates": [item.model_dump(mode="json") for item in found[:3]],
                        }
                    )
            candidates.append(found)
        if questions:
            return AIPlanResult(status="need_clarification", intent=intent, questions=questions)

        chosen = [
            max(items, key=lambda item: (item.rating or 0, -(item.distance_meters or 0)))
            if intent.preferences.prefer_high_rating
            else items[0]
            for items in candidates
        ]
        points = [request.origin] + [item.location for item in chosen]
        distances, durations = await self.map_provider.route_matrix(points, intent.transport_mode)
        departure = intent.departure_time or datetime.now(SHANGHAI).replace(second=0, microsecond=0)
        if departure.tzinfo is None:
            departure = departure.replace(tzinfo=SHANGHAI)
        evaluation, algorithm = optimize_route(departure, intent.tasks, distances, durations)

        planned_stops: list[PlannedStop] = []
        previous = 0
        for position, task_index in enumerate(evaluation.order):
            matrix_index = task_index + 1
            task = intent.tasks[task_index]
            planned_stops.append(
                PlannedStop(
                    task_index=task_index,
                    task=task,
                    poi=chosen[task_index],
                    arrival_time=evaluation.arrivals[position],
                    departure_time=evaluation.departures[position],
                    travel_seconds=durations[previous][matrix_index],
                    travel_meters=distances[previous][matrix_index],
                    constraint_satisfied=not (
                        task.deadline and evaluation.arrivals[position] > task.deadline
                    ),
                )
            )
            previous = matrix_index

        if not evaluation.feasible:
            return AIPlanResult(
                status="infeasible",
                intent=intent,
                stops=planned_stops,
                total_distance_meters=evaluation.total_distance,
                total_travel_seconds=evaluation.total_travel_seconds,
                algorithm=algorithm,
                explanation="当前出发时间和交通方式无法满足全部硬约束，请调整截止时间或交通方式。",
                conflicts=evaluation.conflicts,
            )
        saved = round(evaluation.total_travel_seconds / 60)
        return AIPlanResult(
            status="success",
            intent=intent,
            stops=planned_stops,
            total_distance_meters=evaluation.total_distance,
            total_travel_seconds=evaluation.total_travel_seconds,
            algorithm=algorithm,
            explanation=f"已按真实候选地点与时间约束生成可行路线，预计行程约 {saved} 分钟。",
        )

