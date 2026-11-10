<script setup>
import { computed, inject, ref } from 'vue'
import { api } from '../api'
import { fmtTime, parseJson, mediaUrl } from '../utils'

const store = inject('store')
const toast = inject('toast')

const activeTid = ref(null)
const commentText = ref('')
const list = computed(() => store.moments.moments || [])
const active = computed(() => list.value.find((m) => m.tid === activeTid.value) || null)
const sw = computed(() => (active.value ? (store.moments.switches || {})[active.value.username] || {} : {}))
const interactions = computed(() => store.moments.interactions || [])

const images = computed(() => {
  const j = parseJson(active.value?.media_json, {})
  return j?.images || []
})
const comments = computed(() => parseJson(active.value?.comments_json, []))

function imgSrc(p) {
  const s = String(p || '')
  return /^https?:/.test(s) ? s : mediaUrl(s)
}

function open(tid) { activeTid.value = tid; commentText.value = '' }
function resultToast(r, okMsg) {
  if (r?.ok === false) toast(r.reason || r.error || '操作未完成', 'warning')
  else toast(okMsg)
}

async function toggleDry(v) {
  try { await api.momentDry(v); toast(v ? '已切到模拟模式' : '已切到真跑模式') }
  catch (e) { toast('切换失败：' + e.message, 'error') }
}

async function toggleSwitch(field, v) {
  if (!active.value) return
  try {
    await api.momentSwitch(active.value.username, field, v, active.value.nickname || '')
  } catch (e) { toast('切换失败：' + e.message, 'error') }
}

async function doAction(action) {
  if (!active.value) return
  if (action === 'comment' && !commentText.value.trim()) { toast('先写点评论内容', 'warning'); return }
  try {
    const r = await api.momentAction(active.value.tid, action, commentText.value.trim())
    resultToast(r, action === 'comment' ? '评论已提交' : '操作已提交')
    if (action === 'comment') commentText.value = ''
  } catch (e) { toast('操作失败：' + e.message, 'error') }
}

async function aiComment() {
  if (!active.value) return
  try {
    const r = await api.momentAiComment(active.value.tid)
    if (r.text) { commentText.value = r.text; toast('已生成建议文案') }
    else toast(r.reason || r.error || '没生成文案', 'warning')
  } catch (e) { toast('生成失败：' + e.message, 'error') }
}

async function readAll() {
  try { await api.momentInterRead(); toast('已全部标为已读') }
  catch (e) { toast('操作失败：' + e.message, 'error') }
}
</script>

<template>
  <div class="wb-view">
    <aside class="wb-side">
      <div class="wb-side-head">
        <span style="font-size: 14px; color: var(--wb-muted)">新动态 {{ list.length }}</span>
        <div style="margin-left: auto; display: flex; align-items: center; gap: 6px">
          <span style="font-size: 13px; color: var(--wb-muted)">模拟</span>
          <el-switch size="small" :model-value="!!store.moments.dry_run" @change="toggleDry" />
        </div>
      </div>
      <div class="wb-list">
        <div v-for="m in list" :key="m.tid" class="wb-row" :class="{ on: activeTid === m.tid }" @click="open(m.tid)">
          <div class="wb-avatar">{{ (m.nickname || '?').slice(0, 1) }}</div>
          <div class="meta">
            <div class="nm">{{ m.nickname || m.username }}</div>
            <div class="sub">{{ m.text || '[图片/视频]' }}</div>
          </div>
          <span class="ts">{{ fmtTime(m.create_time, false) }}</span>
        </div>
        <div v-if="!list.length" class="wb-empty">还没有抓到朋友圈动态</div>
      </div>
    </aside>

    <section class="wb-panel">
      <div class="wb-panel-head">
        <div>
          <div class="title">朋友圈互动</div>
          <div class="sub">点赞 / 评论按客户配置，写操作受模拟开关控制</div>
        </div>
        <div class="spacer"></div>
        <el-badge :value="store.moments.unread_inter || 0" :hidden="!store.moments.unread_inter">
          <el-button size="small" @click="readAll">全部已读</el-button>
        </el-badge>
      </div>

      <div class="wb-scroll">
        <div class="wb-page">
          <template v-if="active">
            <div class="wb-card">
              <div style="display: flex; align-items: center; gap: 10px">
                <div class="wb-avatar">{{ (active.nickname || '?').slice(0, 1) }}</div>
                <div style="flex: 1">
                  <div style="font-size: 15px; font-weight: 500">{{ active.nickname || active.username }}</div>
                  <div style="font-size: 13px; color: var(--wb-muted)">{{ fmtTime(active.create_time) }}</div>
                </div>
                <el-tag size="small" effect="plain">自动点赞 {{ sw.auto_like ? '开' : '关' }}</el-tag>
                <el-tag size="small" effect="plain">自动评论 {{ sw.auto_comment ? '开' : '关' }}</el-tag>
              </div>
              <div style="margin-top: 12px; white-space: pre-wrap; line-height: 1.7">{{ active.text || '[图片/视频]' }}</div>
              <div v-if="images.length" class="wb-moment-media">
                <img v-for="(p, i) in images" :key="i" :src="imgSrc(p)" alt="" />
              </div>
            </div>

            <div class="wb-grid2">
              <div class="wb-card">
                <h3>按客户开关</h3>
                <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 10px">
                  <span style="font-size: 14px; width: 64px">自动点赞</span>
                  <el-switch :model-value="!!sw.auto_like" @change="(v) => toggleSwitch('like', v)" />
                </div>
                <div style="display: flex; align-items: center; gap: 12px">
                  <span style="font-size: 14px; width: 64px">自动评论</span>
                  <el-switch :model-value="!!sw.auto_comment" @change="(v) => toggleSwitch('comment', v)" />
                </div>
              </div>
              <div class="wb-card">
                <h3>手动操作</h3>
                <div style="display: flex; gap: 8px; flex-wrap: wrap">
                  <el-button size="small" @click="doAction(active.followed ? 'unlike' : 'like')">
                    {{ active.followed ? '取消点赞' : '点赞' }}
                  </el-button>
                  <el-button size="small" @click="aiComment">AI 代写评论</el-button>
                  <el-button size="small" type="primary" @click="doAction('comment')">发评论</el-button>
                </div>
                <el-input v-model="commentText" size="small" style="margin-top: 10px" placeholder="评论内容" />
              </div>
            </div>

            <div class="wb-card">
              <h3>已有评论 {{ comments.length }}</h3>
              <div v-for="(c, i) in comments" :key="i" style="font-size: 14px; padding: 5px 0; border-bottom: 1px solid var(--wb-border)">
                <b>{{ c.nickname || c.username }}</b>：{{ c.content }}
              </div>
              <div v-if="!comments.length" class="wb-empty">暂无评论</div>
            </div>
          </template>
          <div v-else class="wb-empty">从左边选一条动态</div>

          <div class="wb-card">
            <h3>互动记录 {{ interactions.length }}</h3>
            <div v-for="(it, i) in interactions" :key="i" style="display: flex; gap: 10px; font-size: 14px; padding: 6px 0; border-bottom: 1px solid var(--wb-border)">
              <el-tag size="small" effect="plain">{{ it.type === 1 ? '赞' : '评论' }}</el-tag>
              <span style="flex: 1; min-width: 0">{{ it.from_nickname || it.from_username }} {{ it.content || '' }}</span>
              <span style="color: var(--wb-muted)">{{ fmtTime(it.create_time) }}</span>
            </div>
            <div v-if="!interactions.length" class="wb-empty">暂无互动</div>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>
