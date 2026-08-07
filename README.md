# Мониторинг стоимости поездок между пятью ключевыми городами

Ежедневный Market Snapshot стоимости поездок Москва / Санкт-Петербург / Сочи /
Самара / Казань на горизонте 30 будущих календарных дней, с управленческим
дашбордом, сквозным provenance и историческим слоем.

**Версия спецификации:** SCOPE-R 1.0 (07.08.2026)
**Статус:** MVP v2

---

## Одно предложение о системе

Ночью система заново измеряет рынок по фиксированной матрице из **15 840
логических наблюдений**, сохраняет исходные ответы источников, нормализует их в
Offers, применяет версионируемую методику расчёта и к 10:00 MSK публикует
витрину, в которой **любую цифру можно раскрыть до конкретного предложения
конкретного источника**.

---

## Главный принцип

> Никакая неправильная, неполная или технически сомнительная цифра не должна
> молча попасть на управленческий дашборд.

Отсюда всё остальное: `NO_MARKET` отличается от сбоя, частичная выборка
помечается, исключённое предложение не удаляется, а сохраняется с причиной
исключения, и дашборд никогда не ходит в источники.

---

## Быстрый старт

```bash
docker compose up -d --build
```

| Что | Адрес |
|---|---|
| Дашборд | http://localhost:8080 |
| API + OpenAPI | http://localhost:8081/docs |
| Health | http://localhost:8081/api/v1/health |

Демонстрационный снимок без обращения к источникам:

```bash
docker compose exec api python -m tmo.cli demo-snapshot --days 3
```

Боевой сбор за сегодня:

```bash
docker compose exec api python -m tmo.cli collect --snapshot-date today
```

Подробности — в [RUNBOOK](docs/RUNBOOK.md).

---

## Слои

```text
Sources → Connectors → Raw → Normalization → Offer → Market Snapshot
       → Selection → Aggregation → Calculation → Data Mart → REST API → React Dashboard
```

Правила границ: коннектор не знает бизнес-методику, Calculation Engine не знает
source-specific деталей, фронтенд не содержит расчётной логики.

---

## Карта документации

| Документ | О чём |
|---|---|
| [PHASE0_AUDIT.md](docs/PHASE0_AUDIT.md) | Аудит первой версии, классификация компонентов |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Целевая архитектура v2 |
| [DATA_CONTRACT.md](docs/DATA_CONTRACT.md) | Сущности, поля, инварианты |
| [CALCULATION_METHODOLOGY.md](docs/CALCULATION_METHODOLOGY.md) | Как из предложений получается цифра |
| [COLLECTION_CAPACITY_ANALYSIS.md](docs/COLLECTION_CAPACITY_ANALYSIS.md) | Укладывается ли суточный цикл в SLA |
| [COLLECTION_RELIABILITY.md](COLLECTION_RELIABILITY.md) | Что ломается по ночам |
| [SOURCES_PLAYBOOK.md](SOURCES_PLAYBOOK.md) | Поведение Туту и РЖД |
| [DATA_PIPELINE_PLAYBOOK.md](DATA_PIPELINE_PLAYBOOK.md) | Где число начинает врать |
| [RUNBOOK.md](docs/RUNBOOK.md) | Эксплуатация |
| [USER_GUIDE.md](docs/USER_GUIDE.md) | Как читать дашборд |
| [LIMITATIONS.md](docs/LIMITATIONS.md) | Чего система не утверждает |
| [HANDOFF.md](HANDOFF.md) | Состояние работ, точка входа для продолжения |
