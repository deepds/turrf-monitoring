"""FastAPI-приложение.

Дашборд работает только по заранее собранным данным. Ни один эндпоинт этого
приложения не обращается к Туту, РЖД или любому внешнему источнику — это
архитектурное свойство, а не соглашение: сервисы витрины физически не имеют
доступа к коннекторам.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from tmo.api.routers import admin, charts, coverage, metrics, reference, snapshots, trips
from tmo.core.config import get_settings
from tmo.core.errors import TmoError
from tmo.core.logging import configure_logging, get_logger
from tmo.version import APP_VERSION

logger = get_logger(__name__)

DESCRIPTION = """
Ежедневный мониторинг стоимости поездок между Москвой, Санкт-Петербургом,
Сочи, Самарой и Казанью на горизонте 30 будущих дней.

**Витрина не обращается к источникам.** Все цифры берутся из последнего
опубликованного Market Snapshot и раскрываются до исходных предложений.
"""


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title="Мониторинг стоимости поездок",
        description=DESCRIPTION,
        version=APP_VERSION,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    prefix = settings.api_prefix
    app.include_router(reference.router, prefix=prefix)
    app.include_router(snapshots.router, prefix=prefix)
    app.include_router(trips.router, prefix=prefix)
    app.include_router(charts.router, prefix=prefix)
    app.include_router(metrics.router, prefix=prefix)
    app.include_router(coverage.router, prefix=prefix)
    app.include_router(admin.router, prefix=prefix)

    @app.exception_handler(TmoError)
    async def _domain_error(_: Request, exc: TmoError) -> JSONResponse:
        # Текст доменной ошибки безопасен: секреты в него не попадают, потому
        # что не попадают и в саму ошибку.
        logger.warning("Доменная ошибка", error=str(exc), kind=type(exc).__name__)
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Необработанная ошибка", error=str(exc))
        # Наружу — без подробностей: в трассировке могут оказаться параметры
        # подключения.
        return JSONResponse(status_code=500, content={"detail": "Внутренняя ошибка сервиса"})

    return app


app = create_app()
