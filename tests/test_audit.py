import pytest
from pathlib import Path

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
from assistant.audit.profiles import get_profile
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


def test_general_profile_defines_broad_default_scope():
    profile = get_profile("general")

    assert {"structure", "architecture", "security", "testing", "documentation"} <= set(
        profile.default_scope
    )


def test_project_analyzer_detects_hybrid_and_non_python_tests(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest"}}', encoding="utf-8")
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    (tmp_path / "src" / "test").mkdir(parents=True)
    (tmp_path / "src" / "test" / "AppTest.java").write_text("class AppTest {}", encoding="utf-8")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "App.test.ts").write_text("test('ok', () => {})", encoding="utf-8")
    files = ProjectAnalyzer._collect_files(tmp_path, 50)

    kinds = ProjectAnalyzer._classify_project(tmp_path, files)
    tests = ProjectAnalyzer._discover_test_files(tmp_path, files, {"package.json": '{"scripts":{"test":"vitest"}}'})
    candidates = ProjectAnalyzer._test_candidates(tmp_path, {"package.json": '{"scripts":{"test":"vitest"}}'}, files, tests)

    assert {"web", "jvm"} <= set(kinds)
    assert "src/test/AppTest.java" in tests
    assert "web/App.test.ts" in tests
    assert {item["tool"] for item in candidates} == {"npm", "maven"}


def test_project_analyzer_does_not_offer_ruff_without_python(tmp_path: Path):
    (tmp_path / "Main.java").write_text("class Main {}", encoding="utf-8")
    files = ProjectAnalyzer._collect_files(tmp_path, 10)

    assert ProjectAnalyzer._quality_tools(tmp_path, {}, files) == []


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


def test_evaluator_orders_findings_without_global_score():
    spec = AuditSpec(target=AuditTarget(identifier="project"), scoring=True)
    report = run_audit(spec, findings=[
        AuditFinding(title="Z", category="quality", status=FindingStatus.PASS, score=1),
        AuditFinding(title="A", category="security", status=FindingStatus.FAIL, score=0),
    ])

    assert [finding.title for finding in report.findings] == ["A", "Z"]
    assert "score" not in report.model_dump()
    assert report.passed is False


def test_evaluator_does_not_score_deferred_or_out_of_scope_checks():
    spec = AuditSpec(
        target=AuditTarget(identifier="workspace"),
        scope=["architecture"],
        accepted_constraints=["local prototype"],
        scoring=True,
    )
    report = run_audit(spec, findings=[
        AuditFinding(
            title="Architecture is coherent",
            category="architecture",
            status=FindingStatus.PASS,
            score=1,
        ),
        AuditFinding(
            title="Tests were not run",
            category="testing",
            status=FindingStatus.NOT_APPLICABLE,
            score=None,
        ),
        AuditFinding(
            title="Production deployment is absent",
            category="operations",
            status=FindingStatus.WARN,
            score=0,
            in_scope=False,
        ),
    ])

    assert "score" not in report.model_dump()
    assert report.passed is True
    assert "Production deployment is absent" in report.deferred
    assert report.accepted_constraints == ["local prototype"]


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
