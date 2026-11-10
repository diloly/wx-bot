<script setup>
import { computed, inject, ref } from 'vue'
import WbAvatar from './WbAvatar.vue'
import { api } from '../api'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  contacts: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:modelValue', 'contacts-updated'])
const toast = inject('toast')

const kw = ref('')
const filter = ref('all')
const syncing = ref(false)
const FILTERS = [
  { id: 'all', label: '全部' },
  { id: 'group', label: '群聊' },
  { id: 'friend', label: '好友' },
  { id: 'selected', label: '已选' },
]

const byId = computed(() => new Map(props.contacts.map((c) => [c.conv_id, c])))

// 「选出来固定在上方」：已选项单独渲染成 chips，不随下面列表的筛选/滚动消失。
// 已保存但通讯录里已经不存在的人也照样显示，标灰提醒保存时会被忽略。
const selectedItems = computed(() => props.modelValue.map((id) => {
  const c = byId.value.get(id)
  return c ? { ...c, missing: false }
    : { conv_id: id, name: id, kind: '', avatar_url: '', missing: true }
}))
const withAvatar = computed(() => props.contacts.filter((c) => c.avatar_url).length)

const list = computed(() => props.contacts.filter((c) => {
  if (filter.value === 'selected' && !props.modelValue.includes(c.conv_id)) return false
  if (['group', 'friend'].includes(filter.value) && c.kind !== filter.value) return false
  if (kw.value && !(String(c.name) + String(c.conv_id)).toLowerCase().includes(kw.value.toLowerCase())) return false
  return true
}))

function count(id) {
  if (id === 'all') return props.contacts.length
  if (id === 'selected') return props.modelValue.length
  return props.contacts.filter((c) => c.kind === id).length
}

function toggle(id, checked) {
  const next = new Set(props.modelValue)
  if (checked) next.add(id); else next.delete(id)
  emit('update:modelValue', [...next])
}

// 整行都能点，不用非得瞄准那个小方框；点方框时由 checkbox 自己的 change 处理，
// 这里跳过，避免冒泡导致 toggle 两次相互抵消。
function rowClick(c, e) {
  if (e.target.closest && e.target.closest('.el-checkbox')) return
  toggle(c.conv_id, !props.modelValue.includes(c.conv_id))
}

async function syncAvatars() {
  syncing.value = true
  try {
    const r = await api.avatarsSync()
    if (r.ok === false) { toast(r.error || '同步失败', 'error'); return }
    // 只局部换候选列表，不整页刷新 —— 否则会冲掉正在编辑的表单和未保存的勾选
    if (Array.isArray(r.contacts)) emit('contacts-updated', r.contacts)
    toast(`头像同步完成：新增 ${r.downloaded || 0}、已是最新 ${r.cached || 0}、失败 ${r.failed || 0}`)
  } catch (e) {
    toast('同步失败：' + e.message, 'error')
  } finally {
    syncing.value = false
  }
}
</script>

<template>
  <div class="wb-picker">
    <div class="wb-selected">
      <div class="wb-selected-head">
        <span class="t">已选对象 <b>{{ modelValue.length }}</b></span>
        <el-button v-if="modelValue.length" link type="danger" size="small"
                   @click="emit('update:modelValue', [])">清空</el-button>
        <span class="wb-hint" style="margin-left: auto">选中的固定在这里，点 × 移除</span>
      </div>
      <div class="wb-selected-list">
        <div v-for="it in selectedItems" :key="it.conv_id" class="wb-chip" :class="{ missing: it.missing }">
          <WbAvatar :url="it.avatar_url" :name="it.name" size="sm" />
          <span class="nm" :title="it.name">{{ it.name }}</span>
          <span v-if="it.missing" class="miss">通讯录已无</span>
          <el-tag v-else size="small" effect="plain" :type="it.kind === 'group' ? 'primary' : 'info'">
            {{ it.kind === 'group' ? '群聊' : '好友' }}
          </el-tag>
          <button class="x" title="移除" @click="toggle(it.conv_id, false)">×</button>
        </div>
        <div v-if="!modelValue.length" class="wb-selected-empty">
          还没选对象 —— 在下面列表点名字即可选中，选中的会固定显示在这里
        </div>
      </div>
    </div>

    <div class="wb-picker-tools">
      <div class="wb-filters" style="margin-bottom: 0">
        <el-tag
          v-for="f in FILTERS" :key="f.id"
          :effect="filter === f.id ? 'dark' : 'plain'"
          size="small" style="cursor: pointer" @click="filter = f.id"
        >{{ f.label }} {{ count(f.id) }}</el-tag>
      </div>
      <div class="wb-avatar-stat">
        <span class="wb-hint">头像 {{ withAvatar }}/{{ contacts.length }}</span>
        <el-button link type="primary" size="small" :loading="syncing" @click="syncAvatars">同步头像</el-button>
      </div>
    </div>
    <el-input v-model="kw" size="small" clearable placeholder="搜索名字 / wxid" />
    <div class="wb-picker-list">
      <div v-for="c in list" :key="c.conv_id" class="wb-picker-row"
           :class="{ picked: modelValue.includes(c.conv_id) }" @click="rowClick(c, $event)">
        <el-checkbox
          :model-value="modelValue.includes(c.conv_id)"
          @change="(v) => toggle(c.conv_id, v)"
        />
        <WbAvatar :url="c.avatar_url" :name="c.name" size="sm" />
        <span style="flex: 1; min-width: 0; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">{{ c.name }}</span>
        <el-tag size="small" effect="plain" :type="c.kind === 'group' ? 'primary' : 'info'">
          {{ c.kind === 'group' ? '群聊' : '好友' }}
        </el-tag>
      </div>
      <div v-if="!list.length" class="wb-empty">没有匹配的对象</div>
    </div>
    <div style="margin-top: 6px; font-size: 13px; color: var(--wb-muted)">
      只列通讯录好友与群聊，群里的陌生人已排除 · 整行可点选
    </div>
  </div>
</template>

<style scoped>
.wb-selected {
  background: var(--wb-surface-2); border: 1px solid var(--wb-border);
  border-radius: 10px; padding: 10px 12px; margin-bottom: 12px;
}
.wb-selected-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.wb-selected-head .t { font-size: 14px; }
.wb-selected-head .t b { color: var(--wb-accent); font-size: 16px; }
.wb-selected-list { display: flex; flex-wrap: wrap; gap: 8px; max-height: 138px; overflow-y: auto; }
.wb-chip {
  display: flex; align-items: center; gap: 7px; max-width: 250px;
  background: var(--wb-surface); border: 1px solid var(--wb-border);
  border-radius: 999px; padding: 4px 7px 4px 5px;
}
.wb-chip .nm { font-size: 13px; max-width: 118px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.wb-chip .x {
  border: 0; background: transparent; color: var(--wb-muted); cursor: pointer;
  font-size: 17px; line-height: 1; padding: 0 2px;
}
.wb-chip .x:hover { color: #fa5151; }
.wb-chip.missing { opacity: .65; border-style: dashed; }
.wb-chip .miss { font-size: 12px; color: #e6a23c; white-space: nowrap; }
.wb-selected-empty { font-size: 13px; color: var(--wb-muted); padding: 6px 2px; line-height: 1.6; }
.wb-picker-tools {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px; flex-wrap: wrap; margin-bottom: 8px;
}
.wb-avatar-stat { display: flex; align-items: center; gap: 6px; }
.wb-picker-list { max-height: 220px; overflow-y: auto; margin-top: 8px; }
.wb-picker-row { cursor: pointer; }
.wb-picker-row.picked { background: var(--wb-accent-soft); }
</style>
