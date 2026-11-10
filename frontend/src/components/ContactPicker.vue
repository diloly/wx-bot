<script setup>
import { computed, ref } from 'vue'
import { initial } from '../utils'
import WbAvatar from './WbAvatar.vue'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  contacts: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:modelValue'])

const kw = ref('')
const filter = ref('all')
const FILTERS = [
  { id: 'all', label: '全部' },
  { id: 'group', label: '群聊' },
  { id: 'friend', label: '好友' },
  { id: 'selected', label: '已选' },
]

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
</script>

<template>
  <div>
    <div class="wb-filters">
      <el-tag
        v-for="f in FILTERS" :key="f.id"
        :effect="filter === f.id ? 'dark' : 'plain'"
        size="small" style="cursor: pointer" @click="filter = f.id"
      >{{ f.label }} {{ count(f.id) }}</el-tag>
    </div>
    <el-input v-model="kw" size="small" clearable placeholder="搜索名字 / wxid" />
    <div style="max-height: 220px; overflow-y: auto; margin-top: 8px">
      <div v-for="c in list" :key="c.conv_id" class="wb-picker-row">
        <el-checkbox
          :model-value="modelValue.includes(c.conv_id)"
          @change="(v) => toggle(c.conv_id, v)"
        />
        <WbAvatar :name="c.name" size="sm" />
        <span style="flex: 1; min-width: 0; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">{{ c.name }}</span>
        <el-tag size="small" effect="plain" :type="c.kind === 'group' ? 'primary' : 'info'">
          {{ c.kind === 'group' ? '群聊' : '好友' }}
        </el-tag>
      </div>
      <div v-if="!list.length" class="wb-empty">没有匹配的对象</div>
    </div>
    <div style="margin-top: 6px; font-size: 11px; color: var(--wb-muted)">
      已选 {{ modelValue.length }} 个 · 只列通讯录好友与群聊，群里的陌生人已排除
    </div>
  </div>
</template>
