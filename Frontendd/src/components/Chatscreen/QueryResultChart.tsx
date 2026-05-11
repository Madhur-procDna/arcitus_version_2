'use client';

import React, { useMemo, useRef } from 'react';
import {
  ArcElement,
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  Filler,
  Legend,
  LinearScale,
  LineElement,
  PointElement,
  Tooltip,
} from 'chart.js';
import { Bar, Doughnut, Line, Scatter } from 'react-chartjs-2';

ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  BarElement,
  ArcElement,
  Tooltip,
  Legend,
  Filler,
);

export type ChartKind = 'bar' | 'pie' | 'line' | 'scatter';

export interface ChartPayload {
  kind: ChartKind;
  data: Record<string, unknown>[];
  title?: string;
  description?: string;
  lineSeriesKeys?: string[];
  yAxisLabel?: string;
}

/** Structured chart recommendation from LLM JSON response (Task 4 format). */
export interface ChartRecommendation {
  show_chart: boolean;
  chart_type: 'bar' | 'line' | 'pie' | 'scatter' | 'none';
  x_axis: string;
  y_axis: string;
  title: string;
  rationale: string;
}

/** Arcutis brand color palette — teal/blue primary, muted secondary tones. */
const PALETTE = [
  '#0b5fa5', // Arcutis blue primary
  '#0f7ac4', // blue mid
  '#1a9e8e', // teal
  '#4aa3df', // light blue
  '#83c5ed', // sky
  '#5ab1a5', // muted teal
];

function num(v: unknown): number {
  if (typeof v === 'number') return Number.isFinite(v) ? v : 0;
  const parsed = Number(String(v ?? '').replace(/,/g, '').trim());
  return Number.isFinite(parsed) ? parsed : 0;
}

const _MONTHS = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'] as const;

/**
 * Parse a month-year label such as "Jan 2025", "Jan '25", "jan_25", "jan25" → timestamp.
 * Never delegates to Date.parse (which treats "Jan 25" as January 25th, giving wrong year).
 */
