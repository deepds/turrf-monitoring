/**
 * Перенос снимков между стендами.
 *
 * Один файл туда, один файл обратно. Репозиторий для этого не годится: архив
 * полной матрицы с доказательствами — около 54 МБ, и история git хранила бы
 * каждую версию навсегда.
 *
 * Совпадение при загрузке не считается ошибкой. Принести один и тот же архив
 * дважды — обычное дело, и молча удвоить версии значило бы наказать за
 * неосторожность. Витрина спрашивает, а решение принимает человек: согласие
 * кладёт копию следующей версией — v2, v3.
 */

import { useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Modal,
  Radio,
  Select,
  Progress,
  Space,
  Spin,
  Table,
  Typography,
  Upload,
} from 'antd';
import { DownloadOutlined, InboxOutlined } from '@ant-design/icons';
import { api } from '../api/client';
import type { ImportResult, SnapshotListItem } from '../api/types';
import { dateLabel, percent } from '../format';
import { snapshotStatus } from '../labels';

type Level = 'showcase' | 'evidence';

const LEVEL_NOTE: Record<Level, string> = {
  showcase:
    'Всё, чем витрина рисует цифры: снимок, наблюдения, попытки, расчёт, метрики, ' +
    'витрина поездок. Около 6 МБ на полную матрицу. Раскрытие цифры до конкретного ' +
    'предложения на таком снимке не работает — и витрина об этом скажет.',
  evidence:
    'То же плюс предложения и связи метрик с ними. Около 54 МБ на полную матрицу. ' +
    'Раскрытие цифры до конкретного предложения работает полностью.',
};

