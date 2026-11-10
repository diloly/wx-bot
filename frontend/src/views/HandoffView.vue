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
        <div class="sub">客户表现出意向时通知真人，并暂停该会话的自动回复</div>
      </div>
      <div style="display: flex; align-items: center; gap: 10px">
        <span class="wb-hint">启用</span>
        <el-switch v-model="form.enabled" />
      </div>
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
          <el-form-item label="暂停自动回复"><el-switch v-model="form.pause_auto_reply" /></el-form-item>
          <el-form-item label="跳过本次 AI 回复"><el-switch v-model="form.skip_ai_reply" /></el-form-item>
        </el-form>
      </div>

      <div class="wb-card">
        <h3>通知谁</h3>
        <ContactPicker v-model="selected" :contacts="contacts" />
      </div>
    </div>

    <div style="margin-bottom: 16px">
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
      <span class="wb-hint" style="margin-left: 12px">保存后主进程实时读取，无需重启</span>
    </div>

    <div class="wb-card">
      <h3>接管记录 {{ recent.length }}</h3>
      <div v-for="r in recent" :key="r.id" style="padding: 9px 0; border-bottom: 1px solid var(--wb-border); font-size: 14px">
        <div style="display: flex; gap: 10px; align-items: center">
          <b>{{ r.customer || r.conv_id }}</b>
          <el-tag v-if="r.paused" size="small" type="warning" effect="plain">已暂停回复</el-tag>
          <span style="margin-left: auto; color: var(--wb-muted)">{{ fmtTime(r.ts) }}</span>
        </div>
        <div style="color: var(--wb-muted); margin-top: 4px">「{{ r.quote || '' }}」</div>
        <div style="color: var(--wb-muted); margin-top: 2px">
          {{ r.reason || '' }} · 通知 {{ parseJson(r.targets, []).length }} 个对象
        </div>
      </div>
      <div v-if="!recent.length" class="wb-empty">还没有接管记录</div>
    </div>
  </div>
</template>
