"""Аренда шага цикла: кто именно сейчас работает и до каких пор.

Шаг цикла — сбор семейства, досбор, расчёт — идёт часами и обязан быть один.
Раньше эту роль играли разнесённые по часам записи расписания, и она у них не
получалась: воркер держит восемь потоков, поэтому затянувшийся сбор авиа не
задерживал сбор проживания, а шёл рядом с ним, удваивая одновременность на
источнике.

Аренда решает и вторую задачу, которой расписание не решало вовсе.
``visibility_timeout`` брокера — двадцать часов: столько Redis ждёт, прежде чем
отдать задачу второму потребителю. Значение выбрано, чтобы занятого воркера не
дублировали пятью копиями, и обратной стороной у него была потерянная ночь:
воркер, перезапущенный ``autoheal`` в 03:00, возвращался к работе, а его задача
— нет, и до утра не собиралось ничего.

Аренда живёт минутами и продлевается по ходу работы. Умерший арендатор молчит,
аренда истекает, диспетчер выдаёт шаг заново — и сбор продолжается с того
наблюдения, на котором остановился, потому что собранное из выборки исключается.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import redis

from tmo.core.config import get_settings
from tmo.core.logging import get_logger

logger = get_logger(__name__)

#: Насколько аренда переживает последнее продление. Больше самой долгой паузы
#: внутри шага — пять минут остывания размыкателя плюс запас на пачку: иначе
#: аренда истечёт под работающим арендатором, и шаг пойдёт в две копии.
LEASE_TTL_SECONDS = 900

_KEY_PREFIX = "tmo:lease:"


def collection_lease(snapshot_date: str) -> str:
    """Имя аренды на **любое** обращение к источникам за эти сутки.

    Одно на сбор, а не на семейство. Прежде ключ включал семейство, и авиа с
    проживанием держали разные аренды — то есть могли идти одновременно, ради
    чего аренда и вводилась.

    Окно наложения настоящее: отметка «наблюдение тронуто» ставится в начале
    пачки, а задача семейства завершается спустя ещё несколько минут. Тик
    диспетчера, попавший в этот промежуток, видит семейство обойдённым и
    запускает следующее рядом с работающим. 09.08.2026 обошлось запасом в
    полторы минуты — то есть замысел держался на удаче.

    Досбор пользуется тем же ключом по той же причине: он тоже ходит в
    источник.
    """
    return f"collect:{snapshot_date}"


def _client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url)


def _holder_id() -> str:
    """Кто держит аренду. Имя узла и процесс — чтобы разбор был возможен."""
    return f"{os.uname().nodename if hasattr(os, 'uname') else 'host'}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Lease:
    """Владение шагом. Продлевается тем, кто работает, и только им."""

    def __init__(self, name: str, holder: str, ttl: int = LEASE_TTL_SECONDS) -> None:
        self.name = name
        self.holder = holder
        self.ttl = ttl
        self._key = f"{_KEY_PREFIX}{name}"

    def renew(self) -> bool:
        """Продлевает аренду, если она всё ещё наша.

        Возвращает ``False``, когда аренда уже перехвачена: продолжать работу в
        этом случае нельзя — шаг ведёт кто-то другой.
        """
        client = _client()
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
        )
        try:
            return bool(client.eval(script, 1, self._key, self.holder, self.ttl))
        except redis.RedisError as exc:
            logger.warning("Продление аренды не удалось", lease=self.name, error=str(exc))
            return True  # Недоступность брокера не повод бросать начатую работу.

    def release(self) -> None:
        client = _client()
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        try:
            client.eval(script, 1, self._key, self.holder)
        except redis.RedisError as exc:
            logger.warning("Освобождение аренды не удалось", lease=self.name, error=str(exc))


def is_held(name: str) -> bool:
    """Занят ли шаг прямо сейчас. Ответ на вопрос диспетчера, не более."""
    try:
        return bool(_client().exists(f"{_KEY_PREFIX}{name}"))
    except redis.RedisError as exc:
        # Брокер недоступен — считаем шаг занятым. Ошибка в эту сторону стоит
        # задержки, в обратную — двух сборов на одном источнике.
        logger.warning("Состояние аренды неизвестно", lease=name, error=str(exc))
        return True


@contextmanager
def acquire(name: str, *, ttl: int = LEASE_TTL_SECONDS) -> Iterator[Lease | None]:
    """Берёт аренду шага. Отдаёт ``None``, если шаг уже ведёт кто-то другой."""
    holder = _holder_id()
    key = f"{_KEY_PREFIX}{name}"
    try:
        taken = bool(_client().set(key, holder, nx=True, ex=ttl))
    except redis.RedisError as exc:
        logger.error("Аренда шага недоступна: брокер молчит", lease=name, error=str(exc))
        yield None
        return

    if not taken:
        yield None
        return

    lease = Lease(name, holder, ttl)
    logger.info("Шаг взят в работу", lease=name, holder=holder, ttl=ttl)
    try:
        yield lease
    finally:
        lease.release()
        logger.info("Шаг завершён", lease=name, holder=holder)
