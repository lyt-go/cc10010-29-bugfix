<template>
  <section class="page" data-module="slavalidate">
    <header class="page-head">
      <div>
        <h2>SLA监控管理</h2>
        <p class="page-desc">管理ETL作业的SLA监控规则，对作业延迟进行分级预警和自动升级。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记SLA规则</button>
        <button class="btn" type="button" @click="recalculateAll">按运行水位重算</button>
        <button class="btn" type="button" @click="exportRows">导出SLA监控清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>规则状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>生效状态</th>
          <th>规则版本</th>
          <th>当前履约事件</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ displayCell(row, column) }}</td>
          <td>
            <span :class="['badge', row.生效状态 ? 'badge-on' : 'badge-off']">
              {{ row.生效状态 ? '已生效' : '未生效' }}
            </span>
          </td>
          <td>{{ row.规则版本 ?? 1 }}</td>
          <td>{{ eventSummary(row) }}</td>
          <td class="row-actions">
            <button
              v-for="action in actionsFor(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="row.生效状态"
              class="link"
              type="button"
              @click="recalculate(row)"
            >
              重算履约
            </button>
            <button class="link" type="button" @click="showDetail(row)">详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无SLA监控数据，可先登记SLA规则</td>
        </tr>
      </tbody>
    </table>

    <section v-if="detail" class="detail-panel">
      <header class="detail-head">
        <h3>SLA规则 {{ detail.规则编号 }} · {{ detail.status }} · 版本 {{ detail.规则版本 ?? 1 }}</h3>
        <button class="btn ghost" type="button" @click="detail = null">收起</button>
      </header>
      <p v-if="detail.校验结果" class="detail-note">
        校验：{{ detail.校验结果.ok ? '通过' : '未通过' }}
        <template v-if="detail.校验结果.errors.length"> · {{ detail.校验结果.errors.join('；') }}</template>
        <template v-if="detail.校验结果.warnings.length"> · {{ detail.校验结果.warnings.join('；') }}</template>
      </p>
      <h4>履约事件</h4>
      <table class="data-table">
        <thead>
          <tr><th>事件编号</th><th>预警状态</th><th>预警时间</th><th>水位时间</th><th>规则版本</th><th>是否当前</th><th>通知状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="event in detail.履约事件" :key="event.履约事件编号">
            <td>{{ event.履约事件编号 }}</td>
            <td>{{ event.预警状态 }}</td>
            <td>{{ event.预警时间 ?? '—' }}</td>
            <td>{{ event.水位时间 ?? '—' }}</td>
            <td>{{ event.规则版本 }}</td>
            <td>{{ event.是否当前 ? '是' : '历史' }}</td>
            <td>{{ event.通知状态 }}</td>
          </tr>
          <tr v-if="!detail.履约事件?.length">
            <td colspan="7" class="empty-state">该规则暂无履约事件</td>
          </tr>
        </tbody>
      </table>
      <h4>升级记录</h4>
      <table class="data-table">
        <thead>
          <tr><th>升级记录</th><th>履约事件</th><th>升级状态</th><th>通知渠道</th><th>发送时间</th><th>终止原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in detail.升级记录" :key="item.升级记录编号">
            <td>{{ item.升级记录编号 }}</td>
            <td>{{ item.履约事件编号 }}</td>
            <td>{{ item.升级状态 }}</td>
            <td>{{ item.渠道编号 ?? '—' }}</td>
            <td>{{ item.发送时间 ?? '—' }}</td>
            <td>{{ item.终止原因 ?? '—' }}</td>
          </tr>
          <tr v-if="!detail.升级记录?.length">
            <td colspan="6" class="empty-state">该规则暂无升级记录</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条SLA监控记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="infoMessage" class="info-text">{{ infoMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type FulfillmentEvent = {
  履约事件编号: string
  预警状态: string
  预警时间: string | null
  水位时间: string | null
  规则版本: number
  是否当前: boolean
  通知状态: string
}

type Escalation = {
  升级记录编号: string
  履约事件编号: string
  升级状态: string
  渠道编号: string | null
  发送时间: string | null
  终止原因: string | null
}

type Row = Record<string, string | number | boolean | null> & {
  id: number
  status?: string
  生效状态?: boolean
  规则版本?: number
  当前履约事件?: FulfillmentEvent | null
}

type Detail = Row & {
  校验结果?: { ok: boolean; errors: string[]; warnings: string[] }
  履约事件?: FulfillmentEvent[]
  升级记录?: Escalation[]
}

const ENDPOINT = '/api/slavalidate'
const columns = ["规则编号", "适用作业", "完成截止时间", "预警提前分钟", "超时动作", "升级通知组", "每日频次", "容忍天数"]
const allActions = ["提交规则评审", "生效规则", "关闭规则"]
const statuses = ["草稿", "规则评审中", "已生效", "已关闭"]
const statLabels = ["SLA规则总数", "已生效规则", "关闭规则"] as const

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const infoMessage = ref('')
const filters = ref<Record<string, string>>({})
const detail = ref<Detail | null>(null)
const filterFields = columns.slice(0, 3)

const stats = computed(() => {
  const count = (predicate: (row: Row) => boolean) => rows.value.filter(predicate).length
  return [
    { label: statLabels[0], value: total.value || rows.value.length },
    { label: statLabels[1], value: count((row) => Boolean(row.生效状态)) },
    { label: statLabels[2], value: count((row) => row.status === '已关闭') },
  ]
})

function displayCell(row: Row, column: string) {
  const value = row[column]
  return value === undefined || value === null || value === '' ? '—' : value
}

function eventSummary(row: Row) {
  const event = row.当前履约事件
  if (!event) return '—'
  return `${event.履约事件编号} · ${event.预警状态}`
}

// 关闭规则后要恢复，必须先提交评审，不能直接点“生效规则”。
function actionsFor(row: Row) {
  if (row.status === '已关闭') return ['提交规则评审']
  if (row.status === '已生效') return ['关闭规则']
  return allActions.filter((action) => action !== '关闭规则' || row.status !== '已关闭')
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = 'SLA规则登记入口尚未接入审批流'
}

async function readMessage(response: Response, fallback: string) {
  try {
    const payload = await response.json()
    return typeof payload?.message === 'string' ? payload.message : fallback
  } catch {
    return fallback
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const message = await readMessage(response, 'SLA监控动作未生效，请稍后重试')
    if (!response.ok) {
      throw new Error(message)
    }
    infoMessage.value = message
    await reload()
    if (detail.value?.id === row.id) {
      await showDetail(row)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SLA监控操作失败'
  }
}

async function recalculate(row: Row) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/recalculate`, { method: 'POST' })
    const message = await readMessage(response, 'SLA履约重算失败')
    infoMessage.value = message
    if (!response.ok) {
      errorMessage.value = message
    }
    await reload()
    if (detail.value?.id === row.id) {
      await showDetail(row)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SLA履约重算失败'
  }
}

async function recalculateAll() {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/recalculate`, { method: 'POST' })
    const payload = await response.json()
    infoMessage.value = `${payload.message ?? '已重算'}（${payload.results?.length ?? 0} 条已生效规则）`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SLA履约重算失败'
  }
}

async function showDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) {
      throw new Error('SLA规则详情读取失败')
    }
    detail.value = (await response.json()) as Detail
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SLA规则详情读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('SLA规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SLA监控列表读取失败'
  }
}

onMounted(reload)
</script>
