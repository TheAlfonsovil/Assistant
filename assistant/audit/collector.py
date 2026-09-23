from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

from .models import AuditSpec, Evidence, Facts


class AuditCollector(Protocol):
    name: str

    def collect(self, spec: AuditSpec) -> Facts | Iterable[Evidence] | None: ...


class CollectorContext:
    """Explicit registry used to make collection deterministic and testable."""

    def __init__(self, collectors: Iterable[AuditCollector] = ()) -> None:
        self.collectors = tuple(sorted(collectors, key=lambda item: item.name))

    def collect(self, spec: AuditSpec) -> Facts:
        return collect(spec, self.collectors)


class ReadOnlyCollector:
    """Adapter for a read-only callable; it cannot mutate the target by contract."""

    def __init__(
        self,
        name: str,
        reader: Callable[[AuditSpec], Facts | Iterable[Evidence] | None],
    ) -> None:
        self.name = name
        self._reader = reader

    def collect(self, spec: AuditSpec) -> Facts | Iterable[Evidence] | None:
        return self._reader(spec)


def collect(spec: AuditSpec, collectors: Iterable[AuditCollector] = ()) -> Facts:
    """Run collectors in stable order and merge only observations."""

    evidence: list[Evidence] = []
    values: dict[str, object] = {}
    sources: list[str] = []
    for collector in sorted(collectors, key=lambda item: item.name):
        try:
            result = collector.collect(spec)
        except Exception as exc:  # noqa: BLE001 - isolate untrusted collector failures
            evidence.append(
                Evidence(kind="inference", source=collector.name, value=None,
                         description=f"collector failed: {exc.__class__.__name__}", confidence=0.0)
            )
            continue
        if result is None:
            continue
        if isinstance(result, Facts):
            values.update(result.values)
            evidence.extend(result.evidence)
            sources.append(result.source)
        else:
            evidence.extend(result)
            sources.append(collector.name)
    evidence.sort(key=lambda item: (item.source, item.id))
    return Facts(values=values, evidence=evidence, source=",".join(sorted(set(sources))) or "collector")
