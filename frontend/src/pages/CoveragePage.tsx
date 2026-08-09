/**
 * Покрытие и качество.
 *
 * Экран отвечает на вопрос «можно ли доверять сегодняшней витрине» без чтения
 * логов: сколько наблюдений было запланировано, сколько собралось, где пропуски,
 * что отвечали источники и почему снимок получил свой статус.
 */

import { useEffect, useState } from 'react';
import {
  Alert,
  Card,
  Col,
  Descriptions,
  Progress,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../api/client';
import type { CoverageResponse, FamilyCoverage, SnapshotListItem } from '../api/types';
import { dateLabel, dateTimeLabel, percent } from '../format';
import { confidence, metricType, outcome, snapshotStatus } from '../labels';

//: Исходы, в которых виноват источник. Остальные — наши решения перестать
//: спрашивать, и красным их помечать неверно.
const SOURCE_FAULT = new Set(['TIMEOUT', 'TRANSPORT_ERROR', 'RATE_LIMITED', 'SCHEMA_ERROR']);

const FAMILY_LABEL: Record<string, string> = {
  RAIL: 'Железная дорога',
  AIR: 'Авиа',
  HOTEL: 'Проживание',
  TOTAL: 'Всего',
};

export function CoveragePage({ snapshots }: { snapshots: SnapshotListItem[] }) {
  const [snapshotDate, setSnapshotDate] = useState<string | undefined>(snapshots[0]?.snapshot_date);
  const [attemptNo, setAttemptNo] = useState<number | undefined>();
  const [data, setData] = useState<CoverageResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!snapshotDate && snapshots.length) setSnapshotDate(snapshots[0].snapshot_date);
  }, [snapshots, snapshotDate]);

  // Версии принадлежат дате: при смене даты прежний номер попытки к новой дате
  // отношения не имеет и мог бы указать на чужой снимок либо в пустоту.
  const versions = snapshots.find((item) => item.snapshot_date === snapshotDate)?.versions ?? [];
  useEffect(() => {
    setAttemptNo(undefined);
  }, [snapshotDate]);

  useEffect(() => {
    if (!snapshotDate) return;
    setLoading(true);
    setError(null);
    api
      .coverage(snapshotDate, attemptNo)
      .then(setData)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [snapshotDate, attemptNo]);

  const familyRows: FamilyCoverage[] = data
    ? [...Object.values(data.coverage.by_family), data.coverage.total]
    : [];

  const familyColumns: ColumnsType<FamilyCoverage> = [
    {
      title: 'Категория',
      dataIndex: 'family',
      render: (value: string) => (
        <Typography.Text strong={value === 'TOTAL'}>{FAMILY_LABEL[value] ?? value}</Typography.Text>
      ),
    },
    { title: 'В плане', dataIndex: 'planned' },
    { title: 'Завершено', dataIndex: 'completed' },
    { title: 'С данными', dataIndex: 'successful' },
    { title: 'Частично', dataIndex: 'partial' },
    {
      title: 'Нет рынка',
      dataIndex: 'no_market',
      render: (value: number) => (
        <Space size={4}>
          <span>{value}</span>
          {value > 0 && <Tag>не пропуск</Tag>}
        </Space>
      ),
    },
    {
      title: 'Сбой',
      dataIndex: 'failed',
      render: (value: number) => (value > 0 ? <Tag color="red">{value}</Tag> : value),
    },
    {
      title: 'Не дошло',
      dataIndex: 'missing',
      render: (value: number) => (value > 0 ? <Tag color="volcano">{value}</Tag> : value),
    },
    {
      title: 'Завершённость',
      dataIndex: 'completion',
      render: (value: number) => (
        <Progress
          percent={Math.round(value * 1000) / 10}
          size="small"
          status={value >= 0.98 ? 'success' : value >= 0.85 ? 'active' : 'exception'}
        />
      ),
    },
    {
      title: 'Доля с данными',
      dataIndex: 'data_share',
      render: (value: number) => percent(value),
    },
  ];

  const sourceColumns: ColumnsType<Record<string, any>> = [
    { title: 'Источник', dataIndex: 'source_code' },
    { title: 'Категория', dataIndex: 'family', render: (value: string) => FAMILY_LABEL[value] ?? value },
    { title: 'Обращений', dataIndex: 'attempts' },
    { title: 'Успешно', dataIndex: 'success' },
    { title: 'Частично', dataIndex: 'partial' },
    { title: 'Нет рынка', dataIndex: 'no_market' },
    {
      title: 'Отказы',
      dataIndex: 'failures_by_outcome',
      render: (value: Record<string, number>) =>
        Object.keys(value ?? {}).length === 0 ? (
          '—'
        ) : (
          <Space size={4} wrap>
            {Object.entries(value).map(([code, count]) => (
              <Tooltip key={code} title={outcome(code).hint}>
                {/* Красным помечено только то, что сделал источник. Наши
                    собственные решения перестать спрашивать — размыкатель и
                    исчерпанный бюджет — идут другим цветом: приписывать их
                    источнику значит обвинять его в том, чего он не делал. */}
                <Tag color={SOURCE_FAULT.has(code) ? 'red' : 'orange'}>
                  {outcome(code).label}: {count}
                </Tag>
              </Tooltip>
            ))}
          </Space>
        ),
    },
    { title: 'Предложений', dataIndex: 'offers_parsed' },
    { title: 'HTTP-вызовов', dataIndex: 'http_calls' },
    { title: 'p50, мс', dataIndex: 'p50_latency_ms' },
    { title: 'p95, мс', dataIndex: 'p95_latency_ms' },
  ];

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card size="small">
        <Space>
          <Typography.Text type="secondary">Дата наблюдения</Typography.Text>
          <Select
            value={snapshotDate}
            onChange={setSnapshotDate}
            style={{ minWidth: 240 }}
            options={snapshots.map((item) => ({
              value: item.snapshot_date,
              label: `${dateLabel(item.snapshot_date)} · ${snapshotStatus(item.status).label}${
                item.is_synthetic ? ' · демо' : ''
              }`,
            }))}
          />
          {versions.length > 1 && (
            <>
              <Typography.Text type="secondary">Версия</Typography.Text>
              <Select
                value={attemptNo ?? versions[0].attempt_no}
                onChange={setAttemptNo}
                style={{ minWidth: 200 }}
                options={versions.map((version) => ({
                  value: version.attempt_no,
                  label: `${version.label} · ${snapshotStatus(version.status).label} · ${percent(version.coverage_total)}`,
                }))}
              />
            </>
          )}
        </Space>
      </Card>

      {error && <Alert type="error" showIcon message={error} />}

      <Spin spinning={loading}>
        {data && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            {(data.overview.snapshot.publication_notes ?? []).map((note: any) => (
              <Alert
                key={note.code}
                type={note.severity === 'CRITICAL' ? 'error' : 'warning'}
                showIcon
                message={note.message}
              />
            ))}

            <Card size="small" title="Ход прогона">
              <Descriptions
                size="small"
                column={{ xs: 1, sm: 2, lg: 4 }}
                items={[
                  {
                    key: 'status',
                    label: 'Статус',
                    children: (
                      <Tag color={snapshotStatus(data.overview.snapshot.status).color}>
                        {snapshotStatus(data.overview.snapshot.status).label}
                      </Tag>
                    ),
                  },
                  {
                    key: 'started',
                    label: 'Начат',
                    children: dateTimeLabel(data.overview.snapshot.started_at),
                  },
                  {
                    key: 'primary',
                    label: 'Первичный сбор завершён',
                    children: dateTimeLabel(
                      data.overview.snapshot.primary_collection_finished_at,
                    ),
                  },
                  {
                    key: 'recovery',
                    label: 'Досбор завершён',
                    children: dateTimeLabel(data.overview.snapshot.recovery_finished_at),
                  },
                  {
                    key: 'published',
                    label: 'Опубликован',
                    children: dateTimeLabel(data.overview.snapshot.published_at),
                  },
                  {
                    key: 'holes',
                    label: 'Технических пропусков',
                    children:
                      data.holes.count > 0 ? (
                        <Tag color="red">{data.holes.count}</Tag>
                      ) : (
                        <Tag color="green">нет</Tag>
                      ),
                  },
                ]}
              />
            </Card>

            <Card size="small" title="Покрытие по категориям">
              <Table
                rowKey="family"
                size="small"
                columns={familyColumns}
                dataSource={familyRows}
                pagination={false}
              />
              <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12 }}>
                «Нет рынка» считается завершённым наблюдением: отсутствие прямого сообщения — это
                ответ о рынке, а не технический сбой. Дырой считается только техническое.
              </Typography.Paragraph>
            </Card>

            <Card size="small" title="Что отвечали источники">
              <Table
                rowKey={(row) => `${row.source_code}-${row.family}`}
                size="small"
                columns={sourceColumns}
                dataSource={data.overview.sources}
                pagination={false}
              />
            </Card>

            <Card size="small" title="Распределение уверенности">
              <Row gutter={[16, 16]}>
                {Object.entries(data.overview.confidence_distribution).map(([type, levels]) => (
                  <Col xs={24} md={12} lg={6} key={type}>
                    <Card size="small" type="inner" title={metricType(type)}>
                      <Space direction="vertical" size={4}>
                        {Object.entries(levels).map(([level, count]) => (
                          <Space key={level}>
                            <Tag color={confidence(level).color}>{confidence(level).label}</Tag>
                            <span>{count}</span>
                          </Space>
                        ))}
                      </Space>
                    </Card>
                  </Col>
                ))}
              </Row>
            </Card>
          </Space>
        )}
      </Spin>
    </Space>
  );
}
