<script setup>
import { computed, inject, nextTick, ref, watch } from 'vue'
import { api } from '../api'
import { fmtClock, avatarUrl } from '../utils'
import WbAvatar from '../components/WbAvatar.vue'
import ContactPicker from '../components/ContactPicker.vue'
import EmojiPicker from '../components/EmojiPicker.vue'
import MessageBubble from '../components/MessageBubble.vue'

const store = inject('store')
const toast = inject('toast')
const refreshPage = inject('refreshPage')

const kw = ref('')
const showAll = ref(false)   // 默认只列「托管过」的会话；勾选后才显示全部聊天
const pickerOpen = ref(false)
const selectedConvIds = ref([])   // 一次性勾选，关掉弹窗就清空
const pickerBusy = ref(false)
const draft = ref('')
const sending = ref(false)
const scroller = ref(null)
const draftInput = ref(null)
const fileInput = ref(null)
const imageBusy = ref(false)
const assetOpen = ref(false)
const assets = ref([])

// 表情插到光标处，不是无脑追加到末尾：正文写到一半点表情是常态。
function insertEmoji(s) {
  const ta = draftInput.value?.$el?.querySelector?.('textarea')
  const cur = draft.value || ''
  const pos = (ta && ta.selectionStart != null) ? ta.selectionStart : cur.length
  draft.value = cur.slice(0, pos) + s + cur.slice(pos)
  nextTick(() => {
    if (!ta) return
    ta.focus()
    const p = pos + s.length
    try { ta.setSelectionRange(p, p) } catch (_) { /* 某些浏览器不支持就算了 */ }
  })
}

async function doSendImage(assetId) {
  const r = await api.sendImage(store.activeConv, assetId)
  if (r.ok === false) throw new Error(r.error || '发送失败')
  // 图片不走文本回读，后端也不写看板库；真实图片消息由收消息桥补录上屏
  toast('图片已提交发送；真实消息稍后由回执上屏')
  assetOpen.value = false
}

async function uploadAndSendImage(e) {
  const f = e.target.files?.[0]
  e.target.value = ''
  if (!f || !store.activeConv || imageBusy.value) return
  imageBusy.value = true
  try {
    const up = await api.mediaUpload(f)
    if (up.ok === false) throw new Error(up.error || '上传失败')
    await doSendImage(up.item.id)
  } catch (err) {
    toast('图片发送失败：' + err.message, 'error')
  } finally { imageBusy.value = false }
}

function openImageFile() { fileInput.value?.click() }

async function openAssets() {
  try {
    const r = await api.mediaList()
    assets.value = r.items || []
    assetOpen.value = true
  } catch (e) { toast('读取素材失败：' + e.message, 'error') }
}

async function sendAsset(assetId) {
  if (imageBusy.value || !store.activeConv) return
  imageBusy.value = true
  try { await doSendImage(assetId) }
  catch (e) { toast('图片发送失败：' + e.message, 'error') }
  finally { imageBusy.value = false }
}

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

// 只列「还没托管」的通讯录好友 / 群聊。已托管的已经在左边列表里，不重复出现。
// 直接复用接管页 / 活动页那个选择器：已选固定在上方、头像、整行可点都是现成的。
const pickerContacts = computed(() => (store.contacts || []).filter(
  (c) => !c.auto_reply && ['friend', 'group'].includes(c.kind)))

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

// 勾选完一次提交：后端一个事务写完全部开关，中途失败不会留下半截状态。
// 后端开启时会再校验一次身份（陌生人 / 公众号 / 自己一律跳过），skipped 要如实告诉用户。
async function submitBatch() {
  const ids = [...selectedConvIds.value]
  if (!ids.length || pickerBusy.value) return
  pickerBusy.value = true
  try {
    const r = await api.switchBatch(ids, true)
    if (r.ok === false) throw new Error(r.error || '开启失败')
    const done = new Set(r.updated || [])
    const contacts = store.contacts || []
    contacts.forEach((c) => {
      if (done.has(c.conv_id)) { c.auto_reply = 1; c.switched = 1 }
    })
    const skipped = (r.skipped || []).length
    toast(`已开启托管 ${done.size} 个${skipped ? `，${skipped} 个不在通讯录好友 / 群聊里，已跳过` : ''}`)
    pickerOpen.value = false
    selectedConvIds.value = []
    await refreshPage('chat')      // 重新拉会话列表，左侧立刻出现新托管的占位会话
  } catch (e) {
    toast('开启失败：' + e.message, 'error')
  } finally { pickerBusy.value = false }
}

