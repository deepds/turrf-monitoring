/**
 * Ход сбора за текущие сутки.
 *
 * Живёт над экранами, а не внутри шапки снимка, и это принципиально. Прежде
 * плашка прогресса рисовалась вместе с шапкой, то есть только когда страница
 * получила данные готового снимка. На свежем стенде готовых снимков нет,
 * витрина отвечает 404 — и индикатор идущего сбора пропадал ровно тогда, когда
 * он нужнее всего: в первые сутки после развёртывания, когда единственный
 * вопрос у смотрящего — «а оно вообще работает?».
 *
 * Поэтому состояние цикла запрашивается само по себе и не зависит ни от одного
 * экрана.
 */

import { useEffect, useState } from 'react';
import { Alert, Progress, Space, Typography } from 'antd';
import { api } from '../api/client';
import type { CycleProgress } from '../api/types';
import { dateLabel, percent } from '../format';

/** Как часто перезапрашивать состояние идущего сбора. */
const REFRESH_MS = 30_000;

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

export function useCollectionProgress(): CycleProgress | null {
  const [progress, setProgress] = useState<CycleProgress | null>(null);

  useEffect(() => {
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
    tick();
    const timer = window.setInterval(tick, REFRESH_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  return progress;
}

export function CollectionProgress({ progress }: { progress: CycleProgress }) {
  const total = Math.round((progress.answered.TOTAL ?? 0) * 100);
  const step = STEP_LABEL[progress.step] ?? progress.step;
  const family = progress.step_family
    ? ` · ${FAMILY_LABEL[progress.step_family] ?? progress.step_family}`
    : '';

  return (
    <Alert
      type="info"
      showIcon
      message={`Сбор за ${dateLabel(progress.snapshot_date)} идёт: ${step}${family}`}
      description={
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          <Progress percent={total} size="small" status="active" />
          <Space size={16} wrap>
            {['AIR', 'RAIL', 'HOTEL'].map((code) =>
              progress.answered[code] === undefined ? null : (
                <Typography.Text key={code} type="secondary" style={{ fontSize: 12 }}>
                  {FAMILY_LABEL[code]}: {percent(progress.answered[code])}
                </Typography.Text>
              ),
            )}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              собрано: {progress.answered_count} из {progress.planned}
            </Typography.Text>
            <Typography.Text
              type={progress.holes > 0 ? 'warning' : 'secondary'}
              style={{ fontSize: 12 }}
            >
              пропусков: {progress.holes}
            </Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              до рубежа суток: {Math.floor(progress.minutes_left / 60)} ч{' '}
              {progress.minutes_left % 60} мин
            </Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              обновлено в{' '}
              {new Date().toLocaleTimeString('ru-RU', {
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

/**
 * Что показать, когда готовых снимков ещё нет.
 *
 * Пустой экран с текстом ошибки от API — худший из ответов на вопрос «почему
 * ничего нет». Первые сутки после развёртывания это нормальное состояние, и
 * сказать об этом надо прямо.
 */
export function NoSnapshotsYet({ collecting }: { collecting: boolean }) {
  return (
    <Alert
      type="info"
      showIcon
      message="Готовых снимков пока нет"
      description={
        collecting
          ? 'Первый сбор идёт — его ход виден выше. Витрина заполнится, когда ' +
            'снимок закроется: расчёт и публикация происходят после того, как ' +
            'сбор дошёл до порогов качества либо наступил рубеж суток в 23:00.'
          : 'Сбор за текущие сутки ещё не начинался. Снимок открывается в 00:30, ' +
            'дальше цикл идёт сам. Запустить первый сбор досрочно можно командой ' +
            'из инструкции по развёртыванию.'
      }
    />
  );
}
