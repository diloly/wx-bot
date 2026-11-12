<script setup>
import { computed, inject, onMounted, ref } from 'vue'
import { api } from '../api'
import { fmtTime } from '../utils'
import WbAvatar from '../components/WbAvatar.vue'
import ContactPicker from '../components/ContactPicker.vue'

const store = inject('store')
const toast = inject('toast')

// 数据只在切到本页时拉一次：/api/outreach 会算一遍候选（要查库），
// 放进 2 秒轮询会白白压数据库；点开关后主动 reload 一次即可。
const o = computed(() => store.outreach || {})

const picked = ref([])          // 待加入「不打扰」名单的人
const skipContacts = ref([])    // 可选联系人（只给好友，群不做主动触达）
const busy = ref(false)

function mins(sec) {
  const n = Number(sec || 0)
  if (!n) return '0'
  return n % 60 === 0 ? String(n / 60) : (Math.round(n / 60 * 10) / 10).toString()
}

function sinceDays(ts) {
  if (!ts) return '—'
  return Math.max(0, Math.floor((Date.now() / 1000 - Number(ts)) / 86400)) + ' 天'
}

const metrics = computed(() => {
  const d = o.value
  return [
    { k: '今日已发 / 上限', v: `${d.sent_today || 0} / ${d.daily_unlimited ? '不限' : (d.daily_limit || 0)} 条` },
    { k: '服务启动静默', v: `${mins(d.boot_grace_seconds)} 分钟` },
    { k: '可发送时段', v: `${d.work_start_hour || 0}:00–${d.work_end_hour || 0}:00` },
    { k: '两条间隔', v: `${mins(d.interval_min_seconds)}~${mins(d.interval_max_seconds)} 分钟` },
    { k: '沉默阈值', v: `≥ ${d.silent_days || 0} 天` },
    { k: '同一人冷却', v: `${d.min_gap_days || 0} 天` },
    { k: '单次上限', v: `${d.per_run_limit || 0} 条` },
    { k: '每人累计上限', v: `${d.max_per_contact || 0} 次` },
    { k: '不打扰名单', v: `${d.skip_count || 0} 人` },
  ]
})

async function reload() {
  try { Object.assign(store.outreach, await api.outreach()) }
  catch (e) { console.warn('[outreach] reload', e) }
}

async function toggle(v) {
  try {
    await api.outreachToggle(v)
    toast(v ? '已开启主动激活（下一轮巡检起生效）'
            : '已暂停主动激活；重启服务也不会自动打开')
    await reload()                 // 回读确认开关确实落库
  } catch (e) {
    toast('切换失败：' + e.message, 'error')
    await reload()                 // 失败时把开关显示拉回真实状态
  }
}

// 破冰 / 只碰托管：落 app_setting，主进程下一轮巡检就读到，不用重启
async function toggleFlag(key, v) {
  const label = key === 'include_new_contacts' ? '没聊过也破冰' : '只碰已开托管的客户'
  try {
    await api.outreachConfig({ [key]: v })
    toast(`${label} 已${v ? '打开' : '关闭'}（下一轮巡检生效，不用重启）`)
    await reload()
  } catch (e) {
    toast('保存失败：' + e.message, 'error')
    await reload()
  }
}

async function loadSkip() {
  try {
    const d = await api.outreachSkip()
    skipContacts.value = d.contacts || []
    if (Array.isArray(d.items)) {
      store.outreach.skip_list = d.items
      store.outreach.skip_count = d.items.length
    }
  } catch (e) { console.warn('[outreach] skip', e) }
}

// 选择器里点「同步头像」后会回传新的联系人列表，就地替换，不整页刷新
function onContactsUpdated(list) {
  if (Array.isArray(list)) skipContacts.value = list
}

