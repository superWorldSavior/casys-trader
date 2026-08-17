import { ColorType, createChart, AreaSeries, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";
import type { EquityPoint } from "@/lib/types";
import { parseTs } from "@/lib/format";

type Props = {
  points: EquityPoint[];
  className?: string;
};

export function EquityChart({ points, className }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Area"> | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const chart = createChart(host, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#8d8177",
        fontFamily: "IBM Plex Mono, ui-monospace, monospace",
        fontSize: 11,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: "#26211b" },
        horzLines: { color: "#26211b" },
      },
      rightPriceScale: {
        borderColor: "#332c23",
        scaleMargins: { top: 0.12, bottom: 0.08 },
      },
      timeScale: {
        borderColor: "#332c23",
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: {
        vertLine: { color: "#ffb86f55", labelBackgroundColor: "#1a1815" },
        horzLine: { color: "#ffb86f55", labelBackgroundColor: "#1a1815" },
      },
    });

    const series = chart.addSeries(AreaSeries, {
      lineColor: "#ffb86f",
      topColor: "rgba(255, 184, 111, 0.28)",
      bottomColor: "rgba(255, 184, 111, 0.02)",
      lineWidth: 2,
      priceLineVisible: false,
    });

    chartRef.current = chart;
    seriesRef.current = series;
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    const series = seriesRef.current;
    const chart = chartRef.current;
    if (!series || !chart) return;

    const data = [];
    let lastTime = 0;
    for (const point of points) {
      const date = parseTs(point.ts);
      if (!date) continue;
      let time = Math.floor(date.getTime() / 1000);
      if (time <= lastTime) time = lastTime + 1;
      lastTime = time;
      data.push({ time: time as UTCTimestamp, value: point.equity });
    }
    series.setData(data);
    chart.timeScale().fitContent();
  }, [points]);

  return <div ref={hostRef} className={className} />;
}
