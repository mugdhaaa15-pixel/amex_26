"""
FastAPI surface for the concierge pipeline.

GET  /itinerary            current itinerary snapshot
GET  /metrics               running detection speed / success rate / count
POST /simulate/{scenario}   run the pipeline once, return the full result as JSON
GET  /stream/{scenario}     Server-Sent Events: watch monitor -> decision ->
                             execution -> notification happen live, each event
                             carrying the real data that stage produced.

scenario is one of: cancellation | missed | delay
"""
from __future__ import annotations
import json
import time
from dataclasses import asdict
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .data import (
    base_itinerary, events_for_scenario, candidates_for_disruption,
    PrimaryAirlineClient, FallbackAirlineClient, MockHotelClient, MockNotificationSender,
)
from .monitor import MonitorAgent
from .decision import DecisionAgent
from .execution import ExecutionAgent, AirlineGateway
from .notification import NotificationAgent
from .metrics import MetricsTracker
from .orchestrator import ConciergePipeline

app = FastAPI(title="Autonomous Travel-Disruption Concierge")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # demo only — a real deployment would lock this down
    allow_methods=["*"],
    allow_headers=["*"],
)

# One shared itinerary + metrics tracker for the life of the process,
# so /metrics reflects every /simulate and /stream call made during a demo.
itinerary = base_itinerary()
metrics = MetricsTracker()

airline_client = AirlineGateway(PrimaryAirlineClient(), FallbackAirlineClient())
hotel_client = MockHotelClient()
notification_sender = MockNotificationSender()

monitor_agent = MonitorAgent()
decision_agent = DecisionAgent()
execution_agent = ExecutionAgent(airline_client, hotel_client)
notification_agent = NotificationAgent(notification_sender)


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


def _serializable(obj):
    return json.loads(json.dumps(obj, default=lambda o: asdict(o) if hasattr(o, "__dataclass_fields__") else _json_default(o)))


@app.get("/itinerary")
def get_itinerary():
    return _serializable(itinerary)


@app.get("/metrics")
def get_metrics():
    return metrics.summary()


@app.post("/reset")
def reset():
    global itinerary
    itinerary = base_itinerary()
    return _serializable(itinerary)


@app.post("/simulate/{scenario}")
def simulate(scenario: str):
    global itinerary
    try:
        events = events_for_scenario(scenario)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unknown scenario '{scenario}'")

    itinerary = base_itinerary()  # each demo run starts from the original trip
    pipeline = ConciergePipeline(
        monitor_agent, decision_agent, execution_agent, notification_agent, metrics,
        candidate_provider=candidates_for_disruption,
    )
    result = pipeline.run(itinerary, events)
    return _serializable(result)


