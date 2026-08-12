/**
 * Подписи кодов на экране.
 *
 * В базе и в API коды остаются английскими: их значения попадают в записи
 * наблюдений, в выгрузки и в перенесённые снимки, и переименование значения —
 * это миграция данных, а не правка текста. Перевод живёт здесь, на границе с
 * пользователем.
 *
 * Собрано в одном файле намеренно. Прежде подписи расползались по экранам:
 * уверенность переводилась в индикаторах и оставалась `HIGH` на экране
 * покрытия, тип метрики был расшифрован на карточке метрики и не был на
 * распределении уверенности. Один код, увиденный пользователем в двух видах,
 * читается как две разные вещи.
 */

import type { Confidence } from './api/types';

/** Состояние снимка. `READY` в базе — «Готов» на экране. */
export const SNAPSHOT_STATUS: Record<string, { color: string; label: string }> = {
  READY: { color: 'green', label: 'Готов' },
  DEGRADED: { color: 'orange', label: 'Готов с оговорками' },
  FAILED: { color: 'red', label: 'Не готов' },
  PLANNING: { color: 'default', label: 'Планируется' },
  COLLECTING: { color: 'blue', label: 'Идёт сбор' },
  RECOVERING: { color: 'blue', label: 'Идёт досбор' },
  CALCULATING: { color: 'blue', label: 'Идёт расчёт' },
};

export function snapshotStatus(code: string): { color: string; label: string } {
  return SNAPSHOT_STATUS[code] ?? { color: 'default', label: code };
}

/** Уверенность в метрике. */
export const CONFIDENCE: Record<Confidence, { color: string; label: string; hint: string }> = {
  HIGH: {
    color: 'green',
    label: 'Высокая',
    hint: 'Полная выдача, достаточная выборка, все ожидаемые источники ответили',
  },
  MEDIUM: {
    color: 'gold',
    label: 'Средняя',
    hint: 'Выборка меньше целевой, либо выдача обрезана, либо один из источников не ответил',
  },
  LOW: {
    color: 'red',
    label: 'Низкая',
    hint: 'Очень малая выборка: цифру нельзя считать описанием рынка',
  },
};

export function confidence(code: string): { color: string; label: string; hint: string } {
  return CONFIDENCE[code as Confidence] ?? { color: 'default', label: code, hint: '' };
}

/**
 * Тип метрики — коротко, для заголовков и легенд.
 *
 * Полные определения, где важна каждая оговорка методики, живут на карточке
 * метрики: здесь нужна подпись, помещающаяся в заголовок.
 */
export const METRIC_TYPE: Record<string, string> = {
  RAIL_LEG: 'ЖД, плечо в одну сторону',
  AIR_ROUND_TRIP: 'Авиа, круговой тариф',
  HOTEL_STAY: 'Проживание, весь срок',
  HOTEL_NIGHT: 'Проживание, одна ночь',
};

export function metricType(code: string): string {
  return METRIC_TYPE[code] ?? code;
}

/**
 * Исход обращения к источнику.
 *
 * Различать их важнее, чем кажется: `CIRCUIT_OPEN` и `BUDGET_EXHAUSTED` — это
 * **наши** решения перестать спрашивать, а не отказы источника. Показывать их
 * одним словом «отказ» значило бы приписывать источнику то, чего он не делал.
 */
export const OUTCOME: Record<string, { label: string; hint: string }> = {
  SUCCESS: { label: 'Успешно', hint: 'Источник ответил полной выдачей' },
  PARTIAL: {
    label: 'Частично',
    hint: 'Данные получены, но выборка неполна: обрезана выдача либо кончился бюджет времени',
  },
  NO_MARKET: {
    label: 'Нет рынка',
    hint: 'Источник ответил, предложений нет. Это ответ о рынке, а не сбой',
  },
  TIMEOUT: { label: 'Таймаут', hint: 'Источник не ответил за отведённое время' },
  TRANSPORT_ERROR: {
    label: 'Ошибка связи',
    hint: 'Источник вернул ошибку 5xx либо соединение не состоялось',
  },
  RATE_LIMITED: { label: 'Лимит источника', hint: 'Источник ограничил темп обращений' },
  CIRCUIT_OPEN: {
    label: 'Пропущено размыкателем',
    hint: 'Наше решение перестать спрашивать после серии отказов, а не отказ источника',
  },
  BUDGET_EXHAUSTED: {
    label: 'Не хватило времени',
    hint: 'Бюджет пачки кончился раньше, чем дошла очередь до этого наблюдения',
  },
  SCHEMA_ERROR: { label: 'Ответ не разобран', hint: 'Источник ответил в неожиданном формате' },
  FAILED: { label: 'Отказ', hint: 'Ни один источник не дал пригодного результата' },
};

export function outcome(code: string): { label: string; hint: string } {
  return OUTCOME[code] ?? { label: code, hint: '' };
}

/**
 * Причина, по которой предложение не пошло в расчёт — коротко, для плашки.
 *
 * Здесь только ярлык. Развёрнутая формулировка приходит с сервера
 * (`/reference/dictionary`) и показывается подсказкой: она привязана к
 * методике и меняется вместе с ней, тогда как ярлык должен помещаться в
 * колонку и оставаться одинаковым от экрана к экрану.
 *
 * Ярлык называет признак предложения, а не приговор ему. `WRONG_CAR_TYPE` —
 * «не тот вагон», а не «плохой вагон»: исключение означает, что предложение
 * не отвечает наблюдаемой величине, и ничего не говорит о самом предложении.
 */
