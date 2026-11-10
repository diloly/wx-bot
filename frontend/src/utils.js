export function fmtTime(ts, withDate = true) {
  if (!ts) return ''
  const d = new Date(Number(ts) * 1000)
  if (Number.isNaN(d.getTime())) return ''
  const p = (n) => String(n).padStart(2, '0')
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`
  return withDate ? `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}` : hm
}

export function fmtClock(ts) {
  return fmtTime(ts, false)
}

export function parseCard(raw) {
  if (!raw) return null
  if (typeof raw === 'object') return raw
  try { return JSON.parse(raw) } catch { return null }
}

export function parseJson(raw, fallback = []) {
  if (!raw) return fallback
  if (Array.isArray(raw) || typeof raw === 'object') return raw
  try { return JSON.parse(raw) } catch { return fallback }
}

export function initial(name) {
  return (String(name || '?').trim() || '?').slice(0, 1)
}

export function extOf(path) {
  const s = String(path || '')
  const i = s.lastIndexOf('.')
  return i < 0 ? '' : s.slice(i + 1).toLowerCase()
}

export function isImage(path) {
  return ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'].includes(extOf(path))
}

export function isVideo(path) {
  return ['mp4', 'mov', 'webm'].includes(extOf(path))
}

export function mediaUrl(rel) {
  return '/media/' + String(rel || '').replace(/^\/+/, '')
}

export function avatarUrl(conv, ts) {
  return `/api/avatar/${encodeURIComponent(conv || '')}?v=${ts || 0}`
}

export const SENT_BY = {
  ai: { label: 'AI', type: 'primary' },
  manual: { label: '人工', type: 'info' },
  handoff: { label: '接管通知', type: 'warning' },
  campaign: { label: '活动', type: 'warning' },
}

export const TASK_STATUS = {
  pending: '待处理', generating: '生成中', generated: '已生成',
  sending: '发送中', sent: '已发送', failed: '失败',
  expired: '已过期', handoff: '已转人工', passed: '已通过',
  rejected: '已拒绝', already: '已是好友',
}

export const CAMPAIGN_STATUS = {
  scheduled: '待发送', running: '发送中', sent: '已完成',
  partial: '部分完成', expired: '已过期', canceled: '已取消',
}

export function toDatetimeLocal(ts) {
  if (!ts) return ''
  const d = new Date(Number(ts) * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`
}
