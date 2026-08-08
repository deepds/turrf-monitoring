/**
 * Клиент API.
 *
 * Единственный источник данных для дашборда. Внешних адресов здесь нет и быть
 * не может: витрина не обращается к Туту, РЖД или любому другому источнику.
 */

import type {
  AirChartResponse,
  AirGridResponse,
  CoverageResponse,
  CycleProgress,
  Dictionary,
  HotelChartResponse,
  MetricDetails,
  OffersResponse,
  RailChartResponse,
  SnapshotContext,
  SnapshotListItem,
  TripsResponse,
} from './types';

const BASE = '/api/v1';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, params?: Record<string, unknown>): Promise<T> {
  const url = new URL(BASE + path, window.location.origin);
  Object.entries(params ?? {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      url.searchParams.set(key, String(value));
    }
  });
  const response = await fetch(url.toString(), { headers: { Accept: 'application/json' } });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* тело может быть не JSON — сообщение по коду ответа остаётся верным */
    }
    throw new ApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

export const api = {
  snapshots: () => request<{ snapshots: SnapshotListItem[] }>('/market-snapshots'),

  latestSnapshot: () => request<SnapshotContext & { overview: any }>('/market-snapshots/latest'),

  /** Состояние сбора за текущие сутки. `progress: null` — снимок ещё не открыт. */
  currentCycle: () => request<{ progress: CycleProgress | null }>('/market-snapshots/current'),

  origins: () => request<{ origins: { code: string; name: string }[] }>('/showcase/origins'),

  dictionary: () => request<Dictionary>('/reference/dictionary'),

  methodology: () => request<Record<string, any>>('/reference/methodology'),

  trips: (params: {
    origin: string;
    departure_date: string;
    return_date: string;
    transport_mode: string;
    stars: number;
  }) => request<TripsResponse>('/showcase/trips', params),

  railChart: (params: { origin: string; destination?: string; snapshot_date?: string }) =>
    request<RailChartResponse>('/charts/rail', params),

  airChart: (params: {
    origin: string;
    nights: number;
    destination?: string;
    snapshot_date?: string;
  }) => request<AirChartResponse>('/charts/air', params),

  airGrid: (params: { origin: string; destination: string; snapshot_date?: string }) =>
    request<AirGridResponse>('/charts/air-grid', params),

  hotelChart: (params: { stars: number; snapshot_date?: string }) =>
    request<HotelChartResponse>('/charts/hotels', params),

  metric: (metricId: number) => request<MetricDetails>(`/metrics/${metricId}`),

  metricOffers: (metricId: number, included?: boolean) =>
    request<OffersResponse>(`/metrics/${metricId}/offers`, { included }),

  coverage: (snapshotDate: string, attemptNo?: number) =>
    request<CoverageResponse>(`/coverage/${snapshotDate}`, { attempt_no: attemptNo }),

  exportUrl: (metricId: number, fmt: 'csv' | 'xlsx') =>
    `${BASE}/exports/metrics/${metricId}?fmt=${fmt}`,
};