export const EXCLUSION_REASON: Record<string, string> = {
  NOT_DIRECT: 'Не прямой',
  WRONG_CAR_TYPE: 'Не тот вагон',
  WRONG_CABIN: 'Не эконом',
  REFUNDABLE_FARE: 'Возвратный тариф',
  NOT_ROUND_TRIP: 'Не круговой',
  WRONG_PROPERTY_TYPE: 'Не гостиница',
  WRONG_STARS: 'Не та звёздность',
  WRONG_ROOM_CATEGORY: 'Не та категория номера',
  WRONG_ROUTE: 'Не тот маршрут',
  WRONG_DATES: 'Не те даты',
  NON_POSITIVE_PRICE: 'Нет цены',
  WRONG_CURRENCY: 'Другая валюта',
  DISABLED_PLACES_GROUP: 'Льготные места',
  SALE_FORBIDDEN: 'Продажа закрыта',
  NO_PLACES: 'Мест нет',
  FARE_COLLAPSED_NOT_CHEAPEST: 'Не самый дешёвый тариф',
  DUPLICATE: 'Дубликат',
  STATISTICAL_OUTLIER: 'Выброс',
  UNCLASSIFIED_CAR_TYPE: 'Тип вагона неизвестен',
};

export function exclusionReason(code: string): string {
  return EXCLUSION_REASON[code] ?? code;
}

/**
 * Ворота публикации — коротко, для полосы над витриной.
 *
 * Сервер отдаёт запись вида «Ворота COLLECTION_COMPLETENESS не пройдены»:
 * половина фразы по-русски, половина именем перечисления. Имя остаётся в
 * `publication_notes` как есть — на записи ссылаются прошлые снимки, — а
 * читается человеку отсюда.
 */
export const GATE: Record<string, string> = {
  COLLECTION_COMPLETENESS: 'полнота сбора',
  DATA_VALIDITY: 'достоверность данных',
  CALCULATION_VALIDITY: 'правильность расчёта',
  PUBLICATION_VALIDITY: 'пригодность к публикации',
};

export function gate(code: string): string {
  return GATE[code] ?? code;
}

/**
 * Правила проверки — то, что перечисляется в нарушениях ворот.
 *
 * Формулировка называет нарушенное требование, а не проступок: правило
 * `PRICE_POSITIVE` нарушено, когда цена неположительна, и подпись говорит
 * именно это.
 */
export const VALIDATION_RULE: Record<string, string> = {
  NO_TERMINAL_OUTCOME: 'наблюдения остались в работе',
  SILENT_SOURCE_SKIP: 'завершено без обращения к источнику',
  PRICE_POSITIVE: 'цена неположительна',
  CURRENCY_MATCHES_PROFILE: 'валюта не та, что в методике',
  ROUTE_MATCHES_REQUEST: 'маршрут не совпал с запросом',
  DATES_MATCH_REQUEST: 'даты не совпали с запросом',
  RAIL_COMPARTMENT_ONLY: 'тип вагона вне методики',
  AIR_DIRECT_ONLY: 'рейс не прямой',
  AIR_ROUND_TRIP_NOT_SYNTHESIZED: 'круговой тариф собран из двух односторонних',
  HOTEL_PROPERTY_TYPE_ALLOWED: 'тип размещения вне методики',
  HOTEL_STARS_ALLOWED: 'звёздность вне запрошенной',
  HOTEL_ROOM_CATEGORY_ALLOWED: 'категория номера вне методики',
  HOTEL_STAY_NOT_DERIVED_FROM_NIGHTS: 'цена срока получена умножением цены ночи',
  NO_DUPLICATE_EQUIVALENCE_KEY: 'одно предложение учтено дважды',
  PAGINATION_STATUS_KNOWN: 'полнота выдачи источника неизвестна',
  GOLDEN_DATASET: 'Golden Dataset не сошёлся',
  METRIC_DTO_COMPLETE: 'метрика отдана без обязательного поля',
  PROVENANCE_PRESENT: 'цену нельзя раскрыть до исходного предложения',
};

export function validationRule(code: string): string {
  return VALIDATION_RULE[code] ?? code;
}

/**
 * Запись публикации целиком: сообщение сервера с переведённым именем ворот.
 *
 * Проценты и числа сервер считает сам и кладёт в текст — переписывать его
 * целиком значило бы их потерять. Поэтому заменяется только имя.
 */
export function publicationNote(code: string, message: string): string {
  if (!code.startsWith('GATE_')) return message;
  const name = gate(code.slice('GATE_'.length));
  return `Не пройдены ворота: ${name}`;
}

/** Семейство наблюдений. */
export const FAMILY: Record<string, string> = {
  RAIL: 'Железная дорога',
  AIR: 'Авиа',
  HOTEL: 'Проживание',
  TOTAL: 'Всего',
};

export function family(code: string): string {
  return FAMILY[code] ?? code;
}
