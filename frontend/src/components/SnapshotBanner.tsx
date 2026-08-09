/**
 * Шапка снимка.
 *
 * Пользователь всегда видит, за какую дату показаны данные и в каком они
 * состоянии. Витрина показывает последний **полностью собранный** день — и
 * говорит, почему не сегодняшний, а не молча подставляет вчерашние цифры как
 * сегодняшние.
 *
 * Разделение «идёт сбор» и «провалилось» появилось вместе с моделью сбора по
 * готовности. Прежний текст «не опубликован либо не прошёл ворота» был верен,
 * пока цикл заканчивался к 10:00: к моменту, когда на витрину смотрели, всё
 * было решено. Теперь незакрытый снимок — нормальное состояние двадцати двух
 * часов в сутки, и читать его как провал значит пугать пользователя штатной
 * работой.
 *
 * Ход текущего сбора живёт не здесь, а в `CollectionProgress` над экранами:
 * эта шапка рисуется только при наличии готового снимка, и на свежем стенде её
 * нет вовсе — вместе с ней пропадал бы и индикатор идущего сбора.
 */

import { Alert, Descriptions, Space, Tag, Typography } from 'antd';
import type { SnapshotContext } from '../api/types';
import { dateLabel, dateTimeLabel, percent } from '../format';
import { snapshotStatus } from '../labels';

export function SnapshotBanner({ context }: { context: SnapshotContext }) {
  const status = snapshotStatus(context.status);
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
      {!context.evidence_included && (
        <Alert
          type="warning"
          showIcon
          message="Снимок перенесён без предложений"
          description={
            `Данные загружены со стенда ${context.origin_stand ?? 'неизвестного'} ` +
            'уровнем showcase: цифры и оценка качества на месте, но раскрыть ' +
            'значение до конкретного предложения конкретного источника нельзя — ' +
            'предложения не переносились. Пустой список в карточке метрики ' +
            'означает именно это, а не ошибку расчёта.'
          }
        />
      )}
      {context.is_fallback && !context.is_synthetic && context.fallback_reason !== 'IN_PROGRESS' && (
        <Alert
          type={context.fallback_reason === 'FAILED' ? 'warning' : 'info'}
          showIcon
          message={`Показан снимок за ${dateLabel(context.snapshot_date)}`}
          description={
            context.fallback_reason === 'FAILED'
              ? 'Сбор за сегодня закончен, но снимок не прошёл ворота качества. Витрина показывает последний пригодный.'
              : 'Сбор за сегодня ещё не начинался. Витрина показывает последний полностью собранный день.'
          }
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
                {/* Версия показывается всегда, а не только при нескольких
                    попытках: за одной датой может стоять и собранный здесь
                    снимок, и импортированный со стороннего стенда, и молчание
                    о том, какой именно показан, — худший из вариантов. */}
                <Tag>{context.version_label}</Tag>
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
