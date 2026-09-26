from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

class ProjectAuditDiscovery:
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
        package_manifests = [
            (Path(name), content)
            for name, content in configuration.items()
            if Path(name).name.casefold() == "package.json"
        ]
        for manifest, contents in package_manifests:
            try:
                scripts = json.loads(contents).get("scripts", {})
            except json.JSONDecodeError:
                scripts = {}
            if isinstance(scripts, dict) and "test" in scripts:
                package_directory = manifest.parent.as_posix()
                local_names = {
                    path.name.casefold()
                    for path in files
                    if path.parent == root / manifest.parent
                }
                local_names.update(
                    Path(name).name.casefold()
                    for name in configuration
                    if Path(name).parent == manifest.parent
                )
                if manifest.parent == Path(".") and root.is_dir():
                    local_names.update(path.name.casefold() for path in root.iterdir())
                if "pnpm-lock.yaml" in local_names:
                    tool, command = "pnpm", (
                        f'pnpm --dir "{package_directory}" test'
                        if package_directory != "."
                        else "pnpm test"
                    )
                elif "yarn.lock" in local_names:
                    tool, command = "yarn", (
                        f'yarn --cwd "{package_directory}" test'
                        if package_directory != "."
                        else "yarn test"
                    )
                else:
                    prefix = (
                        f' --prefix "{package_directory}"'
                        if package_directory != "."
                        else ""
                    )
                    tool, command = "npm", f"npm{prefix} test"
                candidates.append({"tool": tool, "command": command, "reason": "package.json exposes a test script"})
        java_test_paths = [
            Path(path)
            for path in test_files
            if Path(path).suffix.casefold() in {".java", ".kt", ".kts"}
        ]
        pom_files = [path for path in files if path.name.casefold() == "pom.xml"]
        if not pom_files and (root / "pom.xml").is_file():
            pom_files = [root / "pom.xml"]
        for pom in pom_files:
            project_directory = pom.parent
            if not any(path.is_relative_to(project_directory.relative_to(root)) for path in java_test_paths):
                continue
            relative_pom = pom.relative_to(root).as_posix()
            wrappers = (
                project_directory / "mvnw.cmd",
                project_directory / "mvnw",
                root / "mvnw.cmd",
                root / "mvnw",
            )
            wrapper = next((path for path in wrappers if path.is_file()), None)
            if wrapper:
                relative_wrapper = wrapper.relative_to(root).as_posix()
                command = f'"{relative_wrapper}" -f "{relative_pom}" -q test'
            else:
                command = f'mvn -q -f "{relative_pom}" test'
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
        package_manifests = [
            (Path(name), content)
            for name, content in configuration.items()
            if Path(name).name.casefold() == "package.json"
        ]
        for manifest, contents in package_manifests:
            try:
                scripts = json.loads(contents).get("scripts", {})
            except json.JSONDecodeError:
                scripts = {}
            if isinstance(scripts, dict) and "lint" in scripts:
                package_directory = manifest.parent.as_posix()
                prefix = (
                    f' --prefix "{package_directory}"'
                    if package_directory != "."
                    else ""
                )
                tools.append({"tool": "package-lint", "command": f"npm{prefix} run lint", "reason": "package.json exposes a lint script"})
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
        for path in ProjectAuditDiscovery._key_files(root, files)[:30]:
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
        return await ProjectAuditDiscovery._run_test_command("ruff check .", cwd, timeout)

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
