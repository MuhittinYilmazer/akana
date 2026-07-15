"""ScheduleEngine — one-shot reminders + recurring scheduled prompts.

Akana can create schedules (from the assistant via tools, or the user via REST/
UI); when one comes due the background engine runs an LLM turn with the
schedule's prompt and delivers the result to a chat thread and/or a connector.

Public surface:

* :class:`~akana_server.schedule.model.ScheduleItem` / ``Delivery`` — the record.
* :class:`~akana_server.schedule.store.ScheduleStore` — atomic CRUD + recurrence.
* :func:`~akana_server.schedule.engine.start_schedule_engine` /
  ``stop_schedule_engine`` — the background poll loop lifecycle.
* :class:`~akana_server.schedule.tools.ScheduleTools` — the model-facing tools.
"""

from __future__ import annotations

from akana_server.schedule.model import Delivery, ScheduleItem
from akana_server.schedule.store import (
    ScheduleStore,
    ScheduleValidationError,
    get_schedule_store,
)

__all__ = [
    "Delivery",
    "ScheduleItem",
    "ScheduleStore",
    "ScheduleValidationError",
    "get_schedule_store",
]
