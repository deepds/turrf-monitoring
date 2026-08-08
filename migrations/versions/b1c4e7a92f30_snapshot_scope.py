"""snapshot scope

Область снимка: наблюдались ли поездки из всех городов или только из части.

Нужно нагрузочному прогону. Снимок по одному городу отправления имеет
собственное покрытие близкое к ста процентам и потому проходит ворота — а
описывает четверть рынка. Без отдельной метки он попал бы на витрину как
сегодняшний рынок.

Revision ID: b1c4e7a92f30
Revises: fed74730d640
Create Date: 2026-08-08 13:05:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = 'b1c4e7a92f30'
down_revision = 'fed74730d640'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default нужен только на время накатывания: существующие снимки
    # собраны по полной матрице, и значение по умолчанию описывает их верно.
    op.add_column(
        'market_snapshots',
        sa.Column(
            'is_partial_scope',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        'market_snapshots',
        sa.Column(
            'scope',
            sa.JSON().with_variant(JSONB(), 'postgresql'),
            nullable=False,
            server_default='{}',
        ),
    )
    op.create_index(
        'ix_market_snapshots_publishable',
        'market_snapshots',
        ['status', 'is_synthetic', 'is_partial_scope', 'snapshot_date'],
    )


def downgrade() -> None:
    op.drop_index('ix_market_snapshots_publishable', table_name='market_snapshots')
    op.drop_column('market_snapshots', 'scope')
    op.drop_column('market_snapshots', 'is_partial_scope')
