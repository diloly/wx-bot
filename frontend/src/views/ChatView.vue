<script setup>
import { computed, inject, nextTick, ref, watch } from 'vue'
import { api } from '../api'
import { fmtClock, avatarUrl } from '../utils'
import WbAvatar from '../components/WbAvatar.vue'
import MessageBubble from '../components/MessageBubble.vue'

const store = inject('store')
const toast = inject('toast')
const refreshPage = inject('refreshPage')

const kw = ref('')
const showAll = ref(false)   // 默认只列「托管过」的会话；勾选后才显示全部聊天
const pickerOpen = ref(false)
const pickerKw = ref('')
const pickerKind = ref('all')
const draft = ref('')
const sending = ref(false)
const scroller = ref(null)

// 不再按 auto_reply 过滤：人工接管会关掉托管，那个客户不能因此从看板消失。
// 默认只列「托管过」的会话（switched=1，含已关闭），避免几百个普通聊天糊满左栏；
// 勾「显示全部会话」才放开。保持后端 last_ts DESC 顺序 —— 刚表达意向的客户
// 排最前，一眼看得到。
const convs = computed(() => (store.conversations || []).filter((c) => {
  if (!showAll.value && !c.auto_reply && !c.switched) return false
  return !kw.value || (String(c.name) + String(c.conv_id)).includes(kw.value)
}))

const active = computed(() => (store.conversations || []).find((c) => c.conv_id === store.activeConv) || null)
const meAvatar = computed(() => avatarUrl(store.me))
const peerAvatar = computed(() => active.value?.avatar_url || '')
const isGroup = computed(() => String(store.activeConv).endsWith('@chatroom'))

const pickerList = computed(() => (store.contacts || []).filter((c) => {
  if (c.auto_reply || !['friend', 'group'].includes(c.kind)) return false
  if (pickerKind.value === 'group' && c.kind !== 'group') return false
  if (pickerKind.value === 'friend' && c.kind !== 'friend') return false
  if (pickerKw.value && !(String(c.name) + String(c.conv_id)).toLowerCase().includes(pickerKw.value.toLowerCase())) return false
  return true
}))

function scrollBottom() {
  nextTick(() => { if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight })
}
watch(() => store.messages.length, scrollBottom)

async function openConv(id) {
  store.activeConv = id
  store.messages = []
  try {
    await api.open(id)
    const d = await api.messages(id)
    store.messages = Array.isArray(d) ? d : []
    scrollBottom()
  } catch (e) { console.warn(e) }
}

async function toggleConv(c, v) {
  try {
    await api.switch(c.conv_id, v)
    c.auto_reply = v ? 1 : 0
    if (v) c.switched = 1           // 数据库已写入 conv_switch，本地也同步，
                                    // 否则下次关掉时会被默认筛选误藏
    // 关掉托管 ≠ 删掉会话：转人工后仍留在左侧，且不清空当前打开的聊天，
    // 真人要能接着看上下文。
  } catch (e) { toast('切换失败：' + e.message, 'error') }
}

async function enableConv(c) {
  try {
    await api.switch(c.conv_id, true)
    c.auto_reply = 1
    c.switched = 1
    pickerOpen.value = false
    toast('已开启托管：' + c.name)
    refreshPage('chat')
  } catch (e) { toast('开启失败：' + e.message, 'error') }
}

async function openPicker() {
  pickerOpen.value = true
  pickerKw.value = ''
  pickerKind.value = 'all'
  await refreshPage('chat')
}

async function send() {
  const text = draft.value.trim()
  if (!text || !store.activeConv || sending.value) return
  sending.value = true
  try {
    const r = await api.send(store.activeConv, text)
    if (r.ok === false) throw new Error(r.error || '发送失败')
    draft.value = ''
    const d = await api.messages(store.activeConv)
    store.messages = Array.isArray(d) ? d : []
    scrollBottom()
  } catch (e) {
    toast('发送失败：' + e.message, 'error')
  } finally { sending.value = false }
}

async function delMessage(id) {
  try {
    await api.deleteMessage(id)
    store.messages = store.messages.filter((m) => m.id !== id)
  } catch (e) { toast('删除失败：' + e.message, 'error') }
}
</script>