// 「同步头像」只更新通讯录里的头像字段，不整页刷新 —— 否则会冲掉未提交的勾选。
function onContactsUpdated(list) {
  if (!Array.isArray(list)) return
  const fresh = new Map(list.map((c) => [c.conv_id, c]))
  const cur = store.contacts || []
  cur.forEach((c) => {
    const f = fresh.get(c.conv_id)
    if (f && f.avatar_url) c.avatar_url = f.avatar_url
  })
  const known = new Set(cur.map((c) => c.conv_id))
  const extra = list.filter((c) => !known.has(c.conv_id)).map((c) => ({ ...c, auto_reply: 0 }))
  if (extra.length) store.contacts = cur.concat(extra)
}

async function openPicker() {
  selectedConvIds.value = []
  pickerOpen.value = true
  await refreshPage('chat')        // 拉最新通讯录 + 托管状态
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
          <div class="wb-composer-tools">
            <EmojiPicker :disabled="!store.activeConv" @pick="insertEmoji" />
            <el-button size="small" :loading="imageBusy" :disabled="!store.activeConv"
                       @click="openImageFile">图片</el-button>
            <el-button size="small" :disabled="!store.activeConv" @click="openAssets">素材库</el-button>
            <input ref="fileInput" type="file" accept="image/*" style="display: none"
                   @change="uploadAndSendImage" />
            <span class="wb-hint">图片发送后由消息回执上屏，不会立刻显示</span>
          </div>
          <el-input
            ref="draftInput"
            v-model="draft" type="textarea" :rows="2" resize="none"
            placeholder="输入消息，Enter 发送（Shift+Enter 换行）"
            @keydown.enter.exact.prevent="send"
          />
          <el-button type="primary" :loading="sending" @click="send">发送</el-button>
        </div>
      </template>
      <div v-else class="wb-empty" style="margin: auto">从左边选一个会话开始</div>
    </section>

    <el-dialog v-model="pickerOpen" title="批量开启托管" width="560px" align-center>
      <ContactPicker
        v-model="selectedConvIds" :contacts="pickerContacts"
        @contacts-updated="onContactsUpdated"
      />
      <template #footer>
        <div class="wb-picker-foot">
          <span class="wb-hint">可一次勾选多个，提交时后端一个事务全部写完</span>
          <div>
            <el-button size="small" @click="pickerOpen = false">取消</el-button>
            <el-button
              type="primary" size="small" :loading="pickerBusy"
              :disabled="!selectedConvIds.length" @click="submitBatch"
            >开启托管{{ selectedConvIds.length ? '(' + selectedConvIds.length + ')' : '' }}</el-button>
          </div>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="assetOpen" title="从素材库选一张图片发送" width="520px" align-center>
      <div class="wb-assets">
        <div v-for="a in assets" :key="a.id" class="wb-asset" @click="sendAsset(a.id)">
          <img :src="a.url" :alt="a.name" />
          <span :title="a.name">{{ a.name }}</span>
        </div>
        <div v-if="!assets.length" class="wb-empty" style="grid-column: 1 / -1">
          素材库还是空的 —— 点「图片」上传一张，上传后会自动发给当前会话
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.wb-composer-tools {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap; width: 100%;
}
.wb-composer-tools .wb-hint { font-size: 12px; }
.wb-assets { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.wb-asset {
  cursor: pointer; padding: 6px; text-align: center;
  border: 1px solid var(--wb-border); border-radius: 8px;
}
.wb-asset:hover { border-color: var(--wb-accent); background: var(--wb-accent-soft); }
.wb-asset img { width: 100%; height: 84px; object-fit: cover; border-radius: 5px; }
.wb-asset span {
  display: block; margin-top: 5px; font-size: 12px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.wb-picker-foot {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; flex-wrap: wrap; width: 100%;
}
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
