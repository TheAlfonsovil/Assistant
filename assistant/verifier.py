import json

from .domain.models import ErrorType, OperationResult, VerificationDecision
from .llm import VerificationResult


class DeterministicVerifier:
    def verify(self, result: OperationResult) -> VerificationResult:
        if result.success and self._acceptance_is_met(result):
            return VerificationResult(
                decision=VerificationDecision.SUCCESS, reason="operation succeeded"
            )
        if result.success:
            return VerificationResult(
                decision=VerificationDecision.RETRY,
                reason="operation succeeded but acceptance evidence was not met",
            )
        if result.error_type and result.retryable:
            return VerificationResult(
                decision=VerificationDecision.RETRY, reason=result.error or "retryable error"
            )
        if result.error_type in {ErrorType.DEPENDENCY_FAILURE, ErrorType.CONFLICT}:
            return VerificationResult(
                decision=VerificationDecision.REPLAN,
                reason=result.error or "execution context requires replanning",
            )
        if result.error_type and result.error_type.value == "USER_REQUIRED":
            return VerificationResult(
                decision=VerificationDecision.WAIT_USER,
                reason=result.error or "user input required",
            )
        return VerificationResult(
            decision=VerificationDecision.FAIL, reason=result.error or "operation failed"
        )

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
        required = expected.get("contains", expected.get("output_contains", []))
        if isinstance(required, str):
            required = [required]
        if required:
            rendered = json.dumps(result.output, ensure_ascii=False, default=str)
            if any(str(value) not in rendered for value in required):
                return False
        return True
