# Развёртывание на ВМ Ubuntu вручную

Порядок для машины, где уже работают два чужих стека: `tour-mvp-03-lin-*`
(параллельная реализация коллеги) и `tco-*` (первая версия наблюдателя).

**Первый стек не трогаем вообще.** Второй убираем — но не так, как кажется
очевидным, см. предупреждение ниже.

Все команды — от `root` (`sudo su`) либо с `sudo` перед каждой.

**`docker compose` работает только из каталога проекта.** Запущенный откуда-то
ещё, он отвечает `no configuration file provided: not found` — это не поломка
стека, а отсутствие `docker-compose.yml` в текущем каталоге. Поэтому каждая
команда ниже начинается с перехода в каталог: блоки рассчитаны на копирование
поодиночке, а не целиком.

Помните и о том, что после `sudo su` домашний каталог — `/root`, а не тот, из
которого вы вошли. Найти проект:

```bash
ls -d /root/turrf_monitoring /home/*/turrf_monitoring 2>/dev/null
```

Дальше в тексте он обозначен как `~/turrf_monitoring`; подставьте фактический
путь, если он иной.

---

## Предупреждение о данных первой версии

`tco-postgres-1` хранит исторические raw-ответы и Offers первой версии. Это
**evidence** — материал для сверки цифр, а не мусор: в HANDOFF первая версия
оставлена работать намеренно именно поэтому.

Поэтому ниже контейнеры удаляются, **а тома остаются**. Команды с `-v` и
`docker volume prune` в этой инструкции отсутствуют сознательно. Если данные
действительно не нужны, удалять их следует отдельным осознанным действием, а не
попутно с уборкой контейнеров.

---

## Шаг 1. Посмотреть, что удаляем

```bash
docker ps -a --filter "name=tco-" --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
```

Ожидается шесть: `tco-ui-1`, `tco-api-1`, `tco-worker-1`, `tco-beat-1`,
`tco-postgres-1`, `tco-redis-1`. Если в списке появилось что-то ещё —
остановитесь и разберитесь, прежде чем продолжать.

Проверьте, что стек коллеги в список не попал:

```bash
docker ps --filter "name=tour-mvp" --format '{{.Names}}\t{{.Status}}'
```

## Шаг 2. Сохранить базу первой версии

```bash
mkdir -p ~/backup
docker exec tco-postgres-1 pg_dumpall -U tco | gzip > ~/backup/tco-$(date +%F).sql.gz
ls -lh ~/backup/
```

Если имя пользователя базы иное, оно видно так: `docker exec tco-postgres-1 env | grep POSTGRES_USER`.

## Шаг 3. Остановить и удалить контейнеры первой версии

```bash
docker stop tco-ui-1 tco-api-1 tco-worker-1 tco-beat-1 tco-postgres-1 tco-redis-1
docker rm   tco-ui-1 tco-api-1 tco-worker-1 tco-beat-1 tco-postgres-1 tco-redis-1
```

Проверка, что тома целы:

```bash
docker volume ls | grep -i tco
```

Освободятся порты `8080`, `127.0.0.1:8000`, `127.0.0.1:5432` — новому стеку они
не нужны, но занятыми быть не должны.

## Шаг 4. Убрать старые образы первой версии

Только образы `travel-cost-observatory`, ничего лишнего:

```bash
docker images --filter "reference=travel-cost-observatory*" --format '{{.Repository}}:{{.Tag}}'
docker rmi travel-cost-observatory:1.0.0 travel-cost-observatory-ui:1.0.0
```

`docker system prune` **не запускайте**: он затронет и стек коллеги.

---

## Шаг 5. Забрать код с GitHub

```bash
cd ~
git clone https://github.com/deepds/turrf-monitoring.git turrf_monitoring
cd turrf_monitoring
git log --oneline -1
```

Ожидаемая верхушка `main` — `7d1aaae` или новее.

Если каталог уже есть:

```bash
cd ~/turrf_monitoring && git fetch origin && git reset --hard origin/main
```

## Шаг 6. Проверить доступность зеркала пакетов

