<template>
  <section class="page" data-module="notifychan">
    <header class="page-head">
      <div>
        <h2>通知渠道管理</h2>
        <p class="page-desc">管理ETL异常和产出的通知渠道配置，支持多渠道分级推送。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记渠道配置</button>
        <button class="btn" type="button" @click="exportRows">导出通知渠道清单</button>
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
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>启用状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span :class="['badge', row.status === '已启用' ? 'badge-on' : 'badge-off']">
              {{ row.status === '已启用' ? '已启用' : '未启用' }}
            </span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无通知渠道数据，可先登记渠道配置</td>
        </tr>
      </tbody>
    </table>

    <section class="detail-panel">
      <header class="detail-head">
        <h3>SLA升级待发送</h3>
        <button class="btn ghost" type="button" @click="loadEscalations">刷新</button>
      </header>
      <p class="detail-note">
        升级记录与SLA监控详情指向同一条SLA履约事件；规则关闭、版本过期或渠道未启用时不会发送。
      </p>
      <table class="data-table">
        <thead>
          <tr>
            <th>升级记录</th>
            <th>规则编号</th>
            <th>规则版本</th>
            <th>履约事件</th>
            <th>升级状态</th>
            <th>通知渠道</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in escalations" :key="item.升级记录编号">
            <td>{{ item.升级记录编号 }}</td>
            <td>{{ item.规则编号 }}</td>
            <td>{{ item.规则版本 }}</td>
            <td>{{ item.履约事件编号 }}</td>
            <td>{{ item.升级状态 }}</td>
            <td>{{ item.渠道编号 ?? '—' }}</td>
            <td>
              <button
                v-if="item.升级状态 === '待发送'"
                class="link"
                type="button"
                @click="sendEscalation(item)"
              >
                发送升级
              </button>
              <span v-else class="muted">—</span>
            </td>
          </tr>
          <tr v-if="!escalations.length">
            <td colspan="7" class="empty-state">当前没有待处理的SLA升级</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条通知渠道记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="infoMessage" class="info-text">{{ infoMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

type Escalation = {
  升级记录编号: string
  规则编号: string
  规则版本: number
  履约事件编号: string
  升级状态: string
  渠道编号: string | null
}

const ENDPOINT = '/api/notifychan'
const columns = ["渠道编号", "渠道类型", "接收地址", "消息模板", "发送频率", "静默时段开始", "静默时段结束", "消息优先级"]
const actions = ["提交配置评审", "启用渠道", "停用渠道"]

const rows = ref<Row[]>([])
const total = ref(0)
const escalations = ref<Escalation[]>([])
const errorMessage = ref('')
const infoMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const stats = computed(() => [
  { label: '渠道配置总数', value: total.value || rows.value.length },
  { label: '已启用渠道', value: rows.value.filter((row) => row.status === '已启用').length },
  { label: '待发送SLA升级', value: escalations.value.filter((item) => item.升级状态 === '待发送').length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '渠道配置登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('通知渠道动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '通知渠道操作失败'
  }
}

async function loadEscalations() {
  try {
    const response = await request(`${ENDPOINT}/sla-escalations`)
    if (!response.ok) {
      throw new Error('SLA升级记录读取失败')
    }
    const payload = await response.json()
    escalations.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SLA升级记录读取失败'
  }
}

async function sendEscalation(item: Escalation) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/sla-escalations/${item.升级记录编号}/send`, {
      method: 'POST',
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(typeof payload?.message === 'string' ? payload.message : 'SLA升级发送失败')
    }
    infoMessage.value = payload.message
    await loadEscalations()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SLA升级发送失败'
    await loadEscalations()
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('渠道配置列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '通知渠道列表读取失败'
  }
}

onMounted(async () => {
  await reload()
  await loadEscalations()
})
</script>
