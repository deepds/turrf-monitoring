import { useEffect, useState } from 'react';
import { Link, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { Alert, ConfigProvider, Layout, Menu, Spin, Typography, theme } from 'antd';
import ruRU from 'antd/locale/ru_RU';
import {
  BarChartOutlined,
  CompassOutlined,
  DashboardOutlined,
  HomeOutlined,
  SafetyCertificateOutlined,
  SwapOutlined,
  TableOutlined,
} from '@ant-design/icons';
import { api } from './api/client';
import type { Dictionary, SnapshotListItem } from './api/types';
import { TripsPage } from './pages/TripsPage';
import { TransportPage } from './pages/TransportPage';
import { AirGridPage } from './pages/AirGridPage';
import { HotelsPage } from './pages/HotelsPage';
import { MetricPage } from './pages/MetricPage';
import { CoveragePage } from './pages/CoveragePage';
import { TransferPage } from './pages/TransferPage';
import {
  CollectionProgress,
  NoSnapshotsYet,
  useCollectionProgress,
} from './components/CollectionProgress';

const NAV = [
  { key: '/trips', icon: <CompassOutlined />, label: 'Куда ехать' },
  { key: '/rail', icon: <BarChartOutlined />, label: 'Транспорт' },
  { key: '/air-grid', icon: <TableOutlined />, label: 'Сетка авиа' },
  { key: '/hotels', icon: <HomeOutlined />, label: 'Проживание' },
  { key: '/coverage', icon: <SafetyCertificateOutlined />, label: 'Покрытие и качество' },
  { key: '/transfer', icon: <SwapOutlined />, label: 'Загрузка' },
];

export function App() {
  const location = useLocation();
  const progress = useCollectionProgress();
  const [snapshots, setSnapshots] = useState<SnapshotListItem[]>([]);
  const [dictionary, setDictionary] = useState<Dictionary | undefined>();
  const [ready, setReady] = useState(false);
  const [fatal, setFatal] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.snapshots(), api.dictionary()])
      .then(([snapshotList, dict]) => {
        setSnapshots(snapshotList.snapshots);
        setDictionary(dict);
      })
      .catch((error) => setFatal(error.message))
      .finally(() => setReady(true));
  }, []);

  const selected = NAV.find((item) => location.pathname.startsWith(item.key))?.key ?? '/trips';

  return (
    <ConfigProvider
      locale={ruRU}
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: { colorPrimary: '#1677ff', borderRadius: 6, fontSize: 14 },
      }}
    >
      <Layout style={{ minHeight: '100vh' }}>
        <Layout.Header style={{ display: 'flex', alignItems: 'center', gap: 32 }}>
          {/* Выворотная версия знака: на тёмной полосе фирменный синий читается
              плохо, поэтому буквы белые, а красная точка остаётся — она и есть
              узнаваемая часть логотипа. Начертание набрано текстом, а не
              картинкой: приближение шрифтом честнее растянутого растра, и
              заменить его на официальный SVG можно одной правкой. */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 16, flexShrink: 0 }}>
            <span
              style={{
                fontSize: 16,
                fontWeight: 500,
                letterSpacing: '0.02em',
                whiteSpace: 'nowrap',
                color: '#fff',
              }}
            >
              ТУРИЗМ<span style={{ color: '#E1261C' }}>.</span>РФ
            </span>
            <span
              aria-hidden
              style={{ width: 1, height: 24, background: 'rgba(255,255,255,0.22)' }}
            />
            <Typography.Text
              style={{ color: 'rgba(255,255,255,0.72)', fontSize: 15, whiteSpace: 'nowrap' }}
            >
              Стоимость поездок
            </Typography.Text>
          </div>
          <Menu
            theme="dark"
            mode="horizontal"
            selectedKeys={[selected]}
            style={{ flex: 1, minWidth: 0 }}
            items={NAV.map((item) => ({
              key: item.key,
              icon: item.icon,
              label: <Link to={item.key}>{item.label}</Link>,
            }))}
          />
          <Typography.Text style={{ color: 'rgba(255,255,255,0.55)', fontSize: 12 }}>
            <DashboardOutlined /> данные только из снимков
          </Typography.Text>
        </Layout.Header>

        <Layout.Content style={{ padding: 24, maxWidth: 1600, width: '100%', margin: '0 auto' }}>
          {fatal && (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 16 }}
              message="Не удалось загрузить состояние витрины"
              description={fatal}
            />
          )}
          {/* Ход сбора — над экранами и независимо от них. Внутри шапки снимка
              он был виден только при наличии готового снимка, то есть пропадал
              на свежем стенде: там ни одного закрытого дня ещё нет, витрина
              отвечает 404, и вместе с ней исчезал единственный признак того,
              что система работает. */}
          {progress && !progress.is_closed && (
            <div style={{ marginBottom: 16 }}>
              <CollectionProgress progress={progress} />
            </div>
          )}
          {ready && !snapshots.length && (
            <div style={{ marginBottom: 16 }}>
              <NoSnapshotsYet collecting={Boolean(progress && !progress.is_closed)} />
            </div>
          )}
          {!ready ? (
            <Spin size="large" style={{ display: 'block', margin: '80px auto' }} />
          ) : (
            <Routes>
              <Route path="/" element={<Navigate to="/trips" replace />} />
              <Route path="/trips" element={<TripsPage dictionary={dictionary} />} />
              <Route
                path="/rail"
                element={<TransportPage snapshots={snapshots} dictionary={dictionary} />}
              />
              <Route
                path="/hotels"
                element={<HotelsPage snapshots={snapshots} dictionary={dictionary} />}
              />
              <Route path="/air-grid" element={<AirGridPage snapshots={snapshots} />} />
              <Route path="/metrics/:metricId" element={<MetricPage dictionary={dictionary} />} />
              <Route path="/coverage" element={<CoveragePage snapshots={snapshots} />} />
              <Route path="/transfer" element={<TransferPage snapshots={snapshots} />} />
              <Route path="*" element={<Navigate to="/trips" replace />} />
            </Routes>
          )}
        </Layout.Content>

        <Layout.Footer style={{ textAlign: 'center', color: 'rgba(0,0,0,0.45)', fontSize: 12 }}>
          Витрина работает только по заранее собранным Market Snapshots и не обращается к внешним
          источникам. Любую цифру можно раскрыть до исходных предложений.
        </Layout.Footer>
      </Layout>
    </ConfigProvider>
  );
}
