import pytest

from assistant.audit import (
    AuditFinding,
    AuditSpec,
    AuditTarget,
    Evidence,
    Facts,
    FindingStatus,
    ReadOnlyCollector,
    TargetKind,
    run_audit,
)
from assistant.project_analysis import ProjectAnalyzer


def test_audit_contract_supports_generic_target_and_normalizes_terms():
    spec = AuditSpec(
        target=AuditTarget(kind=TargetKind.DOCUMENTATION, identifier="docs"),
        scope=[" api ", "api", ""],
        include=["README.md"],
        accepted_constraints=["read only"],
    )

    assert spec.scope == ["api"]
    assert spec.read_only is True
    assert spec.target.kind is TargetKind.DOCUMENTATION


def test_collection_is_read_only_deterministic_and_isolates_failures():
    calls: list[str] = []

    def reader(spec: AuditSpec) -> Facts:
        calls.append(spec.target.identifier)
        return Facts(values={"present": True}, evidence=[
            Evidence(source="z-reader", value="yes")
        ])

    def broken(spec: AuditSpec) -> None:
        raise RuntimeError("no access")

    spec = AuditSpec(target=AuditTarget(identifier="workspace"))
    report = run_audit(spec, [
        ReadOnlyCollector("z-reader", reader),
        ReadOnlyCollector("a-reader", broken),
    ])

    assert calls == ["workspace"]
    assert report.facts.values == {"present": True}
    assert [item.source for item in report.facts.evidence] == ["a-reader", "z-reader"]
    assert report.spec.read_only is True


def test_evaluator_orders_findings_and_calculates_optional_score():
    spec = AuditSpec(target=AuditTarget(identifier="project"), scoring=True)
    report = run_audit(spec, findings=[
        AuditFinding(title="Z", category="quality", status=FindingStatus.PASS, score=1),
        AuditFinding(title="A", category="security", status=FindingStatus.FAIL, score=0),
    ])

    assert [finding.title for finding in report.findings] == ["A", "Z"]
    assert report.score == 0.5
    assert report.passed is False


@pytest.mark.asyncio
async def test_project_audit_publishes_general_report_without_reading_sensitive_files(tmp_path):
    (tmp_path / "README.md").write_text("# Example\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    (tmp_path / ".env").write_text("PRIVATE_TOKEN=must-not-be-read\n", encoding="utf-8")

    result = await ProjectAnalyzer().audit(str(tmp_path), max_files=50)

    assert result.success is True
    report = result.output["audit_report"]
    assert report["spec"]["target"]["kind"] == "project"
    assert report["spec"]["scoring"] is True
    assert report["facts"]["values"]["documentation_files"] == 1
    assert "must-not-be-read" not in str(report)
    assert any(item["id"] == "security.sensitive-files" for item in report["findings"])
