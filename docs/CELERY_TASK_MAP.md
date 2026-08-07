# Карта задач Celery

---

## Очереди

| Очередь | Воркер | Что в ней |
|---|---|---|
| `collect` | `worker` | Сбор: сеть, ожидание темпа, бюджеты |
| `compute` | `worker` | Расчёт и финализация |
| `maintenance` | `worker-service` | Сторож, досбор, ретеншен, сводка |
| `ondemand` | `worker-service` | Разовые запросы пользователя |

Второй воркер свободен по построению. Один воркер, обслуживающий все очереди
сразу, не выполняет собственного лечения: забитый сбором пул не запускает ни
сторожа, ни досбор.

---

## Задачи

| Задача | Очередь | Жёсткий предел | Что делает |
|---|---|---:|---|
| `tmo.open_snapshot` | `collect` | 15 мин | Создаёт снимок и детерминированный план на сутки |
| `tmo.collect_family` | `collect` | 6 ч | Собирает одно семейство наблюдений пачками |
| `tmo.collect_batch` | `collect` | 5 мин | Одна пачка: план → сеть → запись |
| `tmo.recover_snapshot` | `maintenance` | 3 ч | Досбор технических дыр с солью в ключе |
| `tmo.calculate_snapshot` | `compute` | 2 ч | Применяет методику, создаёт `CalculationRun` |
| `tmo.recalculate_snapshot` | `compute` | 2 ч | Пересчёт другой версией; прежний расчёт не трогает |
| `tmo.finalize_snapshot` | `compute` | 30 мин | Покрытие → ворота → статус публикации |
| `tmo.watch_collection_progress` | `maintenance` | 5 мин | Сторож застоя, каждые 5 минут |
| `tmo.purge_raw` | `maintenance` | 1 ч | Ретеншен тел сырых ответов, 45 суток |
| `tmo.daily_quality_digest` | `maintenance` | 15 мин | Суточная сводка качества |

---

## Расписание

```text
00:30  tmo.open_snapshot
01:00  tmo.collect_family RAIL
02:00  tmo.collect_family AIR
05:00  tmo.collect_family HOTEL
08:00  tmo.recover_snapshot
09:00  tmo.calculate_snapshot
09:30  tmo.finalize_snapshot
10:00  tmo.daily_quality_digest
11:00  tmo.purge_raw
*/5    tmo.watch_collection_progress
```

Окна разнесены по семействам: одновременный залп источник не держит — именно
он открывал размыкатель цепи. Каждое окно шире ожидаемой длительности, чтобы
затянувшийся прогон не получил следующий себе в спину.

---

## Бюджеты времени

У каждой пачки два предела, и мягкий строго меньше жёсткого:

```text
мягкий бюджет   240 с   обход прекращается сам, собранное сохраняется, PARTIAL
жёсткий предел  300 с   последняя страховка, в норме не срабатывает
```

Жёсткий предел снимает задачу целиком **вместе с уже полученными данными** —
поэтому он последняя страховка, а не основной механизм.

Бюджет одного наблюдения не может превышать остаток бюджета пачки: одно
затянувшееся наблюдение не должно съедать всю пачку.

---

## Настройки, влияющие на поведение

| Настройка | Умолчание | Смысл |
|---|---|---|
| `task_acks_late` | `true` | При перезапуске пула задача возвращается в очередь, а не теряется |
| `task_reject_on_worker_lost` | `true` | То же при падении воркера |
| `worker_prefetch_multiplier` | `1` | Длинные задачи не застревают за чужой очередью |
| `worker_max_tasks_per_child` | `200` | Защита от накопления состояния в процессе |
| `TMO_WORKER_CONCURRENCY` | `8` | Потоков в пуле сбора |
| `TMO_TUTU_CONCURRENCY` | `8` | Одновременных обращений к Туту (при 12 получен `503`) |
| `TMO_RZD_CONCURRENCY` | `4` | Одновременных обращений к РЖД |

Пул потоков, а не процессов: работа ждёт сети, а не процессора.

---

## Запуск руками

```bash
docker compose exec worker-service celery -A tmo.tasks.celery_app:celery_app call tmo.recover_snapshot
docker compose exec worker-service celery -A tmo.tasks.celery_app:celery_app call tmo.daily_quality_digest
```

Либо через CLI, что нагляднее при расследовании:

```bash
docker compose exec api python -m tmo.cli collect --snapshot-date today
docker compose exec api python -m tmo.cli coverage --snapshot-date today
```

---

## Проверка живости

```bash
docker compose exec worker python -m tmo.tasks.health; echo "код возврата: $?"
```

Проверка спрашивает не «отвечаешь ли ты», а **разбирается ли очередь**.
`celery inspect ping` отвечает из главного процесса и проходит исправно тогда,
когда все процессы пула заняты намертво: пинг проходит, работа стоит.

Пустая очередь считается здоровьем: разбирать нечего, и молчание законно. Так
же трактуется отсутствие снимка — свежая установка не должна падать сразу
после развёртывания.
