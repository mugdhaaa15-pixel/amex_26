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

from .models import Itinerary, FlightStatusEvent, DisruptionReport, RebookingCandidate, ScoredCandidate, Notification, AuditLog
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
    audit_log: AuditLog | None = None
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

        audit_log = AuditLog()
        audit_log.record("monitor", "detected", f"Disruption detected: {disruption.detail} with urgency {disruption.urgency_level}")

        compliant_options = [s for s in scored if s.within_policy]
        if not compliant_options:
            audit_log.record("decision", "no_options", "No policy compliant options found.")
            self.metrics.record_failure("no_policy_compliant_option")
            return RunResult(
                disrupted=True, resolved=False, reason="no_policy_compliant_option",
                disruption=disruption, scored_options=scored, audit_log=audit_log, detect_s=t_detect,
            )

        for i, option in enumerate(compliant_options):
            if i > 0:
                audit_log.record("execution", "fallback", f"Falling back to option {i+1}: {option.candidate.flight_no}")
            
            exec_result = self.execution.execute(itinerary, disruption, option.candidate)
            if exec_result.success:
                audit_log.record("execution", "booking_success", f"Successfully booked {option.candidate.flight_no}")
                notification = self.notification.notify(itinerary, disruption, option.candidate, exec_result)
                resolve_s = time.perf_counter() - t0
                self.metrics.record_success(detect_s=t_detect, resolve_s=resolve_s)
                return RunResult(
                    disrupted=True, resolved=True, disruption=disruption,
                    scored_options=scored, chosen=option.candidate,
                    notification=notification, audit_log=audit_log,
                    detect_s=t_detect, resolve_s=resolve_s,
                )
            
            if exec_result.reason == "seat_unavailable":
                audit_log.record("execution", "contention", f"Seat unavailable for {option.candidate.flight_no}, trying next.")
                continue
            
            # Fatal error
            audit_log.record("execution", "fatal_error", f"Booking failed: {exec_result.reason}")
            self.metrics.record_failure(exec_result.reason or "execution_failed")
            return RunResult(
                disrupted=True, resolved=False, reason=exec_result.reason,
                disruption=disruption, scored_options=scored, audit_log=audit_log, detect_s=t_detect,
            )

        audit_log.record("execution", "exhausted", "All compliant options failed due to contention.")
        self.metrics.record_failure("all_options_exhausted")
        return RunResult(
            disrupted=True, resolved=False, reason="all_options_exhausted",
            disruption=disruption, scored_options=scored, audit_log=audit_log, detect_s=t_detect,
        )
