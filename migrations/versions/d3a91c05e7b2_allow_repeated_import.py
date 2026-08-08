"""Повторная загрузка того же снимка разрешена.

Уникальность `source_digest` защищала от случайного удвоения версий: принести
один и тот же архив дважды — обычное дело. Защита осталась, но переехала туда,
где ей место: загрузка находит совпадение и **спрашивает**, а решение принимает
человек. Согласие означает копию следующей версией — v2, v3, — и ограничение
базы такому согласию мешает.

Индекс остаётся: поиск совпадения по отпечатку идёт на каждой загрузке.

Revision ID: d3a91c05e7b2
Revises: c7d2f1a83b45
"""

from __future__ import annotations

from alembic import op

revision = 'd3a91c05e7b2'
down_revision = 'c7d2f1a83b45'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint('uq_market_snapshots_source_digest', 'market_snapshots', type_='unique')
    op.create_index(
        'ix_market_snapshots_source_digest', 'market_snapshots', ['source_digest']
    )


def downgrade() -> None:
    # Обратный переход возможен, только если дубликатов нет: ограничение
    # уникальности на существующих копиях не встанет. Это свойство данных, а не
    # миграции, и молча удалять копии здесь было бы худшим из решений.
    op.drop_index('ix_market_snapshots_source_digest', table_name='market_snapshots')
    op.create_unique_constraint(
        'uq_market_snapshots_source_digest', 'market_snapshots', ['source_digest']
    )
