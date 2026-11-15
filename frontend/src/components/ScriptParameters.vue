<script setup lang="ts">
import type { ParameterDraft } from '../scriptParams'

const props = withDefaults(defineProps<{ modelValue: ParameterDraft[]; disabled?: boolean; label?: string }>(), { label: '脚本参数' })
const emit = defineEmits<{ 'update:modelValue': [value: ParameterDraft[]] }>()
function update(index: number, patch: Partial<ParameterDraft>) {
  emit('update:modelValue', props.modelValue.map((row, i) => i === index ? { ...row, ...patch } : row))
}
</script>

<template>
  <div class="script-parameters" :aria-label="label">
    <div class="parameter-heading"><strong>{{ label }}（可选）</strong><button type="button" class="text-button" :disabled="disabled" @click="emit('update:modelValue', [...modelValue, { name: '', type: 'text', value: '' }])">＋ 添加参数</button></div>
    <p class="help">按脚本说明填写参数。ID 选择数字，keyword 选择文本；不需要参数时留空。</p>
    <div v-for="(row, index) in modelValue" :key="index" class="parameter-row">
      <label class="field">参数名<input :value="row.name" :aria-label="`${label}第${index + 1}个参数名`" :disabled="disabled" placeholder="例如 item_id" @input="update(index, { name: ($event.target as HTMLInputElement).value })" /></label>
      <label class="field">类型<select :value="row.type" :aria-label="`${label}第${index + 1}个类型`" :disabled="disabled" @change="update(index, { type: ($event.target as HTMLSelectElement).value as ParameterDraft['type'], value: ($event.target as HTMLSelectElement).value === 'boolean' ? 'false' : row.value })"><option value="text">文本</option><option value="number">数字</option><option value="boolean">开关</option></select></label>
      <label class="field">值<select v-if="row.type === 'boolean'" :value="row.value" :aria-label="`${label}第${index + 1}个值`" :disabled="disabled" @change="update(index, { value: ($event.target as HTMLSelectElement).value })"><option value="false">关闭</option><option value="true">开启</option></select><input v-else :type="row.type === 'number' ? 'number' : 'text'" step="any" :value="row.value" :aria-label="`${label}第${index + 1}个值`" :disabled="disabled" @input="update(index, { value: ($event.target as HTMLInputElement).value })" /></label>
      <button type="button" class="text-button danger-text" :aria-label="`删除${label}第${index + 1}个参数`" :disabled="disabled" @click="emit('update:modelValue', modelValue.filter((_, i) => i !== index))">删除</button>
    </div>
  </div>
</template>

<style scoped>
.script-parameters{margin:16px 0;padding:14px 16px;border:1px solid #e2e8f2;border-radius:8px;background:#f8faff}.parameter-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:12px;color:#53637c}.script-parameters .help{margin:8px 0 0}.parameter-row{display:grid;grid-template-columns:minmax(100px,1fr) 90px minmax(100px,1fr) auto;gap:12px;align-items:end}.parameter-row .field{margin:12px 0 0}.parameter-row>button{margin-bottom:10px}@media(max-width:600px){.parameter-row{grid-template-columns:minmax(0,1fr) 90px}.parameter-row>button{justify-self:end}}
</style>
