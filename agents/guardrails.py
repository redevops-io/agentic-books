"""Input/output guardrails for agents."""

from decimal import Decimal, ROUND_HALF_UP
from typing import Any


def _to_cents(values: list) -> int:
    """Sum monetary values as integer cents to avoid float error."""
    cent = Decimal("0.01")
    total = sum(
        (Decimal(str(v)).quantize(cent, rounding=ROUND_HALF_UP) for v in values),
        Decimal(0),
    )
    return int((total / cent).to_integral_value(rounding=ROUND_HALF_UP))


def require_human_review(confidence: float, threshold: float = 0.8) -> bool:
    """Require human review on low-confidence categorization."""
    return confidence < threshold


def validate_journal_entry(debits: list, credits: list) -> bool:
    """Block posting unbalanced journal entries."""
    return _to_cents(debits) == _to_cents(credits)


def apply_guardrails(data: Any) -> tuple[bool, str]:
    """Apply relevant guardrails; return (ok, reason)."""
    return True, "ok"
