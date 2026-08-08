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
 */

import { Alert, Descriptions, Progress, Space, Tag, Typography } from 'antd';
import type { CycleProgress, SnapshotContext } from '../api/types';
import { dateLabel, dateTimeLabel, percent } from '../format';
import { snapshotStatus } from '../labels';

const STEP_LABEL: Record<CycleProgress['step'], string> = {
  OPEN: 'открывается снимок',
  COLLECT: 'первичный сбор',
  RECOVER: 'досбор пропусков',
  CLOSE: 'расчёт и публикация',
  IDLE: 'сутки закрыты',
};

const FAMILY_LABEL: Record<string, string> = {
  AIR: 'Авиа',
  RAIL: 'ЖД',
  HOTEL: 'Проживание',
};

function TodayProgress({ today }: { today: CycleProgress }) {
  const total = Math.round((today.answered.TOTAL ?? 0) * 100);
  const step = STEP_LABEL[today.step] ?? today.step;
  const family = today.step_family ? ` · ${FAMILY_LABEL[today.step_family] ?? today.step_family}` : '';

  return (
    <Alert
      type="info"
      showIcon
      message={`Сбор за ${dateLabel(today.snapshot_date)} идёт: ${step}${family}`}
      description={
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          <Progress percent={total} size="small" status="active" />
          <Space size={16} wrap>
            {['AIR', 'RAIL', 'HOTEL'].map((code) =>
              today.answered[code] === undefined ? null : (
                <Typography.Text key={code} type="secondary" style={{ fontSize: 12 }}>
                  {FAMILY_LABEL[code]}: {percent(today.answered[code])}
                </Typography.Text>
              ),
            )}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              пропусков: {today.holes}
            </Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              до рубежа суток: {Math.floor(today.minutes_left / 60)} ч {today.minutes_left % 60} мин
            </Typography.Text>
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            Доля наблюдений, получивших ответ. Отказ ответом не считается — он
            переспрашивается, пока не даст результат.
          </Typography.Text>
        </Space>
      }
    />
  );
}

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
      {context.today && !context.today.is_closed && <TodayProgress today={context.today} />}
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
