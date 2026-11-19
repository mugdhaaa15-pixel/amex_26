from datetime import datetime, timedelta

from app.models import (
    Itinerary, Leg, HotelBooking, CardmemberPolicy,
    FlightStatusEvent, FlightState, RebookingCandidate,
)
from app.monitor import MonitorAgent
from app.decision import DecisionAgent
from app.execution import ExecutionAgent
from app.notification import NotificationAgent
from app.metrics import MetricsTracker
from app.orchestrator import ConciergePipeline
from app.data import (
    base_itinerary, events_for_scenario, candidates_for_disruption,
    MockAirlineClient, MockHotelClient, MockNotificationSender,
    PrimaryAirlineClient, FallbackAirlineClient
)
from app.execution import AirlineGateway


def make_itinerary():
    return Itinerary(
        cardmember="Test User",
        policy=CardmemberPolicy("platinum", max_fare_delta=200, allow_cabin_downgrade=False,
                                 max_extra_layover_minutes=180, max_arrival_delay_minutes=240),
        legs=[
            Leg("AA118", "JFK", "LHR", datetime(2026, 8, 14, 18, 40), datetime(2026, 8, 15, 6, 55), "business", 4200.0),
            Leg("BA304", "LHR", "CDG", datetime(2026, 8, 15, 9, 15), datetime(2026, 8, 15, 11, 35), "business", 380.0),
        ],
    )


# ---------- Monitor agent ----------

def test_monitor_detects_cancellation():
    it = make_itinerary()
    events = {"AA118": FlightStatusEvent("AA118", "JFK", "LHR", it.legs[0].scheduled_departure,
                                          it.legs[0].scheduled_arrival, state=FlightState.CANCELLED)}
    report = MonitorAgent().evaluate(it, events)
    assert report is not None
    assert report.kind == "cancellation"
    assert report.leg_index == 0


def test_monitor_detects_missed_connection_via_buffer():
    it = make_itinerary()
    # 105m delay leaves only ~15m before the 09:15 connection — below LHR's 75m MCT
    events = {"AA118": FlightStatusEvent("AA118", "JFK", "LHR", it.legs[0].scheduled_departure,
                                          it.legs[0].scheduled_arrival, state=FlightState.DELAYED, delay_minutes=105)}
    report = MonitorAgent().evaluate(it, events)
    assert report is not None
    assert report.kind == "missed_connection"
    assert report.buffer_minutes < 75


def test_monitor_ignores_delay_with_safe_buffer():
    it = make_itinerary()
    # 10m delay still leaves well over 75m to connect
    events = {"AA118": FlightStatusEvent("AA118", "JFK", "LHR", it.legs[0].scheduled_departure,
                                          it.legs[0].scheduled_arrival, state=FlightState.DELAYED, delay_minutes=10)}
    report = MonitorAgent().evaluate(it, events)
    assert report is None


def test_monitor_ignores_on_time_flights():
    it = make_itinerary()
    events = {"AA118": FlightStatusEvent("AA118", "JFK", "LHR", it.legs[0].scheduled_departure,
                                          it.legs[0].scheduled_arrival, state=FlightState.ON_TIME)}
    assert MonitorAgent().evaluate(it, events) is None


# ---------- Decision agent ----------

def test_decision_filters_out_of_policy_option():
    it = make_itinerary()
    report_kind = MonitorAgent().evaluate(
        it, {"AA118": FlightStatusEvent("AA118", "JFK", "LHR", it.legs[0].scheduled_departure,
                                         it.legs[0].scheduled_arrival, state=FlightState.CANCELLED)}
    )
    over_budget = RebookingCandidate("XX1", "JFK", "LHR", it.legs[0].scheduled_departure,
                                      it.legs[0].scheduled_arrival, "business", fare_delta=500.0, stops=0)
    long_layover = RebookingCandidate("XX2", "JFK", "LHR", it.legs[0].scheduled_departure,
                                       it.legs[0].scheduled_arrival, "business", fare_delta=0.0, stops=1,
                                       layover_minutes=400)
    scored, best = DecisionAgent().evaluate(it, report_kind, [over_budget, long_layover])
    assert all(not s.within_policy for s in scored)
    assert best is None


def test_decision_picks_lowest_cost_within_policy():
    it = make_itinerary()
    report = MonitorAgent().evaluate(
        it, {"AA118": FlightStatusEvent("AA118", "JFK", "LHR", it.legs[0].scheduled_departure,
                                         it.legs[0].scheduled_arrival, state=FlightState.CANCELLED)}
    )
    direct_on_time = RebookingCandidate("AA942", "JFK", "LHR", it.legs[0].scheduled_departure,
                                         it.legs[0].scheduled_arrival, "business", fare_delta=0.0, stops=0)
    one_stop_later = RebookingCandidate("XX3", "JFK", "LHR",
                                         it.legs[0].scheduled_departure,
                                         it.legs[0].scheduled_arrival + timedelta(hours=5),
                                         "business", fare_delta=0.0, stops=1, layover_minutes=120)
    scored, best = DecisionAgent().evaluate(it, report, [one_stop_later, direct_on_time])
    assert best is not None
    assert best.candidate.flight_no == "AA942"  # direct + on-time beats a 5h-later one-stop


# ---------- Execution agent ----------

