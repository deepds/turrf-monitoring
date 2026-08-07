/**
 * Типы ответов API.
 *
 * Все числа приходят посчитанными. Фронтенд их только форматирует: медиана,
 * сумма составляющих поездки и уровень уверенности считаются на сервере — это
 * архитектурное требование, а не стилистическое предпочтение.
 */

export type Confidence = 'HIGH' | 'MEDIUM' | 'LOW';
export type TransportMode = 'RAIL' | 'AIR';

export interface SnapshotContext {
  snapshot_id: number;
  snapshot_date: string;
  status: string;
  attempt_no: number;
  is_synthetic: boolean;
  is_fallback: boolean;
  published_at: string | null;
  coverage_total: number;
  coverage_rail: number;
  coverage_air: number;
  coverage_hotel: number;
  publication_notes: PublicationNote[];
  calculation_run_id: number;
  methodology_version: string;
}

export interface PublicationNote {
  code: string;
  severity: 'CRITICAL' | 'WARNING';
  message: string;
  violations?: string[];
}

export interface CityRef {
  code: string;
  name: string;
}

export interface TripRow {
  origin: CityRef;
  destination: CityRef;
  departure_date: string;
  return_date: string;
  nights: number;
  transport_mode: TransportMode;
  stars: number;
  currency: string;
  transport_median: number | null;
  transport_min: number | null;
  accommodation_median: number | null;
  accommodation_min: number | null;
  total_median: number | null;
  total_min: number | null;
  offers_count: number;
  sources_count: number;
  quality_score: number;
  confidence_level: Confidence;
  is_partial: boolean;
  is_complete: boolean;
  warning_codes: string[];
  missing_components: string[];
  transport_metric_ids: number[];
  accommodation_metric_id: number | null;
  transport_composition: string;
}

export interface TripsResponse {
  context: SnapshotContext;
  request: Record<string, unknown>;
  label: string;
  trips: TripRow[];
}

export interface ChartPoint {
  metric_id: number;
  date: string | null;
  day_offset: number | null;
  median: number | null;
  min: number | null;
  offers_count: number;
  sources_count: number;
  confidence_level: Confidence;
  quality_score: number;
  is_partial: boolean;
  is_no_market: boolean;
  no_market_reason: string | null;
  warning_codes: string[];
}

export interface RailSeries {
  destination: CityRef;
  points: ChartPoint[];
}

export interface RailChartResponse {
  context: SnapshotContext;
  mode: 'OVERVIEW' | 'ROUTE_DETAIL';
  origin: CityRef;
  parameters: Record<string, unknown>;
  series: RailSeries[];
}

export interface AirSeries {
  destination: CityRef;
  points: (ChartPoint & { return_date: string | null })[];
}

export interface AirChartResponse {
  context: SnapshotContext;
  mode: 'OVERVIEW' | 'ROUTE_DETAIL';
  origin: CityRef;
  parameters: Record<string, unknown>;
  available_nights: number[];
  series: AirSeries[];
}

export interface AirGridCell {
  metric_id: number;
  departure_date: string | null;
  return_date: string | null;
  nights: number | null;
  day_offset: number | null;
  median: number | null;
  min: number | null;
  offers_count: number;
  sources_count: number;
  confidence_level: Confidence;
  is_partial: boolean;
  is_no_market: boolean;
  no_market_reason: string | null;
  warning_codes: string[];
}

export interface AirGridResponse {
  context: SnapshotContext;
  origin: CityRef;
  destination: CityRef;
  parameters: Record<string, unknown>;
  departure_dates: string[];
  nights_options: number[];
  scale: {
    min: number | null;
    max: number | null;
    priced_cells: number;
    no_market_cells: number;
    total_cells: number;
  };
  cells: AirGridCell[];
}

export interface HotelSeries {
  city: CityRef;
  points: ChartPoint[];
}

export interface HotelChartResponse {
  context: SnapshotContext;
  parameters: Record<string, unknown>;
  series: HotelSeries[];
}

export interface SourceAttempt {
  source_attempt_id: number;
  source_code: string;
  execution_scope: string;
  outcome: string;
  no_market_reason: string | null;
  requested_at: string;
  fetched_at: string | null;
  latency_ms: number | null;
  http_calls: number;
  pages_read: number;
  total_matched: number | null;
  is_partial: boolean;
  partial_reason: string | null;
  offers_parsed: number;
  error_code: string | null;
  error_message: string | null;
  connector_version: string;
  source_tool_version: string | null;
  diagnostics: Record<string, unknown>;
}

export interface MetricDetails {
  metric_id: number;
  metric_type: string;
  snapshot_id: number;
  snapshot_date: string;
  snapshot_status: string;
  is_synthetic: boolean;
  calculation_run_id: number;
  methodology_version: string;
  computed_at: string;
  fetched_at: string | null;
  currency: string;
  median_price: number | null;
  min_price: number | null;
  max_price: number | null;
  p25_price: number | null;
  p75_price: number | null;
  offers_count: number;
  offers_excluded: number;
  sources_count: number;
  source_coverage: number;
  quality_score: number;
  confidence_level: Confidence;
  is_partial: boolean;
  is_no_market: boolean;
  no_market_reason: string | null;
  warning_codes: string[];
  per_source: Record<string, { median: number; offers: number }>;
  observation: Record<string, any>;
  source_attempts: SourceAttempt[];
}

export interface OfferRow {
  offer_id: number;
  is_included: boolean;
  exclusion_reason: string | null;
  exclusion_detail: string | null;
  source_code: string;
  kind: string;
  price: number;
  source_price: number | null;
  price_basis: string;
  currency: string;
  fetched_at: string;
  departure_at: string | null;
  arrival_at: string | null;
  return_departure_at: string | null;
  check_in: string | null;
  check_out: string | null;
  nights: number | null;
  route: string | null;
  carrier: string | null;
  vehicle: string | null;
  car_type: string | null;
  service_class: string | null;
  fare_family: string | null;
  refundable: boolean | null;
  property_name: string | null;
  stars: number | null;
  property_type: string | null;
  room_name: string | null;
  validation_flags: string[];
  fingerprint: string;
  equivalence_key: string;
  deeplink: string | null;
  provenance: {
    source_attempt_id: number;
    raw_response_id: number | null;
    raw_storage_ref: string | null;
    raw_endpoint: string | null;
    raw_sha256: string | null;
    raw_page: number | null;
    requested_at: string | null;
  };
}

export interface OffersResponse {
  metric_id: number;
  count: number;
  included_count: number;
  excluded_count: number;
  offers: OfferRow[];
}

export interface Dictionary {
  exclusion_reasons: Record<string, string>;
  warning_codes: Record<string, string>;
  expected_matrix: Record<string, number>;
}

export interface SnapshotListItem {
  snapshot_date: string;
  status: string;
  is_synthetic: boolean;
  coverage_total: number;
  published_at: string | null;
}

export interface FamilyCoverage {
  family: string;
  planned: number;
  completed: number;
  successful: number;
  partial: number;
  no_market: number;
  failed: number;
  missing: number;
  completion: number;
  data_share: number;
}

export interface CoverageResponse {
  overview: {
    snapshot: Record<string, any>;
    quality_summary: Record<string, any>;
    confidence_distribution: Record<string, Record<string, number>>;
    sources: Array<Record<string, any>>;
  };
  coverage: { snapshot_id: number; total: FamilyCoverage; by_family: Record<string, FamilyCoverage> };
  matrix: { snapshot_id: number; cells: Record<string, Record<string, Record<string, number>>> };
  holes: { count: number; job_ids: number[] };
}
