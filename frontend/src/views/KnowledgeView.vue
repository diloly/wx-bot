<script setup>
import { inject, ref, computed, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import { api } from '../api'
import { fmtTime } from '../utils'

const store = inject('store')
const toast = inject('toast')

const fileInput = ref(null)
const uploading = ref(false)
const testing = ref(false)
const query = ref('你们这个多少钱，能试用吗？')
const diag = ref(null)
const dialogVisible = ref(false)
const dialogReadonly = ref(false)
const savingText = ref(false)
const editId = ref('')
const editName = ref('')
const editText = ref('')
const editNote = ref('')

const k = computed(() => store.knowledge || {})
const items = computed(() => k.value.items || [])
const cfg = computed(() => k.value.config || {})
const stats = computed(() => k.value.stats || {})
const caps = computed(() => k.value.capabilities || {})
const modes = computed(() => k.value.modes || {})

const STATUS = {
  ready: { text: '已生效', type: 'success' },
  needs_text: { text: '待补文字', type: 'warning' },
  disabled: { text: '已停用', type: 'info' },
}

// 配置表单独立成一份本地副本：2s 轮询不碰本页，切页回来再同步一次，
// 避免用户改到一半被响应式中枢覆盖。
const form = ref({ mode: 'auto', max_chars: 12000, top_k: 3 })
watch(cfg, (v) => {
  if (v && v.mode) form.value = { mode: v.mode, max_chars: v.max_chars, top_k: v.top_k }
}, { immediate: true })

async function reload() {
  try {
    Object.assign(store.knowledge, await api.knowledgeList())
  } catch (e) {
    toast('加载知识库失败：' + e.message, 'error')
  }
}

async function onFiles(e) {
  const files = Array.from(e.target.files || [])
  if (!files.length) return
  uploading.value = true
  try {
    const r = await api.knowledgeUpload(files)
    const need = (r.added || []).filter((x) => x.status === 'needs_text').length
    let msg = `已入库 ${r.count || 0} 份`
    if (need) msg += `，${need} 份需要补文字`
    if ((r.errors || []).length) msg += `，${r.errors.length} 份失败`
    toast(msg, (r.errors || []).length ? 'warning' : 'success')
    ;(r.errors || []).forEach((x) => toast(`${x.name}：${x.error}`, 'error'))
    await reload()
    if (diag.value) await runDiag()
  } catch (err) {
    toast('上传失败：' + err.message, 'error')
  } finally {
    uploading.value = false
    e.target.value = ''
  }
}

async function saveConfig() {
  try {
    await api.knowledgeConfig(form.value)
    toast('注入策略已保存，下一条消息就生效（不用重启）')
    await reload()
    if (diag.value) await runDiag()
  } catch (e) {
    toast('保存失败：' + e.message, 'error')
  }
}

async function runDiag() {
  if (!query.value.trim()) { toast('先填一句客户可能说的话', 'warning'); return }
  testing.value = true
  try {
    diag.value = await api.knowledgeDiagnose(query.value.trim())
  } catch (e) {
    toast('自检失败：' + e.message, 'error')
  } finally {
    testing.value = false
  }
}

async function toggle(row, on) {
  try {
    const r = await api.knowledgeToggle(row.id, on)
    if (!r.ok) { toast(r.error || '切换失败', 'error'); await reload(); return }
    await reload()
    if (diag.value) await runDiag()
  } catch (e) {
    toast('切换失败：' + e.message, 'error')
    await reload()
  }
}

async function openText(row, readonly = false) {
  try {
    const r = await api.knowledgeText(row.id)
    editId.value = row.id
    editName.value = row.name
    editNote.value = row.note || ''
    editText.value = r.text || ''
    dialogReadonly.value = readonly
    dialogVisible.value = true
  } catch (e) {
    toast('读取正文失败：' + e.message, 'error')
  }
}

async function saveText() {
  savingText.value = true
  try {
    const r = await api.knowledgeSaveText(editId.value, editText.value)
    if (!r.ok) { toast(r.error || '保存失败', 'error'); return }
    toast('正文已保存，模型下一次回复就能看到')
    dialogVisible.value = false
    await reload()
    if (diag.value) await runDiag()
  } catch (e) {
    toast('保存失败：' + e.message, 'error')
  } finally {
    savingText.value = false
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(`删除「${row.name}」？原文件与提取出的正文都会一并删除。`, '确认删除', {
      confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  try {
    await api.knowledgeDelete(row.id)
    toast('已删除')
    await reload()
    if (diag.value) await runDiag()
  } catch (e) {
    toast('删除失败：' + e.message, 'error')
  }
}
</script>

<template>
  <div class="wb-page">
    <div class="wb-page-head">
      <div>
        <h2>知识库</h2>
        <div class="sub">上传产品资料，转成文字后注入每次 AI 回复；上传完下一条消息即生效</div>
      </div>
      <el-button size="small" @click="reload">刷新</el-button>
    </div>

    <!-- 把「模型怎么知道这些知识」讲清楚：这不是玄学，也不是向量化的功劳 -->
    <div class="wb-card kb-why">
      <h3>模型到底怎么"知道"这些资料？</h3>
      <div class="kb-flow">
        <div class="kb-step"><b>1 上传</b><span>txt / md / pdf / docx / 图片</span></div>
        <div class="kb-arrow">→</div>
        <div class="kb-step"><b>2 转成纯文字</b><span>取文本层 / OCR / 人工补写</span></div>
        <div class="kb-arrow">→</div>
        <div class="kb-step"><b>3 拼进提示词</b><span>每次回复原样塞进 system</span></div>
      </div>
      <p>
        大模型<b>不会读本地文件、没有记忆，也看不懂 PDF 和图片</b>。它唯一的知识来源，
        就是这一次请求里被拼进 system 提示词的那几段原文；下次对话又是一张白纸。
        所以"答得准不准"取决于<b>检索有没有把对的段落召回</b>，跟模型本身无关。
      </p>
      <p>
        目前没有做向量化：现有资料只有 {{ stats.total_chars || 0 }} 字，默认走
        <b>全量注入</b>——一次不漏，比中文关键词检索更稳（客户说"多少钱"、资料写"报价"，
        字面检索会一个字都匹配不上）。等资料大到塞不进字符预算，系统会自动退回关键词检索；
        那时再上 embedding / 向量库才有意义。下面的「检索自检」可以直接看到模型每次读到了什么。
      </p>
    </div>

    <div class="wb-metrics">
      <div class="wb-metric"><div class="k">上传资料</div><div class="v">{{ stats.total || 0 }}</div></div>
      <div class="wb-metric"><div class="k">已生效</div><div class="v">{{ stats.ready || 0 }}</div></div>
      <div class="wb-metric"><div class="k">待补文字</div><div class="v">{{ stats.needs_text || 0 }}</div></div>
      <div class="wb-metric"><div class="k">检索单元</div><div class="v">{{ stats.units || 0 }}</div></div>
      <div class="wb-metric"><div class="k">可注入文字</div><div class="v">{{ stats.total_chars || 0 }}</div></div>
    </div>

    <div class="wb-grid2">
      <div class="wb-card">
        <h3>注入策略</h3>
        <div class="kb-form-row">
          <span>模式</span>
          <el-select v-model="form.mode" size="default" style="flex: 1">
            <el-option v-for="(label, val) in modes" :key="val" :label="label" :value="val" />
          </el-select>
        </div>
        <div class="kb-form-row">
          <span>字符预算</span>
          <el-input-number v-model="form.max_chars" :min="1000" :max="60000" :step="1000" />
        </div>
        <div class="kb-form-row">
          <span>检索条数</span>
          <el-input-number v-model="form.top_k" :min="1" :max="20" />
        </div>
        <p class="kb-tip">
          预算是塞进提示词的资料字数上限（不含人设模板）。auto = 资料小于预算就全量、超了才检索；
          retrieve 只注入关键词命中的前 top_k 段。改完不用重启自动回复进程。
        </p>
        <el-button size="small" type="primary" @click="saveConfig">保存策略</el-button>
      </div>

      <div class="wb-card">
        <h3>检索自检</h3>
        <p class="kb-tip" style="margin-top: 0">
          写一句客户可能说的话，看看这一问模型究竟会被喂到哪几段资料。答非所问时先来这里查。
        </p>
        <el-input v-model="query" type="textarea" :rows="2" placeholder="例如：你们这个多少钱，能试用吗？" />
        <el-button size="small" type="primary" style="margin-top: 10px" :loading="testing" @click="runDiag">
          看模型会读到什么
        </el-button>
        <div v-if="diag" class="kb-diag">
          <div class="kb-diag-head">
            实际策略 <b>{{ diag.effective === 'full' ? '全量注入' : '关键词检索' }}</b>
            （配置 {{ diag.mode }}）· 命中 {{ (diag.matched || []).length }} 段 ·
            注入 {{ (diag.injected || []).length }} 段 / {{ diag.injected_chars }} 字
            <span v-if="diag.truncated" style="color: #e6a23c">（超预算已截断）</span>
          </div>
          <div v-if="!(diag.injected || []).length" class="wb-empty">
            一段都没注入 —— 模型这次只会看到"暂无相关资料"，会回答"我确认一下再回复你"。
          </div>
          <div v-for="(u, i) in diag.injected" :key="i" class="kb-hit">
            <div class="kb-hit-t">
              〔{{ u.file }} / {{ u.title }}〕
              <span>{{ u.chars }} 字<span v-if="diag.effective !== 'full'"> · 相关度 {{ u.score }}</span></span>
            </div>
            <div class="kb-hit-p">{{ u.preview }}…</div>
          </div>
        </div>
      </div>
    </div>

    <div class="wb-card">
      <h3>上传资料</h3>
      <input
        ref="fileInput" type="file" multiple style="display: none" @change="onFiles"
        accept=".txt,.md,.markdown,.csv,.json,.log,.htm,.html,.pdf,.docx,.png,.jpg,.jpeg,.webp,.bmp,.gif,.tif,.tiff"
      />
      <el-button size="small" type="primary" :loading="uploading" @click="fileInput.click()">
        选择文件（可多选）
      </el-button>
      <span class="kb-tip" style="margin-left: 12px">
        txt / md / csv / html 直接读；pdf 取文本层；docx 取段落；单份上限 {{ caps.max_upload_mb || 20 }} MB。
      </span>
      <p class="kb-tip" style="margin-bottom: 0">
        <b>图片和扫描版 PDF 是像素，纯文本模型看不见</b>：上传后会标成「待补文字」，
        请点「查看 / 补文字」把内容粘进去，或装 Tesseract + 中文包后重新上传做 OCR。
        <span v-if="caps.pdf === false">（当前未装 pypdf，PDF 会全部走人工补文字）</span>
        <span v-if="caps.ocr === false">（当前未装 OCR 组件）</span>
      </p>
    </div>

    <div class="wb-card">
      <h3>资料列表 {{ items.length }}</h3>
      <p class="kb-tip" style="margin-top: 0">
        这里只管理<b>通过看板上传</b>的资料。直接放进项目 knowledge/ 目录的手工 .md / .txt
        同样会进入检索（当前共 {{ stats.units || 0 }} 个检索单元、{{ stats.total_chars || 0 }} 字），
        只是不在本列表里显示，需要自己到目录里编辑。
      </p>
      <el-table :data="items" size="small" empty-text="还没有上传资料">
        <el-table-column label="文件" min-width="190">
          <template #default="{ row }">
            <div style="font-size: 14px">{{ row.name }}</div>
            <div v-if="row.note" style="font-size: 13px; color: #e6a23c; line-height: 1.5">{{ row.note }}</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="105">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="(STATUS[row.status] || {}).type || 'info'">
              {{ (STATUS[row.status] || {}).text || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="文字" width="90">
          <template #default="{ row }">{{ row.chars }} 字</template>
        </el-table-column>
        <el-table-column prop="extractor" label="提取方式" width="140" show-overflow-tooltip />
        <el-table-column label="上传" width="120">
          <template #default="{ row }">{{ fmtTime(row.uploaded) }}</template>
        </el-table-column>
        <el-table-column label="生效" width="70">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" :disabled="row.status === 'needs_text'"
                       @change="(v) => toggle(row, v)" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openText(row, false)">查看 / 补文字</el-button>
            <a :href="api.knowledgeFileUrl(row.id)" style="font-size: 13px; color: var(--wb-accent); text-decoration: none">原文件</a>
            <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialogVisible" :title="editName" width="720px" top="8vh">
      <div v-if="editNote" class="kb-tip" style="color: #e6a23c; margin-top: 0">{{ editNote }}</div>
      <p class="kb-tip">
        下面就是<b>模型能看到的全部内容</b>。可以直接改正或粘贴图片/PDF 里的文字；保存后下一次回复立即生效。
      </p>
      <el-input v-model="editText" type="textarea" :rows="16" :readonly="dialogReadonly"
                placeholder="把资料正文粘贴到这里" />
      <template #footer>
        <el-button @click="dialogVisible = false">关闭</el-button>
        <el-button v-if="!dialogReadonly" type="primary" :loading="savingText" @click="saveText">保存正文</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.kb-why h3 { margin-bottom: 12px; }
.kb-why p { font-size: 14px; line-height: 1.85; color: var(--wb-muted); margin: 10px 0 0; }
.kb-why b { color: var(--wb-text); font-weight: 500; }
.kb-flow { display: flex; align-items: stretch; gap: 8px; flex-wrap: wrap; }
.kb-step {
  flex: 1; min-width: 150px; background: var(--wb-surface-2);
  border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column; gap: 4px;
}
.kb-step b { font-size: 15px; font-weight: 500; }
.kb-step span { font-size: 13px; color: var(--wb-muted); }
.kb-arrow { align-self: center; color: var(--wb-muted); font-size: 16px; }
.kb-form-row { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.kb-form-row > span { width: 76px; flex-shrink: 0; font-size: 14px; color: var(--wb-muted); }
.kb-tip { font-size: 13px; color: var(--wb-muted); line-height: 1.75; margin: 10px 0; }
.kb-diag { margin-top: 12px; border-top: 1px solid var(--wb-border); padding-top: 10px; }
.kb-diag-head { font-size: 13px; color: var(--wb-muted); line-height: 1.7; margin-bottom: 8px; }
.kb-diag-head b { color: var(--wb-accent); font-weight: 500; }
.kb-hit { background: var(--wb-surface-2); border-radius: 8px; padding: 9px 12px; margin-bottom: 7px; }
.kb-hit-t { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; }
.kb-hit-t span { color: var(--wb-muted); flex-shrink: 0; }
.kb-hit-p { font-size: 13px; color: var(--wb-muted); line-height: 1.6; margin-top: 5px; }
@media (max-width: 980px) { .kb-arrow { display: none; } }
</style>
