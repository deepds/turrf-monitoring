/**
 * Шапка снимка.
 *
 * Пользователь всегда видит, за какую дату показаны данные и в каком они
 * состоянии. Если сегодняшний прогон провалился, витрина показывает последний
 * пригодный снимок — и говорит об этом прямо, а не молча подставляет вчерашние
 * цифры как сегодняшние.
 */

import { Alert, Descriptions, Space, Tag, Typography } from 'antd';
import type { SnapshotContext } from '../api/types';
import { dateLabel, dateTimeLabel, percent } from '../format';

const STATUS_META: Record<string, { color: string; label: string }> = {
  READY: { color: 'green', label: 'Опубликован' },
  DEGRADED: { color: 'orange', label: 'Опубликован с оговорками' },
  FAILED: { color: 'red', label: 'Не опубликован' },
};

export function SnapshotBanner({ context }: { context: SnapshotContext }) {
  const status = STATUS_META[context.status] ?? { color: 'default', label: context.status };
  const critical = context.publication_notes?.filter((note) => note.severity === 'CRITICAL') ?? [];
  const warnings = context.publication_notes?.filter((note) => note.severity === 'WARNING') ?? [];

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {context.is_synthetic && (
        <Alert
          type="error"
          showIcon
          message="Демонстрационные данные"
          description={
            'Снимок собран воспроизведением, а не наблюдением рынка. ' +
            'Цифры пригодны для показа работы системы и непригодны для решений.'
          }
        />
      )}
      {context.is_fallback && !context.is_synthetic && (
        <Alert
          type="warning"
          showIcon
          message={`Показан снимок за ${dateLabel(context.snapshot_date)}`}
          description="Снимок за сегодня ещё не опубликован либо не прошёл ворота качества. Витрина показывает последний пригодный."
        />
      )}
      {critical.map((note) => (
        <Alert key={note.code} type="error" showIcon message={note.message} />
      ))}
      {warnings.map((note) => (
        <Alert key={note.code} type="warning" showIcon message={note.message} />
      ))}

      <Descriptions
        size="small"
        column={{ xs: 1, sm: 2, md: 3, lg: 6 }}
        items={[
          {
            key: 'date',
            label: 'Дата наблюдения',
            children: (
              <Space size={6}>
                <Typography.Text strong>{dateLabel(context.snapshot_date)}</Typography.Text>
                <Tag color={status.color}>{status.label}</Tag>
              </Space>
            ),
          },
          {
            key: 'published',
            label: 'Опубликован',
            children: dateTimeLabel(context.published_at),
          },
          { key: 'total', label: 'Покрытие', children: percent(context.coverage_total) },
          { key: 'rail', label: 'ЖД', children: percent(context.coverage_rail) },
          { key: 'air', label: 'Авиа', children: percent(context.coverage_air) },
          { key: 'hotel', label: 'Проживание', children: percent(context.coverage_hotel) },
        ]}
      />
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        Методика {context.methodology_version} · расчёт #{context.calculation_run_id} · снимок #
        {context.snapshot_id}
      </Typography.Text>
    </Space>
  );
}
