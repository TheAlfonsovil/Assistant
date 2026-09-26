import pytest
from pathlib import Path

from assistant.project_audit import (
    AuditFinding,
    AuditSpec,
    AuditTarget,
    Evidence,
    Facts,
    FindingStatus,
    TargetKind,
)
from assistant.project_audit.evaluator import evaluate
from assistant.project_audit.profiles import get_profile
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


def test_project_analyzer_builds_test_candidates_for_nested_manifests(tmp_path: Path):
    frontend = tmp_path / "frontend"
    backend = tmp_path / "backend"
    (frontend / "src").mkdir(parents=True)
    (backend / "src" / "test" / "java").mkdir(parents=True)
    package_content = '{"scripts":{"test":"vitest","lint":"eslint ."}}'
    (frontend / "package.json").write_text(package_content, encoding="utf-8")
    (frontend / "src" / "App.test.ts").write_text("test('ok', () => {})", encoding="utf-8")
    (backend / "pom.xml").write_text("<project />", encoding="utf-8")
    (backend / "src" / "test" / "java" / "AppTest.java").write_text(
        "class AppTest {}", encoding="utf-8"
    )
    files = ProjectAnalyzer._collect_files(tmp_path, 50)
    tests = ProjectAnalyzer._discover_test_files(tmp_path, files, {})
    configuration = {"frontend/package.json": package_content}

    candidates = ProjectAnalyzer._test_candidates(tmp_path, configuration, files, tests)
    quality_tools = ProjectAnalyzer._quality_tools(tmp_path, configuration, files)

    assert {item["command"] for item in candidates} == {
        'npm --prefix "frontend" test',
        'mvn -q -f "backend/pom.xml" test',
    }
    assert any(
        item["command"] == 'npm --prefix "frontend" run lint'
        for item in quality_tools
    )


@pytest.mark.asyncio
async def test_project_audit_runs_every_detected_hybrid_test_command(
    tmp_path: Path, monkeypatch
):
    frontend = tmp_path / "frontend"
    backend_test = tmp_path / "backend" / "src" / "test" / "java"
    (frontend / "src").mkdir(parents=True)
    backend_test.mkdir(parents=True)
    (frontend / "package.json").write_text(
        '{"scripts":{"test":"vitest"}}', encoding="utf-8"
    )
    (frontend / "src" / "App.test.ts").write_text("test('ok', () => {})", encoding="utf-8")
    (tmp_path / "backend" / "pom.xml").write_text("<project />", encoding="utf-8")
    (backend_test / "AppTest.java").write_text("class AppTest {}", encoding="utf-8")
    executed = []

    async def fake_run(command: str, cwd: Path, timeout: float) -> dict[str, object]:
        executed.append(command)
        return {"available": True, "command": command, "executed": True, "exit_code": 0}

    monkeypatch.setattr(ProjectAnalyzer, "_run_test_command", staticmethod(fake_run))

    result = await ProjectAnalyzer().audit(str(tmp_path), max_files=50, run_tests=True)

    test_result = result.output["audit"]["test_result"]
    assert executed == [
        'npm --prefix "frontend" test',
        'mvn -q -f "backend/pom.xml" test',
    ]
    assert test_result["completed"] is True
    assert len(test_result["results"]) == 2
    testing_finding = next(
        item for item in result.output["audit_report"]["findings"]
        if item["id"] == "testing.execution"
    )
    assert testing_finding["status"] == "pass"


def test_project_analyzer_does_not_offer_ruff_without_python(tmp_path: Path):
    (tmp_path / "Main.java").write_text("class Main {}", encoding="utf-8")
    files = ProjectAnalyzer._collect_files(tmp_path, 10)

    assert ProjectAnalyzer._quality_tools(tmp_path, {}, files) == []


def test_evaluator_orders_findings_without_global_score():
    spec = AuditSpec(target=AuditTarget(identifier="project"), scoring=True)
    report = evaluate(spec, Facts(), findings=[
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
    report = evaluate(spec, Facts(), findings=[
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
