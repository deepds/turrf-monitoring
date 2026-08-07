/**
 * Тепловая карта наблюдений авиа: дата вылета × длительность поездки.
 *
 * Три решения, без которых карта начинает врать:
 *
 * 1. **Шкала — на маршрут.** Она приходит с сервера и построена по видимым
 *    ценам одного направления. Общая шкала для всех маршрутов покрасила бы
 *    дальнее направление сплошь «дорогим» и превратила бы цвет в сравнение
 *    маршрутов, которым он не является.
 * 2. **Пустая клетка не участвует в шкале.** «Рынка нет» и «пары дат не
 *    существует» — разные виды пустоты, и обе показаны нецветом: серым и
 *    штриховкой. Серый в цветовой шкале читался бы как «дёшево».
 * 3. **Одна hue, светлое → тёмное.** Радуга по цене создаёт границы там, где
 *    их нет в данных.
 */

import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Space, Typography } from 'antd';
import type { AirGridResponse, AirGridCell } from '../api/types';
import { money, shortDate } from '../format';

/** Последовательная шкала: одна hue, светлое → тёмное. */
const RAMP = [
  '#cde2fb',
  '#b7d3f6',
  '#9ec5f4',
  '#86b6ef',
  '#6da7ec',
  '#5598e7',
  '#3987e5',
  '#2a78d6',
  '#256abf',
  '#1c5cab',
  '#184f95',
  '#104281',
  '#0d366b',
];

const CELL = 22;
const GAP = 2;
const LABEL_X = 44;
const LABEL_Y = 34;

function rampColor(value: number, min: number, max: number): string {
  if (max <= min) return RAMP[Math.floor(RAMP.length / 2)];
  const t = (value - min) / (max - min);
  return RAMP[Math.min(RAMP.length - 1, Math.max(0, Math.round(t * (RAMP.length - 1))))];
}

interface Props {
  data: AirGridResponse;
}

