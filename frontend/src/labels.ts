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