@app.get("/stream/{scenario}")
def stream(scenario: str):
    def event_stream():
        global itinerary
        try:
            events = events_for_scenario(scenario)
        except ValueError:
            yield f"event: error\ndata: {json.dumps({'detail': 'unknown scenario'})}\n\n"
            return

        itinerary = base_itinerary()  # each demo run starts from the original trip
        t0 = time.perf_counter()

        def emit(stage: str, status: str, payload: dict):
            data = {"stage": stage, "status": status, "t": round(time.perf_counter() - t0, 3), **payload}
            yield_data = json.dumps(data, default=_json_default)
            return f"event: stage\ndata: {yield_data}\n\n"

        yield emit("monitor", "active", {"message": "Polling live flight status feed"})
        disruption = monitor_agent.evaluate(itinerary, events)
        if disruption is None:
            yield emit("monitor", "none", {"message": "No disruption detected"})
            return
        detect_s = time.perf_counter() - t0
        yield emit("monitor", "done", {
            "kind": disruption.kind, "detail": disruption.detail, "detect_s": round(detect_s, 3),
        })

        yield emit("decision", "active", {"message": "Evaluating alternatives against policy"})
        candidates = candidates_for_disruption(disruption)
        scored, best = decision_agent.evaluate(itinerary, disruption, candidates)
        yield emit("decision", "done", {
            "options": [
                {
                    "flight_no": s.candidate.flight_no,
                    "origin": s.candidate.origin,
                    "destination": s.candidate.destination,
                    "departure": s.candidate.departure.isoformat(),
                    "arrival": s.candidate.arrival.isoformat(),
                    "cabin": s.candidate.cabin,
                    "fare_delta": s.candidate.fare_delta,
                    "stops": s.candidate.stops,
                    "layover_minutes": s.candidate.layover_minutes,
                    "score": round(s.score, 1),
                    "within_policy": s.within_policy,
                    "violations": s.violations,
                    "recommended": (best is not None and s.candidate.flight_no == best.candidate.flight_no),
                }
                for s in scored
            ],
        })

        if best is None:
            metrics.record_failure("no_policy_compliant_option")
            yield emit("decision", "no_option", {"message": "No candidate satisfies policy — escalating to a human agent"})
            return

        # Execution deliberately does NOT happen here. The stream stops after
        # the decision stage and hands control to the client: the cardmember
        # (or the auto-confirm countdown, standing in for the review-window
        # policy) decides which policy-compliant option to execute, then
        # calls POST /execute/{scenario} with that flight number.
        yield emit("awaiting_approval", "pending", {
            "message": "Waiting for cardmember approval or auto-confirm window",
            "recommended_flight_no": best.candidate.flight_no,
        })

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/execute/{scenario}")
def execute(scenario: str, flight_no: str):
    """
    Runs execution + notification for a specific policy-compliant candidate.
    Recomputes monitor + decision fresh (cheap, deterministic for a given
    scenario) so no server-side session state is needed between the SSE
    stream and this call.
    """
    global itinerary
    try:
        events = events_for_scenario(scenario)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unknown scenario '{scenario}'")

    t0 = time.perf_counter()
    itinerary = base_itinerary()
    disruption = monitor_agent.evaluate(itinerary, events)
    detect_s = time.perf_counter() - t0
    if disruption is None:
        raise HTTPException(status_code=400, detail="no disruption for this scenario")

    candidates = candidates_for_disruption(disruption)
    scored, _ = decision_agent.evaluate(itinerary, disruption, candidates)
    
    compliant_options = [s for s in scored if s.within_policy]
    primary_idx = next((i for i, s in enumerate(compliant_options) if s.candidate.flight_no == flight_no), None)
    
    if primary_idx is None:
        raise HTTPException(status_code=400, detail=f"'{flight_no}' is not a policy-compliant option for this disruption")

    # Try requested option first, then fall back to the rest in ranked order
    options_to_try = [compliant_options[primary_idx]] + [
        opt for i, opt in enumerate(compliant_options) if i != primary_idx
    ]

    for i, option in enumerate(options_to_try):
        option.candidate.idempotency_key = f"{itinerary.cardmember}-{disruption.kind}-{option.candidate.flight_no}"

        exec_result = execution_agent.execute(itinerary, disruption, option.candidate)
        if exec_result.success:
            notification = notification_agent.notify(itinerary, disruption, option.candidate, exec_result)
            resolve_s = time.perf_counter() - t0
            metrics.record_success(detect_s=detect_s, resolve_s=resolve_s)

            return {
                "chosen": _serializable(option.candidate),
                "booking_ref": exec_result.booking_ref,
                "new_hotel_check_in": exec_result.new_hotel_check_in.isoformat() if exec_result.new_hotel_check_in else None,
                "notification": _serializable(notification),
                "itinerary": _serializable(itinerary),
                "fallback_occurred": i > 0,
            }
            
        if exec_result.reason == "seat_unavailable":
            continue
            
        metrics.record_failure(exec_result.reason or "execution_failed")
        raise HTTPException(status_code=502, detail=exec_result.reason)

    metrics.record_failure("all_options_exhausted")
    raise HTTPException(status_code=502, detail="all_options_exhausted")
