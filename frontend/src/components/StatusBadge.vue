<script setup>
import { computed } from 'vue'
import { statusTone } from '@/utils/format'

const props = defineProps({ status: { type: String, default: '' } })
const tone = computed(() => statusTone(props.status))
</script>

<template>
  <span class="pill" :class="tone">
    <span class="dot" />
    {{ status || '—' }}
  </span>
</template>
