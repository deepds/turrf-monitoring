"""Поля перенесённого снимка.

Снимок может быть не собран здесь, а загружен с другого стенда. Отличать одно
от другого обязательно: два снимка одной даты — это две версии, и молчание о
том, какая откуда, делает сравнение невозможным.

`source_digest` уникален намеренно: он же защита от повторной загрузки одного
файла. Ограничение уникальности здесь дешевле проверки в коде — код можно
обойти, ограничение нет.

Revision ID: c7d2f1a83b45
Revises: b1c4e7a92f30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c7d2f1a83b45'
down_revision = 'b1c4e7a92f30'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('market_snapshots', sa.Column('source_digest', sa.String(64), nullable=True))
    op.add_column('market_snapshots', sa.Column('origin_stand', sa.String(64), nullable=True))
    op.add_column('market_snapshots', sa.Column('imported_at', sa.DateTime(timezone=True), nullable=True))
    # Умолчание true и для существующих строк: всё, что собрано здесь, несёт
    # свои предложения. Отсутствие доказательств — свойство исключительно
    # загруженных снимков уровня showcase.
    op.add_column(
        'market_snapshots',
        sa.Column('evidence_included', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_unique_constraint(
        'uq_market_snapshots_source_digest', 'market_snapshots', ['source_digest']
    )


def downgrade() -> None:
    op.drop_constraint('uq_market_snapshots_source_digest', 'market_snapshots', type_='unique')
    op.drop_column('market_snapshots', 'evidence_included')
    op.drop_column('market_snapshots', 'imported_at')
    op.drop_column('market_snapshots', 'origin_stand')
    op.drop_column('market_snapshots', 'source_digest')
