import json

from .domain.contracts import CriterionResult, CriterionStatus, Diagnostic
from .domain.models import ErrorType, OperationResult, VerificationDecision
from .llm import VerificationResult


class DeterministicVerifier:
    def verify(self, result: OperationResult) -> VerificationResult:
        violations = result.metadata.get("constraint_violations", [])
        if isinstance(violations, list):
            hard = [
                item for item in violations
                if isinstance(item, dict) and item.get("strength") == "hard"
            ]
            if hard:
                diagnostics = self._constraint_diagnostics(violations)
                return VerificationResult(
                    decision=VerificationDecision.BLOCK,
                    reason="hard task constraint was violated",
                    diagnostics=diagnostics,
                )
        verified = self._verify(result)
        soft_diagnostics = self._constraint_diagnostics(violations)
        if soft_diagnostics:
            verified = verified.model_copy(
                update={"diagnostics": [*verified.diagnostics, *soft_diagnostics]}
            )
        return verified

    def _verify(self, result: OperationResult) -> VerificationResult:
        missing_outputs = result.metadata.get("missing_required_outputs", [])
        if missing_outputs:
            names = ", ".join(
                str(item.get("name", "unnamed"))
                for item in missing_outputs
                if isinstance(item, dict)
            )
            return VerificationResult(
                decision=VerificationDecision.RETRY,
                reason=f"required outputs were not published: {names}",
                criteria_results=self._criteria_results(result, CriterionStatus.FAIL),
                diagnostics=[
                    Diagnostic(
                        code="OUTPUT_REQUIRED_MISSING",
                        message=f"Required outputs were not published: {names}",
                        severity="error",
                        retryable=True,
                        evidence=[f"output:{name}" for name in names.split(", ") if name],
                    )
                ],
                missing_evidence=[f"output:{name}" for name in names.split(", ") if name],
            )
        acceptance_met = self._acceptance_is_met(result)
        criteria_results = self._criteria_results(
            result,
            CriterionStatus.PASS if result.success and acceptance_met else CriterionStatus.FAIL,
        )
        if result.success and acceptance_met:
            return VerificationResult(
                decision=VerificationDecision.SUCCESS,
                reason="operation succeeded",
                criteria_results=criteria_results,
            )
        if result.success:
            return VerificationResult(
                decision=VerificationDecision.RETRY,
                reason="operation succeeded but acceptance evidence was not met",
                criteria_results=criteria_results,
            )
        if result.error_type and result.retryable:
            return VerificationResult(
                decision=VerificationDecision.RETRY,
                reason=result.error or "retryable error",
                criteria_results=criteria_results,
            )
        if result.error_type in {ErrorType.DEPENDENCY_FAILURE, ErrorType.CONFLICT}:
            return VerificationResult(
                decision=VerificationDecision.REPLAN,
                reason=result.error or "execution context requires replanning",
                criteria_results=criteria_results,
            )
        if result.error_type and result.error_type.value == "USER_REQUIRED":
            return VerificationResult(
                decision=VerificationDecision.WAIT_USER,
                reason=result.error or "user input required",
                criteria_results=criteria_results,
            )
        return VerificationResult(
            decision=VerificationDecision.FAIL,
            reason=result.error or "operation failed",
            criteria_results=criteria_results,
        )

    @staticmethod
    def _constraint_diagnostics(violations: object) -> list[Diagnostic]:
        if not isinstance(violations, list):
            return []
        diagnostics: list[Diagnostic] = []
        for index, item in enumerate(violations, start=1):
            if not isinstance(item, dict):
                continue
            constraint_id = str(item.get("id") or f"constraint-{index}")
            description = str(item.get("description") or "constraint violation")
            strength = str(item.get("strength") or "soft")
            diagnostics.append(
                Diagnostic(
                    code="HARD_CONSTRAINT_VIOLATED" if strength == "hard" else "SOFT_CONSTRAINT_VIOLATED",
                    message=description,
                    severity="error" if strength == "hard" else "warning",
                    retryable=False,
                    evidence=[constraint_id],
                )
            )
        return diagnostics

    @staticmethod
    def _criteria_results(
        result: OperationResult, status: CriterionStatus
    ) -> list[CriterionResult]:
        criteria = result.metadata.get("acceptance_criteria", [])
        if not isinstance(criteria, list):
            return []
        results: list[CriterionResult] = []
        for index, criterion in enumerate(criteria, start=1):
            if isinstance(criterion, dict):
                criterion_id = str(criterion.get("id") or f"criterion-{index}")
                description = str(criterion.get("description") or criterion_id)
            else:
                criterion_id = f"criterion-{index}"
                description = str(criterion)
            results.append(
                CriterionResult(
                    criterion_id=criterion_id,
                    status=status,
                    evidence=["operation.success"] if status is CriterionStatus.PASS else [],
                    reason=description,
                )
            )
        return results

    @staticmethod
    def with_tool_evidence(result: OperationResult, evidence: dict) -> OperationResult:
        """Attach deterministic tool evidence without interpreting prose acceptance."""
        if not result.success or not isinstance(result.output, dict) or not evidence:
            return result
        metadata = dict(result.metadata)
        expected = dict(metadata.get("expected") or {})
        fields = expected.get("fields", {})
        if not isinstance(fields, dict):
            fields = {}
        for field in evidence.get("success_fields", []):
            if field in result.output:
                fields.setdefault(field, result.output[field])
        if fields:
            expected["fields"] = fields
        metadata["expected"] = expected
        return result.model_copy(update={"metadata": metadata})

    @staticmethod
    def _acceptance_is_met(result: OperationResult) -> bool:
        expected = result.metadata.get("expected", {})
        if not expected:
            return True
        if not isinstance(expected, dict):
            return False
        if "exit_code" in expected:
            output = result.output if isinstance(result.output, dict) else {}
            if output.get("exit_code") != expected["exit_code"]:
                return False
        for path, value in expected.get("fields", {}).items():
            if DeterministicVerifier._read_path(result.output, path) != value:
                return False
        for path in expected.get("exists", []):
            if DeterministicVerifier._read_path(result.output, path) is None:
                return False
        for path in expected.get("not_exists", []):
            if DeterministicVerifier._read_path(result.output, path) is not None:
                return False
        required = expected.get("contains", expected.get("output_contains", []))
        if isinstance(required, str):
            required = [required]
        if required:
            rendered = json.dumps(result.output, ensure_ascii=False, default=str)
            if any(str(value) not in rendered for value in required):
                return False
        return True

    @staticmethod
    def _read_path(value, path: str):
        current = value
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current
