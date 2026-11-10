<script setup>
import { inject, ref, computed } from 'vue'
import { api } from '../api'
import { fmtTime, TASK_STATUS } from '../utils'

const store = inject('store')
const toast = inject('toast')
const text = ref('')
const fileInput = ref(null)
const counts = computed(() => store.friends.counts || {})

async function toggle(v) {
  try { await api.friendToggle(v); toast(v ? '已开启自动加好友' : '已暂停自动加好友') }
  catch (e) { toast('切换失败：' + e.message, 'error') }
}

function report(r) {
  if (r?.ok === false) { toast(r.error || '导入失败', 'error'); return }
  toast(`导入完成：新增 ${r.added || 0} 条，跳过 ${(r.skipped || []).length} 条`)
}

async function onFile(e) {
  const f = e.target.files?.[0]
  if (!f) return
  try { report(await api.friendImportFile(f)) }
  catch (err) { toast('导入失败：' + err.message, 'error') }
  finally { e.target.value = '' }
}

async function importText() {
  if (!text.value.trim()) { toast('先粘贴手机号或微信号', 'warning'); return }
  try { report(await api.friendImportText(text.value)); text.value = '' }
  catch (e) { toast('导入失败：' + e.message, 'error') }
}
</script>

<template>
  <div class="wb-page">
    <div class="wb-page-head">
      <div>
        <h2>加好友</h2>
        <div class="sub">从 CSV 或粘贴导入，后台按每日上限与限速队列逐个发起</div>
      </div>
      <div style="display: flex; align-items: center; gap: 10px">
        <span class="wb-hint">自动加好友</span>
        <el-switch :model-value="!!store.friends.enabled" @change="toggle" />
      </div>
    </div>

    <div class="wb-metrics">
      <div v-for="k in ['pending', 'sent', 'passed', 'failed', 'already']" :key="k" class="wb-metric">
        <div class="k">{{ TASK_STATUS[k] || k }}</div>
        <div class="v">{{ counts[k] || 0 }}</div>
      </div>
      <div class="wb-metric">
        <div class="k">今日已发 / 上限</div>
        <div class="v">{{ store.friends.sent_today || 0 }}<span style="font-size: 15px; color: var(--wb-muted)"> / {{ store.friends.daily_limit || 0 }}</span></div>
      </div>
    </div>

    <div class="wb-grid2">
      <div class="wb-card">
        <h3>上传 CSV</h3>
        <p style="font-size: 14px; color: var(--wb-muted); line-height: 1.7; margin: 0 0 12px">
          一行一个手机号 / 微信号，UTF-8 或 GBK 均可。
          <a href="/api/friends/template" style="color: var(--wb-accent)">下载模板</a>
        </p>
        <input ref="fileInput" type="file" accept=".csv,.txt" style="display: none" @change="onFile" />
        <el-button size="small" type="primary" @click="fileInput.click()">选择文件</el-button>
      </div>
      <div class="wb-card">
        <h3>直接粘贴</h3>
        <el-input v-model="text" type="textarea" :rows="3" placeholder="每行一个号码，或用逗号分隔" />
        <el-button size="small" type="primary" style="margin-top: 10px" @click="importText">导入</el-button>
      </div>
    </div>

    <div class="wb-card">
      <h3>队列 {{ (store.friends.tasks || []).length }}</h3>
      <el-table :data="store.friends.tasks || []" size="small" max-height="360" empty-text="队列为空">
        <el-table-column prop="keyword" label="号码" width="150" />
        <el-table-column prop="name" label="姓名" width="120" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="row.status === 'passed' ? 'success' : (row.status === 'failed' ? 'danger' : 'info')">
              {{ TASK_STATUS[row.status] || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="reason" label="说明" min-width="140" show-overflow-tooltip />
        <el-table-column label="创建" width="120">
          <template #default="{ row }">{{ fmtTime(row.created) }}</template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>
