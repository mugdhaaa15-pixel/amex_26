"""
Monitor agent — Task 1: "Design an algorithm to monitor live flight data
and detect disruptions such as cancellations or missed connections."

Detection logic (real, not simulated):
  1. Cancellation  — any leg's live status flips to CANCELLED.
  2. Missed connection — a leg is delayed enough that the buffer before
     the next leg's scheduled departure drops below that airport's
     minimum connection time (MCT). This is the same buffer math airlines
     use for IROPS (irregular-operations) rebooking triggers.
  3. Major delay — the first leg is delayed beyond the cardmember's
     policy tolerance, even if no downstream connection is at risk yet
     (worth flagging proactively rather than waiting for it to cascade).

The itinerary's legs are assumed chronological; the earliest disruption
found is returned so downstream agents always act on the first problem
in the chain.
"""
from __future__ import annotations
from datetime import timedelta

from .models import Itinerary, FlightStatusEvent, FlightState, DisruptionReport

# Minimum connection time by airport, in minutes. A real system would
# pull this from an airline/airport reference API; here it's a static
# table standing in for that lookup.
MCT_MINUTES = {
    "LHR": 75,
    "CDG": 60,
    "JFK": 60,
    "ORD": 60,
    "AMS": 50,
    "AUH": 60,
    "default": 60,
}


class MonitorAgent:
    def __init__(self, mct_table: dict[str, int] | None = None):
        self.mct_table = mct_table or MCT_MINUTES

    def evaluate(
        self,
        itinerary: Itinerary,
        events: dict[str, FlightStatusEvent],
    ) -> DisruptionReport | None:
        for i, leg in enumerate(itinerary.legs):
            event = events.get(leg.flight_no)
            if event is None:
                continue  # no live update for this leg yet

            if event.state == FlightState.CANCELLED:
                return DisruptionReport(
                    kind="cancellation",
                    leg_index=i,
                    detail=f"{leg.flight_no} ({leg.origin}\u2192{leg.destination}) was cancelled",
                )

            if event.state == FlightState.DELAYED:
                projected_arrival = event.projected_arrival()

                # Check downstream connection buffer
                if i + 1 < len(itinerary.legs):
                    next_leg = itinerary.legs[i + 1]
                    mct = self.mct_table.get(leg.destination, self.mct_table["default"])
                    buffer_minutes = (next_leg.scheduled_departure - projected_arrival).total_seconds() / 60
                    if buffer_minutes < mct:
                        return DisruptionReport(
                            kind="missed_connection",
                            leg_index=i,
                            detail=(
                                f"{leg.flight_no} delayed {event.delay_minutes}m — only "
                                f"{buffer_minutes:.0f}m to connect to {next_leg.flight_no} "
                                f"at {leg.destination} (needs {mct}m)"
                            ),
                            buffer_minutes=buffer_minutes,
                            affected_leg_index=i + 1,
                        )

                # No connection at risk — is the delay itself over tolerance?
                if event.delay_minutes >= itinerary.policy.max_arrival_delay_minutes:
                    return DisruptionReport(
                        kind="major_delay",
                        leg_index=i,
                        detail=f"{leg.flight_no} delayed {event.delay_minutes}m, beyond policy tolerance",
                    )

        return None
