// 所有后端接口集中在这里。后端是 Flask + 原生 /api，Vue 侧只做同域 fetch，
// 不需要 axios、不需要 baseURL、运行时完全离线。
async function json(url, opts = {}) {
  const r = await fetch(url, opts)
  if (!r.ok) throw new Error('HTTP ' + r.status)
  return r.json()
}

function post(url, body = {}) {
  return json(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

async function del(url) {
  const r = await fetch(url, { method: 'DELETE' })
  if (!r.ok) throw new Error('HTTP ' + r.status)
  return r.json()
}

export const api = {
  // ---- 轮询拉取 ----
  conversations: () => json('/api/conversations'),
  messages: (conv) => json('/api/messages?conv=' + encodeURIComponent(conv)),
  contacts: () => json('/api/contacts'),
  moments: () => json('/api/moments'),
  tasks: () => json('/api/tasks'),
  friends: () => json('/api/friends'),

  // ---- 切页才拉（不进 2s 轮询，避免冲掉正在编辑的表单）----
  handoffConfig: () => json('/api/handoff/config'),
  campaigns: () => json('/api/campaigns'),

  // ---- 消息页 ----
  open: (conv) => post('/api/open', { conv }),
  switch: (conv, enabled) => post('/api/switch', { conv, enabled }),
  send: (conv, text) => post('/api/send', { conv, text }),
  deleteMessage: (id) => post('/api/messages/delete', { id }),

  // ---- 朋友圈 ----
  // 总「模拟/真跑」开关复用会话开关接口，conv 固定为 __moment_dry__
  momentDry: (enabled) => post('/api/switch', { conv: '__moment_dry__', enabled }),
  momentAction: (tid, action, text = '') => post('/api/moment/action', { tid, action, text }),
  momentAiComment: (tid) => post('/api/moment/ai-comment', { tid }),
  momentSwitch: (username, field, enabled, nickname = '') =>
    post('/api/moment/switch', { username, field, enabled, nickname }),
  momentInterRead: () => post('/api/moment/inter-read', {}),

  // ---- 加好友 ----
  friendToggle: (enabled) => post('/api/friends/toggle', { enabled }),
  friendImportText: (text) => post('/api/friends/import', { text }),
  friendImportFile: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    // 不要手动设 Content-Type，浏览器要自己带 multipart boundary
    return json('/api/friends/import', { method: 'POST', body: fd })
  },

  // ---- 人工接管 ----
  handoffSave: (config) => post('/api/handoff/config', config),

  // ---- 活动触达 ----
  campaignCreate: (body) => post('/api/campaigns', body),
  campaignCancel: (id) => post(`/api/campaigns/${id}/cancel`, {}),
  campaignSendNow: (id) => post(`/api/campaigns/${id}/send-now`, {}),
  campaignDelete: (id) => del(`/api/campaigns/${id}`),

  // ---- 知识库（切页才拉，不进 2s 轮询）----
  knowledgeList: () => json('/api/knowledge'),
  knowledgeConfig: (config) => post('/api/knowledge/config', config),
  knowledgeDiagnose: (query) => post('/api/knowledge/diagnose', { query }),
  knowledgeText: (id) => json('/api/knowledge/' + encodeURIComponent(id) + '/text'),
  knowledgeSaveText: (id, text) =>
    post('/api/knowledge/' + encodeURIComponent(id) + '/text', { text }),
  knowledgeToggle: (id, enabled) =>
    post('/api/knowledge/' + encodeURIComponent(id) + '/toggle', { enabled }),
  knowledgeDelete: (id) => del('/api/knowledge/' + encodeURIComponent(id)),
  knowledgeFileUrl: (id) => '/api/knowledge/' + encodeURIComponent(id) + '/file',
  knowledgeUpload: (files) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    // 不要手动设 Content-Type，浏览器要自己带 multipart boundary
    return json('/api/knowledge/upload', { method: 'POST', body: fd })
  },
}
