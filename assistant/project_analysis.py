from __future__ import annotations

import ast
import asyncio
import json
import re
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
                if path.suffix.lower() == ".py":
                    modules.append(
                        {
                            "module": path.with_suffix("").relative_to(started_files).as_posix().replace("/", "."),
                            "file": str(path.relative_to(started_files)),
                        }
                    )
                    self._analyze_python(path, started_files, symbols, edges)
            output = {
                "root": str(started_files),
                "files_analyzed": [str(path.relative_to(started_files)) for path in files],
                "key_files": [
                    str(path.relative_to(started_files))
                    for path in files
                    if path.name in {
                        "package.json", "pom.xml", "build.gradle", "build.gradle.kts",
                        "requirements.txt", "pyproject.toml", "Dockerfile",
                        "docker-compose.yml", "vite.config.js", "vite.config.ts",
                    }
                ][:50],
                "file_count": len(files),
                "languages": dict(languages),
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
        run_tests: bool = False,
        profile: str = "general",
        scope: list[str] | None = None,
        depth: str = "standard",
        accepted_constraints: list[str] | None = None,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        scoring: bool = True,
    ) -> OperationResult:
        """Collect safe project evidence and optionally run detected tests."""
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

        test_files = [
            path.relative_to(project_root).as_posix()
            for path in files
            if path.name.startswith("test_")
            or path.name.endswith("_test.py")
            or path.parts[-2:-1] == ("tests",)
        ]
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
        test_command = self._test_command(project_root, configuration, test_files)
        if run_tests and test_command:
            test_result = await self._run_test_command(test_command, project_root, timeout)
        elif run_tests:
            test_result = {
                "available": False,
                "reason": "No supported test command or test files detected",
            }
        else:
            test_result = {
                "available": bool(test_command),
                "command": test_command,
                "executed": False,
                "reason": "Tests were detected but not run; set run_tests=true to execute them.",
            }
        structural.output.update({
            "audit": {
                "configuration": configuration,
                "dependency_manifests": [name for name in configuration if Path(name).name not in {".gitignore", ".env.example"}],
                "test_files": test_files[:500],
                "test_command": test_command,
                "run_tests": run_tests,
                "test_result": test_result,
                "sensitive_files": sensitive_files,
                "sensitive_file_contents_read": False,
                "limitations": [
                    "Secret-bearing environment files are detected but never read.",
                    "Sonar analysis is not run automatically; availability must be checked separately.",
                ],
            }
        })
        audit_spec = AuditSpec(
            target=AuditTarget(
                kind=TargetKind.PROJECT,
                identifier=str(project_root),
                name=project_root.name,
            ),
            objective="Assess the target and report actionable findings.",
            profile=profile,
            scope=scope or [],
            depth=depth,
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
        ))
        findings.append(AuditFinding(
            id="testing.execution",
            title="Test coverage evidence is limited",
            category="testing",
            status=(
                FindingStatus.PASS
                if test_result.get("executed") and test_result.get("exit_code") == 0
                else FindingStatus.WARN
                if test_files
                else FindingStatus.UNKNOWN
            ),
            severity="medium" if test_files and not test_result.get("executed") else "info",
            message=(
                "Tests were detected but not executed."
                if test_files and not test_result.get("executed")
                else "Tests completed successfully."
                if test_result.get("executed") and test_result.get("exit_code") == 0
                else "No supported test evidence was found."
            ),
            evidence=[evidence[2]],
            recommendations=["Run the detected test command for a complete audit."]
            if test_files and not test_result.get("executed") else [],
            score=0.6 if test_files and not test_result.get("executed") else
            1.0 if test_result.get("executed") and test_result.get("exit_code") == 0 else 0.5,
        ))
        findings.append(AuditFinding(
            id="documentation.available",
            title="Documentation artifacts are available",
            category="documentation",
            status=FindingStatus.PASS if documentation_files else FindingStatus.UNKNOWN,
            severity="info" if documentation_files else "low",
            message=(
                f"Detected {len(documentation_files)} documentation artifact(s)."
                if documentation_files
                else "No common documentation artifact was detected in the bounded inventory."
            ),
            evidence=[evidence[3]],
            recommendations=["Add a README or project guide describing purpose, setup, and validation."]
            if not documentation_files else [],
            score=1.0 if documentation_files else 0.5,
        ))
        findings.append(AuditFinding(
            id="operations.deployment-artifacts",
            title="Deployment artifacts are inventoried",
            category="operations",
            status=FindingStatus.PASS if deployment_files else FindingStatus.UNKNOWN,
            severity="info" if deployment_files else "low",
            message=(
                f"Detected {len(deployment_files)} deployment or automation artifact(s)."
                if deployment_files
                else "No common deployment or automation artifact was detected."
            ),
            evidence=[evidence[3]],
            recommendations=["Document the intended local or production execution path."]
            if not deployment_files else [],
            score=1.0 if deployment_files else 0.5,
        ))
        findings.append(AuditFinding(
            id="repository.ignore-policy",
            title="Repository ignore policy is visible",
            category="configuration",
            status=FindingStatus.PASS if gitignore_rules else FindingStatus.UNKNOWN,
            severity="info" if gitignore_rules else "low",
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
            score=1.0 if gitignore_rules else 0.5,
        ))
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
    def _test_command(root: Path, configuration: dict[str, str], test_files: list[str]) -> str | None:
        if "pyproject.toml" in configuration and test_files:
            return "python -m pytest -q"
        if "requirements.txt" in configuration and test_files:
            return "python -m pytest -q"
        if "package.json" in configuration:
            try:
                scripts = json.loads(configuration["package.json"]).get("scripts", {})
            except json.JSONDecodeError:
                scripts = {}
            if "test" in scripts:
                return "npm test -- --if-present"
        return None

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
                "exit_code": process.returncode,
                "stdout": stdout.decode(errors="replace")[-12000:],
                "stderr": stderr.decode(errors="replace")[-12000:],
            }
        except TimeoutError:
            process.kill()
            await process.wait()
            return {"available": True, "command": command, "timed_out": True}
        except OSError as error:
            return {"available": True, "command": command, "error": str(error)}
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
