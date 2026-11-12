<script setup>
import { computed, inject, nextTick, reactive, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { api } from '../api'
import { fmtTime, CAMPAIGN_STATUS } from '../utils'
import ContactPicker from '../components/ContactPicker.vue'
import EmojiPicker from '../components/EmojiPicker.vue'

const store = inject('store')
const toast = inject('toast')
const refreshPage = inject('refreshPage')

const form = reactive({ name: '', content: '', send_at: '', grace_minutes: 60, image: '' })
const selected = ref([])
const creating = ref(false)
const contacts = computed(() => store.campaigns.contacts || [])
const campaigns = computed(() => store.campaigns.campaigns || [])
// 素材库列表随 /api/campaigns 一起返回，id -> 预览 URL 的反查就在这里做
const assets = computed(() => store.campaigns.media || [])
const pickedAsset = computed(() => assets.value.find((a) => a.id === form.image) || null)

function assetOf(id) { return assets.value.find((a) => a.id === id) || null }
function mediaUrl(id) { const it = assetOf(id); return it ? it.url : '' }
function assetName(id) { const it = assetOf(id); return it ? it.name : '素材已删除' }

// 表情 / 微信表情代码只是插进正文的字符，发送时走原有文本链路
const contentInput = ref(null)
function insertEmoji(s) {
  const ta = contentInput.value?.$el?.querySelector?.('textarea')
  const cur = form.content || ''
  const pos = (ta && ta.selectionStart != null) ? ta.selectionStart : cur.length
  form.content = cur.slice(0, pos) + s + cur.slice(pos)
  nextTick(() => {
    if (!ta) return
    ta.focus()
    try { ta.setSelectionRange(pos + s.length, pos + s.length) } catch (_) { /* 忽略 */ }
  })
}

const assetOpen = ref(false)
const assetBusy = ref(false)
const assetFile = ref(null)
function openAssetFile() { assetFile.value?.click?.() }
function pickAsset(it) { form.image = it.id; assetOpen.value = false }
function clearImage() { form.image = ''; assetOpen.value = false }
async function uploadAsset(e) {
  const f = e.target.files?.[0]
  e.target.value = ''
  if (!f || assetBusy.value) return
  assetBusy.value = true
  try {
    const r = await api.mediaUpload(f)
    if (r.ok === false) throw new Error(r.error || '上传失败')
    // 局部补进素材列表，不用整页刷新（会冲掉刚写好的正文和已选对象）
    const list = Array.isArray(store.campaigns.media) ? store.campaigns.media : []
    store.campaigns.media = [r.item, ...list.filter((a) => a.id !== r.item.id)]
    form.image = r.item.id
    toast('图片已上传并设为配图')
  } catch (err) { toast('上传失败：' + err.message, 'error') }
  finally { assetBusy.value = false }
}

// 详情弹窗：数据本来就在 /api/campaigns 里，不需要新接口。
// 用 id 从 store 里实时取，操作（立即发送/取消）刷新后弹窗内容会跟着更新。
const detailId = ref(null)
const detailVisible = ref(false)
const detail = computed(() => campaigns.value.find((c) => c.id === detailId.value) || null)

const STATUS_NOTE = {
  scheduled: '尚未到计划时间，等待主进程巡检触发。',
  running: '正在逐个发送，结果陆续回写。',
  sent: '全部对象发送成功。',
  partial: '部分对象发送失败或未送达，请看下面的逐条结果。',
  expired: '超过计划时间 + 宽限期仍未发送，已作废（防止隔夜群发）。',
  canceled: '已人工取消，不会发送。',
}

function statusType(s) {
  return { sent: 'success', running: 'warning', scheduled: 'primary', partial: 'warning' }[s] || 'info'
}

// 列表摘要：结果表里可能少于对象数（还在发 / 没投出去），「未发送」要单独算。
function cstat(c) {
  const rs = c.result || []
  const total = (c.targets || []).length
  const ok = rs.filter((r) => r.ok).length
  const fail = rs.filter((r) => !r.ok).length
  return { total, ok, fail, none: Math.max(0, total - ok - fail) }
}

// 把「发送对象」和「逐条结果」按 conv_id 合并成一张表；结果里有但名单里
// 没有的异常记录也列出来，绝不把失败藏掉。
const detailRows = computed(() => {
  const c = detail.value
  if (!c) return []
  const rs = c.result || []
  const targets = c.targets || []
  const byId = new Map(rs.map((r) => [r.conv_id || r.name, r]))
  const rows = targets.map((t) => {
    const r = byId.get(t.conv_id)
    return {
      key: t.conv_id || t.name,
      name: t.name || t.conv_id,
      kind: t.kind === 'group' ? '群聊' : '好友',
      state: r ? (r.ok ? 'ok' : 'fail') : 'none',
      reason: (r && r.reason) || '',
      ts: (r && r.ts) || 0,
    }
  })
  const known = new Set(targets.map((t) => t.conv_id))
  for (const r of rs) {
    const key = r.conv_id || r.name
    if (!known.has(r.conv_id) && key) {
      rows.push({ key, name: r.name || key, kind: '—',
                  state: r.ok ? 'ok' : 'fail', reason: r.reason || '', ts: r.ts || 0 })
    }
  }
  return rows
})

const detailStats = computed(() => {
  const rows = detailRows.value
  return {
    total: rows.length,
    ok: rows.filter((r) => r.state === 'ok').length,
    fail: rows.filter((r) => r.state === 'fail').length,
    none: rows.filter((r) => r.state === 'none').length,
  }
})

function openDetail(c) {
  detailId.value = c.id
  detailVisible.value = true
}

async function copyContent() {
  const text = (detail.value && detail.value.content) || ''
  try {
    await navigator.clipboard.writeText(text)
    toast('正文已复制')
  } catch (e) {
    toast('浏览器不允许自动复制，请手动选中正文：' + e.message, 'warning')
  }
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
    Object.assign(form, { name: '', content: '', send_at: '', grace_minutes: 60, image: '' })
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
    if (act === 'del' && detailId.value === c.id) {
      detailVisible.value = false
      detailId.value = null
    }
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
            <div style="width: 100%">
              <el-input ref="contentInput" v-model="form.content" type="textarea" :rows="4"
                        maxlength="2000" show-word-limit placeholder="写死要发的文案，≤2000 字" />
              <div class="cd-tools"><EmojiPicker @pick="insertEmoji" /></div>
            </div>
          </el-form-item>
          <el-form-item label="配图">
            <div style="width: 100%">
              <div v-if="pickedAsset" class="cd-picked">
                <img :src="pickedAsset.url" alt="配图预览"
                     @error="(e) => { e.target.style.display = 'none' }" />
                <span class="wb-hint">{{ pickedAsset.name }}</span>
                <el-button link type="danger" size="small" @click="clearImage">移除</el-button>
              </div>
              <div class="cd-tools">
                <el-button size="small" @click="assetOpen = true">
                  {{ form.image ? '换一张' : '从素材库选图' }}
                </el-button>
                <el-button size="small" :loading="assetBusy" @click="openAssetFile">上传新图</el-button>
                <input ref="assetFile" type="file" accept="image/*" style="display: none"
                       @change="uploadAsset" />
              </div>
              <div class="wb-hint" style="margin-top: 6px; line-height: 1.6">
                配图和正文一起进输入框、只按一次发送，微信会自动拆成「文字 + 图片」
                两个紧挨着的气泡，中间不再有等待。正文不能为空；图片由消息回执上屏，
                不写假消息。素材只存在本机，不进仓库。
              </div>
            </div>
          </el-form-item>
        </el-form>
      </div>
      <div class="wb-card">
        <h3>发送对象</h3>
        <ContactPicker v-model="selected" :contacts="contacts"
                       @contacts-updated="(v) => store.campaigns.contacts = v" />
      </div>
    </div>

    <div style="margin-bottom: 16px">
      <el-button type="primary" :loading="creating" @click="create">创建活动</el-button>
      <span class="wb-hint" style="margin-left: 12px">看板只写配置，真发送由 auto_reply 主进程执行</span>
    </div>

    <div class="wb-card">
      <h3>活动列表 {{ campaigns.length }}</h3>
      <div v-for="c in campaigns" :key="c.id" class="cd-row">
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
          <b style="font-size: 15px">{{ c.name || '未命名活动' }}</b>
          <el-tag size="small" effect="plain" :type="statusType(c.status)">{{ CAMPAIGN_STATUS[c.status] || c.status }}</el-tag>
          <el-tag v-if="c.image" size="small" effect="plain" type="info">含配图</el-tag>
          <span class="wb-hint">计划 {{ fmtTime(c.send_at) }} · {{ cstat(c).total }} 个对象</span>
          <div style="margin-left: auto; display: flex; gap: 6px">
            <el-button size="small" type="primary" plain @click="openDetail(c)">详情</el-button>
            <el-button v-if="c.status === 'scheduled'" size="small" @click="action(c, 'now')">立即发送</el-button>
            <el-button v-if="c.status === 'scheduled'" size="small" @click="action(c, 'cancel')">取消</el-button>
            <el-button v-if="c.status !== 'scheduled'" size="small" type="danger" plain @click="action(c, 'del')">删除</el-button>
          </div>
        </div>
        <div class="cd-content-clamp">{{ c.content }}</div>
        <div class="cd-summary">
          成功 <b class="ok">{{ cstat(c).ok }}</b> ·
          失败 <b :class="cstat(c).fail ? 'bad' : ''">{{ cstat(c).fail }}</b> ·
          未发送 {{ cstat(c).none }}
          <span class="wb-hint">（点「详情」看正文全文与逐条结果）</span>
        </div>
      </div>
      <div v-if="!campaigns.length" class="wb-empty">还没有活动</div>
    </div>

    <el-dialog v-model="detailVisible" width="780px" top="6vh"
               :title="'活动详情 · ' + ((detail && detail.name) || '未命名活动')">
      <template v-if="detail">
        <div class="cd-meta">
          <div><span>当前状态</span>
            <el-tag size="small" effect="plain" :type="statusType(detail.status)">
              {{ CAMPAIGN_STATUS[detail.status] || detail.status }}
            </el-tag>
          </div>
          <div><span>活动编号</span><b>#{{ detail.id }}</b></div>
          <div><span>计划发送</span><b>{{ fmtTime(detail.send_at) }}</b></div>
          <div><span>创建时间</span><b>{{ fmtTime(detail.created) || '—' }}</b></div>
          <div><span>最后更新</span><b>{{ fmtTime(detail.updated) || '—' }}</b></div>
          <div><span>过点宽限</span><b>{{ detail.grace_minutes || 0 }} 分钟</b></div>
          <div class="cd-note">{{ STATUS_NOTE[detail.status] || '' }}</div>
        </div>

        <div class="cd-section">
          发送正文
          <el-button link type="primary" size="small" @click="copyContent">复制正文</el-button>
        </div>
        <pre class="cd-content">{{ detail.content }}</pre>

        <div class="cd-section">活动配图</div>
        <div v-if="detail.image" class="cd-image">
          <img v-if="mediaUrl(detail.image)" :src="mediaUrl(detail.image)" alt="活动配图" />
          <div v-else class="wb-hint">
            配图素材已删除。执行时会只发正文，并在逐条结果里注明「正文已发出，但配图未发」。
          </div>
          <div class="wb-hint">{{ assetName(detail.image) }}</div>
        </div>
        <div v-else class="wb-hint">本次活动没有配图。</div>

        <div class="cd-section">
          发送对象与逐条结果
          <span class="wb-hint">
            共 {{ detailStats.total }} · 成功 {{ detailStats.ok }} /
            失败 {{ detailStats.fail }} / 未发送 {{ detailStats.none }}
          </span>
        </div>
        <el-table :data="detailRows" size="small" max-height="300" empty-text="没有发送对象">
          <el-table-column prop="name" label="对象" min-width="140" show-overflow-tooltip />
          <el-table-column prop="kind" label="类型" width="72" />
          <el-table-column label="结果" width="90">
            <template #default="{ row }">
              <el-tag size="small" effect="plain"
                      :type="row.state === 'ok' ? 'success' : (row.state === 'fail' ? 'danger' : 'info')">
                {{ row.state === 'ok' ? '成功' : (row.state === 'fail' ? '失败' : '未发送') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="reason" label="说明" min-width="170" show-overflow-tooltip />
          <el-table-column label="时间" width="110">
            <template #default="{ row }">{{ row.ts ? fmtTime(row.ts) : '—' }}</template>
          </el-table-column>
        </el-table>
      </template>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="assetOpen" width="620px" top="8vh" title="选择活动配图">
      <div class="wb-hint" style="margin-bottom: 10px">
        点一张设为本次活动的配图；与正文一次发送，微信会自动拆成两个气泡。素材只存在本机。
      </div>
      <div v-if="assets.length" class="cd-assets">
        <div v-for="a in assets" :key="a.id" class="cd-asset"
             :class="{ on: form.image === a.id }" @click="pickAsset(a)">
          <img :src="a.url" :alt="a.name"
               @error="(e) => { e.target.style.display = 'none' }" />
          <span :title="a.name">{{ a.name }}</span>
        </div>
      </div>
      <div v-else class="wb-empty">素材库还是空的，先上传一张图片</div>
      <template #footer>
        <el-button :loading="assetBusy" @click="openAssetFile">上传新图</el-button>
        <el-button @click="clearImage">不使用配图</el-button>
        <el-button type="primary" @click="assetOpen = false">完成</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.cd-tools { display: flex; align-items: center; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
.cd-picked { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.cd-picked img { width: 46px; height: 46px; object-fit: cover; border-radius: 6px; border: 1px solid var(--wb-border); }
.cd-image img {
  max-width: 260px; max-height: 180px; border-radius: 8px;
  border: 1px solid var(--wb-border); display: block; margin-bottom: 6px;
}
.cd-assets { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; max-height: 380px; overflow-y: auto; }
.cd-asset {
  border: 2px solid transparent; border-radius: 8px; padding: 5px; cursor: pointer;
  background: var(--wb-surface-2); overflow: hidden;
}
.cd-asset.on { border-color: var(--wb-accent); }
.cd-asset img { width: 100%; height: 84px; object-fit: cover; border-radius: 5px; display: block; }
.cd-asset span { display: block; font-size: 12px; color: var(--wb-muted); margin-top: 4px;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cd-row { padding: 12px 0; border-bottom: 1px solid var(--wb-border); }
.cd-content-clamp {
  font-size: 14px; color: var(--wb-muted); margin-top: 6px; white-space: pre-wrap;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.cd-summary { font-size: 13px; color: var(--wb-muted); margin-top: 6px; }
.cd-summary b { font-weight: 500; }
.cd-summary b.ok { color: var(--wb-accent); }
.cd-summary b.bad { color: #f56c6c; }
.cd-meta {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px 16px;
  background: var(--wb-surface-2); border-radius: 10px; padding: 12px 14px; margin-bottom: 14px;
}
.cd-meta > div { display: flex; align-items: center; gap: 8px; font-size: 14px; }
.cd-meta span { color: var(--wb-muted); min-width: 68px; }
.cd-meta b { font-weight: 500; }
.cd-meta .cd-note {
  grid-column: 1 / -1; color: var(--wb-muted); font-size: 13px; line-height: 1.6;
  border-top: 1px solid var(--wb-border); padding-top: 8px;
}
.cd-section {
  display: flex; align-items: center; gap: 12px;
  font-size: 15px; font-weight: 500; margin: 14px 0 8px;
}
.cd-content {
  font-family: inherit; font-size: 14px; line-height: 1.75; color: var(--wb-text);
  white-space: pre-wrap; word-break: break-word; margin: 0;
  background: var(--wb-surface-2); border: 1px solid var(--wb-border);
  border-radius: 8px; padding: 12px 14px; max-height: 200px; overflow-y: auto;
}
@media (max-width: 720px) { .cd-meta { grid-template-columns: 1fr; } }
</style>
