# Единый образ для API, воркеров и планировщика: одна кодовая база, одна
# версия зависимостей, один артефакт для отката.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Индекс пакетов задаётся снаружи. По умолчанию — публичный PyPI; на узлах,
# где `pypi.org` недоступен (а `files.pythonhosted.org` доступен), сборка
# получает адрес зеркала аргументом. Прописывать конкретное зеркало в образ
# нельзя: оно свойство сети развёртывания, а не приложения.
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=""
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60

COPY pyproject.toml README.md ./
COPY tmo ./tmo
RUN pip install -e .

COPY alembic.ini ./
COPY migrations ./migrations
COPY golden ./golden
COPY scripts ./scripts
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Не root: процессу сбора не нужны права на систему.
RUN useradd --create-home --uid 10001 tmo \
    && mkdir -p /var/tmo/raw /var/tmo/export \
    && chown -R tmo:tmo /app /var/tmo
USER tmo

ENV TMO_RAW_STORAGE_PATH=/var/tmo/raw \
    TMO_EXPORT_STORAGE_PATH=/var/tmo/export

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["api"]
