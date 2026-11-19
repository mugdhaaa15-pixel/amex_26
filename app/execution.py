"""
Execution agent — Task 2 (action half) + Task 4: "executes flight and
hotel changes within policy limits" / "Integrate with airline, hotel,
and notification APIs to complete end-to-end travel rearrangements
without manual intervention."

This agent never talks to a specific vendor SDK directly. It depends on
two small interfaces, AirlineClient and HotelClient, which it calls
through. app/data.py provides mock implementations (simulated latency,
occasional failure, idempotent retry) for this prototype; a production
deployment swaps those for real Amadeus/Sabre and hotel-supplier
clients without touching this file.
"""
from __future__ import annotations
from dataclasses import replace

from .models import Itinerary, DisruptionReport, RebookingCandidate, ExecutionResult, Leg


class VendorOfflineError(Exception):
    pass

class SeatUnavailableError(Exception):
    pass

class AirlineClient:
    """Interface a real airline/GDS client must satisfy."""
    def book(self, candidate: RebookingCandidate) -> "BookingConfirmation":
        raise NotImplementedError

class AirlineGateway(AirlineClient):
    """Wraps multiple vendor clients to provide automatic failover."""
    def __init__(self, primary: AirlineClient, fallback: AirlineClient):
        self.primary = primary
        self.fallback = fallback

    def book(self, candidate: RebookingCandidate) -> "BookingConfirmation":
        try:
            return self.primary.book(candidate)
        except VendorOfflineError:
            return self.fallback.book(candidate)


class HotelClient:
    """Interface a real hotel-supplier client must satisfy."""
    def adjust_dates(self, hotel, new_check_in):
        raise NotImplementedError


class ExecutionAgent:
    def __init__(self, airline_client: AirlineClient, hotel_client: HotelClient, max_retries: int = 2):
        self.airline_client = airline_client
        self.hotel_client = hotel_client
        self.max_retries = max_retries
        self._processed_keys = set()

    def execute(
        self,
        itinerary: Itinerary,
        disruption: DisruptionReport,
        chosen: RebookingCandidate,
    ) -> ExecutionResult:
        # Idempotency check
        if chosen.idempotency_key and chosen.idempotency_key in self._processed_keys:
            return ExecutionResult(success=False, reason="duplicate_execution_attempt")

        confirmation = None
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                confirmation = self.airline_client.book(chosen)
                break
            except SeatUnavailableError:
                return ExecutionResult(success=False, reason="seat_unavailable")
            except VendorOfflineError as exc:
                # If Gateway is used, it catches this internally. If it leaks here, both failed.
                last_error = "vendor_offline"
                break
            except Exception as exc:  # simulated transient booking failure
                last_error = str(exc)
                continue

        if confirmation is None:
            return ExecutionResult(success=False, reason=f"airline booking failed after retries: {last_error}")

        if chosen.idempotency_key:
            self._processed_keys.add(chosen.idempotency_key)

        # Replace the affected leg in the itinerary with the confirmed alternative
        idx = disruption.affected_leg_index
        itinerary.legs[idx] = Leg(
            flight_no=chosen.flight_no,
            origin=chosen.origin,
            destination=chosen.destination,
            scheduled_departure=chosen.departure,
            scheduled_arrival=chosen.arrival,
            cabin=chosen.cabin,
            fare_paid=itinerary.legs[idx].fare_paid + chosen.fare_delta,
        )

        new_check_in = None
        if itinerary.hotel is not None:
            updated_hotel = self.hotel_client.adjust_dates(itinerary.hotel, chosen.arrival)
            itinerary.hotel = updated_hotel
            new_check_in = updated_hotel.check_in

        return ExecutionResult(success=True, booking_ref=confirmation.reference, new_hotel_check_in=new_check_in)
