<script setup>
import { ref, watch } from 'vue'
import { initial } from '../utils'

const props = defineProps({
  url: { type: String, default: '' },
  name: { type: String, default: '' },
  size: { type: String, default: '' },
})
const failed = ref(false)
watch(() => props.url, () => { failed.value = false })
</script>

<template>
  <div class="wb-avatar" :class="size">
    <span>{{ initial(name) }}</span>
    <img v-if="url && !failed" :src="url" :alt="name" @error="failed = true" />
  </div>
</template>
