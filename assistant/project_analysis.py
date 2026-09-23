from __future__ import annotations

import ast
import asyncio
import json
import re
import shutil
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .audit import (
    AuditFinding,
    AuditSpec,
    AuditTarget,
    Evidence,
    Facts,
    FindingStatus,
    TargetKind,
    evaluate,
    get_profile,
)
from .domain.models import ErrorType, OperationResult


class ProjectAnalyzer:
    """Builds a bounded structural inventory and dependency graph for a local project."""

    async def analyze(self, root: str, max_files: int = 500) -> OperationResult:
        started_at = datetime.now(UTC)
        started_files = Path(root).resolve()
        try:
            files = await asyncio.to_thread(self._collect_files, started_files, max_files)
            truncated = len(files) > max_files
            files = files[:max_files]
            languages = Counter(path.suffix.lower() or "[no extension]" for path in files)
            symbols: list[dict[str, Any]] = []
            edges: list[dict[str, str]] = []
            modules: list[dict[str, str]] = []
            for path in files:
                if path.suffix.lower() in {
                    ".py", ".java", ".kt", ".kts", ".js", ".jsx", ".ts", ".tsx",
                    ".cs", ".go", ".rs", ".swift", ".rb", ".php",
                }:
                    modules.append(
                        {
                            "module": str(path.relative_to(started_files)).replace("\\", "/"),
                            "file": str(path.relative_to(started_files)).replace("\\", "/"),
                        }
                    )
                if path.suffix.lower() == ".py":
                    self._analyze_python(path, started_files, symbols, edges)
                elif path.suffix.lower() in {
                    ".java", ".kt", ".kts", ".js", ".jsx", ".ts", ".tsx",
                    ".cs", ".go", ".rs", ".swift", ".rb", ".php",
                }:
                    self._analyze_source_symbols(path, started_files, symbols, edges)
            key_files = self._key_files(started_files, files)
            output = {
                "root": str(started_files),
                "files_analyzed": [str(path.relative_to(started_files)) for path in files],
                "key_files": [str(path.relative_to(started_files)) for path in key_files],
                "file_count": len(files),
                "languages": dict(languages),
                "project_kind": self._classify_project(started_files, files),
                "modules": modules[:2000],
                "symbols": symbols[:2000],
                "dependency_edges": edges[:4000],
                "truncated": truncated,
            }
            finished_at = datetime.now(UTC)
            return OperationResult(
                success=True,
                output=output,
                started_at=started_at,
                finished_at=finished_at,
                duration=(finished_at - started_at).total_seconds(),
            )
        except (OSError, ValueError) as error:
            return OperationResult(
                success=False,
                error=str(error),
                error_type=ErrorType.NOT_FOUND if isinstance(error, FileNotFoundError) else ErrorType.INVALID_ARGUMENT,
                retryable=False,
                started_at=started_at,
            )

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
            for path in files
            if path.name in {".env", ".env.local", ".env.production"}
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
        if run_tests and test_command:
            test_result = await self._run_test_command(test_command, project_root, timeout)
        elif run_tests:
            test_result = {
                "available": False,
                "executed": False,
                "reason": "No supported test command or test files detected",
            }
        else:
            test_result = {
                "available": bool(test_command),
                "command": test_command,
                "executed": False,
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
                if test_result.get("executed") and test_result.get("exit_code") == 0
                else FindingStatus.FAIL
                if test_result.get("executed")
                else                 FindingStatus.NOT_APPLICABLE
                if not test_files and not test_command
                else FindingStatus.NOT_RUN
            ),
            severity="info",
            message=(
                "Tests were executed and completed successfully."
                if test_result.get("executed") and test_result.get("exit_code") == 0
                else "No test suite was detected for this project."
                if not test_files and not test_command
                else                 "Tests were not executed."
            ),
            evidence=[evidence[2]],
            recommendations=["Add or document a project-specific test command."]
            if not test_files and not test_command else
            ["Run the detected test command when test validation is in scope."]
            if test_files and not test_result.get("executed") else [],
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
                source="project.audit",
            ),
            findings=findings,
        )
        structural.output["audit_report"] = report.model_dump(mode="json")
        return structural

    @staticmethod
    def _discover_test_files(root: Path, files: list[Path], configuration: dict[str, str]) -> list[str]:
        """Discover tests across common application, mobile and library stacks."""
        candidates: list[Path] = []
        for path in files:
            relative = path.relative_to(root)
            if path.suffix.lower() not in {
                ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".kts",
                ".cs", ".go", ".rs", ".rb", ".php", ".swift",
            }:
                continue
            parts = {part.casefold() for part in relative.parts[:-1]}
            name = path.name.casefold()
            if (
                bool(parts & {"tests", "test", "__tests__", "spec", "androidtest", "instrumentedtests"})
                and path.name != "__init__.py"
                or name.startswith("test_")
                or name.endswith((
                    "_test.py", "_test.go", "_test.rs", "_test.rb",
                    "test.java", "tests.java", "test.kt", "tests.kt",
                    "test.cs", "tests.cs", "test.fs", "tests.fs",
                    ".test.js", ".test.jsx", ".test.ts", ".test.tsx",
                    ".test.vue", ".spec.js", ".spec.jsx", ".spec.ts",
                    ".spec.tsx", ".spec.vue",
                ))
            ):
                candidates.append(path)
        return [str(path.relative_to(root)).replace("\\", "/") for path in sorted(candidates)]

    @staticmethod
    def _test_command(root: Path, configuration: dict[str, str], test_files: list[str]) -> str | None:
        candidates = ProjectAnalyzer._test_candidates(root, configuration, [], test_files)
        return candidates[0]["command"] if candidates else None

    @staticmethod
    def _test_candidates(
        root: Path,
        configuration: dict[str, str],
        files: list[Path],
        test_files: list[str],
    ) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        names = {path.name.casefold() for path in files} | {
            path.name.casefold() for path in root.iterdir()
        } if root.is_dir() else {path.name.casefold() for path in files}
        if test_files and ({"pyproject.toml", "requirements.txt", "setup.cfg"} & names):
            candidates.append({"tool": "pytest", "command": "python -m pytest -q", "reason": "Python test files and manifest detected"})
        if "package.json" in configuration:
            try:
                scripts = json.loads(configuration["package.json"]).get("scripts", {})
            except json.JSONDecodeError:
                scripts = {}
            if isinstance(scripts, dict) and "test" in scripts:
                if "pnpm-lock.yaml" in names:
                    tool, command = "pnpm", "pnpm test"
                elif "yarn.lock" in names:
                    tool, command = "yarn", "yarn test"
                else:
                    tool, command = "npm", "npm test -- --if-present"
                candidates.append({"tool": tool, "command": command, "reason": "package.json exposes a test script"})
        if "pom.xml" in names and test_files:
            command = "mvnw.cmd -q test" if "mvnw.cmd" in names else "mvn -q test"
            candidates.append({"tool": "maven", "command": command, "reason": "Maven project with test sources detected"})
        if "gradlew.bat" in names or "gradlew" in names:
            if test_files or any(name in names for name in {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}):
                command = "gradlew.bat test" if "gradlew.bat" in names else "./gradlew test"
                candidates.append({"tool": "gradle", "command": command, "reason": "Gradle wrapper detected"})
        if any(name.endswith((".sln", ".csproj", ".fsproj", ".vbproj")) for name in names) and test_files:
            candidates.append({"tool": "dotnet", "command": "dotnet test --nologo", "reason": ".NET project with test sources detected"})
        if "go.mod" in names and test_files:
            candidates.append({"tool": "go", "command": "go test ./...", "reason": "Go module with test sources detected"})
        if "cargo.toml" in names and test_files:
            candidates.append({"tool": "cargo", "command": "cargo test", "reason": "Rust package with test sources detected"})
        if "composer.json" in configuration and test_files:
            candidates.append({"tool": "composer", "command": "composer test", "reason": "Composer project with test sources detected"})
        return candidates

    @staticmethod
    def _quality_tools(root: Path, configuration: dict[str, str], files: list[Path]) -> list[dict[str, str]]:
        names = {path.name.casefold() for path in files}
        tools: list[dict[str, str]] = []
        if any(path.suffix.lower() == ".py" for path in files) and (
            "pyproject.toml" in configuration or shutil.which("ruff") is not None
        ):
            tools.append({"tool": "ruff", "command": "ruff check .", "reason": "Python sources detected"})
        if "package.json" in configuration:
            try:
                scripts = json.loads(configuration["package.json"]).get("scripts", {})
            except json.JSONDecodeError:
                scripts = {}
            if isinstance(scripts, dict) and "lint" in scripts:
                tools.append({"tool": "package-lint", "command": "npm run lint", "reason": "package.json exposes a lint script"})
        if {"pom.xml", "build.gradle", "build.gradle.kts"} & names:
            tools.append({"tool": "build-tool", "command": "project-specific static checks", "reason": "JVM build manifest detected"})
        return tools

    @staticmethod
    def _classify_project(root: Path, files: list[Path]) -> list[str]:
        names = {path.name.casefold() for path in files}
        suffixes = {path.suffix.casefold() for path in files}
        kinds: list[str] = []
        if names & {"package.json", "vite.config.js", "vite.config.ts", "next.config.js", "angular.json"}:
            kinds.append("web")
        if names & {"androidmanifest.xml", "settings.gradle", "settings.gradle.kts"} or "androidtest" in {part.casefold() for path in files for part in path.parts}:
            kinds.append("android")
        if names & {"pom.xml", "build.gradle", "build.gradle.kts"} or ".java" in suffixes or ".kt" in suffixes:
            kinds.append("jvm")
        if names & {"pyproject.toml", "requirements.txt", "setup.py"} or ".py" in suffixes:
            kinds.append("python")
        if names & {"go.mod"} or ".go" in suffixes:
            kinds.append("go")
        if names & {"cargo.toml"} or ".rs" in suffixes:
            kinds.append("rust")
        if suffixes & {".ipynb", ".tex", ".bib", ".csv"} or names & {"data", "notebooks", "experiments"}:
            kinds.append("research")
        if suffixes & {".md", ".rst", ".adoc", ".tex"} and not kinds:
            kinds.append("documentation")
        return kinds or ["workspace"]

    @staticmethod
    def _key_files(root: Path, files: list[Path]) -> list[Path]:
        marker_names = {
            "readme", "readme.md", "pyproject.toml", "requirements.txt", "package.json",
            "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
            "settings.gradle.kts", "androidmanifest.xml", "cargo.toml", "go.mod",
            "dockerfile", "compose.yml", "docker-compose.yml", "makefile",
            ".gitignore", "manifest.json", "angular.json",
        }
        selected = [path for path in files if path.name.casefold() in marker_names]
        selected.extend(
            path for path in files
            if path.name.casefold() in {"main.py", "app.py", "main.java", "main.kt", "index.ts", "index.js", "main.go"}
        )
        return sorted(dict.fromkeys(selected), key=lambda path: str(path).casefold())[:80]

    @staticmethod
    def _read_key_file_sections(root: Path, files: list[Path]) -> dict[str, str]:
        sections: dict[str, str] = {}
        for path in ProjectAnalyzer._key_files(root, files)[:30]:
            try:
                if path.stat().st_size > 64 * 1024:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            sections[str(path.relative_to(root)).replace("\\", "/")] = text[:12000]
        return sections

    @staticmethod
    async def _run_ruff(cwd: Path, timeout: float) -> dict[str, object]:
        if shutil.which("ruff") is None:
            return {
                "available": False,
                "executed": False,
                "reason": "ruff executable was not found",
            }
        return await ProjectAnalyzer._run_test_command("ruff check .", cwd, timeout)

    @staticmethod
    async def _run_test_command(command: str, cwd: Path, timeout: float) -> dict[str, object]:
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=max(1.0, timeout))
            return {
                "available": True,
                "command": command,
                "executed": True,
                "exit_code": process.returncode,
                "stdout": stdout.decode(errors="replace")[-12000:],
                "stderr": stderr.decode(errors="replace")[-12000:],
            }
        except TimeoutError:
            process.kill()
            await process.wait()
            return {"available": True, "command": command, "executed": True, "timed_out": True}
        except OSError as error:
            return {"available": True, "command": command, "executed": False, "error": str(error)}
    @staticmethod
    def _collect_files(root: Path, max_files: int) -> list[Path]:
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(f"Project directory does not exist: {root}")
        ignored = {
            ".git",
            ".venv",
            "venv",
            "node_modules",
            "build",
            "dist",
            "__pycache__",
            ".gradle",
            ".pytest_cache",
            ".ruff_cache",
        }
        files = [path for path in root.rglob("*") if path.is_file() and not ignored.intersection(path.parts)]
        return sorted(files)[: max_files + 1]

    @staticmethod
    def _analyze_python(path: Path, root: Path, symbols: list[dict[str, Any]], edges: list[dict[str, str]]) -> None:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            return
        source = str(path.relative_to(root))
        module = path.with_suffix("").relative_to(root).as_posix().replace("/", ".")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.append({"kind": type(node).__name__, "name": node.name, "file": source, "line": node.lineno})
                if isinstance(node, ast.ClassDef):
                    class_id = f"{source}:{node.lineno}:{node.name}"
                    for base in node.bases:
                        edges.append({"from": class_id, "to": ast.unparse(base), "kind": "inherits"})
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call):
                            edges.append({"from": class_id, "to": ast.unparse(child.func), "kind": "composes"})
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    edges.append({"from": module, "to": alias.name, "kind": "imports"})
            elif isinstance(node, ast.ImportFrom):
                edges.append({"from": module, "to": node.module or "", "kind": "imports"})

    @staticmethod
    def _analyze_source_symbols(
        path: Path,
        root: Path,
        symbols: list[dict[str, Any]],
        edges: list[dict[str, str]],
    ) -> None:
        """Extract a small, language-neutral symbol index for non-Python projects."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        source = str(path.relative_to(root)).replace("\\", "/")
        patterns = (
            (r"\b(?:export\s+)?(?:abstract\s+)?(?:class|interface|enum|struct|trait)\s+([A-Za-z_]\w*)", "type"),
            (r"\b(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)", "function"),
            (r"\b(?:pub\s+)?fn\s+([A-Za-z_]\w*)", "function"),
            (r"\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(", "function"),
            (r"\bdef\s+([A-Za-z_]\w*)\s*\(", "function"),
        )
        seen: set[tuple[int, str]] = set()
        for pattern, kind in patterns:
            for match in re.finditer(pattern, text):
                line = text.count("\n", 0, match.start()) + 1
                name = match.group(1)
                key = (line, name)
                if key in seen:
                    continue
                seen.add(key)
                symbols.append({"kind": kind, "name": name, "file": source, "line": line})
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ", "using ", "package ", "require(")):
                edges.append({
                    "from": source,
                    "to": stripped[:300],
                    "kind": "imports",
                    "line": str(line_number),
                })


class SystemGraphAnalyzer:
    """Builds a bounded relationship graph for the Assistant source itself."""

    async def analyze(self, root: str, max_files: int = 300) -> OperationResult:
        started_at = datetime.now(UTC)
        try:
            project_root = Path(root).resolve()
            files = await asyncio.to_thread(ProjectAnalyzer._collect_files, project_root, max_files)
            files = [path for path in files[:max_files] if path.suffix.lower() == ".py"]
            nodes: list[dict[str, Any]] = []
            edges: list[dict[str, str]] = []
            symbols_by_name: dict[str, str] = {}
            parsed: list[tuple[Path, ast.AST, str]] = []

            for path in files:
                relative = path.relative_to(project_root).as_posix()
                module_id = f"module:{relative}"
                nodes.append({"id": module_id, "kind": "module", "name": relative})
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                except (OSError, SyntaxError, UnicodeDecodeError):
                    continue
                parsed.append((path, tree, module_id))
                for item in ast.walk(tree):
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        symbol_id = f"symbol:{relative}:{item.lineno}:{item.name}"
                        nodes.append({
                            "id": symbol_id,
                            "kind": "symbol",
                            "name": item.name,
                            "file": relative,
                            "line": item.lineno,
                        })
                        edges.append({"from": module_id, "to": symbol_id, "kind": "contains"})
                        symbols_by_name.setdefault(item.name, symbol_id)

            for path, tree, module_id in parsed:
                for item in ast.walk(tree):
                    if isinstance(item, ast.Import):
                        for alias in item.names:
                            edges.append({"from": module_id, "to": f"import:{alias.name}", "kind": "imports"})
                    elif isinstance(item, ast.ImportFrom):
                        imported = item.module or "."
                        edges.append({"from": module_id, "to": f"import:{imported}", "kind": "imports"})
                    elif isinstance(item, ast.Call):
                        called = item.func.id if isinstance(item.func, ast.Name) else None
                        target = symbols_by_name.get(called) if called else None
                        if target:
                            edges.append({"from": module_id, "to": target, "kind": "calls"})

            output = {
                "root": str(project_root),
                "files_analyzed": [path.relative_to(project_root).as_posix() for path in files],
                "graph": {
                    "nodes": nodes[:4000],
                    "edges": edges[:8000],
                    "truncated": len(nodes) > 4000 or len(edges) > 8000,
                },
            }
            finished_at = datetime.now(UTC)
            return OperationResult(
                success=True,
                output=output,
                started_at=started_at,
                finished_at=finished_at,
                duration=(finished_at - started_at).total_seconds(),
            )
        except (OSError, ValueError) as error:
            return OperationResult(
                success=False,
                error=str(error),
                error_type=ErrorType.NOT_FOUND if isinstance(error, FileNotFoundError) else ErrorType.INVALID_ARGUMENT,
                retryable=False,
                started_at=started_at,
            )
