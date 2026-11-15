export type ParameterDraft = { name: string; type: 'text' | 'number' | 'boolean'; value: string }

export function scriptParams(rows: ParameterDraft[]): Record<string, string | number | boolean> {
  const names = new Set<string>()
  return Object.fromEntries(rows.map((row, index) => {
    const name = row.name.trim()
    if (!name) throw new Error(`第 ${index + 1} 个参数需要填写名称`)
    if (names.has(name)) throw new Error(`参数名 ${name} 重复，请合并或删除`)
    names.add(name)
    if (row.type === 'number') {
      const value = Number(row.value)
      if (!row.value.trim() || !Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value))) {
        throw new Error(`参数 ${name} 需要填写有效数字`)
      }
      return [name, value]
    }
    if (row.type === 'boolean') return [name, row.value === 'true']
    return [name, row.value]
  }))
}
