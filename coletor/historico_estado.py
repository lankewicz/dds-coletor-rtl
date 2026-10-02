"""Estado recuperável da varredura mensal do histórico."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from .storage import load_json, write_json


@dataclass
class MonthlyCollectionState:
    path: Path
    month: str
    company_key: str
    timezone: ZoneInfo
    completed_days: set[str] = field(default_factory=set)
    attempts_by_day: dict[str, int] = field(default_factory=dict)
    errors_by_day: dict[str, str] = field(default_factory=dict)
    status: str = "new"

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        month: str,
        company_key: str,
        timezone: ZoneInfo,
    ) -> "MonthlyCollectionState":
        payload = load_json(path, {})
        return cls(
            path=path,
            month=month,
            company_key=company_key,
            timezone=timezone,
            completed_days=set(payload.get("completedDays") or []),
            attempts_by_day=dict(payload.get("attemptsByDay") or {}),
            errors_by_day=dict(payload.get("errorsByDay") or {}),
            status=str(payload.get("status") or "new"),
        )

    def pending_days(self, year: int, month: int, total_days: int) -> list[str]:
        return [
            datetime.date(year, month, day).isoformat()
            for day in range(1, total_days + 1)
            if datetime.date(year, month, day).isoformat() not in self.completed_days
        ]

    def record_attempt(self, day: str) -> None:
        self.attempts_by_day[day] = int(self.attempts_by_day.get(day) or 0) + 1

    def mark_success(self, day: str) -> None:
        self.completed_days.add(day)
        self.errors_by_day.pop(day, None)

    def mark_failure(self, day: str, error: Exception) -> None:
        self.errors_by_day[day] = str(error)

    def save(self, status: str, *, year: int, month: int, total_days: int) -> None:
        self.status = status
        pending = self.pending_days(year, month, total_days)
        write_json(self.path, {
            "schemaVersion": 1,
            "month": self.month,
            "companyKey": self.company_key,
            "status": status,
            "completedDays": sorted(self.completed_days),
            "pendingDays": pending,
            "attemptsByDay": self.attempts_by_day,
            "errorsByDay": self.errors_by_day,
            "nextRetryDate": (
                (datetime.datetime.now(self.timezone).date() + datetime.timedelta(days=1)).isoformat()
                if pending else None
            ),
            "updatedAt": datetime.datetime.now(self.timezone).isoformat(),
        })
