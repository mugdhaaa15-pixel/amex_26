"""
Metrics — Task 5: "Test and optimize the concierge agent for disruption
detection speed, rebooking success rate, and notification clarity."

Tracks real timings from each pipeline run (see orchestrator.py), not
hardcoded numbers.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class RunRecord:
    success: bool
    detect_s: float | None = None
    resolve_s: float | None = None
    failure_reason: str | None = None


class MetricsTracker:
    def __init__(self):
        self.runs: list[RunRecord] = []

    def record_success(self, detect_s: float, resolve_s: float) -> None:
        self.runs.append(RunRecord(success=True, detect_s=detect_s, resolve_s=resolve_s))

    def record_failure(self, reason: str) -> None:
        self.runs.append(RunRecord(success=False, failure_reason=reason))

    def summary(self) -> dict:
        total = len(self.runs)
        successes = [r for r in self.runs if r.success]
        n_success = len(successes)
        return {
            "total_runs": total,
            "successful_runs": n_success,
            "success_rate": round(n_success / total, 3) if total else None,
            "avg_detect_s": round(sum(r.detect_s for r in successes) / n_success, 3) if n_success else None,
            "avg_resolve_s": round(sum(r.resolve_s for r in successes) / n_success, 3) if n_success else None,
        }