На части узлов `pypi.org` закрыт, и сборка образа падает на `pip install`.
Проверьте:

```bash
curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 https://pypi.org/simple/
```

`200` — ничего делать не нужно. Иное — в `.env` следующего шага добавьте:

```
TMO_PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
TMO_PIP_TRUSTED_HOST=mirrors.aliyun.com
```

## Шаг 7. Создать `.env`

Подставьте фактический адрес машины вместо `IP_ВМ`.

```bash
cat > ~/turrf_monitoring/.env <<'ENVEOF'
TMO_ENVIRONMENT=pilot
TMO_LOG_LEVEL=INFO
TMO_STAND_NAME=ubuntu-vm
TMO_UI_PORT=8090
TMO_API_PORT=8091
TMO_CORS_ORIGINS=http://IP_ВМ:8090,http://localhost:8090
TMO_WORKER_CONCURRENCY=8
TMO_MAX_JOB_ATTEMPTS=12
TMO_MIN_SOURCE_CONCURRENCY=2
TMO_RECOVERY_CONCURRENCY=2
TMO_CONCURRENCY_GROWTH_AFTER=32
ENVEOF
chmod 600 ~/turrf_monitoring/.env
```

Порты 8090 и 8091 выбраны свободными: стек коллеги занимает 8502 и 8081,
первая версия освободила 8080.

**Пароль базы и админ-токен остаются умолчаниями.** Это заглушки; для машины,
доступной кому-то ещё, задайте `TMO_DB_PASSWORD` и `TMO_ADMIN_TOKEN` здесь же —
пароль базы должен быть задан **до первого запуска**, иначе PostgreSQL
проинициализируется со старым и менять придётся уже внутри базы.

## Шаг 8. Не выставлять базу и брокер наружу

```bash
cat > ~/turrf_monitoring/docker-compose.override.yml <<'YMLEOF'
services:
  postgres:
    ports: !override []
  redis:
    ports: !override []
YMLEOF
```

`!override` обязателен: без него Compose **добавляет** записи к списку портов
базового файла, а не заменяет их, и контейнер пытается занять порт дважды.

## Шаг 9. Собрать и запустить

```bash
cd ~/turrf_monitoring && docker compose build
```

```bash
cd ~/turrf_monitoring && docker compose up -d && sleep 40 && docker compose ps
```

Ожидается восемь контейнеров, семь из них `healthy` (у `beat` проверки нет).
Миграции применяет контейнер `api` при старте — отдельной команды не требуется.

## Шаг 10. Проверить

```bash
curl -s http://localhost:8091/api/v1/health; echo
```

```bash
curl -s -o /dev/null -w 'UI %{http_code}\n' http://localhost:8090/
```

```bash
cd ~/turrf_monitoring && docker compose exec api python -m tmo.cli check-sources
```

`check-sources` — единственная команда, ходящая в сеть. Оба источника должны
ответить `"status": "ok"`. **РЖД доступен не отовсюду**: если он молчит, ЖД
будет собираться одним источником, и это надо знать до первой ночи, а не после.

Дашборд: `http://IP_ВМ:8090`, API и OpenAPI: `http://IP_ВМ:8091/docs`.

## Шаг 11. Первичное наполнение витрины

Сразу после развёртывания готовых снимков нет, и витрина пуста: первый закроется
только в 23:00. Ход текущего сбора виден в плашке сверху, но все экраны с
цифрами до закрытия снимка пустые.

Чтобы витрина сразу показывала, как она выглядит, соберите демонстрационный
снимок на **воспроизведённых** ответах. В сеть он не ходит и на живой сбор не
влияет — дата у него своя.

```bash
cd ~/turrf_monitoring && docker compose run -d --rm --name tmo-demo api cli demo-snapshot --snapshot-date $(date -d '-4 days' +%F) --horizon 30
```

Наблюдать за ним:

```bash
docker logs -f tmo-demo
```