def test_execution_updates_itinerary_and_hotel():
    it = make_itinerary()
    it.hotel = HotelBooking("Le Meurice", "Paris", datetime(2026, 8, 15, 15, 0), datetime(2026, 8, 18, 11, 0))
    report = MonitorAgent().evaluate(
        it, {"AA118": FlightStatusEvent("AA118", "JFK", "LHR", it.legs[0].scheduled_departure,
                                         it.legs[0].scheduled_arrival, state=FlightState.CANCELLED)}
    )
    candidate = RebookingCandidate("AA942", "JFK", "LHR", datetime(2026, 8, 14, 21, 10),
                                    datetime(2026, 8, 15, 9, 25), "business", fare_delta=0.0, stops=0)

    agent = ExecutionAgent(MockAirlineClient(), MockHotelClient(latency_s=0))
    result = agent.execute(it, report, candidate)

    assert result.success
    assert it.legs[0].flight_no == "AA942"
    assert it.hotel.check_in.day == 15  # shifted to match the new arrival day


def test_execution_retries_then_fails_cleanly():
    it = make_itinerary()
    report = MonitorAgent().evaluate(
        it, {"AA118": FlightStatusEvent("AA118", "JFK", "LHR", it.legs[0].scheduled_departure,
                                         it.legs[0].scheduled_arrival, state=FlightState.CANCELLED)}
    )
    candidate = RebookingCandidate("AA942", "JFK", "LHR", it.legs[0].scheduled_departure,
                                    it.legs[0].scheduled_arrival, "business", fare_delta=0.0, stops=0)
    
    # Offline raises VendorOfflineError which breaks the loop if unhandled by Gateway
    always_fails = PrimaryAirlineClient(latency_s=0, offline=True)
    agent = ExecutionAgent(always_fails, MockHotelClient(latency_s=0), max_retries=2)
    result = agent.execute(it, report, candidate)
    assert not result.success
    assert "airline booking failed" in result.reason


def test_execution_idempotency():
    it = make_itinerary()
    report = MonitorAgent().evaluate(
        it, {"AA118": FlightStatusEvent("AA118", "JFK", "LHR", it.legs[0].scheduled_departure,
                                         it.legs[0].scheduled_arrival, state=FlightState.CANCELLED)}
    )
    candidate = RebookingCandidate("AA942", "JFK", "LHR", it.legs[0].scheduled_departure,
                                    it.legs[0].scheduled_arrival, "business", fare_delta=0.0, stops=0)
    candidate.idempotency_key = "test_key_123"
    
    agent = ExecutionAgent(MockAirlineClient(), MockHotelClient(latency_s=0))
    result1 = agent.execute(it, report, candidate)
    assert result1.success

    # Second attempt with same key should fail idempotency check
    result2 = agent.execute(it, report, candidate)
    assert not result2.success
    assert result2.reason == "duplicate_execution_attempt"


# ---------- End-to-end pipeline ----------

def test_pipeline_end_to_end_resolves_cancellation():
    it = base_itinerary()
    events = events_for_scenario("cancellation")
    
    # Set seats on AA942 to 0 to test fallback logic
    def mock_provider(disr):
        cands = candidates_for_disruption(disr)
        for c in cands:
            if c.flight_no == "AA942":
                c.seats_available = 0
        return cands

    pipeline = ConciergePipeline(
        MonitorAgent(), DecisionAgent(),
        ExecutionAgent(AirlineGateway(PrimaryAirlineClient(latency_s=0), FallbackAirlineClient(latency_s=0)), MockHotelClient(latency_s=0)),
        NotificationAgent(MockNotificationSender(latency_s=0)),
        MetricsTracker(),
        candidate_provider=mock_provider,
    )
    result = pipeline.run(it, events)
    assert result.disrupted
    assert result.resolved
    # Since AA942 has 0 seats, it falls back to AA950
    assert result.chosen.flight_no == "AA950"
    assert result.audit_log is not None
    assert result.notification is not None
    assert "AA950" in result.notification.message
    assert result.detect_s is not None and result.resolve_s is not None
    assert result.resolve_s >= result.detect_s


def test_pipeline_handles_all_three_scenarios():
    for scenario in ["cancellation", "missed", "delay"]:
        it = base_itinerary()
        events = events_for_scenario(scenario)
        # Use simple mock provider without 0 seats to just test the flow
        def simple_provider(disr):
            cands = candidates_for_disruption(disr)
            for c in cands:
                c.seats_available = 10
            return cands
            
        pipeline = ConciergePipeline(
            MonitorAgent(), DecisionAgent(),
            ExecutionAgent(AirlineGateway(PrimaryAirlineClient(latency_s=0), FallbackAirlineClient(latency_s=0)), MockHotelClient(latency_s=0)),
            NotificationAgent(MockNotificationSender(latency_s=0)),
            MetricsTracker(),
            candidate_provider=simple_provider,
        )
        result = pipeline.run(it, events)
        assert result.disrupted, f"{scenario} should be detected as a disruption"
        assert result.resolved, f"{scenario} should resolve to a booked alternative"


def test_metrics_track_success_rate_across_runs():
    metrics = MetricsTracker()
    metrics.record_success(detect_s=0.5, resolve_s=1.2)
    metrics.record_success(detect_s=0.4, resolve_s=1.0)
    metrics.record_failure("no_policy_compliant_option")
    summary = metrics.summary()
    assert summary["total_runs"] == 3
    assert summary["successful_runs"] == 2
    assert abs(summary["success_rate"] - 2 / 3) < 1e-3
    assert summary["avg_detect_s"] == round((0.5 + 0.4) / 2, 3)
