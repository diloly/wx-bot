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

  // ---- 切页才拉（不进 2s 轮询，避免冲掉正在编辑的表单 / 反复算候选）----
  handoffConfig: () => json('/api/handoff/config'),
  campaigns: () => json('/api/campaigns'),
  outreach: () => json('/api/outreach'),

  // ---- 主动激活（后台常驻触达）----
  outreachToggle: (enabled) => post('/api/outreach/toggle', { enabled }),
  // 破冰 / 只碰托管：落 app_setting，主进程下一轮巡检就认，不用重启
  outreachConfig: (body) => post('/api/outreach/config', body),
  // 不打扰名单（私人联系人）：列表 + 可选联系人，批量加/删
  outreachSkip: () => json('/api/outreach/skip'),
  outreachSkipSet: (conv_ids, skip = true, note = '') =>
    post('/api/outreach/skip', { conv_ids, skip, note }),

  // ---- 消息页 ----
  open: (conv) => post('/api/open', { conv }),
  switch: (conv, enabled) => post('/api/switch', { conv, enabled }),
  // 批量托管：一次请求单事务写完，避免几十个并发请求撞 SQLite 写锁
  switchBatch: (convs, enabled) => post('/api/switch/batch', { convs, enabled }),
  send: (conv, text) => post('/api/send', { conv, text }),
  // 不用通用 post()：微信没开/素材失效时后端返回 500 + {ok:false,error:人话}，
  // 通用封装会直接抛 "HTTP 500" 把可读提示吞掉。
  sendImage: async (conv, assetId) => {
    const r = await fetch('/api/send/image', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conv, asset_id: assetId }),
    })
    return r.json().catch(() => ({ ok: false, error: 'HTTP ' + r.status }))
  },
  deleteMessage: (id) => post('/api/messages/delete', { id }),

  // ---- 图片素材库（本机私有目录，不入库）----
  mediaList: () => json('/api/media'),
  mediaDelete: (id) => del('/api/media/' + encodeURIComponent(id)),
  mediaUpload: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    // 不要手动设 Content-Type，浏览器要自己带 multipart boundary
    return json('/api/media', { method: 'POST', body: fd })
  },

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

  // ---- 接管 / 活动页共用的联系人选择器 ----
  // 不用通用 post()：微信没开时后端返回 500 + {ok:false,error:人话}，
  // 通用封装会直接抛 "HTTP 500" 把可读提示吞掉。这里无论如何都解析响应体。
  avatarsSync: async () => {
    const r = await fetch('/api/avatars/sync', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })
    return r.json().catch(() => ({ ok: false, error: 'HTTP ' + r.status }))
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
