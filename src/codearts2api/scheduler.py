"""In-process check-in scheduler (Asia/Shanghai by default)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

CheckinJob = Callable[[], Awaitable[Any]]


class CheckinScheduler:
    def __init__(
        self,
        *,
        cron: str,
        timezone: str,
        job: CheckinJob,
    ) -> None:
        self.cron = cron
        self.timezone_name = timezone
        self._job = job
        self._scheduler = AsyncIOScheduler(timezone=ZoneInfo(timezone))
        self.last_run_at: str | None = None
        self.next_run_at: str | None = None
        self.last_error: str | None = None
        self.running = False

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.configure(timezone=ZoneInfo(self.timezone_name))
            self._scheduler.start()
        self._ensure_job()
        self.running = True
        self._refresh_next_run()
        logger.info(
            "Check-in scheduler started cron=%s timezone=%s next_run=%s",
            self.cron,
            self.timezone_name,
            self.next_run_at,
        )

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        self.running = False
        self.next_run_at = None
        logger.info("Check-in scheduler stopped")

    def reschedule(self, *, cron: str | None = None, timezone: str | None = None) -> None:
        """Update cron/timezone and refresh the job without restarting the process."""
        if cron is not None:
            self.cron = cron
        if timezone is not None and timezone != self.timezone_name:
            self.timezone_name = timezone
        if not self.running:
            return
        if not self._scheduler.running:
            self.start()
            return
        self._ensure_job()
        self._refresh_next_run()
        logger.info(
            "Check-in scheduler rescheduled cron=%s timezone=%s next_run=%s",
            self.cron,
            self.timezone_name,
            self.next_run_at,
        )

    def set_job(self, job: CheckinJob) -> None:
        self._job = job

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            if self.running and self._scheduler.running:
                self._ensure_job()
                self._refresh_next_run()
                return
            # A shutdown AsyncIOScheduler cannot be started again.
            if not self._scheduler.running:
                self._scheduler = AsyncIOScheduler(timezone=ZoneInfo(self.timezone_name))
            self.start()
            return
        if self._scheduler.running and self._scheduler.get_job("daily_checkin"):
            self._scheduler.remove_job("daily_checkin")
        self.running = False
        self.next_run_at = None
        logger.info("Check-in scheduler paused")

    def _ensure_job(self) -> None:
        trigger = CronTrigger.from_crontab(self.cron, timezone=ZoneInfo(self.timezone_name))
        self._scheduler.add_job(
            self._run_safe,
            trigger=trigger,
            id="daily_checkin",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    async def _run_safe(self) -> None:
        tz = ZoneInfo(self.timezone_name)
        self.last_run_at = datetime.now(tz).isoformat()
        try:
            await self._job()
            self.last_error = None
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            logger.exception("Scheduled check-in failed")
        finally:
            self._refresh_next_run()

    def _refresh_next_run(self) -> None:
        job = self._scheduler.get_job("daily_checkin") if self._scheduler.running else None
        if job and job.next_run_time is not None:
            self.next_run_at = job.next_run_time.isoformat()
        else:
            self.next_run_at = None

    def snapshot(self) -> dict[str, Any]:
        if self.running:
            self._refresh_next_run()
        return {
            "enabled": self.running,
            "cron": self.cron,
            "timezone": self.timezone_name,
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "last_error": self.last_error,
        }
