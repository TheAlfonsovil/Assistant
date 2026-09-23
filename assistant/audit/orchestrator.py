from __future__ import annotations

from collections.abc import Iterable

from .collector import AuditCollector, collect
from .evaluator import evaluate
from .models import AuditFinding, AuditReport, AuditSpec


def run_audit(
    spec: AuditSpec,
    collectors: Iterable[AuditCollector] = (),
    findings: Iterable[AuditFinding] = (),
) -> AuditReport:
    """Collect observations and evaluate findings using stable ordering."""

    return evaluate(spec, collect(spec, collectors), findings)
