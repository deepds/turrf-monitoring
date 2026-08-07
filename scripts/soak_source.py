"""Нагрузочный замер источника: держит ли он заданный темп на длинном потоке.

Существует потому, что короткий тест ничего не доказывает. Замер на восьми
обращениях проходил без отказов, а двухминутный сбор той же плотности давал
`503`: ломает **длительность**, а не плотность.

Второе правило, купленное ошибкой: **запросы обязаны быть уникальными**.
Повтор одного и того же отдаёт кэш источника — 42 мс против 769 мс на живом
поиске — и измеряет скорость кэша, а не способность обслуживать сбор.

Печатает распределение кодов, задержки и заголовки первого неуспешного
ответа: по ним видно, ответил источник или промежуточный узел.

```bash
SOAK_SECONDS=3600 SOAK_CONCURRENCY=3 SOAK_RATE=180   SOAK_LABEL=nemo python3 scripts/soak_source.py
```
"""
import collections
import datetime
import itertools
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

URL = "https://mcp.tutu.ru/mcp"
DURATION = int(os.environ.get("SOAK_SECONDS", "180"))
CONCURRENCY = int(os.environ.get("SOAK_CONCURRENCY", "3"))
RATE = int(os.environ.get("SOAK_RATE", "60"))  # в минуту
LABEL = os.environ.get("SOAK_LABEL", socket.gethostname())

# Каждый запрос уникален. Повтор одного и того же отдаёт кэш источника (42 мс
# против секунды) и нагрузкой не является: настоящий сбор спрашивает разные
# маршруты и даты, и каждый требует поиска.
CITIES = ["Москва", "Санкт-Петербург", "Сочи", "Самара", "Казань"]
PAIRS = [(a, b) for a in CITIES for b in CITIES if a != b]
BASE = datetime.date.today() + datetime.timedelta(days=2)
_counter = itertools.count()


def make_body():
    n = next(_counter)
    origin, destination = PAIRS[n % len(PAIRS)]
    day = BASE + datetime.timedelta(days=(n // len(PAIRS)) % 28)
    return json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_rail", "arguments": {
            "origin": origin, "destination": destination,
            "departure_date": day.isoformat(), "passengers": 1,
            "direct_only": True, "seat_categories": ["COMPARTMENT"],
            "view": "full", "page_size": 30}},
    }).encode()

codes = collections.Counter()
lat = []
first_bad = {}
lock = threading.Lock()
stop = time.time() + DURATION
gate = threading.Semaphore(0)


def pacer():
    interval = 60.0 / RATE
    while time.time() < stop:
        gate.release()
        time.sleep(interval)
    for _ in range(CONCURRENCY * 2):
        gate.release()


def worker():
    while time.time() < stop:
        gate.acquire()
        if time.time() >= stop:
            return
        req = urllib.request.Request(URL, data=make_body(), headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "travel-monitoring-observatory/2.0"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
                code, hdrs = r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            code, hdrs = e.code, dict(e.headers)
            try:
                body = e.read()[:200].decode("utf-8", "replace")
            except Exception:
                body = ""
            with lock:
                if code not in first_bad:
                    first_bad[code] = {"headers": hdrs, "body": body,
                                       "at_second": round(time.time() - (stop - DURATION), 1)}
        except Exception as e:
            code, hdrs = type(e).__name__, {}
            with lock:
                if code not in first_bad:
                    first_bad[code] = {"error": str(e)[:200],
                                       "at_second": round(time.time() - (stop - DURATION), 1)}
        dt = (time.perf_counter() - t0) * 1000
        with lock:
            codes[code] += 1
            lat.append(dt)


threads = [threading.Thread(target=pacer, daemon=True)]
threads += [threading.Thread(target=worker, daemon=True) for _ in range(CONCURRENCY)]
for t in threads:
    t.start()
for t in threads:
    t.join()

lat.sort()
print(json.dumps({
    "label": LABEL,
    "duration_s": DURATION, "concurrency": CONCURRENCY, "rate_per_min": RATE,
    "requests": sum(codes.values()),
    "codes": {str(k): v for k, v in codes.items()},
    "latency_ms": {
        "p50": round(lat[len(lat)//2]) if lat else None,
        "p95": round(lat[min(len(lat)-1, int(len(lat)*0.95))]) if lat else None,
        "max": round(lat[-1]) if lat else None,
    },
    "first_bad": first_bad,
}, ensure_ascii=False, indent=1))
