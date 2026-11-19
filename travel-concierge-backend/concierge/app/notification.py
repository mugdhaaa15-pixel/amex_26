"""
Notification agent — Task 3 (data half) + Task 4 (notification API):
composes a clear, specific message and hands it to a push/SMS provider.
"""
from __future__ import annotations
from datetime import datetime, timezone

from .models import Itinerary, DisruptionReport, RebookingCandidate, ExecutionResult, Notification


class NotificationSender:
    """Interface a real push/SMS/email provider must satisfy."""
    def send(self, title: str, message: str) -> None:
        raise NotImplementedError


class NotificationAgent:
    def __init__(self, sender: NotificationSender):
        self.sender = sender

    def notify(
        self,
        itinerary: Itinerary,
        disruption: DisruptionReport,
        chosen: RebookingCandidate,
        execution_result: ExecutionResult,
    ) -> Notification:
        message = self._compose(disruption, chosen, execution_result)
        title = "Trip updated automatically"
        self.sender.send(title, message)
        return Notification(title=title, message=message, sent_at=datetime.now(timezone.utc))

    def _compose(
        self,
        disruption: DisruptionReport,
        chosen: RebookingCandidate,
        execution_result: ExecutionResult,
    ) -> str:
        parts = [f"{disruption.detail}."]
        parts.append(
            f"You're now booked on {chosen.flight_no} ({chosen.origin}\u2192{chosen.destination}), "
            f"confirmation {execution_result.booking_ref}."
        )
        if execution_result.new_hotel_check_in:
            parts.append(
                f"Your hotel check-in was moved to {execution_result.new_hotel_check_in.strftime('%b %d, %H:%M')}."
            )
        parts.append("Reply within 10 minutes to choose a different option instead.")
        return " ".join(parts)
