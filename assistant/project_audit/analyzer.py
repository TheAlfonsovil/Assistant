from __future__ import annotations

import re
from pathlib import Path

from . import AuditFinding, AuditSpec, AuditTarget, Evidence, Facts, FindingStatus, TargetKind, evaluate, get_profile
from .discovery import ProjectAuditDiscovery
from ..domain.models import ErrorType, OperationResult

class ProjectAuditMixin:
    _discover_test_files = staticmethod(ProjectAuditDiscovery._discover_test_files)
    _test_candidates = staticmethod(ProjectAuditDiscovery._test_candidates)
    _quality_tools = staticmethod(ProjectAuditDiscovery._quality_tools)
    _classify_project = staticmethod(ProjectAuditDiscovery._classify_project)
    _key_files = staticmethod(ProjectAuditDiscovery._key_files)
    _read_key_file_sections = staticmethod(ProjectAuditDiscovery._read_key_file_sections)
    _run_ruff = staticmethod(ProjectAuditDiscovery._run_ruff)
    _run_test_command = staticmethod(ProjectAuditDiscovery._run_test_command)

    async def audit(
        self,
        root: str,
        max_files: int = 500,
        timeout: float = 60.0,
        run_tests: bool = True,
        profile: str = "general",
        scope: list[str] | None = None,
        depth: str = "standard",
        accepted_constraints: list[str] | None = None,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        scoring: bool = True,
        objective: str = "Assess the target and report actionable findings.",
    ) -> OperationResult:
        """Collect evidence and run the project's detected validation commands."""
        structural = await self.analyze(root, max_files)
        if not structural.success:
            return structural
        project_root = Path(root).resolve()
        files = [Path(project_root / relative) for relative in structural.output["files_analyzed"]]
        manifests = {
            "pyproject.toml",
            "requirements.txt",
            "requirements-dev.txt",
            "package.json",
            "package-lock.json",
            "poetry.lock",
            "Pipfile",
            "Pipfile.lock",
            ".gitignore",
            ".env.example",
        }
        configuration: dict[str, str] = {}
        redaction = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|private[_-]?key)")
        for path in files:
            if path.name not in manifests or path.stat().st_size > 128 * 1024:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            lines = []
            for line in text.splitlines():
                key = line.split("=", 1)[0].strip() if "=" in line else ""
                lines.append(f"{key}=<redacted>" if key and redaction.search(key) else line)
            configuration[path.relative_to(project_root).as_posix()] = "\n".join(lines)

        project_kind = structural.output["project_kind"]
        test_files = self._discover_test_files(project_root, files, configuration)
        sensitive_files = [
            path.relative_to(project_root).as_posix()
            for path in project_root.rglob("*")
            if path.is_file() and path.name in {".env", ".env.local", ".env.production"}
        ]
        documentation_files = [
            path.relative_to(project_root).as_posix()
            for path in files
            if path.suffix.lower() in {".md", ".rst", ".adoc", ".txt"}
            or path.name.casefold() in {"readme", "readme.md", "changelog"}
        ]
        deployment_files = [
            path.relative_to(project_root).as_posix()
            for path in files
            if path.name.casefold() in {
                "dockerfile", "docker-compose.yml", "docker-compose.yaml",
                "compose.yml", "compose.yaml", "makefile",
            }
            or path.suffix.lower() in {".tf", ".bicep"}
        ]
        gitignore = configuration.get(".gitignore", "")
        gitignore_rules = [
            line.strip()
            for line in gitignore.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        test_candidates = self._test_candidates(project_root, configuration, files, test_files)
        test_command = test_candidates[0]["command"] if test_candidates else None
        if run_tests and test_candidates:
            test_runs = []
            for candidate in test_candidates:
                result = await self._run_test_command(
                    candidate["command"], project_root, timeout
                )
                test_runs.append({**candidate, **result})
            all_completed = all(item.get("executed") for item in test_runs)
            any_executed = any(item.get("executed") for item in test_runs)
            any_failed = any(
                item.get("executed")
                and (item.get("exit_code") != 0 or item.get("timed_out"))
                for item in test_runs
            )
            test_result = {
                "available": True,
                "command": test_command,
                "executed": any_executed,
                "completed": all_completed,
                "exit_code": (
                    1 if any_failed else 0 if all_completed else None
                ),
                "results": test_runs,
            }
        elif run_tests:
            test_result = {
                "available": False,
                "executed": False,
                "completed": False,
                "reason": "No supported test command or test files detected",
            }
        else:
            test_result = {
                "available": bool(test_command),
                "command": test_command,
                "executed": False,
                "completed": False,
                "reason": "Test execution was disabled for this audit.",
            }
        quality_tools = self._quality_tools(project_root, configuration, files)
        ruff_result = (
            await self._run_ruff(project_root, timeout)
            if any(item["tool"] == "ruff" for item in quality_tools)
            else {"available": False, "executed": False, "reason": "Not applicable to this project"}
        )
        key_file_sections = self._read_key_file_sections(project_root, files)
        structural.output.update({
            "audit": {
                "project_kind": project_kind,
                "test_candidates": test_candidates,
                "configuration": configuration,
                "dependency_manifests": [name for name in configuration if Path(name).name not in {".gitignore", ".env.example"}],
                "test_files": test_files[:500],
                "test_command": test_command,
                "test_commands": [item["command"] for item in test_candidates],
                "run_tests": run_tests,
                "test_result": test_result,
                "quality_tools": quality_tools,
                "ruff_result": ruff_result,
                "key_file_sections": key_file_sections,
                "sensitive_files": sensitive_files,
                "sensitive_file_contents_read": False,
                "limitations": [
                    "Secret-bearing environment files are detected but never read.",
                ],
            }
        })
        audit_profile = get_profile(profile)
        test_runs = test_result.get("results", [])
        test_failed = any(
            item.get("executed")
            and (item.get("exit_code") != 0 or item.get("timed_out"))
            for item in test_runs
        )
        tests_completed = test_result.get(
            "completed", test_result.get("executed", False)
        )
        effective_scope = list(dict.fromkeys(
            scope if scope else audit_profile.default_scope
        ))
        effective_depth = depth or audit_profile.default_depth.value
        audit_spec = AuditSpec(
            target=AuditTarget(
                kind=TargetKind.PROJECT,
                identifier=str(project_root),
                name=project_root.name,
            ),
            objective=objective,
            profile=profile,
            scope=effective_scope,
            depth=effective_depth,
            accepted_constraints=accepted_constraints or [],
            include=include or [],
            exclude=exclude or [],
            scoring=scoring,
        )
        evidence = [
            Evidence(
                source="project.structure",
                value={
                    "file_count": structural.output["file_count"],
                    "languages": structural.output["languages"],
                    "project_kind": project_kind,
                    "truncated": structural.output["truncated"],
                },
                description="Bounded filesystem inventory was collected.",
            ),
            Evidence(
                source="project.configuration",
                value={
                    "manifests": structural.output["audit"]["dependency_manifests"],
                    "sensitive_files": sensitive_files,
                    "contents_read": False,
                },
                description="Configuration manifests were read with sensitive values redacted; secret-bearing files were not read.",
            ),
            Evidence(
                source="project.tests",
                value={
                    "files": len(test_files),
                    "command": test_command,
                    "candidates": test_candidates,
                    "executed": test_result.get("executed", False),
                    "result": test_result,
                },
                description="Test discovery and optional execution result.",
            ),
            Evidence(
                source="project.workspace",
                value={
                    "documentation_files": documentation_files[:500],
                    "deployment_files": deployment_files[:200],
                    "key_files": structural.output["key_files"],
                    "key_file_sections": key_file_sections,
                    "project_kind": project_kind,
                    "gitignore_present": ".gitignore" in configuration,
                    "gitignore_rules": len(gitignore_rules),
                },
                description="General workspace artifacts were inventoried without executing them.",
            ),
        ]
        findings: list[AuditFinding] = []
        findings.append(AuditFinding(
            id="scope.filesystem-truncated",
            title="The filesystem inventory is bounded",
            category="coverage",
            status=FindingStatus.WARN if structural.output["truncated"] else FindingStatus.PASS,
            severity="medium" if structural.output["truncated"] else "info",
            message=(
                "The inventory was truncated at the configured file limit."
                if structural.output["truncated"]
                else "The inventory completed within the configured file limit."
            ),
            evidence=[evidence[0]],
            recommendations=["Increase max_files or narrow the include scope for a deeper audit."]
            if structural.output["truncated"] else [],
            score=0.6 if structural.output["truncated"] else 1.0,
            effort="low",
        ))
        findings.append(AuditFinding(
            id="security.sensitive-files",
            title="Sensitive configuration files are present",
            category="security",
            status=FindingStatus.WARN if sensitive_files else FindingStatus.PASS,
            severity="medium" if sensitive_files else "info",
            message=(
                f"Detected {len(sensitive_files)} sensitive file(s); their contents were not read."
                if sensitive_files else "No supported sensitive environment files were detected."
            ),
            evidence=[evidence[1]],
            inferences=["The contents and actual secrecy of these files are unknown."]
            if sensitive_files else [],
            recommendations=["Verify that sensitive files are excluded from version control."]
            if sensitive_files else [],
            score=0.7 if sensitive_files else 1.0,
            effort="low",
            confidence=0.98 if sensitive_files else 0.9,
        ))
        findings.append(AuditFinding(
            id="testing.execution",
            title="Test coverage evidence is limited",
            category="testing",
            status=(
                FindingStatus.PASS
                if tests_completed and test_result.get("exit_code") == 0
                else FindingStatus.FAIL
                if test_failed
                else FindingStatus.NOT_APPLICABLE
                if not test_files and not test_command
                else FindingStatus.NOT_RUN
            ),
            severity="info",
            message=(
                "All detected test commands completed successfully."
                if tests_completed and test_result.get("exit_code") == 0
                else "One or more detected test commands failed."
                if test_failed
                else "No test suite was detected for this project."
                if not test_files and not test_command
                else "One or more detected test commands were not executed."
            ),
            evidence=[evidence[2]],
            recommendations=["Add or document a project-specific test command."]
            if not test_files and not test_command else
            ["Run every detected test command when test validation is in scope."]
            if test_files and not tests_completed else [],
            effort="low",
        ))
        findings.append(AuditFinding(
            id="quality.ruff",
            title="Ruff static checks",
            category="quality",
            status=(
                FindingStatus.PASS
                if ruff_result.get("executed") and ruff_result.get("exit_code") == 0
                else FindingStatus.FAIL
                if ruff_result.get("executed")
                else FindingStatus.NOT_RUN
                if any(item["tool"] == "ruff" for item in quality_tools)
                else FindingStatus.NOT_APPLICABLE
            ),
            severity="medium" if ruff_result.get("executed") and ruff_result.get("exit_code") else "info",
            message=(
                "Ruff completed successfully."
                if ruff_result.get("executed") and ruff_result.get("exit_code") == 0
                else "Ruff reported issues."
                if ruff_result.get("executed")
                else "Ruff is not applicable because no Python sources were detected."
                if not any(item["tool"] == "ruff" for item in quality_tools)
                else "Ruff is not installed or was not executed."
            ),
            evidence=[Evidence(
                source="project.quality",
                value=ruff_result,
                description="Ruff availability and execution result.",
            )],
            recommendations=["Install the development dependencies and run ruff check ."]
            if not ruff_result.get("executed") else [],
            effort="low",
        ))
        findings.append(AuditFinding(
            id="documentation.available",
            title="Documentation artifacts are available",
            category="documentation",
            status=FindingStatus.PASS if documentation_files else FindingStatus.NOT_APPLICABLE,
            severity="info",
            message=(
                f"Detected {len(documentation_files)} documentation artifact(s)."
                if documentation_files
                else "No common documentation artifact was detected in the bounded inventory."
            ),
            evidence=[evidence[3]],
            recommendations=["Add a README or project guide describing purpose, setup, and validation."]
            if not documentation_files else [],
            score=1.0 if documentation_files else None,
            effort="low",
        ))
        findings.append(AuditFinding(
            id="operations.deployment-artifacts",
            title="Deployment artifacts are inventoried",
            category="operations",
            status=FindingStatus.PASS if deployment_files else FindingStatus.NOT_APPLICABLE,
            severity="info",
            message=(
                f"Detected {len(deployment_files)} deployment or automation artifact(s)."
                if deployment_files
                else "No common deployment or automation artifact was detected."
            ),
            evidence=[evidence[3]],
            recommendations=["Document the intended local or production execution path."]
            if not deployment_files else [],
            score=1.0 if deployment_files else None,
            effort="medium",
        ))
        findings.append(AuditFinding(
            id="repository.ignore-policy",
            title="Repository ignore policy is visible",
            category="configuration",
            status=FindingStatus.PASS if gitignore_rules else FindingStatus.NOT_APPLICABLE,
            severity="info",
            message=(
                f"Detected {len(gitignore_rules)} non-comment ignore rule(s)."
                if gitignore_rules
                else "No usable .gitignore rules were observed in the bounded inventory."
            ),
            evidence=[evidence[3]],
            inferences=[
                "This audit does not claim that every sensitive file is actually ignored; exact Git matching requires a repository check."
            ],
            recommendations=["Verify sensitive files and local databases are explicitly ignored."]
            if sensitive_files and not gitignore_rules else [],
            score=1.0 if gitignore_rules else None,
            effort="low",
        ))
        requested_scope = {item.casefold() for item in (scope or [])}
        if not requested_scope:
            requested_scope = {item.casefold() for item in effective_scope}
        aliases = {
            "testing": {"testing", "tests", "quality"},
            "security": {"security", "privacy", "configuration"},
            "documentation": {"documentation", "docs", "completeness"},
            "operations": {"operations", "deployment", "deploy"},
            "configuration": {"configuration", "security", "quality"},
            "coverage": {"coverage", "structure", "architecture"},
        }
        for finding in findings:
            category_scope = aliases.get(finding.category, {finding.category})
            finding.in_scope = not requested_scope or bool(category_scope & requested_scope)
            if finding.status is FindingStatus.NOT_APPLICABLE or not finding.in_scope:
                finding.score = None
        report = evaluate(
            audit_spec,
            Facts(
                values={
                    "root": str(project_root),
                    "file_count": structural.output["file_count"],
                    "languages": structural.output["languages"],
                    "test_files": len(test_files),
                    "sensitive_files": sensitive_files,
                    "documentation_files": len(documentation_files),
                    "deployment_files": len(deployment_files),
                    "gitignore_rules": len(gitignore_rules),
                },
                evidence=evidence,
                source="audit.run",
            ),
            findings=findings,
        )
        structural.output["audit_report"] = report.model_dump(mode="json")
        return structural