function parseMonthTime(value: unknown): number | null {
  const s = String(value ?? '').trim();
  if (!s) return null;
  // Normalise separators so "jan_25", "jan-25", "jan25", "jan '25", "jan 2025" all match
  const norm = s.replace(/[_\-']/g, ' ').replace(/\s+/g, ' ').trim();
  const m = norm.match(/^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*(\d{2,4})$/i);
  if (m) {
    const monthIdx = _MONTHS.indexOf(m[1].slice(0, 3).toLowerCase() as typeof _MONTHS[number]);
    if (monthIdx === -1) return null;
    const yy = Number(m[2]);
    const year = yy < 100 ? 2000 + yy : yy;
    return new Date(year, monthIdx, 1).getTime();
  }
  // ISO date strings only (YYYY-MM or YYYY-MM-DD) — avoids ambiguous Date.parse
  const iso = s.match(/^(\d{4})-(\d{1,2})(?:-\d{1,2})?/);
  if (iso) {
    return new Date(Number(iso[1]), Number(iso[2]) - 1, 1).getTime();
  }
  return null;
}

/** Format a month-year value as "Jan 2025". */
function formatMonthLabel(value: unknown): string {
  const s = String(value ?? '').trim();
  const t = parseMonthTime(s);
  if (t === null) return s;
  const d = new Date(t);
  const mon = d.toLocaleDateString('en-US', { month: 'short' });
  return `${mon} ${d.getFullYear()}`;
}

function looksLikeTimeKey(key: string): boolean {
  return /month|date|period|time|calendar/i.test(key);
}

function looksLikeMetricKey(key: string): boolean {
  return /trx|rx|prescriptions?|scripts?|volume|total|count|value|amount/i.test(key) && !looksLikePercentageColumn(key);
}

function norm(rows: Record<string, unknown>[]): { label: string; value: number }[] {
  return rows.map((r) => {
    if ('name' in r && 'value' in r) return { label: formatMonthLabel(r.name), value: num(r.value) };
    const entries = Object.entries(r);
    const timeEntry = entries.find(([k, v]) => looksLikeTimeKey(k) || parseMonthTime(v) !== null);
    const metricEntry = entries.find(([k, v]) => looksLikeMetricKey(k) && parseMonthTime(v) === null);
    if (timeEntry && metricEntry) {
      return { label: formatMonthLabel(timeEntry[1]), value: num(metricEntry[1]) };
    }
    return { label: formatMonthLabel(entries[0]?.[1]), value: num(entries[1]?.[1]) };
  });
}

/** True when column name suggests a % / share metric — pie slices must use raw TRx, not these. */
function looksLikePercentageColumn(name: string): boolean {
  const n = name.toLowerCase();
  if (/\bpct\b|percent|share_pct|_pct$|market_share|penetration/i.test(n)) return true;
  if (n.includes('share') && (n.includes('pct') || n.endsWith('%'))) return true;
  return false;
}

/** Normalize header for fuzzy match (LLM axis hints vs DB/snake_case keys). */
function normHeader(s: string): string {
  return s.toLowerCase().replace(/[\s-]+/g, '_').replace(/_+/g, '_');
}

/**
 * Map chart_recommendation x_axis / y_axis hint to an actual row key.
 * Exact mismatches produced all-zero series (empty bars, Y-axis ~0–1).
 */
function resolveDataColumnKey(sampleRow: Record<string, unknown>, hint: string): string {
  const keys = Object.keys(sampleRow);
  const h = (hint || '').trim();
  if (!h) return keys[0] ?? '';
  if (keys.includes(h)) return h;
  const hl = h.toLowerCase();
  const ci = keys.find((k) => k.toLowerCase() === hl);
  if (ci) return ci;
  const hn = normHeader(h);
  const nrm = keys.find((k) => normHeader(k) === hn);
  if (nrm) return nrm;

  if (hl === 'name' || hl === 'label' || hl === 'category' || hl === 'x') {
    // "segment", "group", "flag" columns are always label columns; try them first.
    const segmentLike = keys.find((k2) => /^(?:segment|group|flag|target|category)$/i.test(k2));
    if (segmentLike) return segmentLike;
    // Broader label pattern — but exclude pure-count columns like "hcp_count".
    const k = keys.find((k2) =>
      /hcp|provider|physician|prescriber|doctor|name|city|state|region|specialty|month|year|date|period|time|category|label/i.test(k2) &&
      !/_count$|^count$/i.test(k2)
    );
    if (k) return k;
    // Fallback: use first column that doesn't look like a number/value
    const nonValue = keys.find(k2 => !/trx|rx|prescriptions?|scripts|volume|total|nrx|count|value|amount/i.test(k2));
    if (nonValue) return nonValue;
    return keys[0] ?? '';
  }
  if (hl === 'value' || hl === 'trx' || hl === 'count' || hl === 'amount' || hl === 'y') {
    // Priority 1: pure TRx / prescription volume columns (e.g. zoryve_trx, total_trx)
    // Must match before generic "count" columns like hcp_count.
    const trxVolume = keys.find(
      (k2) =>
        /trx|nrx|prescriptions?|scripts|volume/i.test(k2) &&
        !looksLikePercentageColumn(k2) &&
        !/_count$/i.test(k2),
    );
    if (trxVolume) return trxVolume;
    // Priority 2: total / aggregate columns
    const totalCol = keys.find(
      (k2) =>
        /^total_|_total$|total_all/i.test(k2) &&
        !looksLikePercentageColumn(k2),
    );
    if (totalCol) return totalCol;
    // Priority 3: any numeric-looking column that isn't a pure entity count or %
    const k = keys.find(
      (k2) =>
        /count|value|amount/i.test(k2) &&
        !looksLikePercentageColumn(k2) &&
        !/_count$/i.test(k2),
    );
    if (k) return k;
    // Fallback: pick any value-like column even if it might be a percentage
    const valueLike = keys.find(k2 => /trx|rx|prescriptions?|scripts|volume|total|nrx|count|value|amount/i.test(k2));
    if (valueLike) return valueLike;
    // Hard fallback: pick the first column that doesn't look like a name
    const fallback = keys.find(k2 => !/hcp|provider|physician|prescriber|doctor|name|city|state|region|specialty|month|year|date|period|time|category|label/i.test(k2));
    if (fallback) return fallback;
  }
  return h;
}

/**
 * For pie charts, pick a TRx volume column when the model chose a % column (avoids ~50/50
 * donuts from two similar penetration % like 26.66 vs 26.92).
 */
function resolvePieValueColumnKey(
  sampleRow: Record<string, unknown>,
  xAxis: string,
  yAxis: string,
): string {
  // Skip override if yAxis is already a proper TRx/volume column.
  const yAxisIsTrx = /trx|nrx|prescriptions?|scripts|volume/i.test(yAxis) && !/_count$/i.test(yAxis);
  if (!looksLikePercentageColumn(yAxis) && yAxisIsTrx) return yAxis;

  const keys = Object.keys(sampleRow);
  const preferred = [
    'zoryve_q1_26_trx',
    'zoryve_q4_25_trx',
    'zoryve_trx',
    'zoryve_trx_q1',
    'total_zoryve_trx',
  ];
  for (const k of preferred) {
    if (keys.includes(k) && k !== xAxis && num(sampleRow[k]) >= 0) return k;
  }

  const byHeuristic = keys.find(
    (k) =>
      k !== xAxis &&
      k !== yAxis &&
      !looksLikePercentageColumn(k) &&
      /zoryve/i.test(k) &&
      /trx|rx|volume|scripts|prescription/i.test(k),
  );
  if (byHeuristic) return byHeuristic;

  const anyVolume = keys.find(
    (k) =>
      k !== xAxis &&
      k !== yAxis &&
      !looksLikePercentageColumn(k) &&
      /trx|volume|count|scripts|rx$/i.test(k) &&
      num(sampleRow[k]) > 0,
  );
  return anyVolume ?? yAxis;
}

function formatYAxisLabel(key: string): string {
  if (!key) return 'Value';
  return key
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

/**
 * Convert a data_table (from LLM JSON response) + chart_recommendation
 * into a ChartPayload for rendering.
 */
function chartRecToPayload(
  rec: ChartRecommendation,
  dataTable: Record<string, unknown>[] | null | undefined,
): ChartPayload | null {
  if (!rec.show_chart || rec.chart_type === 'none') return null;
  if (!dataTable || dataTable.length < 2) return null;

  const row0 = dataTable[0];
  let xKey = resolveDataColumnKey(row0, rec.x_axis);
  let yKey = resolveDataColumnKey(row0, rec.y_axis);
  // Trend fix: if the LLM swapped axes, force time/month onto X and TRx/volume onto Y.
  if (
    rec.chart_type === 'line' &&
    parseMonthTime(row0[yKey]) !== null &&
    parseMonthTime(row0[xKey]) === null
  ) {
    const oldX = xKey;
    xKey = yKey;
    yKey = oldX;
  }
  if (rec.chart_type === 'line' && parseMonthTime(row0[xKey]) === null) {
    const timeKey = Object.keys(row0).find((k) => looksLikeTimeKey(k) || parseMonthTime(row0[k]) !== null);
    const metricKey = Object.keys(row0).find((k) => k !== timeKey && looksLikeMetricKey(k));
    if (timeKey && metricKey) {
      xKey = timeKey;
      yKey = metricKey;
    }
  }
  if (rec.chart_type === 'pie') {
    yKey = resolvePieValueColumnKey(row0, xKey, yKey);
  }

  let data: Record<string, unknown>[] = dataTable.map((row) => ({
    name: formatMonthLabel(row[xKey] ?? ''),
    value: num(row[yKey]),
  }));
  if (rec.chart_type === 'line') {
    data.sort((a, b) => (parseMonthTime(a.name) ?? 0) - (parseMonthTime(b.name) ?? 0));
  }
  // For pie charts, drop rows with a blank label — mixed result tables (e.g. segment
  // rows + HCP detail rows) produce entries with no segment name that create phantom
  // legend swatches.
  if (rec.chart_type === 'pie') {
    data = data.filter((r) => String(r.name ?? '').trim().length > 0);
  }

  const sumAbs = data.reduce((s, r) => s + Math.abs(num(r.value)), 0);
  const anyLabel = data.some((r) => String(r.name ?? '').trim().length > 0);
  if (sumAbs === 0 || !anyLabel) {
    return null;
  }

  return {
    kind: rec.chart_type as ChartKind,
    data,
    title: rec.title,
    description: rec.rationale,
    yAxisLabel: formatYAxisLabel(yKey),
  };
}

/**
 * Return a display-safe chart title.
 * Rejects titles that look like raw user queries (contain "?", start with quotes,
 * or are longer than 80 chars without a newline), falling back to a chart-type label.
 */
function titleOf(chart: ChartPayload): string {
  const raw = (chart.title || '').trim();
  const looksLikeQuery =
    raw.includes('?') ||
    raw.startsWith('"') ||
    raw.startsWith("'") ||
    raw.length > 80 ||
    /^(show|are|is|what|which|who|how|give|list|find|tell|do|does|can)/i.test(raw);
  if (!raw || looksLikeQuery) {
    const kindLabels: Record<string, string> = {
      bar: 'Bar Chart',
      line: 'Trend Over Time',
      pie: 'Breakdown by Segment',
      scatter: 'Scatter Plot',
    };
    return kindLabels[chart.kind] ?? 'Chart';
  }
  return raw;
}

interface QueryResultChartProps {
  chart?: ChartPayload;
  chartRecommendation?: ChartRecommendation;
  dataTable?: Record<string, unknown>[] | null;
}

export const QueryResultChart: React.FC<QueryResultChartProps> = ({
  chart,
  chartRecommendation,
  dataTable,
}) => {
  // Determine which chart payload to render
  const resolvedChart = useMemo((): ChartPayload | null => {
    let payload: ChartPayload | null = null;
    if (chartRecommendation && dataTable) {
      payload = chartRecToPayload(chartRecommendation, dataTable);
    } else if (chart && Array.isArray(chart.data) && chart.data.length > 1) {
      payload = chart;
    }

    if (!payload) return null;

    let yLabel = payload.yAxisLabel;
    let sourceData = dataTable && dataTable.length > 0 ? dataTable : payload.data;
    
    // Create a shallow copy to avoid mutating React props
    const nextPayload = { ...payload };
    if (nextPayload.kind === 'line') {
      nextPayload.data = norm(nextPayload.data)
        .map((row) => ({ name: formatMonthLabel(row.label), value: row.value }))
        .sort((a, b) => (parseMonthTime(a.name) ?? 0) - (parseMonthTime(b.name) ?? 0));
    }

    // TRUNCATE OVERCROWDED CHARTS
    // If a bar/pie chart has too many rows, slice to the top 10 to keep it readable.
    if ((nextPayload.kind === 'bar' || nextPayload.kind === 'pie') && nextPayload.data.length > 15) {
      nextPayload.data = nextPayload.data.slice(0, 10);
      sourceData = sourceData.slice(0, 10);
    }

    let metricColumn: string | undefined;
    if (sourceData && sourceData.length > 0) {
      const keys = Object.keys(sourceData[0]);
      const ignoreList = ['hcp_name','city','state','primary_specialty', 'secondary_specialty', 'hco_name', 'npi_id','region','area', 'q1_26_target_flag', 'q1_26_decile', 'name', 'value'];
      
      metricColumn = keys.find(k => looksLikeMetricKey(k) && !ignoreList.includes(k.toLowerCase()));
      
      if (!metricColumn) {
        // Fallback: just find any column that isn't name/value
        metricColumn = keys.find(k => !['name', 'value', 'hcp_name'].includes(k.toLowerCase()));
      }
    }
    
    if (metricColumn) {
      const formattedMetric = metricColumn
        .replace(/_/g, ' ')
        .replace(/\b\w/g, c => c.toUpperCase());
      
      // Override Y axis label
      yLabel = formattedMetric;

      // Fix 2: Clean title instead of raw user query
      const rowCount = sourceData.length;
      const hasHcp = sourceData[0] && 'hcp_name' in sourceData[0];
      
      // Override title with cleanly formatted one
      if (hasHcp) {
        nextPayload.title = `Top ${rowCount} HCPs by ${formattedMetric}`;
      } else if (nextPayload.kind === 'pie' || nextPayload.kind === 'line') {
        // For pie/line let the existing chart title (from backend) pass through;
        // titleOf() will clean it if it looks like a query string.
      } else {
        nextPayload.title = `Top ${rowCount} by ${formattedMetric}`;
      }

      // Fix 3: Replace "Value" in description/caption with actual metric name
      if (nextPayload.description) {
        nextPayload.description = nextPayload.description.replace(/\bValue\b/g, formattedMetric);
      }
    } else if (!yLabel) {
      yLabel = 'Value';
    }

    return { ...nextPayload, yAxisLabel: yLabel };
  }, [chart, chartRecommendation, dataTable]);


  const data = useMemo(
    () => (resolvedChart ? norm(resolvedChart.data) : []),
    [resolvedChart],
  );
  const canvasRef = useRef<ChartJS<'bar' | 'line' | 'doughnut' | 'scatter'> | null>(null);

  if (!resolvedChart || data.length <= 1) return null;

  const exportPng = () => {
    const c = canvasRef.current;
    if (!c) return;
    const a = document.createElement('a');
    a.href = c.toBase64Image('image/png', 1);
    a.download = `${titleOf(resolvedChart).replace(/\s+/g, '_').toLowerCase()}.png`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };

  /** Shared chart wrapper styling. */
  const wrapClass = 'mt-4 rounded-xl border border-gray-200 bg-white p-4';
  const values = data.map((x) => x.value);
  const maxVal = values.length > 0 ? Math.max(...values) : 0;
  const minVal = values.length > 0 ? Math.min(...values) : 0;
  // If the fluctuation is less than 15% of the maximum value, zoom the Y-axis by not starting at zero.
  const shouldBeginAtZero = maxVal === 0 || (maxVal - minVal) / maxVal > 0.15;
  /** When zoomed, pad Y so low values are not clipped against a tight floor/ceiling. */
  const yAxisPad = 5;
  const yMinPadded = minVal - yAxisPad;
  const yMaxPadded = maxVal + yAxisPad;

  const exportBtn = (
    <button
      type="button"
      onClick={exportPng}
      className="mt-3 rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs font-semibold text-gray-700 hover:bg-gray-50 transition-colors"
    >
      Export Chart
    </button>
  );

  if (resolvedChart.kind === 'bar') {
    const d = {
      labels: data.map((x) => x.label.length > 12 ? x.label.substring(0, 12) + '...' : x.label),
      datasets: [
        {
          label: resolvedChart.yAxisLabel || resolvedChart.title || 'Value',
          data: data.map((x) => x.value),
          backgroundColor: PALETTE[0],
          borderRadius: 6,
          borderSkipped: false,
        },
      ],
    };
    return (
      <div className={wrapClass}>
        <h3 className="mb-3 text-base font-semibold text-[#0b5fa5]">{titleOf(resolvedChart)}</h3>
        <div style={{ minHeight: 300, width: '100%' }}>
          <Bar
            ref={canvasRef as never}
            data={d}
            options={{
              responsive: true,
              maintainAspectRatio: false,
              plugins: {
                legend: { display: false },
                tooltip: { 
                  callbacks: { 
                    title: (ctx) => data[ctx[0].dataIndex].label,
                    label: (ctx) => ` ${(ctx.parsed.y ?? 0).toLocaleString()}` 
                  } 
                },
              },
              scales: {
                x: { 
                  title: { display: true, text: resolvedChart.description || 'Category' },
                  ticks: { maxRotation: 45, minRotation: 45 }
                },
                y: {
                  title: { display: true, text: resolvedChart.yAxisLabel || 'Value' },
                  beginAtZero: shouldBeginAtZero,
                  ...(shouldBeginAtZero
                    ? {}
                    : { min: yMinPadded, max: yMaxPadded }),
                },
              },
            }}
          />
        </div>
        {exportBtn}
      </div>
    );
  }

  if (resolvedChart.kind === 'line') {
    const d = {
      labels: data.map((x) => formatMonthLabel(x.label)),
      datasets: [
        {
          label: resolvedChart.yAxisLabel || resolvedChart.title || 'Value',
          data: data.map((x) => x.value),
          borderColor: PALETTE[0],
          backgroundColor: 'rgba(11,95,165,0.15)',
          tension: 0.4,
          pointRadius: 5,
          pointHoverRadius: 7,
          fill: true,
        },
      ],
    };
    return (
      <div className={wrapClass}>
        <h3 className="mb-3 text-base font-semibold text-[#0b5fa5]">{titleOf(resolvedChart)}</h3>
        <div style={{ minHeight: 300, width: '100%' }}>
          <Line
            ref={canvasRef as never}
            data={d}
            options={{
              responsive: true,
              maintainAspectRatio: false,
              plugins: {
                legend: { display: true, position: 'top' },
                tooltip: { 
                  callbacks: { 
                    title: (ctx) => data[ctx[0].dataIndex].label,
                    label: (ctx) => ` ${(ctx.parsed.y ?? 0).toLocaleString()}` 
                  } 
                },
              },
              scales: {
                x: { 
                  type: 'category',
                  title: { display: true, text: 'Time' },
                  ticks: { maxRotation: 45, minRotation: 45 }
                },
                y: {
                  type: 'linear',
                  title: { display: true, text: resolvedChart.yAxisLabel || 'Value' },
                  beginAtZero: shouldBeginAtZero,
                  ...(shouldBeginAtZero
                    ? {}
                    : { min: yMinPadded, max: yMaxPadded }),
                },
              },
            }}
          />
        </div>
        {exportBtn}
      </div>
    );
  }

  if (resolvedChart.kind === 'scatter') {
    const pts = data.map((x, i) => ({ x: i + 1, y: x.value, _l: x.label }));
    return (
      <div className={wrapClass}>
        <h3 className="mb-3 text-base font-semibold text-[#0b5fa5]">{titleOf(resolvedChart)}</h3>
        <div style={{ minHeight: 300, width: '100%' }}>
          <Scatter
            ref={canvasRef as never}
            data={{ datasets: [{ label: 'Data', data: pts, backgroundColor: PALETTE[0], pointRadius: 6 }] }}
            options={{
              responsive: true,
              maintainAspectRatio: false,
              plugins: {
                tooltip: {
                  callbacks: {
                    label: (ctx) => `${pts[ctx.dataIndex]?._l ?? 'Point'}: ${(ctx.parsed.y ?? 0).toLocaleString()}`,
                  },
                },
              },
              scales: {
                x: { title: { display: true, text: resolvedChart.description || 'Index' } },
                y: {
                  title: { display: true, text: resolvedChart.yAxisLabel || 'Value' },
                  beginAtZero: shouldBeginAtZero,
                  ...(shouldBeginAtZero
                    ? {}
                    : { min: yMinPadded, max: yMaxPadded }),
                },
              },
            }}
          />
        </div>
        {exportBtn}
      </div>
    );
  }

  // Default: pie rendered as donut
  const total = data.reduce((s, x) => s + x.value, 0) || 1;
  const pieSlices = data.slice(0, 6);
  const d = {
    labels: pieSlices.map((x) => x.label),
    datasets: [
      {
        data: pieSlices.map((x) => x.value),
        // Slice palette to exactly the number of data points so Chart.js
        // doesn't generate phantom legend entries for unused colors.
        backgroundColor: PALETTE.slice(0, pieSlices.length),
        borderWidth: 2,
        borderColor: '#fff',
      },
    ],
  };
  return (
    <div className={wrapClass}>
      <h3 className="mb-3 text-base font-semibold text-[#0b5fa5]">{titleOf(resolvedChart)}</h3>
      <div style={{ minHeight: 300, width: '100%' }}>
        <Doughnut
          ref={canvasRef as never}
          data={d}
          options={{
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { position: 'bottom', labels: { padding: 12 } },
              tooltip: {
                callbacks: {
                  label: (ctx) => {
                    const v = num(ctx.parsed);
                    const pct = ((v / total) * 100).toFixed(1);
                    return ` ${ctx.label}: ${v.toLocaleString()} TRx (${pct}% of total)`;
                  },
                },
              },
            },
          }}
        />
      </div>
      {exportBtn}
    </div>
  );
};
