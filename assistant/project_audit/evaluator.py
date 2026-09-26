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
    failures = sum(
        finding.status is FindingStatus.FAIL and finding.in_scope
        for finding in ordered
    )
    deferred = [
        finding.title
        for finding in ordered
        if not finding.in_scope
        or finding.status in {
            FindingStatus.UNKNOWN,
            FindingStatus.NOT_RUN,
            FindingStatus.NOT_APPLICABLE,
        }
    ]
    breakdown = {
        status.value: sum(finding.status is status for finding in ordered)
        for status in FindingStatus
    }
    summary = (
        f"{len(ordered)} finding(s); {failures} in-scope failure(s); "
        f"{len(deferred)} deferred or not-applicable check(s)"
    )
    return AuditReport(
        spec=spec,
        findings=ordered,
        facts=facts,
        summary=summary,
        breakdown=breakdown,
        accepted_constraints=spec.accepted_constraints,
        deferred=deferred,
    )