export function AirGrid({ data }: Props) {
  const navigate = useNavigate();
  const [hover, setHover] = useState<{ cell: AirGridCell; x: number; y: number } | null>(null);

  const { departure_dates: dates, nights_options: nights, scale } = data;

  const index = useMemo(() => {
    const map = new Map<string, AirGridCell>();
    data.cells.forEach((cell) => map.set(`${cell.departure_date}|${cell.nights}`, cell));
    return map;
  }, [data.cells]);

  if (!dates.length || !nights.length) {
    return <Typography.Text type="secondary">Нет наблюдений по этому маршруту</Typography.Text>;
  }

  const width = LABEL_X + dates.length * (CELL + GAP);
  const height = LABEL_Y + nights.length * (CELL + GAP);
  const hasScale = scale.min !== null && scale.max !== null;

  return (
    <div style={{ position: 'relative' }}>
      <div style={{ overflowX: 'auto', paddingBottom: 8 }}>
        <svg
          width={width}
          height={height}
          role="img"
          aria-label="Сетка стоимости авиа: дата вылета по горизонтали, длительность поездки по вертикали"
        >
          <defs>
            {/* Штриховка для «пары дат не существует»: это не цвет и не ноль. */}
            <pattern id="tmo-absent" width="6" height="6" patternTransform="rotate(45)"
                     patternUnits="userSpaceOnUse">
              <rect width="6" height="6" fill="#f5f6f8" />
              <line x1="0" y1="0" x2="0" y2="6" stroke="#d9d9d9" strokeWidth="1.5" />
            </pattern>
          </defs>

          {dates.map((date, column) =>
            column % 3 === 0 ? (
              <text
                key={`x-${date}`}
                x={LABEL_X + column * (CELL + GAP) + CELL / 2}
                y={LABEL_Y - 12}
                textAnchor="middle"
                fontSize={11}
                fill="rgba(0,0,0,0.55)"
              >
                {shortDate(date)}
              </text>
            ) : null,
          )}

          {nights.map((night, row) =>
            night % 2 === 1 || night === nights[0] ? (
              <text
                key={`y-${night}`}
                x={LABEL_X - 8}
                y={LABEL_Y + row * (CELL + GAP) + CELL / 2 + 4}
                textAnchor="end"
                fontSize={11}
                fill="rgba(0,0,0,0.55)"
              >
                {night}
              </text>
            ) : null,
          )}

          {nights.map((night, row) =>
            dates.map((date, column) => {
              const cell = index.get(`${date}|${night}`);
              const x = LABEL_X + column * (CELL + GAP);
              const y = LABEL_Y + row * (CELL + GAP);

              if (!cell) {
                return (
                  <rect
                    key={`${date}-${night}`}
                    x={x}
                    y={y}
                    width={CELL}
                    height={CELL}
                    rx={3}
                    fill="url(#tmo-absent)"
                  />
                );
              }

              const fill =
                cell.median !== null && hasScale
                  ? rampColor(cell.median, scale.min as number, scale.max as number)
                  : '#eceff3';

              return (
                <rect
                  key={`${date}-${night}`}
                  x={x}
                  y={y}
                  width={CELL}
                  height={CELL}
                  rx={3}
                  fill={fill}
                  stroke={cell.is_partial ? '#eb6834' : 'none'}
                  strokeWidth={cell.is_partial ? 1.5 : 0}
                  style={{ cursor: 'pointer' }}
                  onMouseEnter={(event) =>
                    setHover({
                      cell,
                      x: event.clientX,
                      y: event.clientY,
                    })
                  }
                  onMouseMove={(event) =>
                    setHover({ cell, x: event.clientX, y: event.clientY })
                  }
                  onMouseLeave={() => setHover(null)}
                  onClick={() => navigate(`/metrics/${cell.metric_id}`)}
                />
              );
            }),
          )}
        </svg>
      </div>

      <Legend scale={scale} />

      {hover && (
        <div
          style={{
            position: 'fixed',
            left: Math.min(hover.x + 14, window.innerWidth - 260),
            top: hover.y + 14,
            zIndex: 1000,
            background: '#fff',
            border: '1px solid #d9d9d9',
            borderRadius: 6,
            padding: '8px 10px',
            boxShadow: '0 4px 14px rgba(0,0,0,0.12)',
            pointerEvents: 'none',
            maxWidth: 250,
          }}
        >
          <Typography.Text strong style={{ fontSize: 13 }}>
            {shortDate(hover.cell.departure_date)} → {shortDate(hover.cell.return_date)}
          </Typography.Text>
          <div style={{ fontSize: 12, color: 'rgba(0,0,0,0.55)' }}>
            {hover.cell.nights} ноч. · {hover.cell.offers_count} предл.
          </div>
          <div style={{ fontSize: 14, marginTop: 4 }}>
            {hover.cell.is_no_market
              ? hover.cell.no_market_reason === 'NO_DIRECT_SERVICE'
                ? 'Прямых рейсов нет'
                : 'Предложений нет'
              : `${money(hover.cell.median)} · от ${money(hover.cell.min)}`}
          </div>
          {hover.cell.is_partial && (
            <div style={{ fontSize: 11, color: '#d4380d', marginTop: 2 }}>
              выдача обрезана — медиана смещена вниз
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Legend({ scale }: { scale: AirGridResponse['scale'] }) {
  return (
    <Space size={20} wrap style={{ marginTop: 4 }}>
      <Space size={6} align="center">
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {money(scale.min)}
        </Typography.Text>
        <span
          style={{
            display: 'inline-block',
            width: 130,
            height: 10,
            borderRadius: 5,
            background: `linear-gradient(to right, ${RAMP[0]}, ${RAMP[RAMP.length - 1]})`,
          }}
        />
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {money(scale.max)}
        </Typography.Text>
      </Space>

      <Space size={6} align="center">
        <span
          style={{
            display: 'inline-block',
            width: 14,
            height: 14,
            borderRadius: 3,
            background: '#eceff3',
          }}
        />
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          рынка нет
        </Typography.Text>
      </Space>

      <Space size={6} align="center">
        <span
          style={{
            display: 'inline-block',
            width: 14,
            height: 14,
            borderRadius: 3,
            background:
              'repeating-linear-gradient(45deg, #f5f6f8 0 3px, #d9d9d9 3px 4.5px)',
          }}
        />
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          пары дат нет в горизонте
        </Typography.Text>
      </Space>

      <Space size={6} align="center">
        <span
          style={{
            display: 'inline-block',
            width: 14,
            height: 14,
            borderRadius: 3,
            background: '#9ec5f4',
            border: '1.5px solid #eb6834',
          }}
        />
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          выдача обрезана
        </Typography.Text>
      </Space>

      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        шкала построена по этому маршруту — цвета разных маршрутов несравнимы
      </Typography.Text>
    </Space>
  );
}
