<script setup>
import { computed, inject, reactive, ref, watch } from 'vue'
import { api } from '../api'
import { fmtTime, parseJson } from '../utils'
import ContactPicker from '../components/ContactPicker.vue'

const store = inject('store')
const toast = inject('toast')
const refreshPage = inject('refreshPage')

const DEFAULTS = {
  enabled: true,
  watch_scope: 'activated',
  watch_days: 7,
  detector: 'ai',
  cooldown_hours: 24,
  pause_auto_reply: true,
  skip_ai_reply: true,
  handoff_timeout_hours: 2,
}

const STATE_LABEL = {
  waiting: '待人工接手', taken: '真人已接手', expired: '超时·AI 已恢复', done: '已完成',
}
const STATE_TYPE = {
  waiting: 'warning', taken: 'success', expired: 'info', done: 'info',
}

const form = reactive({ ...DEFAULTS })
const selected = ref([])
const saving = ref(false)
const contacts = computed(() => store.handoff.contacts || [])
const recent = computed(() => store.handoff.recent || [])

watch(() => store.handoff.config, (c) => {
  Object.assign(form, DEFAULTS, c || {})
  selected.value = (c?.targets || []).map((t) => t.conv_id || t)
}, { immediate: true })

async function save() {
  saving.value = true
  try {
    const targets = contacts.value
      .filter((c) => selected.value.includes(c.conv_id))
      .map((c) => ({ conv_id: c.conv_id, name: c.name, kind: c.kind }))
    const r = await api.handoffSave({ ...form, targets })
    if (r.ok === false) throw new Error(r.error || '保存失败')
    toast('接管配置已保存')
    await refreshPage('handoff')
  } catch (e) { toast('保存失败：' + e.message, 'error') }
  finally { saving.value = false }
}
</script>

<template>
  <div class="wb-page">
    <div class="wb-page-head">
      <div>
        <h2>人工接管</h2>
        <div class="sub">客户表现出意向时先通知真人；真人真正回复后才交出会话</div>
      </div>
      <div style="display: flex; align-items: center; gap: 10px">
        <span class="wb-hint">启用</span>
        <el-switch v-model="form.enabled" />
      </div>
    </div>

    <div class="ho-flow">
      <b>交出会话的时机</b>：检测到意向 → 通知真人（<b>托管保持开启</b>，等待期客户再发消息由 AI 正常接住）
      → 真人在该客户会话里回了一句话 → 才正式关闭 AI 托管。
      若 {{ form.handoff_timeout_hours }} 小时内无人接手，自动解除待接管、恢复常规自动回复并留一条记录。
      通知一条都没投出去时不进入待接管，也不动托管开关。
    </div>

    <div class="wb-grid2">
      <div class="wb-card">
        <h3>判定与动作</h3>
        <el-form label-width="112px" size="small">
          <el-form-item label="监听范围">
            <el-select v-model="form.watch_scope" style="width: 100%">
              <el-option label="仅近期被激活的客户" value="activated" />
              <el-option label="所有会话" value="all" />
            </el-select>
          </el-form-item>
          <el-form-item label="激活回溯天数"><el-input-number v-model="form.watch_days" :min="1" :max="90" /></el-form-item>
          <el-form-item label="意向判定">
            <el-select v-model="form.detector" style="width: 100%">
              <el-option label="AI 二分类（失败降级关键词）" value="ai" />
              <el-option label="仅关键词" value="keywords" />
            </el-select>
          </el-form-item>
          <el-form-item label="同会话冷却(小时)"><el-input-number v-model="form.cooldown_hours" :min="1" :max="720" /></el-form-item>
          <el-form-item label="等待接手(小时)"><el-input-number v-model="form.handoff_timeout_hours" :min="0" :max="72" /></el-form-item>
          <el-form-item label="接手后关托管"><el-switch v-model="form.pause_auto_reply" /></el-form-item>
          <el-form-item label="跳过本次 AI 回复"><el-switch v-model="form.skip_ai_reply" /></el-form-item>
        </el-form>
      </div>

      <div class="wb-card">
        <h3>通知谁</h3>
        <ContactPicker v-model="selected" :contacts="contacts"
                       @contacts-updated="(v) => store.handoff.contacts = v" />
      </div>
    </div>

    <div style="margin-bottom: 16px">
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
      <span class="wb-hint" style="margin-left: 12px">保存后主进程实时读取，无需重启</span>
    </div>

    <div class="wb-card">
      <h3>接管记录 {{ recent.length }}</h3>
      <div v-for="r in recent" :key="r.id" style="padding: 9px 0; border-bottom: 1px solid var(--wb-border); font-size: 14px">
        <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap">
          <b>{{ r.customer || r.conv_id }}</b>
          <el-tag size="small" effect="plain" :type="STATE_TYPE[r.state || 'done']">
            {{ STATE_LABEL[r.state || 'done'] }}
          </el-tag>
          <el-tag v-if="r.state === 'taken' && r.paused" size="small" type="warning" effect="plain">AI 托管已关</el-tag>
          <span style="margin-left: auto; color: var(--wb-muted)">{{ fmtTime(r.ts) }}</span>
        </div>
        <div style="color: var(--wb-muted); margin-top: 4px">「{{ r.quote || '' }}」</div>
        <div style="color: var(--wb-muted); margin-top: 2px">
          {{ r.reason || '' }} · 通知 {{ parseJson(r.targets, []).length }} 个对象
          <template v-if="r.resolved_ts"> · 收口 {{ fmtTime(r.resolved_ts) }}</template>
        </div>
      </div>
      <div v-if="!recent.length" class="wb-empty">还没有接管记录</div>
    </div>
  </div>
</template>

<style scoped>
.ho-flow {
  font-size: 14px; line-height: 1.8; color: var(--wb-text);
  background: var(--wb-surface-2); border: 1px solid var(--wb-border);
  border-left: 3px solid var(--wb-accent);
  border-radius: 8px; padding: 10px 14px; margin-bottom: 14px;
}
.ho-flow b { font-weight: 500; }
</style>
