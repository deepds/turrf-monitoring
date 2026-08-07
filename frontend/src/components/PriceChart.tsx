/**
 * График стоимости на горизонте 30 дней.
 *
 * Точка, у которой рынка нет, и точка, которую не удалось наблюдать, — разные
 * вещи: первая рисуется разрывом с подписью «прямого сообщения нет», вторая
 * просто отсутствует и видна на экране покрытия. Клик по точке открывает
 * детализацию цены.
 */

import { useNavigate } from 'react-router-dom';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Typography } from 'antd';
import type { ChartPoint } from '../api/types';
import { money, SERIES_COLORS, shortDate } from '../format';

export interface ChartSeries {
  key: string;
  label: string;
  points: ChartPoint[];
}

interface Props {
  series: ChartSeries[];
  height?: number;
  valueKey?: 'median' | 'min';
}

interface Row {
  date: string;
  label: string;
  [key: string]: number | string | null;
}

export function PriceChart({ series, height = 420, valueKey = 'median' }: Props) {
  const navigate = useNavigate();

  const dates = Array.from(
    new Set(series.flatMap((item) => item.points.map((point) => point.date ?? ''))),
  )
    .filter(Boolean)
    .sort();

  const metricIndex = new Map<string, number>();
  const rows: Row[] = dates.map((date) => {
    const row: Row = { date, label: shortDate(date) };
    series.forEach((item) => {
      const point = item.points.find((candidate) => candidate.date === date);
      // Отсутствие рынка — не ноль. Ноль нарисовал бы падение цены до нуля
      // там, где сообщения просто нет.
      row[item.key] = point && !point.is_no_market ? (point[valueKey] ?? null) : null;
      if (point) metricIndex.set(`${item.key}|${date}`, point.metric_id);
    });
    return row;
  });

  if (!rows.length) {
    return <Typography.Text type="secondary">Нет данных за выбранный снимок</Typography.Text>;
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
        <CartesianGrid strokeDasharray="3 3" strokeOpacity={0.35} />
        <XAxis dataKey="label" tick={{ fontSize: 12 }} minTickGap={12} />
        <YAxis
          tick={{ fontSize: 12 }}
          width={86}
          tickFormatter={(value: number) => new Intl.NumberFormat('ru-RU').format(value)}
        />
        <Tooltip
          formatter={(value: number, name: string) => [money(value), name]}
          labelFormatter={(label: string) => `Дата: ${label}`}
        />
        <Legend />
        {series.map((item, index) => (
          <Line
            key={item.key}
            type="monotone"
            dataKey={item.key}
            name={item.label}
            stroke={SERIES_COLORS[index % SERIES_COLORS.length]}
            strokeWidth={2}
            dot={{ r: 2.5, cursor: 'pointer' }}
            activeDot={{
              r: 6,
              cursor: 'pointer',
              onClick: (_event: unknown, payload: any) => {
                const date = payload?.payload?.date;
                const metricId = metricIndex.get(`${item.key}|${date}`);
                if (metricId) navigate(`/metrics/${metricId}`);
              },
            }}
            connectNulls={false}
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
