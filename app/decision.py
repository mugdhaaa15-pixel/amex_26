"""
Decision agent — Task 2 (evaluation half): "Implement autonomous
rebooking logic that evaluates alternatives ... within policy limits."

Two-stage process:
  1. Policy filter — every candidate is checked against the cardmember's
     CardmemberPolicy. Any violation (fare over budget, disallowed cabin
     downgrade, layover too long, arrival too late) disqualifies it from
     autonomous execution, but it is still returned (flagged) so the
     interface can show why it was excluded.
  2. Scoring — candidates that pass policy are ranked by a weighted cost
     function. Lower score = better. This is a simple, explainable
     multi-criteria model rather than a black box, which matters for a
     financial product: a judge or compliance reviewer can see exactly
     why option A beat option B.
"""
from __future__ import annotations

from .models import (
    Itinerary,
    DisruptionReport,
    RebookingCandidate,
    ScoredCandidate,
    cabin_rank,
)

# Relative weights for the cost function. Tuned so that adding a stop is
# nearly always worse than a moderate fare increase, and lateness is
# penalized per minute rather than as a step function.
WEIGHT_ARRIVAL_DELAY_PER_MIN = 1.0
WEIGHT_FARE_DELTA_PER_DOLLAR = 0.5
WEIGHT_PER_STOP = 90.0
WEIGHT_EXCESS_LAYOVER_PER_MIN = 0.3
LAYOVER_GRACE_MINUTES = 60  # layover minutes below this add no penalty


def _mock_visa_check(candidate: RebookingCandidate, cardmember: str) -> bool:
    # A real system would call an immigration database (e.g., Timatic)
    # Here we mock it: say, EY34 connects in AUH and requires a transit visa.
    if candidate.flight_no == "EY34":
        return True
    return False


class DecisionAgent:
    def evaluate(
        self,
        itinerary: Itinerary,
        disruption: DisruptionReport,
        candidates: list[RebookingCandidate],
    ) -> tuple[list[ScoredCandidate], ScoredCandidate | None]:
        policy = itinerary.policy
        original_leg = itinerary.legs[disruption.affected_leg_index]

        scored: list[ScoredCandidate] = []
        for c in candidates:
            violations: list[str] = []

            if c.fare_delta > policy.max_fare_delta:
                violations.append(
                    f"fare +${c.fare_delta:.0f} exceeds max +${policy.max_fare_delta:.0f}"
                )

            if cabin_rank(c.cabin) < cabin_rank(original_leg.cabin) and not policy.allow_cabin_downgrade:
                violations.append(f"cabin downgrade to {c.cabin} not permitted")

            if c.stops > 0 and c.layover_minutes > policy.max_extra_layover_minutes:
                violations.append(
                    f"layover {c.layover_minutes}m exceeds max {policy.max_extra_layover_minutes}m"
                )

            arrival_delay_minutes = (c.arrival - original_leg.scheduled_arrival).total_seconds() / 60
            if arrival_delay_minutes > policy.max_arrival_delay_minutes:
                violations.append(
                    f"arrives {arrival_delay_minutes:.0f}m later than policy allows "
                    f"({policy.max_arrival_delay_minutes}m)"
                )

            if _mock_visa_check(c, itinerary.cardmember):
                violations.append("transit visa required for layover airport")

            score = self._score(c, arrival_delay_minutes, policy)
            scored.append(
                ScoredCandidate(
                    candidate=c,
                    score=score,
                    within_policy=not violations,
                    violations=violations,
                )
            )

        # Policy-compliant options first, then lowest (best) score
        scored.sort(key=lambda s: (not s.within_policy, s.score))
        best = next((s for s in scored if s.within_policy), None)
        return scored, best

    def _score(self, c: RebookingCandidate, arrival_delay_minutes: float, policy) -> float:
        w_delay = WEIGHT_ARRIVAL_DELAY_PER_MIN
        w_fare = WEIGHT_FARE_DELTA_PER_DOLLAR
        w_stop = WEIGHT_PER_STOP
        w_layover = WEIGHT_EXCESS_LAYOVER_PER_MIN

        if policy.strictness_profile == "strict":
            w_fare *= 2.0
        elif policy.strictness_profile == "flexible":
            w_fare *= 0.5
            w_delay *= 1.5

        excess_layover = max(c.layover_minutes - LAYOVER_GRACE_MINUTES, 0) if c.stops > 0 else 0
        return (
            max(arrival_delay_minutes, 0) * w_delay
            + max(c.fare_delta, 0) * w_fare
            + c.stops * w_stop
            + excess_layover * w_layover
        )
