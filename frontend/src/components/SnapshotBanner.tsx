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

import { useEffect, useState } from 'react';
import { Alert, Descriptions, Progress, Space, Tag, Typography } from 'antd';
import { api } from '../api/client';
import type { CycleProgress, SnapshotContext } from '../api/types';
import { dateLabel, dateTimeLabel, percent } from '../format';
import { snapshotStatus } from '../labels';

/** Как часто перезапрашивать состояние идущего сбора. */
const REFRESH_MS = 30_000;

/**
 * Свежее состояние сбора.
 *
 * Контекст снимка приходит вместе с данными экрана и загружается один раз. Для
 * закрытого снимка этого достаточно — он не меняется. Для идущего сбора
 * достаточно ровно наоборот: плашка прогресса существует, чтобы на него
 * смотреть, а застывшие цифры не отличить от вставшего сбора.
 *
 * Перезапрашивается только состояние цикла, а не данные экрана: это несколько
 * счётчиков против графиков и таблиц на десятки тысяч строк.
 */
function useLiveProgress(initial: CycleProgress | null): CycleProgress | null {
  const [progress, setProgress] = useState(initial);

  useEffect(() => setProgress(initial), [initial]);

  const closed = progress?.is_closed ?? true;
  useEffect(() => {
    if (closed) return;
    let alive = true;
    const tick = () => {
      api
        .currentCycle()
        .then((payload) => {
          if (alive) setProgress(payload.progress);
        })
        .catch(() => {
          /* сеть моргнула — оставляем прежние цифры до следующей попытки */
        });
    };
    const timer = window.setInterval(tick, REFRESH_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [closed]);

  return progress;
}

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
              собрано: {today.answered_count} из {today.planned}
            </Typography.Text>
            <Typography.Text
              type={today.holes > 0 ? 'warning' : 'secondary'}
              style={{ fontSize: 12 }}
            >
              пропусков: {today.holes}
            </Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              до рубежа суток: {Math.floor(today.minutes_left / 60)} ч {today.minutes_left % 60} мин
            </Typography.Text>
            {/* Время последнего обновления: без него застывшая страница
                неотличима от вставшего сбора. */}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              обновлено в {new Date().toLocaleTimeString('ru-RU', {
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
              })}
            </Typography.Text>
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            Проценты — доля наблюдений, получивших ответ. Пропуски — те, к которым
            сбор уже подходил и ответа не получил; они переспрашиваются, пока не
            дадут результат. Наблюдения, до которых очередь ещё не дошла, в
            пропуски не входят.
          </Typography.Text>
        </Space>
      }
    />
  );
}

export function SnapshotBanner({ context }: { context: SnapshotContext }) {
  const status = snapshotStatus(context.status);
  const today = useLiveProgress(context.today);
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
      {today && !today.is_closed && <TodayProgress today={today} />}
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