export function TransferPage({ snapshots }: { snapshots: SnapshotListItem[] }) {
  const [snapshotDate, setSnapshotDate] = useState<string | undefined>(
    snapshots[0]?.snapshot_date,
  );
  const [attemptNo, setAttemptNo] = useState<number | undefined>();
  const [level, setLevel] = useState<Level>('evidence');
  const [result, setResult] = useState<ImportResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Ход двух долгих операций. Обе идут минутами, и без явного этапа экран
  // выглядит замершим: пользователь жмёт кнопку второй раз и запускает вторую
  // сборку архива на сервере.
  const [download, setDownload] = useState<{ stage: string; text: string } | null>(null);
  const [upload, setUpload] = useState<{ percent: number; text: string } | null>(null);

  const selected = snapshots.find((item) => item.snapshot_date === snapshotDate);
  const versions = selected?.versions ?? [];
  const attempt = attemptNo ?? versions[0]?.attempt_no;

  const startDownload = async () => {
    if (!snapshotDate || attempt === undefined) return;
    setError(null);
    try {
      await api.downloadArchive(snapshotDate, attempt, level, {
        onStage: (stage) => {
          if (stage === 'building') {
            setDownload({
              stage,
              text: 'Сервер собирает архив. Полная матрица с доказательствами — около полутора минут',
            });
          }
          if (stage === 'done') setDownload(null);
        },
        onBytes: (received, total) => {
          const mb = (received / 1024 / 1024).toFixed(1);
          setDownload({
            stage: 'downloading',
            text: total
              ? `Скачивание: ${mb} из ${(total / 1024 / 1024).toFixed(1)} МБ`
              : `Скачивание: ${mb} МБ`,
          });
        },
      });
    } catch (exc) {
      setDownload(null);
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const startUpload = async (file: File, force = false) => {
    setError(null);
    setUpload({ percent: 0, text: 'Отправка файла' });
    try {
      const outcome = await api.uploadArchive(file, force, {
        onProgress: (sent, total) => {
          const percent = Math.round((sent / total) * 100);
          setUpload({
            percent,
            text: `Отправка: ${(sent / 1024 / 1024).toFixed(1)} из ${(total / 1024 / 1024).toFixed(1)} МБ`,
          });
        },
        onStage: (stage) => {
          if (stage === 'importing') {
            setUpload({
              percent: 100,
              text: 'Файл отправлен. Сервер распаковывает и пишет в базу — это минуты',
            });
          }
        },
      });
      setResult(outcome);
      if (outcome.status === 'DUPLICATE') {
        Modal.confirm({
          title: 'Такой снимок уже есть в базе',
          content: (
            <Space direction="vertical" size={8}>
              <Typography.Text>
                {`Снимок за ${dateLabel(outcome.snapshot_date)} с этим содержимым уже загружен `}
                {`как ${outcome.version_label}`}
                {outcome.origin_stand ? ` со стенда ${outcome.origin_stand}` : ''}.
              </Typography.Text>
              <Typography.Text type="secondary">
                Загрузить ещё раз? Копия ляжет следующей версией, обе останутся
                видимыми и различимыми на витрине.
              </Typography.Text>
            </Space>
          ),
          okText: 'Загрузить копией',
          cancelText: 'Отменить',
          onOk: () => startUpload(file, true),
        });
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setUpload(null);
    }
  };

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="Перенос снимка на другой стенд"
        description={
          'Скачайте архив здесь и загрузите его на другом стенде этой же вкладкой. ' +
          'Снимок появится там отдельной версией своей даты и будет виден на всех ' +
          'экранах наравне с собранными на месте.'
        }
      />

      <Card size="small" title="Выгрузка">
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Space wrap>
            <Typography.Text type="secondary">Дата наблюдения</Typography.Text>
            <Select
              value={snapshotDate}
              onChange={(value) => {
                setSnapshotDate(value);
                setAttemptNo(undefined);
              }}
              style={{ minWidth: 240 }}
              options={snapshots.map((item) => ({
                value: item.snapshot_date,
                label: `${dateLabel(item.snapshot_date)} · ${snapshotStatus(item.status).label}`,
              }))}
            />
            <Typography.Text type="secondary">Версия</Typography.Text>
            <Select
              value={attempt}
              onChange={setAttemptNo}
              style={{ minWidth: 220 }}
              options={versions.map((version) => ({
                value: version.attempt_no,
                label: `${version.label} · ${percent(version.coverage_total)}`,
              }))}
            />
          </Space>

          <Radio.Group value={level} onChange={(event) => setLevel(event.target.value)}>
            <Radio.Button value="evidence">С доказательствами</Radio.Button>
            <Radio.Button value="showcase">Только витрина</Radio.Button>
          </Radio.Group>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {LEVEL_NOTE[level]}
          </Typography.Text>

          <Button
            type="primary"
            icon={<DownloadOutlined />}
            loading={Boolean(download)}
            disabled={!snapshotDate || attempt === undefined || Boolean(download)}
            onClick={() => void startDownload()}
          >
            {download ? 'Готовится…' : 'Скачать архив'}
          </Button>

          {download && (
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              {/* Пока сервер собирает архив, известного процента нет: это одна
                  длинная операция на его стороне. Полоса показывает, что работа
                  идёт, а не сколько её осталось, — врать процентом хуже. */}
              <Progress
                percent={100}
                status="active"
                showInfo={false}
                strokeColor={download.stage === 'building' ? '#faad14' : undefined}
              />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {download.text}
              </Typography.Text>
            </Space>
          )}
        </Space>
      </Card>

      <Card size="small" title="Загрузка">
        {upload && (
          <Space direction="vertical" size={4} style={{ width: '100%', marginBottom: 12 }}>
            <Progress percent={upload.percent} status="active" />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {upload.text}
            </Typography.Text>
          </Space>
        )}
        <Spin spinning={Boolean(upload)} tip={upload?.text ?? ''}>
          <Upload.Dragger
            accept=".tar,.tar.gz,.tgz"
            maxCount={1}
            showUploadList={false}
            beforeUpload={(file) => {
              void startUpload(file as unknown as File);
              // Возврат false отключает собственную отправку Ant Design:
              // файл уходит нашим запросом, с обработкой совпадений.
              return false;
            }}
          >
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">Перетащите архив снимка или выберите файл</p>
            <p className="ant-upload-hint">
              Архив, полученный выгрузкой на другом стенде
            </p>
          </Upload.Dragger>
        </Spin>
      </Card>

      {error && <Alert type="error" showIcon message="Загрузка не удалась" description={error} />}

      {result && result.status === 'IMPORTED' && (
        <Card size="small" title="Снимок загружен">
          <Descriptions
            size="small"
            column={{ xs: 1, sm: 2, md: 4 }}
            items={[
              { key: 'date', label: 'Дата', children: dateLabel(result.snapshot_date) },
              { key: 'version', label: 'Версия', children: result.version_label },
              { key: 'level', label: 'Уровень', children: result.level ?? '—' },
              {
                key: 'evidence',
                label: 'Предложения',
                children: result.evidence_included ? 'перенесены' : 'не переносились',
              },
            ]}
          />
          {result.rows && (
            <Table
              size="small"
              style={{ marginTop: 12 }}
              pagination={false}
              rowKey="table"
              columns={[
                { title: 'Таблица', dataIndex: 'table' },
                { title: 'Строк', dataIndex: 'rows', align: 'right' as const },
              ]}
              dataSource={Object.entries(result.rows).map(([table, rows]) => ({ table, rows }))}
            />
          )}
        </Card>
      )}
    </Space>
  );
}
