<script setup>
import { reactive, ref, watch, provide, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Moon, Sunny } from '@element-plus/icons-vue'
import { api } from './api'
import ChatView from './views/ChatView.vue'
import MomentView from './views/MomentView.vue'
import FriendsView from './views/FriendsView.vue'
import TasksView from './views/TasksView.vue'
import OutreachView from './views/OutreachView.vue'
import HandoffView from './views/HandoffView.vue'
import CampaignView from './views/CampaignView.vue'
import KnowledgeView from './views/KnowledgeView.vue'

const NAV = [
  { id: 'chat', label: '消息' },
  { id: 'moment', label: '朋友圈' },
  { id: 'friends', label: '加好友' },
  { id: 'tasks', label: '维护任务' },
  { id: 'outreach', label: '主动激活' },
  { id: 'handoff', label: '人工接管' },
  { id: 'campaign', label: '活动触达' },
  { id: 'knowledge', label: '知识库' },
]

const active = ref((location.hash || '').replace('#', ''))
if (!NAV.some((n) => n.id === active.value)) active.value = 'chat'
const dark = ref(document.documentElement.classList.contains('dark'))

// 全看板共享的响应式中枢。原来原生 JS 里的 pickerSig / convSig / momentSigs
// 是为了「2 秒轮询别把用户正在输入的表单重绘掉」手搓的 diff —— Vue 接管后
// 直接改数据即可，DOM 不会整体重建，这些签名全部不需要了。
const store = reactive({
  me: '',
  autoReplyEnabled: false,
  allowGroups: false,
  conversations: [],
  contacts: [],
  activeConv: '',
  messages: [],
  moments: { moments: [], interactions: [], unread_inter: 0, dry_run: true, max_actions: 0, switches: {} },
  tasks: { counts: {}, tasks: [] },
  friends: { enabled: false, daily_limit: 0, sent_today: 0, counts: {}, tasks: [] },
  outreach: {
    enabled: false, persisted: false, source: '', config_enabled: false,
    sent_today: 0, daily_limit: 0, daily_unlimited: true, candidates: [],
    // 两个可改的行为开关 + 不打扰名单（私人联系人）
    include_new_contacts: false, require_switch: false,
    skip_count: 0, skip_list: [],
  },
  handoff: { ok: false, config: {}, contacts: [], recent: [] },
  campaigns: { ok: false, campaigns: [], contacts: [] },
  knowledge: { items: [], config: {}, modes: {}, capabilities: {}, stats: {} },
})

provide('store', store)
const toast = (msg, type = 'success') => ElMessage({ message: msg, type, duration: 2200 })
provide('toast', toast)
provide('refreshPage', loadPage)

watch(dark, (v) => {
  document.documentElement.classList.toggle('dark', v)
  localStorage.setItem('wb-theme', v ? 'dark' : 'light')
})

let timer = null

// 会话列表单独抽出来：开启托管后要立刻刷新左侧，而不是等下一轮 2s 轮询。
async function loadConversations() {
  const d = await api.conversations()
  store.conversations = d.conversations || []
  store.autoReplyEnabled = !!d.auto_reply_enabled
  store.allowGroups = !!d.allow_groups
  store.me = d.me || ''
}

async function poll() {
  const jobs = [
    loadConversations(),
    api.moments().then((d) => Object.assign(store.moments, d)),
    api.tasks().then((d) => Object.assign(store.tasks, d)),
    api.friends().then((d) => Object.assign(store.friends, d)),
  ]
  // /api/messages 返回的是**裸数组**，不是 {messages: []}
  if (store.activeConv) {
    jobs.push(api.messages(store.activeConv).then((d) => {
      store.messages = Array.isArray(d) ? d : []
    }))
  }
  await Promise.allSettled(jobs)
}

// 接管页 / 活动页 / 知识库页 / 主动激活页不进轮询：表单正在编辑时被覆盖会丢输入，
// 而且 /api/outreach 每次都会算一遍候选（查库），没必要 2 秒跑一次。
async function loadPage(id) {
  try {
    if (id === 'chat') {
      await loadConversations()       // 刚开了托管的会话马上出现在左侧
      store.contacts = (await api.contacts()).contacts || []
    }
    else if (id === 'handoff') Object.assign(store.handoff, await api.handoffConfig())
    else if (id === 'campaign') Object.assign(store.campaigns, await api.campaigns())
    else if (id === 'outreach') Object.assign(store.outreach, await api.outreach())
    else if (id === 'knowledge') Object.assign(store.knowledge, await api.knowledgeList())
  } catch (e) {
    console.warn('[page]', id, e)
  }
}

function nav(id) {
  active.value = id
  // hash 让刷新/分享链接能停在同一个 tab，也方便直接开 /#knowledge 排查
  history.replaceState(null, '', '#' + id)
  loadPage(id)
}

window.addEventListener('hashchange', () => {
  const id = (location.hash || '').replace('#', '')
  if (NAV.some((n) => n.id === id) && id !== active.value) {
    active.value = id
    loadPage(id)
  }
})

function badge(id) {
  if (id === 'tasks') return store.tasks.counts?.open || 0
  if (id === 'moment') return store.moments.unread_inter || 0
  if (id === 'friends') return store.friends.counts?.pending || 0
  return 0
}

onMounted(() => {
  loadPage(active.value)
  poll()
  timer = setInterval(poll, 2000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="wb-app">
    <header class="wb-top">
      <div class="wb-brand"><span class="dot"></span><span>客户维护看板</span></div>
      <nav class="wb-nav">
        <button v-for="n in NAV" :key="n.id" :class="{ on: active === n.id }" @click="nav(n.id)">
          {{ n.label }}
          <span v-if="badge(n.id)" class="wb-nbadge">{{ badge(n.id) }}</span>
        </button>
      </nav>
      <div class="wb-top-right">
        <span class="wb-hint">运行中 · 2s 刷新</span>
        <el-switch v-model="dark" inline-prompt :active-icon="Moon" :inactive-icon="Sunny" />
        <a class="wb-legacy" href="/legacy" title="回到旧版单文件看板">旧版</a>
      </div>
    </header>
    <main class="wb-main">
      <ChatView v-show="active === 'chat'" />
      <MomentView v-show="active === 'moment'" />
      <FriendsView v-show="active === 'friends'" />
      <TasksView v-show="active === 'tasks'" />
      <OutreachView v-show="active === 'outreach'" />
      <HandoffView v-show="active === 'handoff'" />
      <CampaignView v-show="active === 'campaign'" />
      <KnowledgeView v-show="active === 'knowledge'" />
    </main>
  </div>
</template>

<style scoped>
.wb-nbadge {
  display: inline-block; min-width: 16px; padding: 0 4px; margin-left: 5px;
  border-radius: 8px; background: #fa5151; color: #fff;
  font-size: 11px; line-height: 16px; text-align: center;
}
</style>
