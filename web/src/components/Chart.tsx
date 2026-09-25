import { SankeyChart, BarChart, LineChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

echarts.use([SankeyChart, BarChart, LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

/** A Primer token's current value, so charts follow the light or dark theme. */
export function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function baseTextStyle() {
  return { color: token("--fgColor-default"), fontFamily: token("--fontStack-sansSerif"), fontSize: 12 };
}

export function Chart({ option, height = 280 }: { option: echarts.EChartsCoreOption; height?: number }) {
  const element = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const latest = useRef(option);
  latest.current = option;

  useEffect(() => {
    if (!element.current) return;
    chart.current = echarts.init(element.current, undefined, { renderer: "canvas" });
    const resize = () => chart.current?.resize();
    window.addEventListener("resize", resize);
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    // Re-apply the option so token colours update when the OS theme changes.
    const retheme = () => chart.current?.setOption(latest.current, true);
    scheme.addEventListener("change", retheme);
    return () => {
      window.removeEventListener("resize", resize);
      scheme.removeEventListener("change", retheme);
      chart.current?.dispose();
      chart.current = null;
    };
    // Init runs once; the option is applied by the effect below.
  }, []);

  useEffect(() => {
    chart.current?.setOption(option, true);
  }, [option]);

  return <div ref={element} style={{ width: "100%", height }} />;
}
