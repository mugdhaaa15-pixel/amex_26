"""
Orchestrator — the end-to-end pipeline the challenge asks for.

Monitor -> Decision -> Execution -> Notification, with metrics recorded
on every run. Each stage is a plain method call here (synchronous,
easy to test); app/main.py wraps this same pipeline in a generator so a
web client can watch the stages happen in real time over SSE.
"""
from __future__ import annotations
import time
from dataclasses import dataclass

from .models import Itinerary, FlightStatusEvent, DisruptionReport, RebookingCandidate, ScoredCandidate, Notification
from .monitor import MonitorAgent
from .decision import DecisionAgent
from .execution import ExecutionAgent
from .notification import NotificationAgent
from .metrics import MetricsTracker


@dataclass
class RunResult:
    disrupted: bool
    resolved: bool = False
    reason: str | None = None
    disruption: DisruptionReport | None = None
    scored_options: list[ScoredCandidate] | None = None
    chosen: RebookingCandidate | None = None
    notification: Notification | None = None
    detect_s: float | None = None
    resolve_s: float | None = None


class ConciergePipeline:
    def __init__(
        self,
        monitor: MonitorAgent,
        decision: DecisionAgent,
        execution: ExecutionAgent,
        notification: NotificationAgent,
        metrics: MetricsTracker,
        candidate_provider,  # callable(disruption) -> list[RebookingCandidate]
    ):
        self.monitor = monitor
        self.decision = decision
        self.execution = execution
        self.notification = notification
        self.metrics = metrics
        self.candidate_provider = candidate_provider

    def run(self, itinerary: Itinerary, events: dict[str, FlightStatusEvent]) -> RunResult:
        t0 = time.perf_counter()

        disruption = self.monitor.evaluate(itinerary, events)
        if disruption is None:
            return RunResult(disrupted=False)
        t_detect = time.perf_counter() - t0

        candidates = self.candidate_provider(disruption)
        scored, best = self.decision.evaluate(itinerary, disruption, candidates)

        if best is None:
            self.metrics.record_failure("no_policy_compliant_option")
            return RunResult(
                disrupted=True, resolved=False, reason="no_policy_compliant_option",
                disruption=disruption, scored_options=scored, detect_s=t_detect,
            )

        exec_result = self.execution.execute(itinerary, disruption, best.candidate)
        if not exec_result.success:
            self.metrics.record_failure(exec_result.reason or "execution_failed")
            return RunResult(
                disrupted=True, resolved=False, reason=exec_result.reason,
                disruption=disruption, scored_options=scored, detect_s=t_detect,
            )

        notification = self.notification.notify(itinerary, disruption, best.candidate, exec_result)
        resolve_s = time.perf_counter() - t0
        self.metrics.record_success(detect_s=t_detect, resolve_s=resolve_s)

        return RunResult(
            disrupted=True,
            resolved=True,
            disruption=disruption,
            scored_options=scored,
            chosen=best.candidate,
            notification=notification,
            detect_s=t_detect,
            resolve_s=resolve_s,
        )
