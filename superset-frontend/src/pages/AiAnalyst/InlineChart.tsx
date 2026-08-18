/** Ad-hoc ECharts rendering for the display_chart agent tool. Pure
 * client-side: nothing is created in Superset. */
import { useEffect, useRef } from 'react';
import * as echarts from 'echarts';
import { styled } from '@apache-superset/core/theme';
import type { InlineChart as InlineChartData } from './types';

const Card = styled.div`
  align-self: stretch;
  flex: none; /* fixed-height canvas must not shrink when the feed overflows */
  border: 1px solid ${({ theme }) => theme.colorSplit};
  border-radius: 8px;
  padding: ${({ theme }) => theme.sizeUnit * 2}px;
  background: ${({ theme }) => theme.colorBgContainer};
  h4 {
    margin: 0 0 ${({ theme }) => theme.sizeUnit}px;
  }
  .canvas {
    width: 100%;
    height: 320px;
  }
`;

function buildOption(c: InlineChartData): echarts.EChartsOption {
  const { chart_type: type, rows, x, y, series } = c;

  if (type === 'pie') {
    return {
      tooltip: { trigger: 'item' },
      legend: { type: 'scroll' },
      series: [{
        type: 'pie',
        radius: ['30%', '65%'],
        data: rows.map(r => ({ name: String(r[x]), value: Number(r[y[0]]) })),
      }],
    };
  }

  if (type === 'scatter') {
    return {
      tooltip: { trigger: 'item' },
      xAxis: { type: 'value', name: x },
      yAxis: { type: 'value', name: y[0] },
      series: [{
        type: 'scatter',
        data: rows.map(r => [Number(r[x]), Number(r[y[0]])]),
      }],
    };
  }

  const categories = Array.from(new Set(rows.map(r => String(r[x]))));
  const echartsType = type === 'bar' ? 'bar' : 'line';
  const areaStyle = type === 'area' ? {} : undefined;
  let seriesDefs: echarts.SeriesOption[];
  if (series) {
    const names = Array.from(new Set(rows.map(r => String(r[series]))));
    seriesDefs = names.map(name => ({
      name,
      type: echartsType,
      areaStyle,
      data: categories.map(cat => {
        const row = rows.find(
          r => String(r[x]) === cat && String(r[series]) === name,
        );
        return row ? Number(row[y[0]]) : null;
      }),
    }));
  } else {
    seriesDefs = y.map(col => ({
      name: col,
      type: echartsType,
      areaStyle,
      data: categories.map(cat => {
        const row = rows.find(r => String(r[x]) === cat);
        return row ? Number(row[col]) : null;
      }),
    }));
  }
  return {
    tooltip: { trigger: 'axis' },
    legend: seriesDefs.length > 1 ? { type: 'scroll' } : undefined,
    grid: { left: 48, right: 16, top: 32, bottom: 48 },
    xAxis: { type: 'category', data: categories, axisLabel: { rotate: 30 } },
    yAxis: { type: 'value' },
    series: seriesDefs,
  };
}

export default function InlineChart({ chart }: { chart: InlineChartData }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return undefined;
    const instance = echarts.init(ref.current);
    instance.setOption(buildOption(chart));
    const onResize = () => instance.resize();
    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('resize', onResize);
      instance.dispose();
    };
  }, [chart]);

  return (
    <Card data-test="ai-analyst-inline-chart">
      <h4>{chart.title}</h4>
      <div className="canvas" ref={ref} />
    </Card>
  );
}
