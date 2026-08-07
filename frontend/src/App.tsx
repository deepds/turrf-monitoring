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

const NAV = [
  { key: '/trips', icon: <CompassOutlined />, label: 'Куда ехать' },
  { key: '/rail', icon: <BarChartOutlined />, label: 'Транспорт' },
  { key: '/air-grid', icon: <TableOutlined />, label: 'Сетка авиа' },
  { key: '/hotels', icon: <HomeOutlined />, label: 'Проживание' },
  { key: '/coverage', icon: <SafetyCertificateOutlined />, label: 'Покрытие и качество' },
];

export function App() {
  const location = useLocation();
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
          <Typography.Text strong style={{ color: '#fff', fontSize: 16, whiteSpace: 'nowrap' }}>
            Стоимость поездок
          </Typography.Text>
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
