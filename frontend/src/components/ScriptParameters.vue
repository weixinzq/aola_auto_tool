<script setup lang="ts">
withDefaults(defineProps<{ modelValue: string; disabled?: boolean; label?: string }>(), { label: '脚本参数' })
defineEmits<{ 'update:modelValue': [value: string] }>()
</script>
<template>
  <textarea class="script-parameters" :value="modelValue" :aria-label="label" :disabled="disabled" rows="3" spellcheck="false" @input="$emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)" />
</template>
<style scoped>
.script-parameters{display:block;box-sizing:border-box;width:100%;margin:14px 0;border:1px solid #dce3ed;border-radius:6px;padding:10px 12px;background:#fff;color:#36465f;font:13px/1.6 Consolas,monospace;resize:vertical}
.script-parameters:focus{outline:2px solid #b8cafa;outline-offset:1px}
</style>
