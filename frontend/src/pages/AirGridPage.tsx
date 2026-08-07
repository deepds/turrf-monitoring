/**
 * Сетка авиа: всё, что наблюдалось по одному маршруту.
 *
 * Линия по дате вылета отвечает на вопрос «сколько стоит улететь в этот день,
 * если поездка на неделю». Сетка отвечает на другой: «на каких парах дат
 * поездка дешевле» — и показывает это, ничего не усредняя.
 *
 * Маршрут ровно один. Четыре карты рядом нечитаемы, а общая шкала цвета
 * сделала бы дальнее направление сплошь «дорогим».
 */

import { useEffect, useState } from 'react';
import { Alert, Card, Col, Row, Select, Space, Spin, Statistic, Typography } from 'antd';
import { api } from '../api/client';
import type { AirGridResponse, SnapshotListItem } from '../api/types';
import { AirGrid } from '../components/AirGrid';
import { SnapshotBanner } from '../components/SnapshotBanner';
import { Hint } from '../components/Indicators';
import { dateLabel, money, percent } from '../format';

interface Props {
  snapshots: SnapshotListItem[];
}

export function AirGridPage({ snapshots }: Props) {
  const [cities, setCities] = useState<{ code: string; name: string }[]>([]);
  const [origin, setOrigin] = useState('MOW');
  const [destination, setDestination] = useState('AER');
  const [snapshotDate, setSnapshotDate] = useState<string | undefined>(undefined);
  const [data, setData] = useState<AirGridResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.origins().then((res) => setCities(res.origins));
  }, []);

  useEffect(() => {
    if (origin === destination) return;
    setLoading(true);
    setError(null);
    api
      .airGrid({ origin, destination, snapshot_date: snapshotDate })
      .then(setData)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [origin, destination, snapshotDate]);

  const scale = data?.scale;

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {data && <SnapshotBanner context={data.context} />}

      <Card size="small">
        <Row gutter={[16, 16]} align="bottom">
          <Col xs={12} md={5}>
            <Typography.Text type="secondary">Откуда</Typography.Text>
            <Select
              value={origin}
              onChange={(value) => {
                setOrigin(value);
                if (value === destination) {
                  setDestination(cities.find((c) => c.code !== value)?.code ?? destination);
                }
              }}
              style={{ width: '100%' }}
              options={cities.map((city) => ({ value: city.code, label: city.name }))}
            />
          </Col>
          <Col xs={12} md={5}>
            <Typography.Text type="secondary">Куда</Typography.Text>
            <Select
              value={destination}
              onChange={setDestination}
              style={{ width: '100%' }}
              options={cities
                .filter((city) => city.code !== origin)
                .map((city) => ({ value: city.code, label: city.name }))}
            />
          </Col>
          <Col xs={12} md={5}>
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
        </Row>
        <div style={{ marginTop: 12 }}>
          <Hint>
            По горизонтали — дата вылета, по вертикали — длительность поездки в ночах. Каждая
            клетка — настоящий круговой тариф на эту пару дат, а не производная величина. Клик
            открывает детализацию цены.
          </Hint>
        </div>
      </Card>

      {error && <Alert type="error" showIcon message={error} />}

      <Spin spinning={loading}>
        {scale && (
          <Row gutter={[16, 16]}>
            <Col xs={12} md={6}>
              <Card size="small">
                <Statistic
                  title="Самая дешёвая пара дат"
                  value={scale.min ?? undefined}
                  formatter={() => money(scale.min)}
                />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card size="small">
                <Statistic
                  title="Самая дорогая"
                  value={scale.max ?? undefined}
                  formatter={() => money(scale.max)}
                />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card size="small">
                <Statistic title="Пар дат с ценой" value={scale.priced_cells} />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card size="small">
                <Statistic
                  title="Наблюдений всего"
                  value={scale.total_cells}
                  suffix={
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {scale.total_cells
                        ? percent(scale.priced_cells / scale.total_cells, 0)
                        : '—'}{' '}
                      с ценой
                    </Typography.Text>
                  }
                />
              </Card>
            </Col>
          </Row>
        )}

        <Card
          size="small"
          style={{ marginTop: 16 }}
          title={
            data
              ? `${data.origin.name} → ${data.destination.name}: круговой тариф по парам дат`
              : 'Сетка авиа'
          }
        >
          {data && <AirGrid data={data} />}
        </Card>
      </Spin>
    </Space>
  );
}
