<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { init, use, type EChartsCoreOption, type EChartsType } from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import { AriaComponent, GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'
import type { UsageReport } from '@/api/usage'

use([BarChart, LineChart, GridComponent, LegendComponent, TooltipComponent, AriaComponent, SVGRenderer])
const props = defineProps<{ report: UsageReport }>()
const charts = new Map<HTMLElement, EChartsType>()
const root = ref<HTMLElement>()
let observer: ResizeObserver | undefined
const trafficMetric = ref('requests')
const labels = computed(() => props.report.buckets.map(bucket => new Date(bucket.at * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })))
const options = computed(() => {
  const buckets = props.report.buckets
  const series = (name: string, color: string, key: keyof typeof buckets[number], type = 'bar') => ({ name, type, color, data: buckets.map(item => item[key]), barMaxWidth: 24, smooth: false, connectNulls: false })
  return [
    [series(trafficMetric.value === 'requests' ? '请求' : 'Tokens', '#60a5fa', trafficMetric.value === 'requests' ? 'requests' : 'total_tokens')],
    [series('成功', '#70dab0', 'success'), series('失败', '#ff9ba7', 'failed'), series('取消', '#e5b96a', 'cancelled')].map(item => ({ ...item, stack: 'outcome' })),
    [series('首正文 P50', '#a78bfa', 'first_body_p50', 'line'), series('首正文 P95', '#60a5fa', 'first_body_p95', 'line')],
    [series('输入', '#60a5fa', 'input_tokens'), series('输出', '#70dab0', 'output_tokens'), series('缓存命中', '#a78bfa', 'cached_tokens')],
  ].map((items, index): EChartsCoreOption => ({
    animation: false, aria: { enabled: true },
    backgroundColor: 'transparent', textStyle: { color: '#98a6ba', fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif' },
    grid: { left: 14, right: 20, top: 38, bottom: 12, containLabel: true },
    tooltip: { trigger: 'axis', confine: true, renderMode: 'richText', backgroundColor: '#1b222d', borderColor: '#2a3341', textStyle: { color: '#e6edf5' } },
    legend: { top: 0, textStyle: { color: '#98a6ba' } },
    xAxis: { type: 'category', data: labels.value, axisLine: { lineStyle: { color: '#2a3341' } }, axisTick: { show: false }, axisLabel: { hideOverlap: true, fontSize: 10 } },
    yAxis: { type: 'value', name: index === 2 ? 'ms' : '', splitLine: { lineStyle: { color: '#252d39' } }, axisLabel: { formatter: (value: number) => value >= 1000 ? `${+(value / 1000).toFixed(1)}k` : String(value) } },
    series: items,
  }))
})
function render() { [...charts.values()].forEach((chart, index) => chart.setOption(options.value[index]!, true)) }
onMounted(() => {
  root.value?.querySelectorAll<HTMLElement>('[data-chart]').forEach(element => charts.set(element, init(element, undefined, { renderer: 'svg' })))
  observer = new ResizeObserver(() => charts.forEach(chart => chart.resize()))
  if (root.value) observer.observe(root.value)
  render()
})
watch(options, render)
onUnmounted(() => { observer?.disconnect(); charts.forEach(chart => chart.dispose()); charts.clear() })
</script>

<template>
  <div
    ref="root"
    class="usage-charts"
  >
    <article
      v-for="(title, index) in ['请求流量', '请求结果', '流式首正文耗时', 'Token 使用']"
      :key="title"
      class="console-panel usage-chart-panel"
    >
      <div class="panel-heading">
        <h2>{{ title }}</h2><select
          v-if="index === 0"
          v-model="trafficMetric"
          aria-label="流量指标"
        >
          <option value="requests">
            请求数
          </option><option value="tokens">
            Tokens
          </option>
        </select><small v-if="index === 3">缓存是输入的子集</small>
      </div>
      <div
        data-chart
        class="usage-chart"
        :aria-label="title"
      />
    </article>
  </div>
</template>