Занимает около десяти минут: полная матрица из 15 840 наблюдений, расчёт и
публикация. Запускается **отвязанным** (`run -d`), а не через `exec`: `exec`
привязан к ssh-сессии, и обрыв связи убил бы прогон на середине.

Готово, когда снимок появится со статусом `READY`:

```bash
cd ~/turrf_monitoring && docker compose exec -T postgres psql -U tmo -d tmo -c "SELECT snapshot_date, status, is_synthetic, round(coverage_total::numeric,3) FROM market_snapshots ORDER BY id;"
```

**Витрина пометит эти данные красным баннером «Демонстрационные данные».** Так и
задумано: синтетика не выдаёт себя за рынок, и снять пометку нельзя ни одной
настройкой. Когда закроется первый живой снимок, витрина переключится на него
сама — живой день всегда новее демонстрационного.

Удалить демонстрационный снимок, когда он больше не нужен:

```bash
cd ~/turrf_monitoring && docker compose exec -T postgres psql -U tmo -d tmo -c "DELETE FROM market_snapshots WHERE is_synthetic;"
```

---

## Что будет дальше само

Расписание знает один час — **00:30**, открытие операционных суток. Дальше
каждые пять минут работает диспетчер: открывает снимок, ведёт сбор по
семействам (авиа → ЖД → проживание), досбирает пропуски и в 23:00 закрывает
снимок расчётом и публикацией.

Ждать первых данных на витрине следует не раньше, чем закроется первый снимок.
Пока сбор идёт, витрина показывает **последний полностью собранный день**, а
состояние текущего — в плашке сверху, обновляется сама раз в 30 секунд.

Первый запуск можно не ждать до полуночи:

```bash
cd ~/turrf_monitoring && docker compose exec worker-service python -c "from tmo.tasks.collection import advance_snapshot; print(advance_snapshot.apply().get())"
```

## Если нужно остановить сбор

```bash
cd ~/turrf_monitoring && docker compose stop beat worker worker-service
```

База и витрина остаются доступными. Обратно:

```bash
cd ~/turrf_monitoring && docker compose start beat worker worker-service
```

Останавливать надо все три: досбор идёт в очереди `collect`, но диспетчер живёт
на `worker-service`, и без него шаг просто не будет выдан — а с ним будет.

## Обновление до новой версии

```bash
cd ~/turrf_monitoring && git fetch origin && git reset --hard origin/main && docker compose build api ui && docker compose up -d
```

Перезапуск воркера посреди сбора безопасен: наблюдения, брошенные убитой
пачкой, возвращаются в план, и шаг продолжится с того места. Цена — до
**15 минут простоя**, пока не истечёт аренда прошлого шага. Это ожидаемое
поведение, а не сбой.

**Схему и код обновляйте вместе.** Миграция, применённая к базе, делает её
несовместимой со старым кодом: alembic не найдёт ревизию в своём каталоге и
контейнер `api` не запустится.

---

## Диагностика, если сбор не идёт

Что решает диспетчер:

```bash
cd ~/turrf_monitoring && docker compose logs worker-service --since 20m | grep advance_snapshot | tail -3
```

Чем занят сбор:

```bash
cd ~/turrf_monitoring && docker compose logs worker --since 30m | tail -20
```

Состояние цикла (единственная команда, которой каталог не нужен):

```bash
curl -s http://localhost:8091/api/v1/market-snapshots/current
```

Кто держит право на сбор:

```bash
cd ~/turrf_monitoring && docker compose exec redis redis-cli KEYS 'tmo:lease:*'
```

Проверка живости воркера:

```bash
cd ~/turrf_monitoring && docker compose exec worker python -m tmo.tasks.health; echo "код: $?"
```

Две вещи, которые выглядят поломкой и ею не являются:

**«Шаг уже выполняется» в ответе диспетчера** — нормально, пока аренду держит
работающий шаг. Ненормально, если ключа аренды в Redis нет, а ответ прежний.

**Цифры на плашке стоят несколько минут** — попытки пишутся по завершении
пачки, а не по одному наблюдению. При тяжёлых наблюдениях пачка идёт до четырёх
минут.
