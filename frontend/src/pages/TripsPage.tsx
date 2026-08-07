/**
 * Блок A — «Куда ехать».
 *
 * Руководитель выбирает город, даты, транспорт и категорию гостиницы и видит
 * допустимые направления с расчётной стоимостью поездки. Каждая цена
 * кликабельна и раскрывается до исходных предложений.
 */

import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Card,
  Col,
  DatePicker,
  Empty,
  Radio,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tooltip,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import dayjs, { type Dayjs } from 'dayjs';
import { api } from '../api/client';
import type { Dictionary, TripRow, TripsResponse } from '../api/types';
import { SnapshotBanner } from '../components/SnapshotBanner';
import { ConfidenceTag, Hint, MissingBadge, SampleTag, WarningTags } from '../components/Indicators';
import { money, nightsLabel } from '../format';

interface Props {
  dictionary?: Dictionary;
}

export function TripsPage({ dictionary }: Props) {
  const [origins, setOrigins] = useState<{ code: string; name: string }[]>([]);
  const [origin, setOrigin] = useState<string>('MOW');
  const [range, setRange] = useState<[Dayjs, Dayjs] | null>(null);
  const [mode, setMode] = useState<'AIR' | 'RAIL'>('AIR');
  const [stars, setStars] = useState(4);
  const [data, setData] = useState<TripsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [horizon, setHorizon] = useState<{ min: Dayjs; max: Dayjs } | null>(null);

  useEffect(() => {
    api.origins().then((res) => setOrigins(res.origins));
    api.latestSnapshot().then((snapshot) => {
      const base = dayjs(snapshot.snapshot_date);
      const min = base.add(1, 'day');
      const max = base.add(30, 'day');
      setHorizon({ min, max });
      setRange([min.add(6, 'day'), min.add(13, 'day')]);
    });
  }, []);

  useEffect(() => {
    if (!range) return;
    setLoading(true);
    setError(null);
    api
      .trips({
        origin,
        departure_date: range[0].format('YYYY-MM-DD'),
        return_date: range[1].format('YYYY-MM-DD'),
        transport_mode: mode,
        stars,
      })
      .then(setData)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [origin, range, mode, stars]);

  const nights = range ? range[1].diff(range[0], 'day') : 0;

  const columns = useMemo<ColumnsType<TripRow>>(
    () => [
      {
        title: 'Направление',
        dataIndex: ['destination', 'name'],
        key: 'destination',
        render: (_: unknown, row) => (
          <Space direction="vertical" size={0}>
            <Typography.Text strong>{row.destination.name}</Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {row.origin.name} → {row.destination.name}
            </Typography.Text>
          </Space>
        ),
      },
      {
        title: 'Расчётная стоимость поездки',
        key: 'total',
        sorter: (a, b) => (a.total_median ?? Infinity) - (b.total_median ?? Infinity),
        defaultSortOrder: 'ascend',
        render: (_: unknown, row) =>
          row.total_median === null ? (
            <MissingBadge components={row.missing_components} />
          ) : (
            <Space direction="vertical" size={0}>
              <Typography.Text strong style={{ fontSize: 16 }}>
                {money(row.total_median)}
              </Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                от {money(row.total_min)}
              </Typography.Text>
            </Space>
          ),
      },
      {
        title: 'Транспорт',
        key: 'transport',
        render: (_: unknown, row) => (
          <Space direction="vertical" size={0}>
            <Tooltip title={row.transport_composition}>
              <span>
                {row.transport_metric_ids.length > 0 ? (
                  <Space size={4} split="+">
                    {row.transport_metric_ids.map((id) => (
                      <Link key={id} to={`/metrics/${id}`}>
                        {row.transport_metric_ids.length > 1 ? 'плечо' : money(row.transport_median)}
                      </Link>
                    ))}
                  </Space>
                ) : (
                  '—'
                )}
              </span>
            </Tooltip>
            <Typography.Text style={{ fontSize: 13 }}>
              {money(row.transport_median)}
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {' '}
                · от {money(row.transport_min)}
              </Typography.Text>
            </Typography.Text>
          </Space>
        ),
      },
      {
        title: 'Проживание',
        key: 'stay',
        render: (_: unknown, row) =>
          row.accommodation_metric_id ? (
            <Space direction="vertical" size={0}>
              <Link to={`/metrics/${row.accommodation_metric_id}`}>
                {money(row.accommodation_median)}
              </Link>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                от {money(row.accommodation_min)} · {nightsLabel(row.nights)}
              </Typography.Text>
            </Space>
          ) : (
            '—'
          ),
      },
      {
        title: 'Доверие',
        key: 'quality',
        render: (_: unknown, row) => (
          <Space direction="vertical" size={4}>
            <Space size={4}>
              <ConfidenceTag level={row.confidence_level} />
              <SampleTag offers={row.offers_count} sources={row.sources_count} />
            </Space>
            <WarningTags codes={row.warning_codes} dictionary={dictionary?.warning_codes} />
          </Space>
        ),
      },
    ],
    [dictionary],
  );

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {data && <SnapshotBanner context={data.context} />}

      <Card size="small">
        <Row gutter={[16, 16]} align="bottom">
          <Col xs={24} md={6}>
            <Typography.Text type="secondary">Город отправления</Typography.Text>
            <Select
              value={origin}
              onChange={setOrigin}
              style={{ width: '100%' }}
              options={origins.map((city) => ({ value: city.code, label: city.name }))}
            />
          </Col>
          <Col xs={24} md={8}>
            <Typography.Text type="secondary">Даты поездки</Typography.Text>
            <DatePicker.RangePicker
              value={range}
              onChange={(value) => value && setRange(value as [Dayjs, Dayjs])}
              style={{ width: '100%' }}
              format="DD.MM.YYYY"
              allowClear={false}
              disabledDate={(current) =>
                !horizon || current.isBefore(horizon.min, 'day') || current.isAfter(horizon.max, 'day')
              }
            />
          </Col>
          <Col xs={12} md={5}>
            <Typography.Text type="secondary">Транспорт</Typography.Text>
            <br />
            <Segmented
              block
              value={mode}
              onChange={(value) => setMode(value as 'AIR' | 'RAIL')}
              options={[
                { label: 'Авиа', value: 'AIR' },
                { label: 'ЖД', value: 'RAIL' },
              ]}
            />
          </Col>
          <Col xs={12} md={5}>
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
        </Row>
        <div style={{ marginTop: 12 }}>
          <Hint>
            {mode === 'RAIL'
              ? 'ЖД: сумма двух отдельно наблюдавшихся плеч в купе плюс настоящая бронь на весь срок. Не пакетный тур.'
              : 'Авиа: настоящий круговой тариф на эту пару дат плюс настоящая бронь на весь срок. Не пакетный тур.'}
          </Hint>
        </div>
      </Card>

      {error && <Alert type="error" showIcon message={error} />}

      <Spin spinning={loading}>
        {data && data.trips.length > 0 && (
          <Row gutter={[16, 16]}>
            {data.trips.slice(0, 4).map((trip) => (
              <Col xs={24} sm={12} lg={6} key={trip.destination.code}>
                <Card size="small">
                  <Statistic
                    title={trip.destination.name}
                    value={trip.total_median ?? undefined}
                    formatter={() => money(trip.total_median)}
                    suffix={<ConfidenceTag level={trip.confidence_level} />}
                  />
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    транспорт {money(trip.transport_median)} · проживание{' '}
                    {money(trip.accommodation_median)}
                  </Typography.Text>
                </Card>
              </Col>
            ))}
          </Row>
        )}

        <Card
          size="small"
          title={data?.label ?? 'Расчётная стоимость поездки'}
          extra={
            range && (
              <Typography.Text type="secondary">
                {nightsLabel(nights)} · {stars}★
              </Typography.Text>
            )
          }
        >
          <Table
            rowKey={(row) => row.destination.code}
            columns={columns}
            dataSource={data?.trips ?? []}
            pagination={false}
            size="middle"
            locale={{
              emptyText: (
                <Empty description="Нет наблюдений на выбранные даты в этом снимке" />
              ),
            }}
          />
        </Card>
      </Spin>
    </Space>
  );
}
