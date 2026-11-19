"""
Mock data and mock API clients.

Everything in this file is the seam a real integration replaces:
- events_for_scenario() would become a live feed subscription (e.g.
  Amadeus Flight Status / a webhook consumer) instead of a fixed dict.
- INVENTORY / candidates_for_disruption() would become a live
  availability search (Amadeus Flight Offers Search).
- MockAirlineClient / MockHotelClient / MockNotificationSender would
  become real API clients. They implement the same interfaces defined
  in execution.py and notification.py, so nothing else in app/ changes.

Simulated latency and a small chance of a transient booking failure are
included so the execution agent's retry logic in execution.py has
something real to do.
"""
from __future__ import annotations
import random
import time
import uuid
from datetime import datetime

from .models import (
    Itinerary, Leg, HotelBooking, CardmemberPolicy,
    FlightStatusEvent, FlightState, RebookingCandidate, DisruptionReport,
)
from .execution import AirlineClient, HotelClient, VendorOfflineError, SeatUnavailableError
from .notification import NotificationSender

random.seed(7)  # deterministic demo runs


def base_itinerary() -> Itinerary:
    return Itinerary(
        cardmember="Priya Sharma",
        policy=CardmemberPolicy(
            tier="platinum",
            max_fare_delta=200.0,
            allow_cabin_downgrade=False,
            max_extra_layover_minutes=180,
            max_arrival_delay_minutes=240,
        ),
        legs=[
            Leg("AA118", "JFK", "LHR", datetime(2026, 8, 14, 18, 40), datetime(2026, 8, 15, 6, 55), "business", 4200.0),
            Leg("BA304", "LHR", "CDG", datetime(2026, 8, 15, 9, 15), datetime(2026, 8, 15, 11, 35), "business", 380.0),
        ],
        hotel=HotelBooking("Le Meurice", "Paris", datetime(2026, 8, 15, 15, 0), datetime(2026, 8, 18, 11, 0)),
    )


def events_for_scenario(scenario: str) -> dict[str, FlightStatusEvent]:
    it = base_itinerary()
    leg0, leg1 = it.legs

    if scenario == "cancellation":
        return {
            "AA118": FlightStatusEvent(
                "AA118", leg0.origin, leg0.destination, leg0.scheduled_departure, leg0.scheduled_arrival,
                state=FlightState.CANCELLED, confidence_score=0.99
            )
        }
    if scenario == "missed":
        # delayed just enough to blow the 75-minute LHR connection buffer
        return {
            "AA118": FlightStatusEvent(
                "AA118", leg0.origin, leg0.destination, leg0.scheduled_departure, leg0.scheduled_arrival,
                state=FlightState.DELAYED, delay_minutes=105, confidence_score=0.95
            )
        }
    if scenario == "delay":
        # big delay, but far enough ahead that no connection is at risk yet
        return {
            "AA118": FlightStatusEvent(
                "AA118", leg0.origin, leg0.destination, leg0.scheduled_departure, leg0.scheduled_arrival,
                state=FlightState.DELAYED, delay_minutes=360, confidence_score=0.90
            )
        }
    raise ValueError(f"unknown scenario: {scenario}")


