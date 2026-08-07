/**
 * Блок B — «Транспорт».
 *
 * Два вида транспорта на одной оси дат, но величины у них разные, и это
 * приходится удерживать явно:
 *
 * * **ЖД** наблюдается плечом на дату отправления — 30 точек на маршрут,
 *   ось однозначна;
 * * **авиа** наблюдается парой дат, и на каждую дату вылета приходится своя
 *   цена для каждой длительности поездки. Линия существует только как срез
 *   этой сетки, поэтому длительность выбирает пользователь, а не программа.
 *
 * Ни одна точка не является суммой двух односторонних тарифов: такой величины
 * на рынке нет. Всю сетку целиком показывает отдельная страница «Сетка авиа».
 */

import { useEffect, useMemo, useState } from 'react';
import { Alert, Card, Col, Row, Segmented, Select, Space, Spin, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import type {
  AirChartResponse,
  ChartPoint,
  Dictionary,
  RailChartResponse,
  SnapshotListItem,
} from '../api/types';
import { PriceChart, type ChartSeries } from '../components/PriceChart';
import { SnapshotBanner } from '../components/SnapshotBanner';
import { ConfidenceTag, Hint, NoMarketBadge, SampleTag, WarningTags } from '../components/Indicators';
import { dateLabel, money, nightsLabel } from '../format';

type Mode = 'RAIL' | 'AIR';

interface Props {
  snapshots: SnapshotListItem[];
  dictionary?: Dictionary;
}

const DEFAULT_NIGHTS = 7;

export function TransportPage({ snapshots, dictionary }: Props) {
  const [mode, setMode] = useState<Mode>('RAIL');
  const [origins, setOrigins] = useState<{ code: string; name: string }[]>([]);
  const [origin, setOrigin] = useState('MOW');
  const [destination, setDestination] = useState<string | undefined>(undefined);
  const [snapshotDate, setSnapshotDate] = useState<string | undefined>(undefined);
  const [metric, setMetric] = useState<'median' | 'min'>('median');
  const [nights, setNights] = useState(DEFAULT_NIGHTS);
  const [data, setData] = useState<RailChartResponse | AirChartResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.origins().then((res) => setOrigins(res.origins));
  }, []);

  useEffect(() => {
    setLoading(true);
    setError(null);
    const request =
      mode === 'RAIL'
        ? api.railChart({ origin, destination, snapshot_date: snapshotDate })
        : api.airChart({ origin, destination, nights, snapshot_date: snapshotDate });
    request
      .then((value) => setData(value))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [mode, origin, destination, snapshotDate, nights]);

  const availableNights = useMemo<number[]>(
    () => (data && 'available_nights' in data ? data.available_nights : []),
    [data],
  );

  const series = useMemo<ChartSeries[]>(
    () =>
      (data?.series ?? []).map((item) => ({
        key: item.destination.code,
        label: item.destination.name,
        points: item.points,
      })),
    [data],
  );

  const detailRows = useMemo<ChartPoint[]>(
    () => (destination ? (data?.series?.[0]?.points ?? []) : []),
    [data, destination],
  );

  const columns: ColumnsType<ChartPoint> = [
    {
      title: mode === 'RAIL' ? 'Дата отправления' : 'Дата вылета',
      dataIndex: 'date',
      render: (value: string, row) => <Link to={`/metrics/${row.metric_id}`}>{dateLabel(value)}</Link>,
    },
    ...(mode === 'AIR'
      ? [
          {
            title: 'Дата возврата',
            dataIndex: 'return_date',
            render: (value: string | null) => dateLabel(value),
          } as ColumnsType<ChartPoint>[number],
        ]
      : []),
    {
      title: 'Медиана',
      dataIndex: 'median',
      render: (value: number | null, row) =>
        row.is_no_market ? <NoMarketBadge reason={row.no_market_reason} /> : money(value),
    },
    { title: 'Минимум', dataIndex: 'min', render: (value: number | null) => money(value) },
    {
      title: 'Выборка',
      key: 'sample',
      render: (_: unknown, row) => (
        <SampleTag offers={row.offers_count} sources={row.sources_count} />
      ),
    },
    {
      title: 'Доверие',
      key: 'confidence',
      render: (_: unknown, row) => (
        <Space size={4}>
          <ConfidenceTag level={row.confidence_level} />
          <WarningTags codes={row.warning_codes} dictionary={dictionary?.warning_codes} />
        </Space>
      ),
    },
  ];

  const destinations = origins.filter((city) => city.code !== origin);

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {data && <SnapshotBanner context={data.context} />}

      <Card size="small">
        <Row gutter={[16, 16]} align="bottom">
          <Col xs={24} md={4}>
            <Typography.Text type="secondary">Транспорт</Typography.Text>
            <br />
            <Segmented
              block
              value={mode}
              onChange={(value) => setMode(value as Mode)}
              options={[
                { label: 'ЖД', value: 'RAIL' },
                { label: 'Авиа', value: 'AIR' },
              ]}
            />
          </Col>
          <Col xs={24} md={4}>
            <Typography.Text type="secondary">Город отправления</Typography.Text>
            <Select
              value={origin}
              onChange={(value) => {
                setOrigin(value);
                setDestination(undefined);
              }}
              style={{ width: '100%' }}
              options={origins.map((city) => ({ value: city.code, label: city.name }))}
            />
          </Col>
          <Col xs={24} md={6}>
            <Typography.Text type="secondary">Направление</Typography.Text>
            <br />
            <Segmented
              block
              value={destination ?? 'ALL'}
              onChange={(value) => setDestination(value === 'ALL' ? undefined : String(value))}
              options={[
                { label: 'Все', value: 'ALL' },
                ...destinations.map((city) => ({ label: city.name, value: city.code })),
              ]}
            />
          </Col>
          {mode === 'AIR' && (
            <Col xs={12} md={4}>
              <Typography.Text type="secondary">Длительность поездки</Typography.Text>
              <Select
                value={nights}
                onChange={setNights}
                style={{ width: '100%' }}
                options={(availableNights.length ? availableNights : [DEFAULT_NIGHTS]).map(
                  (value) => ({ value, label: nightsLabel(value) }),
                )}
              />
            </Col>
          )}
          <Col xs={12} md={3}>
            <Typography.Text type="secondary">Дата наблюдения</Typography.Text>
            <Select
              value={snapshotDate ?? 'LATEST'}
              onChange={(value) => setSnapshotDate(value === 'LATEST' ? undefined : value)}
              style={{ width: '100%' }}
              options={[
                { value: 'LATEST', label: 'Последний' },
                ...snapshots.map((item) => ({
                  value: item.snapshot_date,
                  label: `${dateLabel(item.snapshot_date)}${item.is_synthetic ? ' (демо)' : ''}`,
                })),
              ]}
            />
          </Col>
          <Col xs={12} md={3}>
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
            {mode === 'RAIL' ? (
              <>
                Только ЖД, только купе, прямой поезд, один пассажир, одно плечо. 30 дат
                отправления. Клик по точке открывает детализацию цены.
              </>
            ) : (
              <>
                Настоящий круговой тариф, эконом, прямой, невозвратный, один пассажир. Длительность
                поездки задана явно: у авиа на каждую дату вылета приходится своя цена для каждой
                длительности. Всю сетку сразу показывает страница «Сетка авиа».
              </>
            )}
          </Hint>
        </div>
      </Card>

      {error && <Alert type="error" showIcon message={error} />}

      <Spin spinning={loading}>
        <Card
          size="small"
          title={
            mode === 'RAIL'
              ? `Стоимость плеча из города ${data?.origin?.name ?? ''}`
              : `Круговой тариф из города ${data?.origin?.name ?? ''}, ${nightsLabel(nights)}`
          }
        >
          <PriceChart series={series} valueKey={metric} />
        </Card>

        {destination && (
          <Card size="small" title="Точки маршрута" style={{ marginTop: 16 }}>
            <Table
              rowKey="metric_id"
              size="small"
              columns={columns}
              dataSource={detailRows}
              pagination={{ pageSize: 15, showSizeChanger: false }}
            />
          </Card>
        )}
      </Spin>
    </Space>
  );
}