async function addSkip() {
  if (!picked.value.length) return
  const n = picked.value.length
  busy.value = true
  try {
    const r = await api.outreachSkipSet(picked.value, true)
    toast(`已加入不打扰名单 ${n} 人，现共 ${r.count} 人`)
    picked.value = []
    await loadSkip()
    await reload()
  } catch (e) {
    toast('加入失败：' + e.message, 'error')
  } finally {
    busy.value = false
  }
}

async function removeSkip(row) {
  try {
    await api.outreachSkipSet([row.conv_id], false)
    toast(`已移出：${row.name || row.conv_id}`)
    await loadSkip()
    await reload()
  } catch (e) {
    toast('移除失败：' + e.message, 'error')
  }
}

// 候选表里直接「不打扰」：比先去名单里翻人快得多
async function skipCandidate(row) {
  try {
    await api.outreachSkipSet([row.conv_id], true)
    toast(`已加入不打扰名单：${row.name}`)
    await reload()
  } catch (e) {
    toast('操作失败：' + e.message, 'error')
  }
}

onMounted(loadSkip)
</script>

<template>
  <div class="wb-page">
    <div class="wb-page-head">
      <div>
        <h2>主动激活</h2>
        <div class="sub">
          也叫「主动触达 / 存量激活」：后台按节奏给长期沉默的客户主动发一条，
          把话聊起来。这里管开关、触达范围和不该打扰的人，不改客户名单。
        </div>
      </div>
      <el-button size="small" @click="reload">刷新状态</el-button>
    </div>

    <!-- 主开关：一眼能看见，也是本次需求的核心 -->
    <div class="ou-hero" :class="{ on: o.enabled }">
      <div class="ou-hero-main">
        <div class="ou-hero-title">
          <b>后台主动触达</b>
          <el-tag size="small" effect="dark" :type="o.enabled ? 'success' : 'info'">
            {{ o.enabled ? '运行中' : '已暂停' }}
          </el-tag>
          <span class="wb-hint">来源：{{ o.source || '—' }}</span>
        </div>
        <p v-if="o.enabled">
          已开启：工作时段内每 5 分钟巡检一轮，队列空闲且符合沉默 / 冷却条件时，
          挑一个客户发一条。客户主动找上来时回复优先，主动消息自动让路。
        </p>
        <p v-else>
          已暂停：主进程下一轮巡检（最多 5 分钟）就停手，不会主动给任何客户发消息。
          回复客户来消息、人工接管通知、活动触达都不受这个开关影响。
        </p>
      </div>
      <div class="ou-hero-switch">
        <el-switch size="large" :model-value="!!o.enabled" @change="toggle" />
        <span class="wb-hint">{{ o.enabled ? '点此暂停' : '点此开启' }}</span>
      </div>
    </div>

    <div class="ou-notice" :class="o.persisted ? 'ok' : 'warn'">
      <template v-if="o.persisted">
        开关已写进数据库（app_setting），<b>重启服务不会自动打开或关闭</b>；
        想改状态只能在这里点，或由手动脚本显式触发。
      </template>
      <template v-else>
        目前用的是配置默认值（config.json 的 outreach.enabled =
        {{ o.config_enabled ? 'true' : 'false' }}），还没在看板点过开关。
        <b>点一次开关就会固定到数据库</b>，之后一直以看板设置为准。
      </template>
    </div>

    <!-- 触达范围：两个行为开关，改完下一轮巡检即生效，不必重启主进程 -->
    <div class="wb-card">
      <h3>触达范围</h3>
      <p class="wb-hint" style="margin: -4px 0 6px">
        这两个开关点了立刻落库，主进程下一轮巡检（≤5 分钟）就按新设置挑人。
      </p>
      <div class="ou-row">
        <div class="ou-row-txt">
          <b>没聊过也破冰</b>
          <span class="wb-hint">
            加了好友但从没说过话的人，也让他进入候选（AI 写自我介绍 + 价值点 + 低压钩子）。
            默认关——给没说过话的人主动发消息最像群发营销。
          </span>
        </div>
        <el-switch :model-value="!!o.include_new_contacts"
                   @change="(v) => toggleFlag('include_new_contacts', v)" />
      </div>
      <div class="ou-row">
        <div class="ou-row-txt">
          <b>只碰已开托管的客户</b>
          <span class="wb-hint">
            打开后等于正向白名单：只有你在会话列表里<b>显式开了托管</b>的人才会被主动搭话，
            其余一律跳过。想精确控制「只对这几个客户做主动触达」就用它。
          </span>
        </div>
        <el-switch :model-value="!!o.require_switch"
                   @change="(v) => toggleFlag('require_switch', v)" />
      </div>
    </div>

    <!-- 不打扰名单：硬排除，私人联系人放这里 -->
    <div class="wb-card">
      <h3>不打扰名单（{{ o.skip_count || 0 }} 人）</h3>
      <p class="wb-hint" style="margin: -4px 0 10px">
        微信里那些<b>家人、朋友、同事</b>这类私人联系人放进来，AI 绝不会主动给他们发任何消息——
        破冰、唤回都不碰，沉默天数 / 冷却 / 累计次数这些规则一概不再考虑。这是硬排除，优先级最高。
      </p>

      <ContactPicker v-model="picked" :contacts="skipContacts"
                     @contacts-updated="onContactsUpdated" />
      <div class="ou-skip-actions">
        <el-button type="primary" size="small" :loading="busy" :disabled="!picked.length"
                   @click="addSkip">
          加入不打扰{{ picked.length ? `（${picked.length} 人）` : '' }}
        </el-button>
        <span class="wb-hint">在上面的列表里点名字选中，再点这里加入</span>
      </div>

      <el-table :data="o.skip_list || []" size="small" style="margin-top: 12px"
                empty-text="名单为空 —— 目前所有好友都可能被主动触达">
        <el-table-column label="对象" min-width="200">
          <template #default="{ row }">
            <div class="ou-who">
              <WbAvatar :url="row.avatar_url" :name="row.name" size="sm" />
              <span class="nm">{{ row.name || row.conv_id }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="备注" min-width="120" show-overflow-tooltip>
          <template #default="{ row }">{{ row.note || '—' }}</template>
        </el-table-column>
        <el-table-column label="加入时间" width="140">
          <template #default="{ row }">{{ row.created ? fmtTime(row.created) : '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" align="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="removeSkip(row)">移出</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="wb-metrics ou-metrics">
      <div v-for="m in metrics" :key="m.k" class="wb-metric">
        <div class="k">{{ m.k }}</div>
        <div class="v">{{ m.v }}</div>
      </div>
    </div>

    <div class="wb-card">
      <h3>当前符合规则的候选（最多 5 个，只看不改）</h3>
      <p class="wb-hint" style="margin: -4px 0 10px">
        即使开关打开，也只会从这里挑人，而且每次最多发一条；没具体话题可承接的会跳过，不发空泛问候。
        看到不该打扰的人，直接点右边「不打扰」。
      </p>
      <el-table :data="o.candidates || []" size="small" empty-text="当前没有符合条件的客户">
        <el-table-column label="对象" min-width="170">
          <template #default="{ row }">
            <div class="ou-who">
              <WbAvatar :url="row.avatar_url" :name="row.name" size="sm" />
              <span class="nm">{{ row.name }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="row.kind === 'icebreak' ? 'warning' : 'primary'">
              {{ row.kind === 'icebreak' ? '没聊过·破冰' : '承接旧话题' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最后一条消息" width="120">
          <template #default="{ row }">{{ row.last_ts ? fmtTime(row.last_ts) : '—' }}</template>
        </el-table-column>
        <el-table-column label="距今" width="80">
          <template #default="{ row }">{{ sinceDays(row.last_ts) }}</template>
        </el-table-column>
        <el-table-column label="最后是谁说的" width="110">
          <template #default="{ row }">
            {{ row.last_role === 'them' ? '对方' : (row.last_role === 'me' ? '我方' : '—') }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" align="right">
          <template #default="{ row }">
            <el-button link type="warning" size="small" @click="skipCandidate(row)">不打扰</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="wb-card">
      <h3>关于「重启服务就发一条」</h3>
      <ul class="ou-list">
        <li>以前进程一启动就立刻巡检一轮，只要上次触达超过了条间间隔，就会马上补发一条。</li>
        <li>现在加了启动静默期：服务启动后 {{ mins(o.boot_grace_seconds) }} 分钟内不主动发任何消息，
            之后才按正常间隔走。重启不再自带一条主动消息。</li>
        <li>配置默认改为关闭；要长期跑，得在这个页面把开关打开。</li>
        <li>频率、时段、沉默天数这些参数不在看板里改，在项目根目录 <code>config.json</code> 的
            <code>outreach</code> 块调整，主进程下次启动生效。</li>
      </ul>
      <p class="wb-hint" style="margin: 10px 0 0">
        想人工看着一次性发几条，用 <code>python activate.py --preview</code> 先看文案、
        <code>python activate.py --send</code> 再真发；那条路径不看这个开关，但同样会跳过不打扰名单。
        合规红线不变：只碰通讯录好友，禁止无差别加陌生人或骚扰营销。
      </p>
    </div>
  </div>
</template>

<style scoped>
.ou-hero {
  display: flex; align-items: center; gap: 20px; justify-content: space-between;
  background: var(--wb-surface-2); border: 1px solid var(--wb-border);
  border-left: 4px solid var(--wb-muted);
  border-radius: 12px; padding: 16px 20px; margin-bottom: 12px;
}
.ou-hero.on { border-left-color: var(--wb-accent); background: var(--wb-accent-soft); }
.ou-hero-main { flex: 1; min-width: 0; }
.ou-hero-title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ou-hero-title b { font-size: 17px; font-weight: 600; }
.ou-hero p { margin: 8px 0 0; font-size: 14px; line-height: 1.7; color: var(--wb-muted); }
.ou-hero-switch {
  display: flex; flex-direction: column; align-items: center; gap: 8px;
  flex-shrink: 0; padding-left: 18px;
}
.ou-notice {
  font-size: 14px; line-height: 1.7; padding: 10px 14px;
  border-radius: 8px; margin-bottom: 14px; border: 1px solid var(--wb-border);
  background: var(--wb-surface-2); color: var(--wb-text);
}
.ou-notice.warn { border-color: #e6a23c; }
.ou-notice.ok { border-color: var(--wb-accent); }
.ou-notice b { font-weight: 600; }
.ou-metrics .wb-metric .v { font-size: 17px; line-height: 1.5; margin-top: 6px; }
.ou-list { margin: 0; padding-left: 20px; font-size: 14px; line-height: 1.9; color: var(--wb-text); }
.ou-list code, .wb-hint code {
  background: var(--wb-surface-2); padding: 1px 5px; border-radius: 4px; font-size: 13px;
}
.wb-card + .wb-card { margin-top: 14px; }
/* 触达范围两个开关：左边说明、右边开关，窄屏自动换行 */
.ou-row {
  display: flex; align-items: center; gap: 18px; justify-content: space-between;
  padding: 12px 0; border-top: 1px solid var(--wb-border);
}
.ou-row:first-of-type { border-top: 0; }
.ou-row-txt { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.ou-row-txt b { font-size: 15px; }
.ou-row-txt .wb-hint { line-height: 1.7; }
.ou-skip-actions { display: flex; align-items: center; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
.ou-who { display: flex; align-items: center; gap: 8px; min-width: 0; }
.ou-who .nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 720px) {
  .ou-hero { flex-direction: column; align-items: flex-start; }
  .ou-hero-switch { padding-left: 0; flex-direction: row; }
  .ou-row { align-items: flex-start; }
}
</style>
