<script setup lang="ts">

defineProps<{ page: number; pages: number; total: number }>()
defineEmits<{ 'update:page': [page: number] }>()
</script>

<template>
  <nav v-if="pages > 1" class="account-pagination" aria-label="账号分页">
    <div><span>第 {{ page }} / {{ pages }} 页 · 共 {{ total }} 个账号</span></div>
    <div class="button-row"><label class="page-picker">跳至 <select :value="page" aria-label="选择账号页码" @change="$emit('update:page', Number(($event.target as HTMLSelectElement).value))"><option v-for="number in pages" :key="number" :value="number">第 {{ number }} 页</option></select></label><button type="button" class="button secondary small" :disabled="page <= 1" @click="$emit('update:page', page - 1)">上一页</button><button type="button" class="button secondary small" :disabled="page >= pages" @click="$emit('update:page', page + 1)">下一页</button></div>
  </nav>
</template>

<style scoped>
.account-pagination { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; padding: 12px 24px; border-top: 1px solid #edf0f5; color: #667385; font-size: 11px; }
.page-picker { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.page-picker select { width: auto; min-width: 92px; padding: 8px 12px; border: 1px solid #dce3ed; border-radius: 8px; background: #fff; color: #405572; font: inherit; }
</style>
