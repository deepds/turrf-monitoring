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

  /**
   * Скачивает архив снимка, сообщая о ходе.
   *
   * Не навигацией браузера, как раньше: сервер сначала **формирует** архив —
   * полная матрица с доказательствами собирается около полутора минут, — и всё
   * это время окно не показывает ничего. Пользователь видит замерший экран и
   * жмёт кнопку повторно, запуская вторую сборку.
   *
   * `onStage` вызывается при смене этапа, `onBytes` — по мере получения тела.
   * Общий размер известен не всегда: сервер может отдавать потоком без
   * `Content-Length`, и тогда процент показать нечем — остаются байты.
   */
  downloadArchive: async (
    snapshotDate: string,
    attemptNo: number,
    level: 'showcase' | 'evidence',
    hooks: {
      onStage?: (stage: 'building' | 'downloading' | 'done') => void;
      onBytes?: (received: number, total: number | null) => void;
    } = {},
  ): Promise<void> => {
    hooks.onStage?.('building');
    const url = `${BASE}/market-snapshots/${snapshotDate}/archive?attempt_no=${attemptNo}&level=${level}`;
    const response = await fetch(url);
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

    hooks.onStage?.('downloading');
    const header = response.headers.get('Content-Length');
    const total = header ? Number(header) : null;
    const reader = response.body?.getReader();
    const chunks: BlobPart[] = [];
    let received = 0;
    if (reader) {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        received += value.byteLength;
        hooks.onBytes?.(received, total);
      }
    } else {
      chunks.push(await response.blob());
    }

    const disposition = response.headers.get('Content-Disposition') ?? '';
    const match = /filename="?([^";]+)"?/.exec(disposition);
    const name = match?.[1] ?? `tmo-snapshot-${snapshotDate}-v${attemptNo}-${level}.tar`;

    const href = URL.createObjectURL(new Blob(chunks, { type: 'application/x-tar' }));
    const anchor = document.createElement('a');
    anchor.href = href;
    anchor.download = name;
    anchor.click();
    URL.revokeObjectURL(href);
    hooks.onStage?.('done');
  },

  /**
   * Загружает архив снимка, сообщая о ходе отправки.
   *
   * `XMLHttpRequest`, а не `fetch`: ход отправки тела в `fetch` не наблюдаем ни
   * в одном браузере, а отправляются десятки мегабайт. `force` — согласие
   * положить копию новой версией.
   */
  uploadArchive: (
    file: File,
    force = false,
    hooks: {
      onProgress?: (sent: number, total: number) => void;
      onStage?: (stage: 'sending' | 'importing') => void;
    } = {},
  ): Promise<ImportResult> =>
    new Promise((resolve, reject) => {
      const body = new FormData();
      body.append('file', file);
      const request = new XMLHttpRequest();
      request.open('POST', `${BASE}/market-snapshots/archive?force=${force}`);
      request.upload.onprogress = (event) => {
        hooks.onProgress?.(event.loaded, event.total || file.size);
        // Тело ушло целиком — дальше сервер распаковывает и пишет в базу, и
        // это самая долгая часть: полная матрица кладётся минутами.
        if (event.total && event.loaded >= event.total) hooks.onStage?.('importing');
      };
      request.onload = () => {
        if (request.status >= 200 && request.status < 300) {
          try {
            resolve(JSON.parse(request.responseText));
          } catch {
            reject(new ApiError('Ответ сервера не разобран', request.status));
          }
          return;
        }
        let detail = `HTTP ${request.status}`;
        try {
          const payload = JSON.parse(request.responseText);
          if (payload?.detail) detail = String(payload.detail);
        } catch {
          /* тело не разобралось — остаётся код */
        }
        reject(new ApiError(detail, request.status));
      };
      request.onerror = () => reject(new ApiError('Связь с сервером потеряна', 0));
      hooks.onStage?.('sending');
      request.send(body);
    }),

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
