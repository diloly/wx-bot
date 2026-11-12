<script setup>
import { ref } from 'vue'

// 「表情」两种口径：
// 1) Unicode emoji —— 作为普通文本发出，微信会渲染成彩色 emoji，走现有文本
//    发送链路（含粘贴回读 + 三态确认），最稳；
// 2) 微信表情代码 —— [微笑]/[握手] 这类文本，微信客户端**可能**转成黄脸表情，
//    也可能原样以文字发出。这里只负责把代码插进输入框，不模拟点表情面板
//    （Qt 自绘控件读不到，硬点容易误发）。
defineProps({ disabled: { type: Boolean, default: false } })
const emit = defineEmits(['pick'])

const tab = ref('emoji')
const EMOJIS = [
  '😀', '😄', '😊', '🙂', '😉', '😍', '🥰', '😎',
  '🤔', '😐', '😅', '😂', '😭', '😡', '😱', '🥺',
  '👍', '👏', '🙏', '🤝', '💪', '👌', '🙌', '👋',
  '❤️', '🌹', '🎉', '🎊', '✨', '🔥', '💡', '✅',
  '🎯', '🚀', '🌟', '💯', '📌', '📅', '☀️', '🍀',
]
const WX_CODES = [
  '[微笑]', '[憨笑]', '[愉快]', '[偷笑]', '[呲牙]', '[捂脸]', '[奸笑]', '[机智]',
  '[害羞]', '[闭嘴]', '[睡]', '[大哭]', '[尴尬]', '[发怒]', '[调皮]', '[惊讶]',
  '[难过]', '[抓狂]', '[疑问]', '[晕]', '[衰]', '[敲打]', '[再见]', '[擦汗]',
  '[鼓掌]', '[坏笑]', '[委屈]', '[快哭了]', '[亲亲]', '[可怜]', '[笑脸]', '[破涕为笑]',
  '[嘿哈]', '[吃瓜]', '[加油]', '[旺柴]', '[好的]', '[哇]', '[握手]', '[OK]',
  '[玫瑰]', '[凋谢]', '[爱心]', '[心碎]', '[拥抱]', '[强]', '[拳头]', '[666]',
]
</script>

<template>
  <el-popover placement="top-start" :width="310" trigger="click" :disabled="disabled">
    <template #reference>
      <el-button size="small" :disabled="disabled" title="插入表情">表情</el-button>
    </template>
    <div class="emoji-box">
      <div class="emoji-tabs">
        <span :class="{ on: tab === 'emoji' }" @click="tab = 'emoji'">Emoji</span>
        <span :class="{ on: tab === 'wx' }" @click="tab = 'wx'">微信表情代码</span>
      </div>
      <div v-if="tab === 'emoji'" class="emoji-grid">
        <button v-for="e in EMOJIS" :key="e" type="button" @click="emit('pick', e)">{{ e }}</button>
      </div>
      <div v-else class="emoji-grid wx">
        <button v-for="e in WX_CODES" :key="e" type="button" :title="e" @click="emit('pick', e)">{{ e }}</button>
      </div>
      <div class="emoji-hint">
        点一下插到光标处。微信表情代码可能被微信转成黄脸表情，也可能原样以文字发出。
      </div>
    </div>
  </el-popover>
</template>

<style scoped>
.emoji-box { font-size: 13px; }
.emoji-tabs { display: flex; gap: 12px; margin-bottom: 8px; border-bottom: 1px solid var(--wb-border); }
.emoji-tabs span { cursor: pointer; padding: 4px 2px 7px; color: var(--wb-muted); }
.emoji-tabs span.on { color: var(--wb-accent); font-weight: 600; border-bottom: 2px solid var(--wb-accent); }
.emoji-grid {
  display: grid; grid-template-columns: repeat(8, 1fr); gap: 2px;
  max-height: 190px; overflow-y: auto;
}
.emoji-grid.wx { grid-template-columns: repeat(3, 1fr); }
.emoji-grid button {
  border: 0; background: transparent; cursor: pointer; font-size: 19px;
  padding: 4px 0; border-radius: 5px; color: var(--wb-text);
}
.emoji-grid.wx button { font-size: 12px; white-space: nowrap; }
.emoji-grid button:hover { background: var(--wb-accent-soft); }
.emoji-hint { margin-top: 8px; font-size: 11px; color: var(--wb-muted); line-height: 1.5; }
</style>
