export function scriptParams(text: string): Record<string, unknown> {
  if (!text.trim()) return {}
  let value: unknown
  try {
    value = JSON.parse(text, (_key, item: unknown) => {
      if (typeof item === 'number' && (!Number.isFinite(item) || (Number.isInteger(item) && !Number.isSafeInteger(item)))) {
        throw new Error('参数包含无效或超出安全范围的数字')
      }
      return item
    })
  } catch (error) {
    if (error instanceof SyntaxError) throw new Error('参数 JSON 格式错误')
    throw error
  }
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error('参数必须为 JSON 对象')
  return value as Record<string, unknown>
}
