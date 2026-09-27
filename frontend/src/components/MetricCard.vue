<script setup>
defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], default: 0 },
  hint: { type: String, default: '' },
})
</script>

<template>
  <div class="card stat">
    <small>{{ label }}</small>
    <strong>{{ value }}</strong>
    <span v-if="hint" class="muted" style="font-size:12px">{{ hint }}</span>
  </div>
</template>