def candidates_for_disruption(disruption: DisruptionReport) -> list[RebookingCandidate]:
    """Stand-in for a live availability search, keyed by disruption kind."""
    if disruption.kind == "cancellation":
        return [
            # 0 seats available triggers contention logic
            RebookingCandidate("AA942", "JFK", "LHR", datetime(2026, 8, 14, 21, 10), datetime(2026, 8, 15, 9, 25), "business", fare_delta=0.0, stops=0, seats_available=0),
            RebookingCandidate("AA950", "JFK", "LHR", datetime(2026, 8, 14, 22, 10), datetime(2026, 8, 15, 10, 15), "business", fare_delta=0.0, stops=0, seats_available=4),
            RebookingCandidate("VS4", "JFK", "LHR", datetime(2026, 8, 15, 8, 0), datetime(2026, 8, 15, 19, 55), "premium_economy", fare_delta=-120.0, stops=0, seats_available=9),
            RebookingCandidate("AA118X", "JFK", "LHR", datetime(2026, 8, 14, 19, 30), datetime(2026, 8, 15, 12, 45), "business", fare_delta=0.0, stops=1, layover_minutes=290, seats_available=2),
        ]
    if disruption.kind == "missed_connection":
        return [
            RebookingCandidate("BA305", "LHR", "CDG", datetime(2026, 8, 15, 12, 30), datetime(2026, 8, 15, 14, 50), "business", fare_delta=0.0, stops=0),
            RebookingCandidate("BA308", "LHR", "CDG", datetime(2026, 8, 15, 13, 40), datetime(2026, 8, 15, 16, 0), "business", fare_delta=0.0, stops=0),
            RebookingCandidate("AF1681", "LHR", "CDG", datetime(2026, 8, 15, 16, 5), datetime(2026, 8, 15, 18, 25), "business", fare_delta=0.0, stops=0),
            RebookingCandidate("BA310X", "LHR", "CDG", datetime(2026, 8, 15, 12, 0), datetime(2026, 8, 15, 17, 20), "business", fare_delta=0.0, stops=1, layover_minutes=220),
        ]
    if disruption.kind == "major_delay":
        return [
            RebookingCandidate("BA306", "LHR", "CDG", datetime(2026, 8, 15, 15, 20), datetime(2026, 8, 15, 17, 40), "business", fare_delta=0.0, stops=0),
            RebookingCandidate("EY34", "LHR", "CDG", datetime(2026, 8, 15, 18, 0), datetime(2026, 8, 16, 3, 10), "business", fare_delta=40.0, stops=1, layover_minutes=310),
        ]
    return []


class MockBookingConfirmation:
    def __init__(self, reference: str):
        self.reference = reference


class MockAirlineClient(AirlineClient):
    """Legacy mock for backwards compatibility, mostly unused now."""
    def book(self, candidate: RebookingCandidate) -> MockBookingConfirmation:
        return MockBookingConfirmation(reference=f"CNF-{uuid.uuid4().hex[:8].upper()}")

class PrimaryAirlineClient(AirlineClient):
    def __init__(self, latency_s: float = 0.15, offline: bool = False):
        self.latency_s = latency_s
        self.offline = offline

    def book(self, candidate: RebookingCandidate) -> MockBookingConfirmation:
        time.sleep(self.latency_s)
        if self.offline:
            raise VendorOfflineError("Amadeus is down")
        if candidate.seats_available <= 0:
            raise SeatUnavailableError(f"No seats left on {candidate.flight_no}")
        return MockBookingConfirmation(reference=f"CNF-{uuid.uuid4().hex[:8].upper()}")

class FallbackAirlineClient(AirlineClient):
    def __init__(self, latency_s: float = 0.2):
        self.latency_s = latency_s

    def book(self, candidate: RebookingCandidate) -> MockBookingConfirmation:
        time.sleep(self.latency_s)
        if candidate.seats_available <= 0:
            raise SeatUnavailableError(f"No seats left on {candidate.flight_no}")
        return MockBookingConfirmation(reference=f"FB-{uuid.uuid4().hex[:8].upper()}")


class MockHotelClient(HotelClient):
    def __init__(self, latency_s: float = 0.1):
        self.latency_s = latency_s

    def adjust_dates(self, hotel: HotelBooking, new_arrival: datetime) -> HotelBooking:
        time.sleep(self.latency_s)
        # keep the same length of stay, shift check-in to match new arrival
        stay_length = hotel.check_out - hotel.check_in
        new_check_in = new_arrival.replace(hour=15, minute=0)  # standard check-in time
        return HotelBooking(hotel.name, hotel.city, new_check_in, new_check_in + stay_length)


class MockNotificationSender(NotificationSender):
    def __init__(self, latency_s: float = 0.1):
        self.latency_s = latency_s
        self.sent: list[tuple[str, str]] = []

    def send(self, title: str, message: str) -> None:
        time.sleep(self.latency_s)
        self.sent.append((title, message))
