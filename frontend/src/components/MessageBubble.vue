<script setup>
import { computed, ref } from 'vue'
import {
  parseCard, mediaUrl, isImage, isVideo, fmtClock, SENT_BY, extOf,
} from '../utils'
import WbAvatar from './WbAvatar.vue'

const props = defineProps({
  msg: { type: Object, required: true },
  convId: { type: String, default: '' },
  meAvatar: { type: String, default: '' },
  peerAvatar: { type: String, default: '' },
})
const emit = defineEmits(['delete'])

const isMe = computed(() => props.msg.role === 'me')
const showAvatar = computed(() => !String(props.convId).endsWith('@chatroom'))
const avatar = computed(() => (isMe.value ? props.meAvatar : props.peerAvatar))
const by = computed(() => (isMe.value ? SENT_BY[props.msg.sent_by] : null))
const card = computed(() => parseCard(props.msg.card))
const media = computed(() => props.msg.media || '')
const isImg = computed(() => !!media.value && isImage(media.value))
const isVid = computed(() => !!media.value && isVideo(media.value))
const fileExt = computed(() => extOf(media.value).toUpperCase())
const quote = computed(() => card.value?.quote || null)
const finder = computed(() => card.value?.finder || null)
const cardKind = computed(() => card.value?.kind || '')
const imgFailed = ref(false)
</script>

<template>
  <div class="wb-msg" :class="{ me: isMe }">
    <WbAvatar v-if="showAvatar" :url="avatar" :name="isMe ? '我' : '对'" size="sm" />
    <div v-else style="width: 28px; flex-shrink: 0"></div>

    <div class="wb-bubble">
      <div v-if="by" style="margin-bottom: 4px">
        <el-tag size="small" :type="by.type" effect="plain">{{ by.label }}</el-tag>
      </div>

      <!-- 引用回复 -->
      <template v-if="quote">
        <div class="wb-quote">{{ quote.name }}：{{ quote.text }}</div>
        <div>{{ card.title || msg.text }}</div>
      </template>

      <!-- 视频号分享 -->
      <a v-else-if="finder" class="wb-linkcard" :href="finder.video || card.url || '#'" target="_blank" rel="noopener">
        <img v-if="finder.cover" :src="mediaUrl(finder.cover)" style="width: 100%; max-height: 160px; object-fit: cover; border-radius: 8px" alt="" />
        <div class="t" style="margin-top: 8px">{{ finder.desc || card.title || '视频号' }}</div>
        <div class="f">{{ finder.nickname || '视频号' }}</div>
      </a>

      <!-- 链接 / 小程序 / 通用 app 卡片 -->
      <a v-else-if="['link', 'app'].includes(cardKind)" class="wb-linkcard" :href="card.url || '#'" target="_blank" rel="noopener">
        <div style="display: flex; gap: 10px; align-items: flex-start">
          <div style="flex: 1; min-width: 0">
            <div class="t">{{ card.title || msg.text }}</div>
            <div class="d">{{ card.size || card.descr || '' }}</div>
          </div>
          <img v-if="card.thumb" :src="mediaUrl(card.thumb)" style="width: 46px; height: 46px; object-fit: cover; border-radius: 6px" alt="" />
        </div>
        <div class="f">{{ card.source || '链接' }}</div>
      </a>

      <!-- 文件卡片 -->
      <a v-else-if="cardKind === 'file'" class="wb-linkcard" :href="media ? mediaUrl(media) : (card.url || '#')" :download="media ? '' : undefined" target="_blank" rel="noopener">
        <div style="display: flex; gap: 10px; align-items: center">
          <div style="width: 38px; height: 38px; border-radius: 8px; background: var(--wb-accent-soft); color: var(--wb-accent); display: flex; align-items: center; justify-content: center; font-size: 11px">
            {{ card.ext || fileExt || '文件' }}
          </div>
          <div style="flex: 1; min-width: 0">
            <div class="t">{{ card.title || msg.text }}</div>
            <div class="d">{{ card.size || '点击下载' }}</div>
          </div>
        </div>
      </a>

      <!-- 其它卡片：退化成文本 -->
      <div v-else-if="card">{{ card.title || msg.text }}</div>

      <!-- 图片 / 视频 / 文件（无 card） -->
      <template v-else-if="media">
        <img v-if="isImg && !imgFailed" class="media" :src="mediaUrl(media)" alt="图片" @error="imgFailed = true" />
        <video v-else-if="isVid" class="media" :src="mediaUrl(media)" controls></video>
        <a v-else :href="mediaUrl(media)" download style="display: inline-flex; align-items: center; gap: 8px; text-decoration: none">
          <el-tag effect="plain" size="small">{{ fileExt || '文件' }}</el-tag>
          <span>{{ msg.text || '下载文件' }}</span>
        </a>
      </template>

      <div v-else>{{ msg.text }}</div>
    </div>

    <div class="side">
      <span>{{ fmtClock(msg.ts) }}</span>
      <el-button
        v-if="msg.id" class="del" link size="small" type="danger"
        title="只删看板里的这条" @click="emit('delete', msg.id)"
      >×</el-button>
    </div>
  </div>
</template>
