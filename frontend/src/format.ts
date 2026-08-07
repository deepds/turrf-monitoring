/**
 * Форматирование.
 *
 * Здесь и только здесь фронтенд трогает числа — и только для показа. Ни одна
 * функция этого файла не складывает, не усредняет и не выводит новую величину:
 * расчёт живёт на сервере.
 */

const MONEY = new Intl.NumberFormat('ru-RU', {
  style: 'currency',
  currency: 'RUB',
  maximumFractionDigits: 0,
});

const MONEY_PRECISE = new Intl.NumberFormat('ru-RU', {
  style: 'currency',
  currency: 'RUB',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return MONEY.format(value);
}

export function moneyPrecise(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return MONEY_PRECISE.format(value);
}

export function percent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return '—';
  return `${(value * 100).toFixed(digits)} %`;
}

export function dateLabel(value: string | null | undefined): string {
  if (!value) return '—';
  const [year, month, day] = value.slice(0, 10).split('-');
  return `${day}.${month}.${year}`;
}

export function shortDate(value: string | null | undefined): string {
  if (!value) return '';
  const [, month, day] = value.slice(0, 10).split('-');
  return `${day}.${month}`;
}

export function dateTimeLabel(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function nightsLabel(nights: number): string {
  const mod10 = nights % 10;
  const mod100 = nights % 100;
  if (mod10 === 1 && mod100 !== 11) return `${nights} ночь`;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return `${nights} ночи`;
  return `${nights} ночей`;
}

export function offersLabel(count: number): string {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return `${count} предложение`;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return `${count} предложения`;
  return `${count} предложений`;
}

export const SERIES_COLORS = [
  '#1677ff',
  '#fa8c16',
  '#52c41a',
  '#eb2f96',
  '#722ed1',
  '#13c2c2',
];
