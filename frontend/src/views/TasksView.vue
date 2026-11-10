<script setup>
import { inject, computed } from 'vue'
import { fmtTime, TASK_STATUS } from '../utils'

const store = inject('store')
const counts = computed(() => store.tasks.counts || {})

const KEYS = ['pending', 'generating', 'generated', 'sending', 'sent', 'failed', 'expired', 'handoff']

function kindLabel(k) {
  return {
    reply: '自动回复', first_touch: '首条激活', outreach: '主动触达',
    handoff: '接管通知', campaign: '活动触达',
  }[k] || k
}

function statusType(s) {
  if (s === 'sent' || s === 'passed') return 'success'
  if (s === 'failed' || s === 'expired') return 'danger'
  if (s === 'handoff') return 'warning'
  return 'info'
}
</script>

<template>
  <div class="wb-page">
    <div class="wb-page-head">
      <div>
        <h2>维护任务</h2>
        <div class="sub">收消息 → AI 生成 → 发送 的账本，只读展示；最近 30 条</div>
      </div>
      <el-tag effect="plain" size="small">进行中 {{ counts.open || 0 }}</el-tag>
    </div>

    <div class="wb-metrics">
      <div v-for="k in KEYS" :key="k" class="wb-metric">
        <div class="k">{{ TASK_STATUS[k] || k }}</div>
        <div class="v">{{ counts[k] || 0 }}</div>
      </div>
    </div>

    <div class="wb-card">
      <el-table :data="store.tasks.tasks || []" size="small" max-height="460" empty-text="暂无任务">
        <el-table-column label="类型" width="110">
          <template #default="{ row }">{{ kindLabel(row.kind) }}</template>
        </el-table-column>
        <el-table-column prop="name" label="会话" width="150" show-overflow-tooltip />
        <el-table-column prop="text" label="内容" min-width="180" show-overflow-tooltip />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="statusType(row.status)">
              {{ TASK_STATUS[row.status] || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="reason" label="原因" min-width="130" show-overflow-tooltip />
        <el-table-column label="时间" width="120">
          <template #default="{ row }">{{ fmtTime(row.created) }}</template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>
