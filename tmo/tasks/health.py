"""Проверка живости воркера.

Спрашивает не «отвечаешь ли ты», а **разбирается ли очередь**. ``celery
inspect ping`` отвечает из главного процесса и проходит исправно тогда, когда
все процессы пула заняты намертво: пинг проходит, работа стоит.

Запускается как ``python -m tmo.tasks.health``; код возврата 0 — здоров.
"""

from __future__ import annotations

import sys
from datetime import timedelta

from sqlalchemy import func, select

from tmo.core.config import get_settings
from tmo.core.enums import JobStatus
from tmo.core.timeutil import now_utc, operational_date_for
from tmo.db import models
from tmo.db.session import session_scope

#: Наблюдения, которые действительно отданы в очередь. ``PLANNED`` сюда не
#: входит намеренно: расписание разносит семейства по ночи (авиа в 21:00, ЖД в
#: 05:15, проживание в 05:30), и между ними тысячи наблюдений часами лежат
#: запланированными. Считать их незавершённой работой значит объявлять застоем
#: паузу, заложенную в расписание, — и получать перезапуск воркера от autoheal
#: ровно к моменту, когда следующему семейству пора начинать.
IN_FLIGHT_STATUSES = (JobStatus.DISPATCHED.value, JobStatus.RUNNING.value)


def check() -> tuple[bool, str]:
    settings = get_settings()
    threshold = timedelta(minutes=settings.stall_threshold_minutes)

    with session_scope() as session:
        snapshot_id = session.scalar(
            select(models.MarketSnapshot.id)
            .where(models.MarketSnapshot.snapshot_date == operational_date_for())
            .order_by(models.MarketSnapshot.attempt_no.desc())
            .limit(1)
        )
        if snapshot_id is None:
            # Свежая установка не должна падать сразу после развёртывания.
            return True, "Снимка за сегодня нет: разбирать нечего"

        pending = session.scalar(
            select(func.count(models.CollectionJob.id)).where(
                models.CollectionJob.snapshot_id == snapshot_id,
                models.CollectionJob.status.in_(IN_FLIGHT_STATUSES),
            )
        )
        if not pending:
            return True, "В работе ничего нет"

        last_completion = session.scalar(
            select(func.max(models.CollectionJob.completed_at)).where(
                models.CollectionJob.snapshot_id == snapshot_id
            )
        )

    if last_completion is None:
        return True, "Сбор ещё не начинал завершать наблюдения"

    idle = now_utc() - last_completion
    if idle > threshold:
        return False, (
            f"В работе {pending} наблюдений, но завершений нет "
            f"{idle.total_seconds() / 60:.0f} мин при пороге "
            f"{settings.stall_threshold_minutes} мин"
        )
    return True, f"Очередь разбирается: последнее завершение {idle.total_seconds() / 60:.1f} мин назад"


def main() -> int:
    healthy, message = check()
    print(message)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
