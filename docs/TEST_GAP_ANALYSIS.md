# Test Gap Analysis

**Дата:** 07.08.2026
**Сравнивается:** набор тестов v1 (22 файла) против требований SCOPE-R E1

---

## 1. Принцип отбора

Тест имеет ценность, если он ловит **дефект, который однажды дал неверную
цифру**. Тест, проверяющий, что функция возвращает то, что написано в функции,
ценности не имеет и в новый набор не переносится.

Второй принцип: **тест обязан проверять боевой код**. Golden Dataset разбирает
записанный ответ тем же парсером, что и ночной прогон; заглушка вместо парсера
превратила бы набор в проверку заглушки.

---

## 2. Что перенесено из v1 и почему

| Тест v1 | Какой дефект защищает | Где в v2 |
|---|---|---|
| `test_flight_fare_selection` | Тарифная сетка выдавалась за предложения: 30 рейсов → 136 «предложений», медиана на третьем тарифе | `tests/test_selection.py::test_fare_collapse_keeps_one_row_per_itinerary` |
| `test_rail_market_scope` | Круговой ЖД как cartesian product плеч | `tests/test_planner.py::test_rail_is_observed_per_leg` |
| `test_tutu_rail_classes` | Класс вагона выводился из кода обслуживания по первому символу: `2В` и `2С` — сидячие, а не купе | `tests/test_connector_contracts.py::test_rail_car_type_comes_from_seat_category` |
| `test_outlier_boundary` | Агрессивная чистка выбросов на малой выборке удаляет рынок, а не шум | `tests/test_statistics.py::test_small_sample_is_not_cleaned` |
| `test_failure_cases` | Таймаут, размыкатель, обрыв пагинации | `tests/test_failure_injection.py` |
| `test_source_contract_gaps` | Schema drift источника | `tests/test_connector_contracts.py` |
| `test_observation_grid` | Состав сетки | `tests/test_planner.py::test_matrix_size_matches_specification` |
| `test_statistics` | Медиана чётной выборки | `tests/test_statistics.py` |
| `test_monitoring_cadence` | Сценарий должен попадать ровно в одно окно | Неприменимо: расписание v2 задано по семействам, а не тегами сетки |

**Не перенесено:** `test_scenario_lifecycle`, `test_scenario_component_scope`,
`test_profile_resolution`, `test_profiles_and_sources`, `test_showcase_on_demand`,
`test_monitoring_batch_job` — все относятся к scenario-centric модели, которой
в v2 нет.

---

## 3. Разрывы, которые закрывает новый набор

### 3.1. Не было теста на «дашборд не считает бизнес-логику»

**Разрыв.** SCOPE-R P17 Gate 4 требует, чтобы API отдавал готовые DTO.
Соглашение без проверки — это надежда.

**Закрыто.** `gate_publication_validity` проверяет машинно: у метрики с данными
обязаны быть заполнены `median`, `min` и `confidence_level`. Тест
`test_gates.py::test_incomplete_dto_blocks_publication` подтверждает, что
неполный DTO блокирует публикацию.

### 3.2. Не было теста на неизменяемость методики

**Разрыв.** SCOPE-R R2 запрещает менять активную версию на месте.

**Закрыто.** `test_methodology.py::test_editing_registered_version_is_rejected`.
Дефект уже поймал сам себя в разработке: правка `baseline_v1.yaml` после
регистрации остановила прогон.

### 3.3. Не было теста на «NO_MARKET ≠ сбой»

**Разрыв.** Инвариант E2.2.

**Закрыто.** `test_coverage.py::test_no_market_counts_as_completed_and_is_not_a_hole`.

### 3.4. Не было теста на идемпотентность с областью исполнения

**Разрыв.** Дефект v1: досбор молча возвращал `SKIPPED_IDEMPOTENT`.

**Закрыто.** `test_idempotency.py` — три случая: повтор той же области не
создаёт вторую попытку; досбор создаёт; принудительный повтор с солью создаёт.

### 3.5. Не было PostgreSQL-набора

**Разрыв.** SCOPE-R E1.2 объявляет его обязательным.

**Закрыто.** `tests/test_postgres_integration.py`, помечен `@pytest.mark.postgres`,
запускается при заданном `TMO_TEST_DATABASE_URL`. Проверяет миграции, JSONB,
уникальные ограничения, timestamptz, оконные функции, конкурентную запись,
provenance-джойны и наличие индексов.

### 3.6. Golden Dataset строился на демо-дампе

**Разрыв.** SCOPE-R P23 прямо запрещает использовать старый seed как acceptance
evidence.

**Закрыто.** `golden/recorded_raw` + `expected_offers` + `expected_metrics`,
разбор боевым парсером, прогон через `tmo.cli golden` и через ворота публикации.

---

## 4. Состав нового набора

| Файл | Что проверяет | Уровень |
|---|---|---|
| `test_planner.py` | Размер матрицы 15 840, детерминированность, отсутствие дублей ключей, наблюдение ЖД плечом | unit |
| `test_statistics.py` | Медиана, перцентили, политика выбросов на границах | unit |
| `test_selection.py` | Схлопывание тарифной сетки, отбраковка классов, льготные группы, причина у каждого исключения | unit |
| `test_normalization.py` | Приведение базы цены, классификация типа объекта, отметки нарушений | unit |
| `test_quality.py` | Пороги уверенности, «один источник по составу scope — не LOW», понижения | unit |
| `test_connector_contracts.py` | Разбор записанных ответов: schema drift, пагинация, пустой ответ, счётчик мест РЖД, аэропорт против города | contract |
| `test_golden.py` | Golden Dataset целиком | golden |
| `test_pipeline.py` | Сквозной прогон на воспроизведении: план → сбор → расчёт → ворота → публикация | integration |
| `test_coverage.py` | Покрытие, дыры, `NO_MARKET` | integration |
| `test_idempotency.py` | Ключ идемпотентности и область исполнения | integration |
| `test_gates.py` | Четыре ворот публикации | integration |
| `test_methodology.py` | Неизменяемость версии, пересчёт другой версией | integration |
| `test_api.py` | Контракт REST v1, включая экспорт | api |
| `test_failure_injection.py` | Таймаут, ограничение темпа, битая схема, обрыв пагинации, размыкатель, исчерпание бюджета, повторный досбор | failure |
| `test_postgres_integration.py` | Специфика PostgreSQL | postgres |
| `test_capacity.py` | Планирование и расчёт полной матрицы в пределах бюджета | capacity |

---

## 5. Чего набор не покрывает

Названо прямо, потому что неназванный пробел выглядит как покрытие.

| Пробел | Почему не закрыт | Чем компенсируется |
|---|---|---|
| Живые обращения к Туту и РЖД | В CI нет сети, а тест, зависящий от чужого сервиса, ломается не по нашей вине | `tmo.cli check-sources` в эксплуатации; connector contract tests на записанных ответах |
| Многочасовая нагрузка на источник | Требует часов и согласия источника | Soak-прогон при вводе в эксплуатацию, зафиксирован в `RUNBOOK.md` |
| Frontend E2E | Требует браузерного стенда | Ручной сценарий из десяти шагов в `docs/ACCEPTANCE.md`; типы фронтенда проверяются `tsc --noEmit` в CI |
| Сверка с сайтами до рубля | Требует человека | Операционная процедура: дип-линк на предложение выводится в детализации цены |
| Восстановление PostgreSQL после рестарта | Требует управления контейнерами из теста | Проверяется в failure injection частично (обрыв соединения), полностью — в `RUNBOOK.md` |
