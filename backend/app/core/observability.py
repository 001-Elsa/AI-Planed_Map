from collections import defaultdict
from threading import Lock


class MetricsRegistry:
    """Small dependency-free Prometheus exporter for the single-process service."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._observations: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[int, float]] = {}

    @staticmethod
    def _key(name: str, labels: dict[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted(labels.items()))

    def increment(self, name: str, labels: dict[str, str] | None = None, value: float = 1) -> None:
        with self._lock:
            self._counters[self._key(name, labels or {})] += value

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        key = self._key(name, labels or {})
        with self._lock:
            count, total = self._observations.get(key, (0, 0.0))
            self._observations[key] = (count + 1, total + value)

    @staticmethod
    def _labels(labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""
        escaped = [
            f'{key}="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
            for key, value in labels
        ]
        return "{" + ",".join(escaped) + "}"

    def render(self) -> str:
        lines = [
            "# HELP mapgo_build_info Static service build information.",
            "# TYPE mapgo_build_info gauge",
            'mapgo_build_info{service="mapgo"} 1',
        ]
        with self._lock:
            counters = list(self._counters.items())
            observations = list(self._observations.items())
        for (name, labels), value in sorted(counters):
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name}{self._labels(labels)} {value:g}")
        for (name, labels), (count, total) in sorted(observations):
            lines.append(f"# TYPE {name} summary")
            lines.append(f"{name}_count{self._labels(labels)} {count}")
            lines.append(f"{name}_sum{self._labels(labels)} {total:g}")
        return "\n".join(lines) + "\n"


metrics = MetricsRegistry()
