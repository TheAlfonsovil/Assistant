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
        scored = [finding.score for finding in ordered if finding.score is not None]
        score = sum(scored) / len(scored) if scored else None
    failures = sum(finding.status is FindingStatus.FAIL for finding in ordered)
    summary = f"{len(ordered)} finding(s); {failures} failure(s)"
    return AuditReport(spec=spec, findings=ordered, facts=facts, score=score, summary=summary)
