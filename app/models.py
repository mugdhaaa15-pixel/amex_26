"""
Domain models for the Autonomous Travel-Disruption Concierge.

These are deliberately transport-agnostic: nothing here knows about
Amadeus, Sabre, or any specific airline API. A real integration swaps
out app/data.py's mock clients for real API clients that produce/consume
these same dataclasses.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class FlightState(str, Enum):
    ON_TIME = "on_time"
    DELAYED = "delayed"
    CANCELLED = "cancelled"


@dataclass
class FlightStatusEvent:
    """A single update from a live flight-status feed."""
    flight_no: str
    origin: str
    destination: str
    scheduled_departure: datetime
    scheduled_arrival: datetime
    state: FlightState
    delay_minutes: int = 0
    actual_arrival: datetime | None = None
    confidence_score: float = 1.0

    def projected_arrival(self) -> datetime:
        return self.actual_arrival or (self.scheduled_arrival + timedelta(minutes=self.delay_minutes))


@dataclass
class Leg:
    """One flight segment in a cardmember's itinerary."""
    flight_no: str
    origin: str
    destination: str
    scheduled_departure: datetime
    scheduled_arrival: datetime
    cabin: str          # "economy" | "premium_economy" | "business" | "first"
    fare_paid: float


@dataclass
class HotelBooking:
    name: str
    city: str
    check_in: datetime
    check_out: datetime


@dataclass
class CardmemberPolicy:
    """Autonomous-rebooking guardrails tied to the card tier."""
    tier: str
    max_fare_delta: float              # dollars the agent may spend above the original fare
    allow_cabin_downgrade: bool        # may it drop the cardmember a cabin class
    max_extra_layover_minutes: int     # longest single layover it may accept on a connecting option
    max_arrival_delay_minutes: int     # how late vs. the original schedule is still acceptable
    strictness_profile: str = "balanced" # "strict", "balanced", "flexible"


@dataclass
class Itinerary:
    cardmember: str
    policy: CardmemberPolicy
    legs: list[Leg]
    hotel: HotelBooking | None = None


@dataclass
class RebookingCandidate:
    """One alternative flight the decision agent can consider."""
    flight_no: str
    origin: str
    destination: str
    departure: datetime
    arrival: datetime
    cabin: str
    fare_delta: float          # positive = costs more than original, negative = credit
    stops: int
    layover_minutes: int = 0   # 0 for direct flights
    seats_available: int = 9
    idempotency_key: str | None = None


@dataclass
class DisruptionReport:
    kind: str            # "cancellation" | "missed_connection" | "major_delay"
    leg_index: int        # index of the leg where the problem was detected
    detail: str
    buffer_minutes: float | None = None
    affected_leg_index: int = -1  # index of the leg that actually needs rebooking
    urgency_level: str = "high"   # "high", "medium", "low"

    def __post_init__(self):
        if self.affected_leg_index == -1:
            self.affected_leg_index = self.leg_index


CABIN_RANK = {"economy": 0, "premium_economy": 1, "business": 2, "first": 3}


def cabin_rank(cabin: str) -> int:
    return CABIN_RANK.get(cabin, 0)


@dataclass
class ScoredCandidate:
    candidate: RebookingCandidate
    score: float                # lower is better
    within_policy: bool
    violations: list[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    success: bool
    booking_ref: str | None = None
    reason: str | None = None
    new_hotel_check_in: datetime | None = None


@dataclass
class Notification:
    title: str
    message: str
    sent_at: datetime


@dataclass
class AuditLogEntry:
    stage: str
    action: str
    rationale: str
    timestamp: datetime = field(default_factory=datetime.now)

@dataclass
class AuditLog:
    entries: list[AuditLogEntry] = field(default_factory=list)
    
    def record(self, stage: str, action: str, rationale: str):
        self.entries.append(AuditLogEntry(stage, action, rationale))
