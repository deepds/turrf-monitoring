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
  ImportResult,
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

  /**
   * Ссылка на архив снимка.
   *
   * Скачивание идёт навигацией браузера, а не fetch: архив полной матрицы —
   * десятки мегабайт, и тянуть его в память страницы ради того, чтобы тут же
   * отдать в файл, незачем.
   */
  archiveUrl: (snapshotDate: string, attemptNo: number, level: 'showcase' | 'evidence') =>
    `${BASE}/market-snapshots/${snapshotDate}/archive?attempt_no=${attemptNo}&level=${level}`,

  /** Загружает архив снимка. `force` — согласие положить копию новой версией. */
  uploadArchive: async (file: File, force = false): Promise<ImportResult> => {
    const body = new FormData();
    body.append('file', file);
    const response = await fetch(`${BASE}/market-snapshots/archive?force=${force}`, {
      method: 'POST',
      body,
    });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        if (payload?.detail) detail = String(payload.detail);
      } catch {
        /* тело не разобралось — остаётся код */
      }
      throw new ApiError(detail, response.status);
    }
    return response.json();
  },

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
