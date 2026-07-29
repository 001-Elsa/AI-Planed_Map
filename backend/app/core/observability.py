from collections import defaultdict
from threading import Lock


class MetricsRegistry:
    """Small dependency-free Prometheus exporter for the single-process service."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._observations: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[int, float]] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, float]] = {}
        self._histogram_buckets = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)

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
            buckets = self._histograms.setdefault(
                key,
                {f"{bucket}": 0.0 for bucket in self._histogram_buckets}
                | {"+Inf": 0.0, "sum": 0.0},
            )
            buckets["sum"] += value
            for bucket in self._histogram_buckets:
                if value <= bucket:
                    buckets[f"{bucket}"] += 1
            buckets["+Inf"] += 1

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
            histograms = list(self._histograms.items())
        for (name, labels), value in sorted(counters):
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name}{self._labels(labels)} {value:g}")
        for (name, labels), (count, total) in sorted(observations):
            lines.append(f"# TYPE {name} summary")
            lines.append(f"{name}_count{self._labels(labels)} {count}")
            lines.append(f"{name}_sum{self._labels(labels)} {total:g}")
        for (name, labels), buckets in sorted(histograms):
            hist_name = f"{name}_histogram"
            lines.append(f"# TYPE {hist_name} histogram")
            cumulative = 0.0
            for bucket in self._histogram_buckets:
                cumulative += buckets[f"{bucket}"]
                label_set = labels + (("le", str(bucket)),)
                lines.append(f"{hist_name}_bucket{self._labels(label_set)} {cumulative:g}")
            label_inf = labels + (("le", "+Inf"),)
            lines.append(f"{hist_name}_bucket{self._labels(label_inf)} {buckets['+Inf']:g}")
            lines.append(f"{hist_name}_sum{self._labels(labels)} {buckets['sum']:g}")
            lines.append(f"{hist_name}_count{self._labels(labels)} {buckets['+Inf']:g}")
        return "\n".join(lines) + "\n"


metrics = MetricsRegistry()
