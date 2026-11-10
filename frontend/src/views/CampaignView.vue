<script setup>
import { computed, inject, reactive, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { api } from '../api'
import { fmtTime, CAMPAIGN_STATUS } from '../utils'
import ContactPicker from '../components/ContactPicker.vue'

const store = inject('store')
const toast = inject('toast')
const refreshPage = inject('refreshPage')

const form = reactive({ name: '', content: '', send_at: '', grace_minutes: 60 })
const selected = ref([])
const creating = ref(false)
const contacts = computed(() => store.campaigns.contacts || [])
const campaigns = computed(() => store.campaigns.campaigns || [])

function statusType(s) {
  return { sent: 'success', running: 'warning', scheduled: 'primary', partial: 'warning' }[s] || 'info'
}

async function create() {
  if (!form.content.trim()) { toast('先写活动正文', 'warning'); return }
  if (!form.send_at) { toast('先选发送时间', 'warning'); return }
  if (!selected.value.length) { toast('至少选一个发送对象', 'warning'); return }
  creating.value = true
  try {
    const targets = contacts.value
      .filter((c) => selected.value.includes(c.conv_id))
      .map((c) => ({ conv_id: c.conv_id, name: c.name, kind: c.kind }))
    const r = await api.campaignCreate({ ...form, targets })
    if (r.ok === false) throw new Error(r.error || '创建失败')
    toast('活动已排期')
    Object.assign(form, { name: '', content: '', send_at: '', grace_minutes: 60 })
    selected.value = []
    await refreshPage('campaign')
  } catch (e) { toast('创建失败：' + e.message, 'error') }
  finally { creating.value = false }
}

async function action(c, act) {
  const labels = { now: '立即发送', cancel: '取消', del: '删除' }
  try {
    await ElMessageBox.confirm(`确定${labels[act]}「${c.name || '未命名活动'}」？`, '请确认', { type: 'warning' })
  } catch { return }
  try {
    if (act === 'now') await api.campaignSendNow(c.id)
    else if (act === 'cancel') await api.campaignCancel(c.id)
    else await api.campaignDelete(c.id)
    toast('操作完成')
    await refreshPage('campaign')
  } catch (e) { toast('操作失败：' + e.message, 'error') }
}
</script>

<template>
  <div class="wb-page">
    <div class="wb-page-head">
      <div>
        <h2>活动触达</h2>
        <div class="sub">固定文案 + 指定时间 + 指定对象，到点由主进程逐个发出；不走 AI、不占主动触达额度</div>
      </div>
    </div>

    <div class="wb-grid2">
      <div class="wb-card">
        <h3>新建活动</h3>
        <el-form label-width="90px" size="small">
          <el-form-item label="名称"><el-input v-model="form.name" placeholder="可留空" /></el-form-item>
          <el-form-item label="发送时间">
            <el-date-picker
              v-model="form.send_at" type="datetime" style="width: 100%"
              format="YYYY-MM-DD HH:mm" value-format="YYYY-MM-DD[T]HH:mm"
              placeholder="选择日期时间"
            />
          </el-form-item>
          <el-form-item label="过点宽限(分)"><el-input-number v-model="form.grace_minutes" :min="1" :max="1440" /></el-form-item>
          <el-form-item label="正文">
            <el-input v-model="form.content" type="textarea" :rows="4" maxlength="2000" show-word-limit placeholder="写死要发的文案，≤2000 字" />
          </el-form-item>
        </el-form>
      </div>
      <div class="wb-card">
        <h3>发送对象</h3>
        <ContactPicker v-model="selected" :contacts="contacts" />
      </div>
    </div>

    <div style="margin-bottom: 16px">
      <el-button type="primary" :loading="creating" @click="create">创建活动</el-button>
      <span class="wb-hint" style="margin-left: 12px">看板只写配置，真发送由 auto_reply 主进程执行</span>
    </div>

    <div class="wb-card">
      <h3>活动列表 {{ campaigns.length }}</h3>
      <div v-for="c in campaigns" :key="c.id" style="padding: 12px 0; border-bottom: 1px solid var(--wb-border)">
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
          <b style="font-size: 15px">{{ c.name || '未命名活动' }}</b>
          <el-tag size="small" effect="plain" :type="statusType(c.status)">{{ CAMPAIGN_STATUS[c.status] || c.status }}</el-tag>
          <span style="font-size: 13px; color: var(--wb-muted)">计划 {{ fmtTime(c.send_at) }} · {{ (c.targets || []).length }} 个对象</span>
          <div style="margin-left: auto; display: flex; gap: 6px">
            <el-button v-if="c.status === 'scheduled'" size="small" @click="action(c, 'now')">立即发送</el-button>
            <el-button v-if="c.status === 'scheduled'" size="small" @click="action(c, 'cancel')">取消</el-button>
            <el-button v-if="c.status !== 'scheduled'" size="small" type="danger" plain @click="action(c, 'del')">删除</el-button>
          </div>
        </div>
        <div style="font-size: 14px; color: var(--wb-muted); margin-top: 6px; white-space: pre-wrap">{{ c.content }}</div>
        <div v-if="(c.result || []).length" style="font-size: 13px; color: var(--wb-muted); margin-top: 6px">
          结果：成功 {{ c.result.filter((r) => r.ok).length }} / 失败 {{ c.result.filter((r) => !r.ok).length }}
        </div>
      </div>
      <div v-if="!campaigns.length" class="wb-empty">还没有活动</div>
    </div>
  </div>
</template>
