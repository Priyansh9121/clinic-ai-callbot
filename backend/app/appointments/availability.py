"""Availability search.

Read-only. Returns slots that are genuinely bookable *right now*; the booking
service re-verifies under a row lock, because anything read here can be taken
by another caller microseconds later.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.metrics import M, metrics
from app.core.timeutils import DayPeriod, day_bounds, period_bounds, utc_now
from app.database.models.appointment_type import AppointmentType
from app.database.models.availability import AvailabilitySlot
from app.database.models.enums import SlotStatus
from app.database.models.practitioner import Practitioner


@dataclass(frozen=True, slots=True)
class AvailabilityQuery:
    """A validated availability request.

    Constructed by the API layer or the conversation engine — never directly
    from model output. Free-text date expressions are resolved to a concrete
    ``day`` before reaching this point.
    """

    clinic_id: uuid.UUID
    appointment_type_id: uuid.UUID | None = None
    practitioner_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    day: date | None = None
    period: DayPeriod = DayPeriod.ANY
    date_from: datetime | None = None
    date_to: datetime | None = None
    limit: int = 20


class AvailabilityService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    def _window(self, query: AvailabilityQuery, tz: ZoneInfo) -> tuple[datetime, datetime]:
        """Resolve the query into an explicit UTC [start, end) window.

        The lower bound is never earlier than 'now + booking lead time', so a
        slot that has already started can never be offered.
        """
        earliest = utc_now() + timedelta(minutes=self._settings.booking_lead_minutes)

        if query.day is not None:
            if query.period is DayPeriod.ANY:
                start, end = day_bounds(query.day, tz)
            else:
                start, end = period_bounds(query.day, query.period, tz)
        elif query.date_from is not None and query.date_to is not None:
            start, end = query.date_from, query.date_to
        else:
            start = earliest
            end = start + timedelta(days=self._settings.availability_search_days)

        return max(start, earliest), end

    def _base_query(self, query: AvailabilityQuery, start: datetime, end: datetime) -> Select:
        stmt = (
            select(AvailabilitySlot)
            .join(Practitioner, Practitioner.id == AvailabilitySlot.practitioner_id)
            .join(
                AppointmentType,
                AppointmentType.id == AvailabilitySlot.appointment_type_id,
            )
            .where(
                Practitioner.clinic_id == query.clinic_id,
                Practitioner.active.is_(True),
                AppointmentType.active.is_(True),
                AvailabilitySlot.status == SlotStatus.AVAILABLE,
                AvailabilitySlot.start_time >= start,
                AvailabilitySlot.start_time < end,
            )
            .order_by(AvailabilitySlot.start_time, AvailabilitySlot.id)
            .limit(min(query.limit, 100))
        )
        if query.appointment_type_id:
            stmt = stmt.where(AvailabilitySlot.appointment_type_id == query.appointment_type_id)
        if query.practitioner_id:
            stmt = stmt.where(AvailabilitySlot.practitioner_id == query.practitioner_id)
        if query.location_id:
            stmt = stmt.where(AvailabilitySlot.location_id == query.location_id)
        return stmt

    async def search(self, query: AvailabilityQuery, tz: ZoneInfo) -> list[AvailabilitySlot]:
        start, end = self._window(query, tz)
        if end <= start:
            return []
        with metrics.timer(M.AVAILABILITY_SEARCH):
            result = await self._session.execute(self._base_query(query, start, end))
        return list(result.scalars().unique().all())

    async def search_with_fallback(
        self, query: AvailabilityQuery, tz: ZoneInfo
    ) -> tuple[list[AvailabilitySlot], bool]:
        """Search, then widen once if the caller's exact preference is empty.

        Returns ``(slots, is_exact)``. ``is_exact`` is False when the result
        came from the widened search, which the conversation engine must say
        out loud ("nothing Friday afternoon, but I have Friday morning") rather
        than quietly offering something the caller did not ask for.
        """
        slots = await self.search(query, tz)
        if slots:
            return slots, True

        # Widen 1: same day, any period.
        if query.day is not None and query.period is not DayPeriod.ANY:
            widened = AvailabilityQuery(
                clinic_id=query.clinic_id,
                appointment_type_id=query.appointment_type_id,
                practitioner_id=query.practitioner_id,
                location_id=query.location_id,
                day=query.day,
                period=DayPeriod.ANY,
                limit=query.limit,
            )
            slots = await self.search(widened, tz)
            if slots:
                return slots, False

        # Widen 2: forward from the requested day across the search window.
        if query.day is not None:
            start = day_bounds(query.day, tz)[0]
            widened = AvailabilityQuery(
                clinic_id=query.clinic_id,
                appointment_type_id=query.appointment_type_id,
                practitioner_id=query.practitioner_id,
                location_id=query.location_id,
                period=query.period,
                date_from=start,
                date_to=start + timedelta(days=self._settings.availability_search_days),
                limit=query.limit,
            )
            slots = await self.search(widened, tz)
            if slots:
                # Honour the requested period within the widened date range.
                if query.period is not DayPeriod.ANY:
                    from app.core.timeutils import period_of

                    filtered = [s for s in slots if period_of(s.start_time, tz) == query.period]
                    if filtered:
                        return filtered, False
                return slots, False

        return [], False

    async def get_slot(self, slot_id: uuid.UUID) -> AvailabilitySlot | None:
        return await self._session.get(AvailabilitySlot, slot_id)
