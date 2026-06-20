"""Variance analysis agent."""

from decimal import Decimal


class VarianceAgent:
    """Agent for variance analysis."""

    def analyze(self, actual: float, budget: float) -> dict:
        actual = Decimal(str(actual))
        budget = Decimal(str(budget))
        variance = actual - budget
        pct = variance / budget if budget else Decimal(0)
        return {"variance": variance, "pct": pct}
