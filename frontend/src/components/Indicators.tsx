/**
 * Индикаторы доверия к цифре.
 *
 * Ни одна цифра на витрине не показывается голой: рядом всегда видно, из
 * скольких предложений она получена, сколько источников её подтвердили и что
 * с ней не так. Частичные и низкоуверенные данные не скрываются — они
 * маркируются.
 */

import { Space, Tag, Tooltip, Typography } from 'antd';
import {
  ExclamationCircleOutlined,
  InfoCircleOutlined,
  StopOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import type { Confidence } from '../api/types';

const CONFIDENCE_META: Record<Confidence, { color: string; label: string; hint: string }> = {
  HIGH: {
    color: 'green',
    label: 'Высокая',
    hint: 'Полная выдача, достаточная выборка, все ожидаемые источники ответили',
  },
  MEDIUM: {
    color: 'gold',
    label: 'Средняя',
    hint: 'Выборка меньше целевой, либо выдача обрезана, либо один из источников не ответил',
  },
  LOW: {
    color: 'red',
    label: 'Низкая',
    hint: 'Очень малая выборка: цифру нельзя считать описанием рынка',
  },
};

export function ConfidenceTag({ level }: { level: Confidence }) {
  const meta = CONFIDENCE_META[level];
  return (
    <Tooltip title={meta.hint}>
      <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
        {meta.label}
      </Tag>
    </Tooltip>
  );
}

export function SampleTag({
  offers,
  sources,
}: {
  offers: number;
  sources: number;
}) {
  return (
    <Tooltip title="Предложений в расчёте / источников, подтвердивших выборку">
      <Tag style={{ marginInlineEnd: 0 }}>
        {offers} / {sources} ист.
      </Tag>
    </Tooltip>
  );
}

export function WarningTags({
  codes,
  dictionary,
}: {
  codes: string[];
  dictionary?: Record<string, string>;
}) {
  if (!codes?.length) return null;
  return (
    <Space size={4} wrap>
      {codes.map((code) => (
        <Tooltip key={code} title={dictionary?.[code] ?? code}>
          <Tag color="orange" icon={<WarningOutlined />} style={{ marginInlineEnd: 0 }}>
            {SHORT_WARNING[code] ?? code}
          </Tag>
        </Tooltip>
      ))}
    </Space>
  );
}

const SHORT_WARNING: Record<string, string> = {
  PARTIAL_SAMPLE: 'обрезано',
  SMALL_SAMPLE: 'мало данных',
  SINGLE_SOURCE: 'один источник',
  SOURCE_DISAGREEMENT: 'расхождение',
  OUTLIERS_NOT_REMOVED: 'разброс',
  SOURCE_FAILURE_IN_SAMPLE: 'сбой источника',
  STALE_FETCH: 'несвежее',
  SERVER_FILTER_UNCONFIRMED: 'фильтр не подтверждён',
  UNVERIFIED_CATEGORY_DROPPED: 'класс не определён',
};

export function NoMarketBadge({ reason }: { reason: string | null }) {
  const label =
    reason === 'NO_DIRECT_SERVICE'
      ? 'Прямого сообщения нет'
      : reason === 'ALL_FILTERED_OUT'
        ? 'Всё отфильтровано методикой'
        : 'Предложений нет';
  return (
    <Tooltip title="Это ответ о рынке, а не технический сбой: наблюдение состоялось">
      <Tag icon={<StopOutlined />} color="default">
        {label}
      </Tag>
    </Tooltip>
  );
}

export function MissingBadge({ components }: { components: string[] }) {
  const labels: Record<string, string> = {
    TRANSPORT_LEG: 'нет одного плеча',
    TRANSPORT_PRICE: 'нет цены транспорта',
    ACCOMMODATION: 'нет наблюдения проживания',
    ACCOMMODATION_PRICE: 'нет цены проживания',
  };
  return (
    <Tooltip title="Итог не собран: перечислено, чего именно не хватило">
      <Tag icon={<ExclamationCircleOutlined />} color="volcano">
        {components.map((code) => labels[code] ?? code).join(', ')}
      </Tag>
    </Tooltip>
  );
}

export function Hint({ children }: { children: React.ReactNode }) {
  return (
    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
      <InfoCircleOutlined style={{ marginInlineEnd: 6 }} />
      {children}
    </Typography.Text>
  );
}
