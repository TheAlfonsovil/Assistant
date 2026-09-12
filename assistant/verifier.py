from .domain.models import OperationResult, VerificationDecision
from .llm import VerificationResult


class DeterministicVerifier:
    def verify(self, result: OperationResult) -> VerificationResult:
        if result.success:
            return VerificationResult(
                decision=VerificationDecision.SUCCESS, reason="operation succeeded"
            )
        if result.error_type and result.retryable:
            return VerificationResult(
                decision=VerificationDecision.RETRY, reason=result.error or "retryable error"
            )
        if result.error_type and result.error_type.value == "USER_REQUIRED":
            return VerificationResult(
                decision=VerificationDecision.WAIT_USER,
                reason=result.error or "user input required",
            )
        return VerificationResult(
            decision=VerificationDecision.BLOCK, reason=result.error or "operation failed"
        )