<template>
  <div class="wb-view">
    <aside class="wb-side">
      <div class="wb-side-head">
        <el-input v-model="kw" size="small" clearable placeholder="搜索名字 / 备注 / wxid" />
        <el-button size="small" type="primary" circle title="开启新的托管会话" @click="openPicker">+</el-button>
      </div>
      <div class="wb-side-filter">
        <el-checkbox v-model="showAll" size="small">显示全部会话</el-checkbox>
        <span class="wb-hint">{{ convs.length }} 个会话</span>
      </div>
      <div class="wb-list">
        <div v-for="c in convs" :key="c.conv_id" class="wb-row" :class="{ on: store.activeConv === c.conv_id }" @click="openConv(c.conv_id)">
          <WbAvatar :url="c.avatar_url" :name="c.name" />
          <div class="meta">
            <div class="nm">
              <span class="nm-text">{{ c.name }}</span>
              <span v-if="c.handoff_state === 'waiting'" class="wb-flag handoff">待人工接手</span>
              <span v-else-if="!c.auto_reply && (c.handoff_state === 'taken' || c.handoff_ts)" class="wb-flag handoff">已转人工</span>
              <span v-else-if="!c.auto_reply && c.switched" class="wb-flag">已关托管</span>
              <span v-else-if="!c.auto_reply" class="wb-flag">未托管</span>
            </div>
            <div class="sub">{{ c.last_msg || '暂无消息' }}</div>
          </div>
          <div style="display: flex; flex-direction: column; align-items: flex-end; gap: 4px; flex-shrink: 0">
            <span v-if="c.last_msg" class="ts">{{ fmtClock(c.last_ts) }}</span>
            <el-switch
              size="small" :model-value="!!c.auto_reply"
              @click.stop @change="(v) => toggleConv(c, v)"
            />
          </div>
        </div>
        <div v-if="!convs.length" class="wb-empty">
          没有匹配的会话<br />点右上角 <b>+</b> 从通讯录里挑要托管的对象
        </div>
      </div>
    </aside>

    <section class="wb-panel">
      <template v-if="active">
        <div class="wb-panel-head">
          <WbAvatar :url="active.avatar_url" :name="active.name" size="sm" />
          <div>
            <div class="title">{{ active.name }}</div>
            <div class="sub">
              {{ isGroup ? '群聊' : '通讯录好友' }} ·
              {{ active.handoff_state === 'waiting' ? '待人工接手（AI 暂代）'
                 : (active.auto_reply ? '托管中' : (active.handoff_ts ? '已转人工' : '未托管')) }}
            </div>
          </div>
          <div class="spacer"></div>
          <span class="wb-hint">AI 自动回复</span>
          <el-switch :model-value="!!active.auto_reply" @change="(v) => toggleConv(active, v)" />
        </div>

        <div ref="scroller" class="wb-scroll">
          <div class="wb-msgs">
            <MessageBubble
              v-for="m in store.messages" :key="m.id || (m.ts + '_' + m.text)"
              :msg="m" :conv-id="store.activeConv" :me-avatar="meAvatar" :peer-avatar="peerAvatar"
              @delete="delMessage"
            />
            <div v-if="!store.messages.length" class="wb-empty">还没有聊天记录，发一条消息开始吧</div>
          </div>
        </div>

        <div class="wb-composer">
          <el-input
            v-model="draft" type="textarea" :rows="2" resize="none"
            placeholder="输入消息，Enter 发送（Shift+Enter 换行）"
            @keydown.enter.exact.prevent="send"
          />
          <el-button type="primary" :loading="sending" @click="send">发送</el-button>
        </div>
      </template>
      <div v-else class="wb-empty" style="margin: auto">从左边选一个会话开始</div>
    </section>

    <el-dialog v-model="pickerOpen" title="选择要开启托管的会话" width="480px" align-center>
      <div class="wb-filters">
        <el-tag
          v-for="f in [{ id: 'all', label: '全部' }, { id: 'friend', label: '好友' }, { id: 'group', label: '群聊' }]"
          :key="f.id" size="small" style="cursor: pointer"
          :effect="pickerKind === f.id ? 'dark' : 'plain'" @click="pickerKind = f.id"
        >{{ f.label }}</el-tag>
      </div>
      <el-input v-model="pickerKw" size="small" clearable placeholder="搜索名字 / 备注 / wxid" />
      <div style="max-height: 46vh; overflow-y: auto; margin-top: 10px">
        <div v-for="c in pickerList" :key="c.conv_id" class="wb-picker-row" @click="enableConv(c)">
          <WbAvatar :url="c.avatar_url" :name="c.name" size="sm" />
          <span style="flex: 1; min-width: 0; font-size: 14px">{{ c.name }}</span>
          <el-tag v-if="!c.has_conv" size="small" effect="plain">未聊过</el-tag>
          <el-tag size="small" effect="plain" :type="c.kind === 'group' ? 'primary' : 'info'">
            {{ c.kind === 'group' ? '群聊' : '好友' }}
          </el-tag>
          <span style="color: var(--wb-accent); font-size: 19px">+</span>
        </div>
        <div v-if="!pickerList.length" class="wb-empty">没有可开启的会话</div>
      </div>
      <template #footer>
        <span class="wb-hint">只列通讯录好友与群聊，群里的陌生人已排除</span>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.wb-side-filter {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px; padding: 0 12px 8px;
}
.wb-row .nm { display: flex; align-items: center; gap: 5px; }
.nm-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wb-flag {
  flex-shrink: 0; padding: 0 5px; border-radius: 4px;
  font-size: 11px; line-height: 16px; font-weight: 400; white-space: nowrap;
  color: var(--wb-muted); background: var(--wb-surface-2);
  border: 1px solid var(--wb-border);
}
.wb-flag.handoff {
  color: #e6a23c; background: rgba(230, 162, 60, .12);
  border-color: rgba(230, 162, 60, .38);
}
html.dark .wb-flag.handoff { color: #f0b862; }
</style>
