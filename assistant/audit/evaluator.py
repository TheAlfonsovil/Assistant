from __future__ import annotations

from collections.abc import Iterable

from .models import AuditFinding, AuditReport, AuditSpec, Facts, FindingStatus


def evaluate(
    spec: AuditSpec,
    facts: Facts,
    findings: Iterable[AuditFinding] = (),
) -> AuditReport:
    """Evaluate supplied observations without performing I/O or making guesses."""

    ordered = sorted(findings, key=lambda finding: (finding.title, finding.category, finding.id))
    score = None
    if spec.scoring:
        scored = [
            finding.score
            for finding in ordered
            if finding.in_scope
            and finding.score is not None
            and finding.status in {
                FindingStatus.PASS,
                FindingStatus.FAIL,
                FindingStatus.WARN,
            }
        ]
        score = sum(scored) / len(scored) if scored else None
    failures = sum(
        finding.status is FindingStatus.FAIL and finding.in_scope
        for finding in ordered
    )
    deferred = [
        finding.title
        for finding in ordered
        if not finding.in_scope
        or finding.status in {FindingStatus.UNKNOWN, FindingStatus.NOT_APPLICABLE}
    ]
    summary = (
        f"{len(ordered)} finding(s); {failures} in-scope failure(s); "
        f"{len(deferred)} deferred or not-applicable check(s)"
    )
    return AuditReport(
        spec=spec,
        findings=ordered,
        facts=facts,
        score=score,
        summary=summary,
        accepted_constraints=spec.accepted_constraints,
        deferred=deferred,
    )
