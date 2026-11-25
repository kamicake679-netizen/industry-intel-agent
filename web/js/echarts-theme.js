/* ECharts「情报简报」主题与图表 Option 构造器 */

(function (global) {
  "use strict";

  function cssVar(name, fallback) {
    var val = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return val || fallback;
  }

  function prefersReducedMotion() {
    return !!(global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function getPalette() {
    return {
      paper: cssVar("--paper", "#F3EFE6"),
      paper2: cssVar("--paper-2", "#E9E3D6"),
      ink: cssVar("--ink", "#1B1A17"),
      ink2: cssVar("--ink-2", "#55514A"),
      ink3: cssVar("--ink-3", "#8A847A"),
      rule: cssVar("--rule", "#D6CEBD"),
      accent: cssVar("--accent", "#C8401A"),
      blue: cssVar("--blue", "#23395B"),
      desk: cssVar("--desk", "#161512"),
      desk2: cssVar("--desk-2", "#1F1D19"),
      deskFg: cssVar("--desk-fg", "#E9E3D6"),
      deskMute: cssVar("--desk-mute", "#8F887C"),
      deskRule: cssVar("--desk-rule", "#2E2B26"),
      mono: cssVar("--mono", "monospace"),
      sans: cssVar("--sans", "sans-serif")
    };
  }

  function register() {
    if (!global.echarts) return;
    var p = getPalette();

    global.echarts.registerTheme("brief", {
      backgroundColor: "transparent",
      color: [p.ink2, p.blue, p.ink3],
      textStyle: { fontFamily: p.sans, color: p.ink2 },
      tooltip: {
        backgroundColor: p.paper,
        borderColor: p.ink,
        borderWidth: 1,
        textStyle: { color: p.ink, fontSize: 12, fontFamily: p.mono },
        extraCssText: "box-shadow:none;border-radius:0;"
      },
      categoryAxis: {
        axisLine: { show: true, lineStyle: { color: p.rule } },
        axisTick: { show: false },
        axisLabel: { color: p.ink3, fontSize: 12 },
        splitLine: { show: false }
      },
      valueAxis: {
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: p.ink3, fontSize: 12, fontFamily: p.mono },
        splitLine: { show: false }
      }
    });

    global.echarts.registerTheme("brief-desk", {
      backgroundColor: "transparent",
      color: [p.deskMute, p.accent],
      textStyle: { fontFamily: p.sans, color: p.deskFg },
      tooltip: {
        backgroundColor: p.desk2,
        borderColor: p.deskRule,
        borderWidth: 1,
        textStyle: { color: p.deskFg, fontSize: 12, fontFamily: p.mono },
        extraCssText: "box-shadow:none;border-radius:0;"
      },
      categoryAxis: {
        axisLine: { show: true, lineStyle: { color: p.deskRule } },
        axisTick: { show: false },
        axisLabel: { color: p.deskMute, fontSize: 11 },
        splitLine: { show: false }
      },
      valueAxis: {
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: p.deskMute, fontSize: 11, fontFamily: p.mono },
        splitLine: { show: false }
      }
    });
  }

  /**
   * 构造柱状/条形图 Option，支持 highlightIndex 与第 6 名以后降透明度。
   */
  function barOption(opts) {
    var p = getPalette();
    var labels = opts.labels || [];
    var values = opts.values || [];
    var highlightIndex = typeof opts.highlightIndex === "number" ? opts.highlightIndex : 0;
    var horizontal = opts.horizontal !== false;
    var unit = opts.unit || "";
    var decimals = typeof opts.decimals === "number" ? opts.decimals : 1;
    var fadeAfter = opts.fadeAfter === undefined ? 5 : opts.fadeAfter;
    var animDur = prefersReducedMotion() ? 0 : 400;

    var data = values.map(function (val, idx) {
      var isHot = highlightIndex >= 0 && idx === highlightIndex;
      var opacity = isHot ? 1 : (fadeAfter >= 0 && idx >= fadeAfter ? 0.55 : 1);
      return {
        value: val,
        itemStyle: {
          color: isHot ? p.accent : p.ink2,
          opacity: opacity,
          borderRadius: 0
        }
      };
    });

    var categoryAxis = {
      type: "category",
      data: labels,
      inverse: horizontal,
      axisLine: { show: horizontal, lineStyle: { color: p.rule } },
      axisTick: { show: false },
      axisLabel: {
        color: p.ink,
        fontSize: 12,
        width: horizontal ? 96 : undefined,
        overflow: "truncate"
      }
    };

    var valueAxis = {
      type: "value",
      show: false,
      max: function (v) {
        return Math.ceil(v.max * 1.18);
      }
    };

    return {
      animationDuration: animDur,
      grid: horizontal
        ? { left: 104, right: 56, top: 8, bottom: 8, containLabel: false }
        : { left: 12, right: 12, top: 26, bottom: 28, containLabel: false },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "none" },
        formatter: function (params) {
          var item = Array.isArray(params) ? params[0] : params;
          if (!item) return "";
          var formatted = Number(item.value || 0).toFixed(decimals);
          return item.name + "：" + formatted + unit;
        }
      },
      xAxis: horizontal ? valueAxis : categoryAxis,
      yAxis: horizontal ? categoryAxis : valueAxis,
      series: [
        {
          type: "bar",
          barWidth: horizontal ? 11 : 22,
          data: data,
          label: {
            show: true,
            position: horizontal ? "right" : "top",
            formatter: function (params) {
              var num = Number(params.value || 0).toFixed(decimals);
              return unit ? "{val|" + num + "}{unit|" + unit + "}" : "{val|" + num + "}";
            },
            rich: {
              val: {
                color: p.ink,
                fontFamily: p.mono,
                fontSize: 12
              },
              unit: {
                color: p.ink2,
                fontFamily: p.mono,
                fontSize: 10,
                padding: [0, 0, 0, 1]
              }
            }
          }
        }
      ]
    };
  }

  /**
   * 构造经验—薪资阶梯图（竖向柱，全墨灰，柱端标数值）。
   */
  function ladderOption(expSalary) {
    var labels = (expSalary || []).map(function (r) { return r.experience; });
    var values = (expSalary || []).map(function (r) { return r.avg_salary; });
    return barOption({
      labels: labels,
      values: values,
      highlightIndex: -1,
      horizontal: false,
      unit: "K",
      fadeAfter: -1
    });
  }

  /**
   * 构造问询台深色迷你图。
   */
  function miniOption(chart) {
    var p = getPalette();
    var animDur = prefersReducedMotion() ? 0 : 400;
    var labels = chart.labels || [];
    var values = chart.values || [];
    var isLine = chart.type === "line";
    var hasFloat = values.some(function (v) {
      return typeof v === "number" && !Number.isInteger(v);
    });
    var fmtVal = function (params) {
      var n = Number(params.value || 0);
      return hasFloat ? n.toFixed(1) : String(Math.round(n));
    };

    return {
      animationDuration: animDur,
      grid: { left: 12, right: 12, top: 26, bottom: 24, containLabel: false },
      tooltip: { trigger: "axis", axisPointer: { type: "none" } },
      xAxis: {
        type: "category",
        data: labels,
        axisLine: { lineStyle: { color: p.deskRule } },
        axisLabel: { color: p.deskMute, fontSize: 11, interval: 0, overflow: "truncate", width: 58 }
      },
      yAxis: {
        type: "value",
        show: false,
        max: function (v) { return Math.ceil(v.max * 1.22); }
      },
      series: [
        isLine
          ? {
              type: "line",
              data: values,
              symbol: "circle",
              symbolSize: 5,
              lineStyle: { color: p.accent, width: 2 },
              itemStyle: { color: p.accent },
              label: { show: true, position: "top", color: p.deskFg, fontFamily: p.mono, fontSize: 11, formatter: fmtVal }
            }
          : {
              type: "bar",
              barWidth: 20,
              data: values.map(function (val, idx) {
                return {
                  value: val,
                  itemStyle: { color: idx === 0 ? p.accent : p.deskMute, borderRadius: 0 }
                };
              }),
              label: { show: true, position: "top", color: p.deskFg, fontFamily: p.mono, fontSize: 11, formatter: fmtVal }
            }
      ]
    };
  }

  global.BriefCharts = {
    register: register,
    barOption: barOption,
    ladderOption: ladderOption,
    miniOption: miniOption
  };
})(window);
