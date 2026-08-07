/**
 * Блок C — «Проживание».
 *
 * Все пять городов сразу, одна ночь, один взрослый, один номер. Блок не связан
 * с датами блока «Куда ехать»: multi-night бронь смешивает несколько
 * календарных дней в одной точке, а график должен отвечать за конкретную дату
 * заезда.
 */

import { useEffect, useMemo, useState } from 'react';
import { Alert, Card, Col, Radio, Row, Segmented, Select, Space, Spin, Typography } from 'antd';
import { api } from '../api/client';
import type { Dictionary, HotelChartResponse, SnapshotListItem } from '../api/types';
import { PriceChart, type ChartSeries } from '../components/PriceChart';
import { SnapshotBanner } from '../components/SnapshotBanner';
import { Hint } from '../components/Indicators';
import { dateLabel } from '../format';

interface Props {
  snapshots: SnapshotListItem[];
  dictionary?: Dictionary;
}

export function HotelsPage({ snapshots }: Props) {
  const [stars, setStars] = useState(4);
  const [snapshotDate, setSnapshotDate] = useState<string | undefined>(undefined);
  const [metric, setMetric] = useState<'median' | 'min'>('median');
  const [data, setData] = useState<HotelChartResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    api
      .hotelChart({ stars, snapshot_date: snapshotDate })
      .then(setData)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [stars, snapshotDate]);

  const series = useMemo<ChartSeries[]>(
    () =>
      (data?.series ?? []).map((item) => ({
        key: item.city.code,
        label: item.city.name,
        points: item.points,
      })),
    [data],
  );

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {data && <SnapshotBanner context={data.context} />}

      <Card size="small">
        <Row gutter={[16, 16]} align="bottom">
          <Col xs={24} md={8}>
            <Typography.Text type="secondary">Категория гостиницы</Typography.Text>
            <br />
            <Radio.Group
              value={stars}
              onChange={(event) => setStars(event.target.value)}
              optionType="button"
              buttonStyle="solid"
              options={[
                { label: '3★', value: 3 },
                { label: '4★', value: 4 },
                { label: '5★', value: 5 },
              ]}
            />
          </Col>
          <Col xs={12} md={8}>
            <Typography.Text type="secondary">Дата наблюдения</Typography.Text>
            <Select
              value={snapshotDate ?? 'LATEST'}
              onChange={(value) => setSnapshotDate(value === 'LATEST' ? undefined : value)}
              style={{ width: '100%' }}
              options={[
                { value: 'LATEST', label: 'Последний снимок' },
                ...snapshots.map((item) => ({
                  value: item.snapshot_date,
                  label: `${dateLabel(item.snapshot_date)}${item.is_synthetic ? ' (демо)' : ''}`,
                })),
              ]}
            />
          </Col>
          <Col xs={12} md={8}>
            <Typography.Text type="secondary">Показатель</Typography.Text>
            <br />
            <Segmented
              block
              value={metric}
              onChange={(value) => setMetric(value as 'median' | 'min')}
              options={[
                { label: 'Медиана', value: 'median' },
                { label: 'Минимум', value: 'min' },
              ]}
            />
          </Col>
        </Row>
        <div style={{ marginTop: 12 }}>
          <Hint>
            Каждая точка — одна ночь, один взрослый, один номер, только гостиницы выбранной
            категории. Последняя точка горизонта — заезд D+30, выезд D+31.
          </Hint>
        </div>
      </Card>

      {error && <Alert type="error" showIcon message={error} />}

      <Spin spinning={loading}>
        <Card size="small" title={`Стоимость ночи, ${stars}★`}>
          <PriceChart series={series} valueKey={metric} />
        </Card>
      </Spin>
    </Space>
  );
}
