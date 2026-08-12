/**
 * Детализация цены.
 *
 * Экран существует ради одного вопроса: «откуда взялась эта цифра». Здесь
 * видно всё — методика, выборка, включённые и исключённые предложения с
 * причиной исключения, источник, момент фактического получения и ссылка на
 * файл исходного ответа.
 */

import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { DownloadOutlined, LinkOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../api/client';
import type { Dictionary, MetricDetails, OfferRow, SourceAttempt } from '../api/types';
import { ConfidenceTag, NoMarketBadge, WarningTags } from '../components/Indicators';
import { dateLabel, dateTimeLabel, money, moneyPrecise, percent } from '../format';
import { exclusionReason, outcome } from '../labels';

const METRIC_TYPE_LABEL: Record<string, string> = {
  // Состав вагонов задаётся версией методики, а подпись статична: под
  // baseline_v2 к купе добавлены сидячие места скоростных поездов. Что вошло
  // в конкретный расчёт, видно в списке предложений ниже.
  RAIL_LEG: 'ЖД, плечо в одну сторону, 1 пассажир',
  AIR_ROUND_TRIP: 'Авиа, настоящий круговой тариф, эконом, прямой, 1 пассажир',
  HOTEL_STAY: 'Проживание, настоящая бронь на весь срок, 1 взрослый, 1 номер',
  HOTEL_NIGHT: 'Проживание, одна ночь, 1 взрослый, 1 номер',
};

const OUTCOME_COLOR: Record<string, string> = {
  SUCCESS: 'green',
  PARTIAL: 'gold',
  NO_MARKET: 'default',
  TIMEOUT: 'red',
  RATE_LIMITED: 'red',
  SCHEMA_ERROR: 'red',
  TRANSPORT_ERROR: 'red',
  CIRCUIT_OPEN: 'volcano',
  BUDGET_EXHAUSTED: 'volcano',
  FAILED: 'red',
};

export function MetricPage({ dictionary }: { dictionary?: Dictionary }) {
  const { metricId } = useParams<{ metricId: string }>();
  const [details, setDetails] = useState<MetricDetails | null>(null);
  const [offers, setOffers] = useState<OfferRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!metricId) return;
    setLoading(true);
    setError(null);
    Promise.all([api.metric(Number(metricId)), api.metricOffers(Number(metricId))])
      .then(([metric, offerList]) => {
        setDetails(metric);
        setOffers(offerList.offers);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [metricId]);

  if (error) return <Alert type="error" showIcon message={error} />;
  if (loading || !details) return <Spin />;

  const offerColumns: ColumnsType<OfferRow> = [
    {
      title: 'В расчёте',
      dataIndex: 'is_included',
      width: 200,
      filters: [
        { text: 'Включено', value: true },
        { text: 'Исключено', value: false },
      ],
      onFilter: (value, row) => row.is_included === value,
      render: (included: boolean, row) =>
        included ? (
          <Tag color="green">включено</Tag>
        ) : (
          <Tooltip
            title={
              (dictionary?.exclusion_reasons?.[row.exclusion_reason ?? ''] ??
                row.exclusion_reason ??
                'причина не указана') + (row.exclusion_detail ? ` · ${row.exclusion_detail}` : '')
            }
          >
            <Tag color="red">
              {row.exclusion_reason ? exclusionReason(row.exclusion_reason) : 'исключено'}
            </Tag>
          </Tooltip>
        ),
    },
    {
      title: 'Цена',
      dataIndex: 'price',
      sorter: (a, b) => a.price - b.price,
      render: (value: number, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{moneyPrecise(value)}</Typography.Text>
          {row.source_price !== null && row.source_price !== value && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              источник: {moneyPrecise(row.source_price)}
            </Typography.Text>
          )}
        </Space>
      ),
    },
    { title: 'Источник', dataIndex: 'source_code', width: 110 },
    {
      title: 'Что это',
      key: 'what',
      render: (_: unknown, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>{row.property_name ?? row.route ?? '—'}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {[
              row.vehicle,
              row.carrier,
              row.car_type,
              row.service_class,
              row.fare_family,
              row.stars ? `${row.stars}★` : null,
              row.room_name,
            ]
              .filter(Boolean)
              .join(' · ')}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: 'Даты',
      key: 'dates',
      render: (_: unknown, row) =>
        row.check_in ? (
          <Typography.Text style={{ fontSize: 12 }}>
            {dateLabel(row.check_in)} → {dateLabel(row.check_out)}
          </Typography.Text>
        ) : (
          <Typography.Text style={{ fontSize: 12 }}>
            {dateTimeLabel(row.departure_at)}
            {row.return_departure_at ? ` / ${dateTimeLabel(row.return_departure_at)}` : ''}
          </Typography.Text>
        ),
    },
    {
      title: 'Получено',
      dataIndex: 'fetched_at',
      render: (value: string) => (
        <Typography.Text style={{ fontSize: 12 }}>{dateTimeLabel(value)}</Typography.Text>
      ),
    },
    {
      title: 'Провенанс',
      key: 'provenance',
      render: (_: unknown, row) => (
        <Space direction="vertical" size={0}>
          <Tooltip title={`Обращение #${row.provenance.source_attempt_id}, файл ${row.provenance.raw_storage_ref ?? '—'}`}>
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
              raw #{row.provenance.raw_response_id ?? '—'} · стр. {row.provenance.raw_page ?? '—'}
            </Typography.Text>
          </Tooltip>
          {row.deeplink && (
            <a href={row.deeplink} target="_blank" rel="noreferrer noopener">
              <LinkOutlined /> проверить на сайте
            </a>
          )}
        </Space>
      ),
    },
  ];

  const attemptColumns: ColumnsType<SourceAttempt> = [
    { title: 'Источник', dataIndex: 'source_code' },
    { title: 'Область', dataIndex: 'execution_scope' },
    {
      title: 'Исход',
      dataIndex: 'outcome',
      render: (value: string, row) => (
        <Space direction="vertical" size={0}>
          <Tooltip title={outcome(value).hint}>
            <Tag color={OUTCOME_COLOR[value] ?? 'default'}>{outcome(value).label}</Tag>
          </Tooltip>
          {row.error_message && (
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
              {row.error_message}
            </Typography.Text>
          )}
        </Space>
      ),
    },
    { title: 'Получено', dataIndex: 'fetched_at', render: (value: string) => dateTimeLabel(value) },
    { title: 'Задержка, мс', dataIndex: 'latency_ms' },
    { title: 'Страниц', dataIndex: 'pages_read' },
    {
      title: 'Всего у источника',
      dataIndex: 'total_matched',
      render: (value: number | null, row) => (
        <Space size={4}>
          <span>{value ?? '—'}</span>
          {row.is_partial && <Tag color="gold">обрезано: {row.partial_reason}</Tag>}
        </Space>
      ),
    },
    { title: 'Разобрано', dataIndex: 'offers_parsed' },
  ];

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {details.is_synthetic && (
        <Alert
          type="error"
          showIcon
          message="Демонстрационные данные"
          description="Метрика посчитана по воспроизведённым, а не наблюдённым ответам источников."
        />
      )}

      <Card size="small">
        <Row gutter={[16, 16]}>
          <Col xs={12} md={5}>
            <Statistic
              title="Медиана"
              value={details.median_price ?? undefined}
              formatter={() => money(details.median_price)}
            />
          </Col>
          <Col xs={12} md={5}>
            <Statistic
              title="Минимум"
              value={details.min_price ?? undefined}
              formatter={() => money(details.min_price)}
            />
          </Col>
          <Col xs={12} md={4}>
            <Statistic title="Предложений" value={details.offers_count} />
          </Col>
          <Col xs={12} md={4}>
            <Statistic title="Исключено" value={details.offers_excluded} />
          </Col>
          <Col xs={12} md={6}>
            <Space direction="vertical" size={4}>
              <Typography.Text type="secondary">Доверие</Typography.Text>
              <Space size={6} wrap>
                <ConfidenceTag level={details.confidence_level} />
                <Tag>качество {details.quality_score.toFixed(2)}</Tag>
                <Tag>источников {details.sources_count}</Tag>
                {details.is_partial && <Tag color="gold">выборка обрезана</Tag>}
                {details.is_no_market && <NoMarketBadge reason={details.no_market_reason} />}
              </Space>
              <WarningTags codes={details.warning_codes} dictionary={dictionary?.warning_codes} />
            </Space>
          </Col>
        </Row>
      </Card>

      <Card
        size="small"
        title={METRIC_TYPE_LABEL[details.metric_type] ?? details.metric_type}
        extra={
          <Space>
            <Button
              icon={<DownloadOutlined />}
              href={api.exportUrl(details.metric_id, 'csv')}
              download
            >
              CSV
            </Button>
            <Button
              icon={<DownloadOutlined />}
              type="primary"
              href={api.exportUrl(details.metric_id, 'xlsx')}
              download
            >
              Excel
            </Button>
          </Space>
        }
      >
        <Descriptions
          size="small"
          column={{ xs: 1, sm: 2, lg: 3 }}
          items={[
            { key: 'metric', label: 'metric_id', children: details.metric_id },
            { key: 'snapshot', label: 'snapshot_id', children: details.snapshot_id },
            {
              key: 'sdate',
              label: 'Дата наблюдения',
              children: `${dateLabel(details.snapshot_date)} (${details.snapshot_status})`,
            },
            {
              key: 'methodology',
              label: 'Версия методики',
              children: `${details.methodology_version} · расчёт #${details.calculation_run_id}`,
            },
            {
              key: 'fetched',
              label: 'Фактически получено',
              children: dateTimeLabel(details.fetched_at),
            },
            { key: 'computed', label: 'Рассчитано', children: dateTimeLabel(details.computed_at) },
            {
              key: 'route',
              label: 'Наблюдение',
              children: [
                details.observation.origin?.name,
                details.observation.destination?.name,
                details.observation.city?.name,
              ]
                .filter(Boolean)
                .join(' → '),
            },
            {
              key: 'dates',
              label: 'Даты наблюдения',
              children:
                details.observation.check_in
                  ? `${dateLabel(details.observation.check_in)} → ${dateLabel(details.observation.check_out)}`
                  : `${dateLabel(details.observation.service_date)}${
                      details.observation.return_date
                        ? ` → ${dateLabel(details.observation.return_date)}`
                        : ''
                    }`,
            },
            {
              key: 'coverage',
              label: 'Покрытие источниками',
              children: percent(details.source_coverage),
            },
            {
              key: 'spread',
              label: 'Квартили',
              children: `${money(details.p25_price)} … ${money(details.p75_price)}`,
            },
            { key: 'max', label: 'Максимум выборки', children: money(details.max_price) },
            {
              key: 'per_source',
              label: 'Медиана по источникам',
              children:
                Object.entries(details.per_source).length > 0
                  ? Object.entries(details.per_source)
                      .map(([code, value]) => `${code}: ${money(value.median)} (${value.offers})`)
                      .join(' · ')
                  : '—',
            },
          ]}
        />
      </Card>

      <Card size="small">
        <Tabs
          items={[
            {
              key: 'offers',
              label: `Предложения (${offers.length})`,
              children: (
                <Table
                  rowKey="offer_id"
                  size="small"
                  columns={offerColumns}
                  dataSource={offers}
                  pagination={{ pageSize: 25, showSizeChanger: true }}
                  rowClassName={(row) => (row.is_included ? '' : 'row-excluded')}
                />
              ),
            },
            {
              key: 'attempts',
              label: `Обращения к источникам (${details.source_attempts.length})`,
              children: (
                <Table
                  rowKey="source_attempt_id"
                  size="small"
                  columns={attemptColumns}
                  dataSource={details.source_attempts}
                  pagination={false}
                  expandable={{
                    expandedRowRender: (row) => (
                      <pre style={{ margin: 0, fontSize: 12, whiteSpace: 'pre-wrap' }}>
                        {JSON.stringify(row.diagnostics, null, 2)}
                      </pre>
                    ),
                  }}
                />
              ),
            },
          ]}
        />
      </Card>
    </Space>
  );
}
