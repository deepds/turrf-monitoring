#!/bin/sh
# Точка входа контейнера. Роль задаётся первым аргументом.
#
# Миграции применяет только API: несколько процессов, одновременно
# накатывающих схему, дают гонку на alembic_version.

set -eu

role="${1:-api}"

wait_for_db() {
  echo "Ожидание базы…"
  for _ in $(seq 1 60); do
    if python -c "
import sys
from sqlalchemy import create_engine, text
from tmo.core.config import get_settings
try:
    create_engine(get_settings().database_url).connect().execute(text('select 1'))
except Exception:
    sys.exit(1)
" 2>/dev/null; then
      echo "База доступна"
      return 0
    fi
    sleep 2
  done
  echo "База недоступна: выходим" >&2
  exit 1
}

case "$role" in
  api)
    wait_for_db
    echo "Применение миграций…"
    alembic upgrade head
    exec uvicorn tmo.api.app:app --host 0.0.0.0 --port 8000 --proxy-headers --no-server-header
    ;;
  worker)
    wait_for_db
    # Сбор и расчёт: всё длинное и сетевое. Пул потоков, потому что работа
    # ждёт сети, а не процессора.
    exec celery -A tmo.tasks.celery_app:celery_app worker \
      --queues collect,compute --concurrency "${TMO_WORKER_CONCURRENCY:-4}" \
      --pool threads --loglevel INFO --hostname worker@%h
    ;;
  worker-service)
    wait_for_db
    # Обслуживание и запросы пользователя. Свободен по построению: сторож и
    # досбор обязаны работать, когда сбор занят целиком.
    exec celery -A tmo.tasks.celery_app:celery_app worker \
      --queues maintenance,ondemand --concurrency 2 \
      --pool threads --loglevel INFO --hostname service@%h
    ;;
  beat)
    wait_for_db
    exec celery -A tmo.tasks.celery_app:celery_app beat --loglevel INFO
    ;;
  health)
    exec python -m tmo.tasks.health
    ;;
  cli)
    shift
    exec python -m tmo.cli "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
