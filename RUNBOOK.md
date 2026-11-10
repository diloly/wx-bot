# 微信客户维护机器人 — 完整运行路径

> 2026-10-13 整理。环境：Weixin 4.1.13.65（RadiumWMPF 架构）+ Python 3.13。
> 路线：收 = 本地数据库解密（只读）；发 = 键盘驱动（非注入）；AI = OpenAI 兼容云端模型。

## 一、系统全景（数据流）

```
[收] 微信写本地 SQLCipher 库
      → auto_reply.py / wx_recv_bridge.py（WeChatDB 解密轮询，10s，只读）
      → monitor_store.py（wechat_monitor.db，server_id 去重）
      → dashboard.py（http://localhost:5000，2s 刷新）

[发] auto_reply.py（AI 闭环）或 wechat_ui.py（手动/定时）
      → 键盘驱动（控件树切回聊天页 → Ctrl+F 搜 → 粘贴名字 → Enter → 粘贴消息 → Enter）
      → 写回 monitor_store(role=me) → 看板上屏
```
（切回聊天页不能省：客户端停在朋友圈页时会话列表/搜索框不渲染，Ctrl+F 静默失效，见第八节。）

| 组件 | 角色 | 状态 |
|---|---|---|
| wechatauto-replica（PyPI 库） | 数据库解密底座（WeChatDB） | 已装，体检 20/20 |
| wx_recv_bridge.py | 收消息轮询 → 入库（只收不发） | 新 |
| auto_reply.py | 收→AI→键盘发→写库 闭环 | 新 |
| wechat_ui.py | 键盘发送 + 定时主动外发 | 已改（conv_id 归并会话） |
| ai_client.py | 大模型文案生成（OpenAI 兼容） | 已有 |
| monitor_store.py | 统一消息库（看板数据底座） | 已有 |
| dashboard.py | 网页看板 | 已有 |

## 二、一次性准备

1. `pip install -r requirements.txt`（依赖清单在 `requirements.txt`，环境自选）
2. `wechatauto 体检`（数据库密钥 20/20、图片密钥 OK；控件树缺失只影响 GUI 发送，不影响本路径）
3. 编辑 `ai_secrets.json`（**AI 配置统一在这一个文件**，含 api_key，不要外发/截图；已加入 .gitignore）：
   - `provider` / `model`：填你自己接入的 OpenAI 兼容服务与模型名
   - 接自定义/云端大模型时：`provider` 设 `custom`，`base_url` 写全端点（**自带 `/v1`，代码不拼**），`model` 写该服务实际支持的模型名
   - `system_prompt` / `temperature` / `max_tokens`：可选，不填用代码内置默认
   编辑 `config.json`：
   - `auto_reply` 块：白名单 `contacts`、合并窗口 `merge_window_seconds` / 频率闸 `max_replies_per_hour`
   - `contacts` 可加 `"wxid"` 字段做精确匹配（可选）
4. 确认 `ai_secrets.json` 里的 OpenAI 兼容端点可达（**不需要本地 Ollama**）；
   只看看板时连 AI 都不需要

## 三、日常运行（三个终端）

> **环境**：Python 3.10+，用你自己的虚拟环境（venv / conda / 系统 Python 均可），
> 激活后 `pip install -r requirements.txt`。下文命令里的 `python` 一律指
> **你当前激活的解释器**，不绑定任何具体路径、盘符或环境名。
> 所有命令在**项目根目录**（clone 下来的那个目录）执行；配置、密钥、知识库、
> 消息库都按脚本所在目录定位，日志与相对路径输出以项目根为准。
>
> **只看看板时只需终端 A**：dashboard 是只读的，不抢微信焦点、不发消息。
> 前端构建产物 `static/dist/` 已入库，**不需要装 node、不需要 npm run build**；
> 打开 http://localhost:5000 是 Vue 新版，`/legacy` 是旧版回退。

**终端 A — 看板（常开，最小启动）**
```
python dashboard.py
```

**终端 B — 收消息 + AI 自动回复（二选一）**
```
python wx_recv_bridge.py          # B1 只收不发（纯监控上屏，不调 AI、不碰 UI）
python auto_reply.py              # B2 演练：默认 dry_run=true，AI 生成但不发送
python auto_reply.py --no-dry     # B2 真发：抢键鼠焦点，运行时别碰电脑
```

> 默认 `config.json` 是 `auto_reply.enabled=true` + `dry_run=true`：
> 会收消息、生成回复、写任务账本，**不会真发**。真发只能靠显式 `--no-dry`。
> 终端 A / B 都必须是带 `db_gate` 的新版（WeChatDB 解密副本按进程隔离）。

**终端 C — 主动外发 / 存量激活（按需）**
```
python wechat_ui.py --inspect
python wechat_ui.py --send --template followup --tag old --no-dry
python wechat_ui.py --send --ai --intent "周末问候" --tag vip --no-dry
python wechat_ui.py --schedule --no-dry
python activate.py                 # 只列候选；--preview 出文案；--send 才真发
```

> **首次运行注意**：首轮要全量解密全部数据库 + 为每个新会话建基线（分钟级、一次性，之后每轮秒级）。
> 终端长时间没输出不是卡死——看 `[scan] n/总数 名字` 进度行在走；
> 也可 `python -u xxx.py` 强制无缓冲输出。想快速验证单轮：`wx_recv_bridge.py --once`（跑完即退）。
>
> **增量水印（ scalability ）**：基线建好后，每轮只查「sort_seq > 水印」的新消息，
> 空闲会话 0 行返回（亚毫秒级）——成千上万联系人也不会每轮全量遍历。
> `[poll]` 行显示每轮「基线/增量」会话数与耗时，可自证。

## 媒体真实渲染（图片/表情/视频/文件/链接卡片）

- 新消息遇媒体类型**自动下载解密**落地到 `wechat_media/`，看板气泡直接渲染
  `<img>` / `<video>` / 文件 chip / 链接卡片（标题+描述+封面图）。
- 图片密钥链：cfgDword 派生 → 本地缓存（体检时已拿到）→ 内存扫描，**离线可用**。
- 历史消息补渲染（一次性，幂等可重跑；先存了占位的旧记录也补上）：
  ```
  python wx_recv_bridge.py --media-backfill --depth 50
  ```
- **原图模式**（`--original`，仅限手动回填）：图片强制下载 `_h.dat` 真原件（清晰），
  但会**驱动微信界面**（点开气泡、滚动消息列表）——运行时勿操作电脑；
  不想滚界面加 `--no-scroll-original`。轮询路径只走本地解密档，绝不碰界面。
- 群聊发送者显示名：**微信群备注优先于昵称**（如「某总」优于本名），
  群成员表 5 分钟缓存（`_group_member_name`），刚改的备注下一轮生效。
- **卡片仿微信四段式**：标题（2 行截断）+ 大小/描述 + 右侧类型图标
  （PDF 红 / W 蓝 / X 绿；app 分享用首字母占位）+ 底栏来源（微信电脑版 / 千问APP / 腾讯会议）；
  文件卡点击即下载，链接卡新窗口打开。旧卡片记录重跑一次 `--media-backfill` 即升级为新版式。
- 已知限制：语音（SILK）浏览器不能播，仍显示占位；`.wxgf` 动画表情容器
  不能直接渲染（下载成功也显示占位）；卡片封面/app 图标走 CDN 下载，失败则用类型图标/首字母占位。

### 前端构建与一键回退（2026-11-10）

看板前端已从「单文件 HTML + 手写 CSS + 原生 JS」迁到 Vue 3 + Element Plus + Vite，
但**部署形态没变**：Flask 仍是唯一进程，既提供 `/api/*` 又托管 `static/dist/`。

- 源码 `frontend/`，产物 `static/dist/`（app.js ≈1.2MB、app.css ≈370KB，
  全量引入 Element Plus 的体积，本机 localhost 无感）。产物**入库**，
  所以 clone 下来 `python dashboard.py` 直接有页面，不需要 node。
- 改前端：`cd frontend && npm install && npm run build`。产物文件名固定不带 hash，
  每次覆盖同名文件，git diff 不会堆旧版本；Flask 不用重启，浏览器强刷即可。
  开发期 `npm run dev` 起 5173，Vite 把 `/api` 与 `/media` 代理到 5000。
- **回退有两条**：访问 `/legacy` 直接回旧看板；把 `static/dist/index.html`
  移走，根路由也会自动 `render_template("index.html")`。旧页面一直保留在
  `templates/index.html`，所有 `/api/*` 一个都没改，回退后功能完整。
- 主题：右上角开关切亮/暗，偏好写 localStorage；Element Plus 的
  `dark/css-vars.css` 负责组件，自定义 `--wb-*` 变量负责自有布局。
- 迁移收益：原生版为了「2 秒轮询别把用户正在输入的表单重绘掉」手搓的
  `pickerSig` / `convSig` / `momentSigs` 签名 diff 全部删除，Vue 响应式更新
  天然不重建 DOM。这是上独立前端最实在的一条，其余（TS、HMR、组件单测、
  静态托管扩容）对单机单用户看板只是顺带。
- 验收记录：`npm run build` 通过；Flask test_client 下 `/`、`/legacy`、
  `/assets/app.js`、`/assets/app.css` 与 7 个读接口全部 200；无头 Chrome
  渲染后 DOM 含 `data-v-app`、6 个导航齐全、控制台零报错。

## 四、每会话自动回复开关（默认全关）

**核心规则：只有开关打开的会话才会触发 AI 自动回复，默认全部关闭。**

- **在哪管**：看板（`dashboard.py`）左侧会话列表——**只列「已开启托管」的会话**，
  每行右侧一个小开关，点一下即关（关掉就从列表里消失，聊天区不受影响）。
- **开新的**：点搜索框右侧的 **「+」** 打开选择器 —— 可按**名字/备注/wxid 搜索**、
  按**群聊 / 私聊**分类筛、或按 `config.json → contacts` 里配的 **tags** 筛
  （标签跟着 contacts 的 `name`/`wxid` 匹配，与 auto_reply 同一份配置），
  点一行即开启托管，该会话立刻出现在左侧列表。
- **为什么只列已开启的**：几百个会话全列出来左栏没法用，也看不出哪些真在托管。
- 开关存 `wechat_monitor.db` 的 `conv_switch` 表，重启不丢。
- **何时生效**：`auto_reply.py` 每轮轮询前重读开关表——看板改完，下一轮（默认 10s）生效，不用重启。
- **判定顺序**（全过才回复）：总开关 `auto_reply.enabled` → 消息类型 → 群开关 `allow_groups`
  → **本会话开关** → 白名单 `contacts`（配了才叠加）→ 同会话冷却 → 每小时上限。
- **群聊**：`allow_groups=false` 时群的开关置灰，开了也不回（先改 config 再开）。
- **总开关未开时**：看板顶部显示黄色提示条，解释「会话开关暂不生效」。
- `auto_reply.py` 启动日志会打印当前已开开关的会话数；一个都没开时提示去看板开。

## 五、自动回复安全闸门（config.auto_reply）

| 闸门 | 默认 | 作用 |
|---|---|---|
| `enabled` | false | 总开关：false=只监控上屏不回复（当前状态）；测满意再开 |
| `dry_run` | true | 只打印不真发；`--no-dry` 才发 |
| `reply_types` | ["文本"] | 只回文本，图片/语音不回（type 是中文字符串） |
| `allow_groups` | false | 群消息不自动回 |
| `contacts` | [] | 可选的二级过滤：空=不额外限制（由每会话开关决定）；填了=还需匹配白名单 |
| `rhythm` | natural | 拟人节流档位（natural/calm/fast/off）：两次发送最小间隔 + 连发 6 条强制冷却 30–75s。与合并窗口/每小时上限叠加取严；状态落盘 `~/.wechatauto/rhythm.json`，auto_reply 与 wechat_ui 手动外发共享节流计数 |
| `merge_window_seconds` | 8 | 合并窗口：收到消息后静默这么久（无新消息）才把攒的几条合并成一条任务交给 AI。取代旧的 `cooldown_seconds`（已删） |
| `max_replies_per_hour` | 20 | 全局每小时上限（防风控） |
| FAILSAFE | 开 | 鼠标甩到屏幕左上角立即急停 |

### 5.1 自动回复是 24 小时的（有意如此）

自动回复**没有任何时段限制**，凌晨收到也照回——客户半夜发消息，早上才回就失去意义了。
全项目只有两处卡时段，都跟"主动找别人"有关，不是回复：

| 行为 | 时段 | config |
|---|---|---|
| 自动回复 | 24 小时不限 | —（只有 `max_replies_per_hour` 限速） |
| 主动触达（主动找人说话） | 8:00–21:00 | `outreach.work_start_hour/end_hour` |
| 加好友（主动加人） | 9:00–21:00 | `add_friend.work_start_hour/end_hour` |

主动触达的完整配置项与两套入口见 `COMMANDS.md` 2.12。

### 5.2 人工接管：什么时候机器人应该闭嘴

自动回复越顺手，越容易把一条真线索聊丢。所以「客户有意向」时不是继续回，而是
**把会话交出去**：

1. 判定范围默认收紧到「最近 7 天被主动激活过的会话」——只对已知客户的回复判定，
   不是全量聊天扫描；
2. `ai_client.classify()` 做一次独立二分类（不套销售话术、不查知识库），
   失败自动降级关键词；
3. 命中后给看板上勾选的**群聊 + 私聊（真人）**各发一条内部通知，内容含客户名、
   触发原话、判定依据、时间；
4. 同时关掉该客户会话的自动回复，本次也不生成 AI 回复，避免抢真人的话；
5. 落 `handoff_log`，同一会话 24 小时内不重复通知（客户连说三句只打扰真人一次）。

通知复用自动回复的发送队列与三态回读校验，`kind='handoff'` 不占主动触达额度。
配置在看板「人工接管」tab，写 `app_setting`，改完立即生效不用重启。
详见 `COMMANDS.md` 2.13。

**验收时重点看两条**：① 客户说「不需要」时绝不能触发；② 一个通知对象都没投出去时，
`_maybe_handoff()` 必须返回 False，让机器人照常回客户——不能把人晾在那。

### 5.3 活动触达：定时批量发，别把自动回复堵死

活动触达是「人写死文案 + 指定时刻 + 指定对象」的一次性发送，和 AI 主动触达
共用发送队列但互不相干（`kind='campaign'` 不进触达计数）。三条工程约束：

1. **看板只写配置，不发送**。真正发在 `auto_reply` 的 `campaign-worker` 里，
   否则两个进程会同时抢微信前台焦点（UI 门能排队，但没理由制造这种争抢）。
2. **过点宽限**：`grace_minutes` 默认 60，超时的 `scheduled` 直接标 `expired`。
   这条最重要——进程关机一晚上，第二天开机不能把昨晚的活动全补发出去。
3. **同一活动只保留 1 条在途**，20s 巡检逐条投递。一次选 50 个人若一股脑塞进
   `_send_q`，客户此时发消息要排在几十条后面，自动回复等于被堵死。

状态机用条件 UPDATE 抢占（`claim_campaign`），重启后残留的 `running` 超过
30 分钟标 `partial` 不再自动重投；每个对象的结果回写 `campaign.result`，
全部回齐才收口成 `sent` / `partial`。详见 `COMMANDS.md` 2.15。

看板「活动触达 → 详情」可看全文与逐条结果（2026-11-10）：状态、活动编号、
计划发送 / 创建 / 最后更新时间、过点宽限、状态说明、发送正文全文（可复制），
以及按 `conv_id` 把 `targets` 与 `result` 合并成的
「对象 / 类型 / 成功·失败·未发送 / 失败原因 / 回写时间」表；结果里多出来但
名单里没有的异常记录也会列出，不把失败藏掉。字段本来就随 `/api/campaigns`
全量返回，弹窗只做展示、不新增接口，改完只需重新构建前端 + 强刷浏览器。

## 六、聊天记录备份（export_history.py）

```
python export_history.py                    # 全量导出 backups/history_时间戳.json
python export_history.py --user wxid_xxx    # 只导指定会话
python export_history.py --limit 500        # 每会话上限
```

- wechatauto 的 `export_history` 封装；输出含**客户聊天内容**，属敏感数据：
  `backups/` 已在 .gitignore，不要外发/截图/传公网。
- 与 `wechat_monitor.db`（收发同库、含程序发送记录）互补：这是微信侧的原始底档。

## 七、销售人设与产品知识库（AI 回复的「大脑」）

### 三层 prompt 结构

```
system = 定位纪律（卖 SaaS 工具，不接项目）+ 身份纪律（被问才承认AI，没问不主动提）
       + 销售人设 + 聊天风格 + 产品知识（资料原文注入，小库默认全量）+ 输出纪律
user   = 客户称呼/标签/背景备注 + 本次沟通意图 + 最近对话上下文
```

- **定位纪律**（2026-11-06 新增，最容易答错的一条）：公司卖的是**为设计提效的
  AI SaaS 平台**（核心产品「AI照明工作站」A站），**不是设计服务商**。客户问
  「能不能帮我把这个项目做了/代出套图/接个单」→ 统一回「我们是给设计师用的工具，
  具体能不能覆盖您的需求，我让同事看下再回您」，不接活、不报工期、不承诺代做。
- **身份纪律**（内置模板，合规红线）：AI 生成内容须可识别身份——被问到
  「你是机器人吗/是AI吗」**必须坦然承认**（示例话术在模板里），不冒充真人、
  不编造真人状态（不说自己在外跑客户/开会）；**客户没问就不主动提**；
  承认后拉回价值（响应快、资料准），把 AI 身份说成优势。
- **人设可配**（`config.json` 的 `ai` 块，当前值）：
  `persona`=「小光」、`company`=「中照金智（广州）数据科技」、
  `product`=「AI照明工作站（A站）」、`positioning`=「为设计提效的 AI SaaS 平台」。
  这四个字段同时喂给「日常回复」和「首条自我介绍」两处 prompt，改一处全局生效。
- **完全接管 prompt**：`ai_secrets.json` 写 `system_prompt`（非空即整体覆盖内置模板；
  当前已清空走模板）。非保密的 prompt 推荐写在 `config.json` 的 `ai.system_prompt`。

### 产品知识库 knowledge/（2026-11-10 改造）

**先纠正一个常见误解**：大模型**不读本地文件、没有跨次记忆、也看不懂 PDF 和图片**。
它「知道」的全部内容，就是这一次 `/chat/completions` 请求里被拼进 `system` 提示词的
那几段资料原文。下一次对话又是一张白纸。所以**答得准不准 = 检索有没有召回对的段落**，
跟模型本身、跟有没有向量化没有直接关系。向量化解决的是「资料多到塞不进 prompt 时
如何召回」，不是「模型如何知道」。

- **资料进库的唯一通道**：`knowledge/` 顶层的 `.md` / `.txt`（`_` 开头的文件和目录
  是元数据，不进检索）。`## 小节` 是检索单元，长文由上传层自动切片。
- **看板「知识库」tab**：可多选上传 txt / md / csv / html / pdf / docx / png / jpg 等，
  后台抽成纯文本后，在顶层生成一份 `kb-*.md` 镜像给 `ai_client` 扫。原文件放
  `knowledge/_files/`、正文放 `knowledge/_text/`、索引放 `_meta.json`，整个目录
  被 `.gitignore` 忽略——公司资料不进公开仓库。
- **上传完不用重启**：`ai_client.load_knowledge()` 按目录 mtime 自动重载，运行参数
  `knowledge/_config.json` 也纳入了签名，改策略同样下一条消息生效。
- **抽不出文字的文件绝不假装已入库**：扫描版 PDF（无文本层）和图片是像素，纯文本
  模型看不见。这类文件状态为「待补文字」，不生成 `.md` 镜像、不进检索，看板提示
  人工粘贴正文；若本机装了 Tesseract + 中文语言包，会先尝试 OCR。
  可选依赖见 `requirements.txt`，缺库只降级、不报错。
- **注入策略**（看板可改，也可用 `config.json` 的 `knowledge` 块设默认值）：
  - `mode=auto`（默认）：资料总量 ≤ `max_chars` 就**全量注入**，超预算自动退回检索。
    当前资料只有几千字，全量一次不漏，比字面检索稳（客户说「多少钱」、资料写「报价」，
    bigram 一个字都对不上）。
  - `mode=full`：不检索，全部原文按文件顺序注入。
  - `mode=retrieve`：中文 bigram 关键词检索，只注入命中的 `top_k` 段；标题命中权重 3、
    正文命中权重 1（旧实现用 `set()` 把 x3 权重去重吃掉了，已修）。
  - `max_chars`：塞进提示词的资料字数预算；单元级筛选 + `build_system()` 字符级硬截
    双保险，单个超长单元也撑不爆 prompt。
- **检索自检**：知识库页输入一句客户可能说的话，会原样跑一遍选段逻辑，列出
  「命中几段 / 实际注入哪几段 / 各段原文开头 / 相关度」。AI 答非所问时先来这里看
  模型到底读到了什么，而不是先怀疑模型。
- **防编造**：prompt 明令资料里没有的（价格细节/合同/上线时间）统一答
  「我确认一下再回复你」，不许编功能、编价格、编案例。
- **什么时候才需要向量化**：当全量注入超预算、且字面检索频繁漏召回（同义/口语问法
  命中不到）时，再上 embedding + 向量库做语义召回；那时仍只是「选哪几段塞进 prompt」，
  模型本身依旧不存知识。拓展路线见 `knowledge/_说明.md`。

## 八、朋友圈轻互动（读自动 / 写按开关）

两条路线，互不依赖：

- **读（全自动，但非纯 DB）**：`moment_ops.MomentOps.poll_new()` 走 MomentDB 解密 sns.db，
  零 UI、水印增量。auto_reply.py 每 6 轮（约 60s）调一次：新圈 + 未读
  赞/评论通知落 `moments` / `moment_inter` 表，看板「朋友圈」页展示
  （左栏新圈列表，点开右栏看详情）。
  **但纯 DB 读只能看到客户端已拉取的数据**（2026-10-16 实测：微信 PC 只在
  用户点朋友圈页工具栏「刷新」按钮时才向服务端拉新圈——光切页停留不够，
  时间线仍是旧数据，sns.db 不更新，poll_new 照样 0 条；这就是「必须人工
  点开一次朋友圈才落库」的根因）。
  故 UI 刷新路线 `refresh()`：`focus_wechat` → `SwitchToMoments` →
  **点「刷新」按钮**（UIA 找 Name='刷新' 的 XTabBarItem，在页面顶部
  mmui::SNSWindowToolBar，与「消息/发表/独立窗口显示」并排；拟人坐标点击，
  不节流——它是导航动作且 gate 冷却会长时间占前台）→ 停留
  `moment.refresh_dwell` 秒（默认 4）等同步。auto_reply 按
  `moment.refresh_interval`（默认 180s，0=关闭）定期刷新后再读库——
  「实时」的上限就是一个刷新周期。刷新期间短暂抢占焦点。
  **刷完停哪（2026-10-16 二次重构，用户拍板「返回聊天这步不需要，没必要」）**：
  刷新后就地停在朋友圈页，**全程不回聊天页**——紧随其后的自动互动本就要
  在此定位，切回聊天再切回来不仅多余，且正是当天自动点赞全失败的根因
  （`moment` 属性只在首次初始化时导航，切回聊天后 find_moment 在聊天页白跑，
  库打「滚轮落点不属于微信 (0,0)」「未能定位到目标朋友圈」）。
  **唯一的「回聊天页」是发消息前一步**：`wechat_ui.send_to_contact` 在 Ctrl+F
  之前自带 `switch_to_chat_page()`——库的 SwitchToMoments 会把主窗留在
  朋友圈页，那里会话列表/搜索框整片不渲染、Ctrl+F 静默失效（库作者
  uia_driver.back_to_chat_tab 注释亦载此坑），故切页不能省。控件树点导航
  「微信」tab（窄窗口开着会话时点两次退出会话），随后 `_require_foreground`
  双保险。这条路径**只有真收到聊天消息、要发消息时才走**，所以微信停在
  朋友圈页不会破坏发消息，两条路线互不依赖。
  写操作前有双保险：`_ensure_foreground()`（已在前台则跳过，免枚举竞态；
  focus_wechat 本身带重试，且窗口识别不再依赖标题——开着会话时主窗标题是
  联系人名，类名 Qt515 才是稳定标识）+ `_ensure_moments_page()`（不在
  朋友圈页就重新导航，SwitchToMoments 已在页面时 0.1s 空转）。
- **写（默认手动，可自动）**：`MomentActor` 走 UIA——热激活 mmui 控制树后按 Name 找
  「赞/评论」按钮、读 BoundingRectangle 中心坐标点击。手动：看板按钮或
  `moment_ops.py --like/--comment`。自动：`moment_switch` 表按客户两个开关
  （自动点赞 / 自动评论，默认全关，看板右栏「互动管理」面板管理），poll 到
  新圈时 `auto_interact()` 按开关执行——只对开后**新到**的圈生效，不回溯
  历史圈，不会批量补赞。自动评论由小光人设生成（`moment.ai_comment=false`
  可全局关），评论不可逆故独立开关、默认关。模拟模式下自动互动也会唤起
  微信窗口验证定位（不真点），真跑才执行。
  **前台要求（2026-10-16 踩坑）**：库的「点发现→朋友圈」导航和 find_moment
  滚动都要求微信主窗口是**真前台**——后台进程（auto_reply）里热激活会成功、
  UIA 树也物化，但点击落空、时间线控件不出现，定位必失败（手动跑时微信在
  前台所以一直正常）。故每次写操作前 `moment_ops._ensure_foreground()` 先调
  `wechat_ui.focus_wechat()` 把微信提上前台；定位失败还会自动重试一次。

看板布局（2026-10-16 调整）：朋友圈页左栏只放新圈列表；右栏默认是
**「互动管理」面板**——「模拟/真跑」开关 + 按客户「赞/评」开关都收在这里
（原放在左栏顶部，窄栏看不全；dry-run 开关也不再塞在圈子详情页）。点某条
圈进详情，详情顶栏有「← 互动管理」返回按钮和模式标签（点标签也可回面板）。

水印坑（2026-10-15 实测）：SQL tid 列是负整数、与新旧无关；真正的自增序列在
XML `<id>`（20 位、超 int64）。poll_new 全量拉取 → Python int 排序 → 取最近
200 条 → `id > cursor` 过滤 → `add_moment` 按 tid 去重 → 推进水印。已验证：
水印回拨能补收、重复 poll 幂等。

写操作三道闸：① dry-run（看板「互动管理」面板开关
`__moment_dry__`，默认模拟）；② 每日上限 `max_actions_per_day`
（**0 = 不限次数，当前默认 0**——2026-10-16 用户取消固定 10 次：正常人朋友圈大一点
也不止 10 个，点赞是低风险动作、鼠标又是拟人化的；填正数即恢复硬上限）；
③ 库内置 rhythm 连发冷却——**这道才是真正防「机械连击」的闸**。定位不到/超上限一律拒绝。
**放行标准 = 开关 / 人工点击本身**：2026-10-16 移除 `config.moment.watch`
白名单——按客户开关就是人工确认；白名单是早于开关设计的第二道闸，属冗余，
还造成过「开了开关却被静默跳过」的困惑。自动模式同样只认开关。

**光标轨迹全程拟人（2026-10-16）**：库的点赞链路有两处**瞬时/直线**移动——
`_locate_more_click` 点「…」用 `pyautogui.moveTo(..., duration=0.1)`（直线冲刺）、
`_click_float_button` 点「赞/评论」用 `pyautogui.click(x, y)`（瞬移，注意
pyautogui 的 click 内部走 `_mouseMoveDrag` 而非 `moveTo`，两个都得接管），
另外 `_send_scroll` 滚时间线前用 `user32.SetCursorPos` 直接落位。库装在
site-packages、不宜改（重装即丢），所以 `moment_ops._human_cursor()` 在**一次写操作内**
临时把 `pyautogui.moveTo/click` 与 `wechatauto.moment._send_scroll` 接管成
`rhythm.move_to`（二次贝塞尔 + 末段减速），finally 还原。点赞全程都在 UI 门内，
无并发冲突。`_send_scroll` 的包装会先照原逻辑做「落点不属于微信就别动」判定，
避免平移到别的窗口上。

配置在 `config.json` 的 `moment` 块（max_actions_per_day（0=不限）/ ai_comment）；
命令速查见 COMMANDS.md 2.6。自动互动开关存 `moment_switch` 表
（monitor_store.set/get_moment_switches），与 conv_switch 同款语义：
无记录=全关，开开关只对之后新到的圈生效；面板会把开了开关的客户长期列出
（暂无新圈也有行，nickname 落表展示）。

### 并发：UI 独占与排队（ui_gate.py，2026-10-16）

**问题**：微信 PC 只有**一份 UI**（一个主窗口、一份键鼠焦点）。而会抢前台的动作
分散在**两个进程**里——`auto_reply.py`（键盘发送 / 朋友圈刷新 / 自动点赞评论）
和 `dashboard.py`（看板手动发送 / 点赞评论按钮）。此前没有任何互斥，两边同时
动手就是互相抢前台：历史上出过 `Ctrl+F` 发进看板浏览器、朋友圈刷新到一半被
消息发送切回聊天页导致定位在聊天页白跑（`滚轮落点不属于微信 (0,0)`）。

**方案**：`ui_gate.py` 提供全机唯一的「UI 门」，两条约束（用户拍板）：
**操作不能丢弃**——拿不到门就排队等，绝不是 try-lock 失败就跳过；
**只能排队**——同一时刻只允许一个操作持门。

分两层实现，缺一不可：

- **跨进程**：Windows 命名互斥体（`CreateMutexW` + `WaitForSingleObject`）。
  两个进程排同一队。选 Mutex 而非锁文件，是因为它有内核级「持有者退出即释放」：
  持门进程崩溃或被 Ctrl+C，等待者的 `WaitForSingleObject` 返回
  `WAIT_ABANDONED` 并**直接获得所有权**，天然自愈，不会留一个永远解不开的死锁
  （锁文件方案必须自写租约 + 过期，容易出错）。
- **进程内**：`threading.RLock`。看板是 Flask(`threaded=True`)，同进程多请求会
  并发；RLock 允许**同线程重入**，所以「一段业务里再调底层动作」不会自我锁死。

**临界区边界**（这是设计的关键，不是随手包一层）：

| 动作 | 门外做了哪些事 | 门内做了什么 |
|---|---|---|
| `send_to_contact` | AI 生成文案、rhythm 节流等待、写回看板库 | 聚焦→切页→Ctrl+F→粘贴→Enter→发送节流等待 |
| `MomentOps.refresh` | — | 聚焦→切页→点「刷新」→等 dwell→物化时间线 |
| `MomentOps.like/comment` | `_write_gate` 校验（读库） | 提前台→确认朋友圈页→定位→点击 |
| `auto_interact` | **全部 AI 评论文案先生成**（网络请求） | 逐条点赞/评论（**一条一进门**） |

三条取舍写在代码注释里，也记在这：
① **AI 请求绝不占门**——生成文案是数秒级网络请求，占着门会把「UI 只有一个」
的瓶颈从几秒放大到几十秒；所以朋友圈自动互动改成「先生成一批文案，再逐条
进门执行」两阶段。
② **逐条而非整批**——一批 10 条自动点赞如果整批持门会独占微信一两分钟，消息
回复全被堵住；改成一条一进门，中间让出 UI 给回复插队。代价是被插队后下一条
要重新导航回朋友圈页（`_ensure_moments_page` 兜底，约 1~2s）。
③ **发送后的 rhythm 等待留在门内**——那段时间微信仍应独占前台，放出去会让
两条外发贴太近（风控）。

**跨进程的缓存失效兜底**：`MomentOps` 的 `WeChat`/`Moment` 控件是长生命周期
缓存，另一个进程切过页会让本进程缓存的 UIA 引用失效。故 `_ensure_moments_page`
首次失败时会**丢弃缓存重建一次**再试，避免把「对方刚切过页」误判成「定位失败」。

**超时（任务级，见下一小节）**：自动回复与自动点赞用的**不是**这个全局值，而是
每条任务自己的剩余预算；`ui.lock_timeout_seconds`（180s）只是没带预算时（看板
手动按钮）的兜底。超时行为：自动回复/自动点赞**放弃这一条**（记日志、异常不冒泡），
看板按钮**返回明确报错**（可再点一次）。

**日志**：`[ui] XX：排队 N.Ns 后取得 UI（有别的操作在占微信）`＝正常排队；
`[ui] 检测到上一个进程异常退出留下的锁，已自动接管`＝自愈；`等待微信 UI 空闲
超时`＝查谁卡住了。命令速查见 COMMANDS.md 2.7。

### 任务流水线与任务级超时（auto_reply.py，2026-10-16）

**流水线**：四个线程各司其职，谁都不等谁——主线程只轮询收消息 + 判闸门 + 建任务
入队（立刻返回）；AI 线程串行消费待生成队列；发送线程独立消费待发送队列；
朋友圈线程独立跑刷新与自动互动。

为什么生成与发送要拆开（用户拍板原话：「不用 ai 完成一条，等发送完再给另一条 ai。
而是 ai 请求完成一条就直接发另一条 ai 请求」）：发送要抢微信 UI（几秒）。原来
「生成 A → 发送 A → 生成 B」一条龙里，这几秒白白占着 AI 的等待窗口，等于把两个
瓶颈串成一个。现在 AI 只管生成、发送只管发。**顺序仍有保证**：AI 线程单线程、
按 FIFO 消费，产出的发送队列顺序＝入队顺序（两个队列各只有一个 worker，不会乱序）。

朋友圈轮询也搬出了主线程：这一整套含 UI 刷新（带 4s 停留）和逐条点赞，一轮能占
几十秒；留在主线程会把「收到消息 → 立刻发起 AI 请求」整条流水线一起冻住，正好
抵消流水线改造的意义。节奏改为每 `interval × 6` 秒一轮（与原来「每 6 轮约 60s」等效）。

**任务级超时**（用户拍板：「对超时，对单条任务做限制，我们每个消息的回复
（每个用户算一条）任务，每个点赞算一条任务」）：

- 一条「回复任务」＝**一个用户的一次回复**，不是一条消息、也不是一次 AI 请求。
  `ReplyTask.deadline` 在**投递那一刻**就定死（受理时刻 + `auto_reply.task_timeout_seconds`），
  所以「排队等候」也算在预算里——任务不会因为前面排了别人而无限期挂着。
- 预算覆盖 AI 生成 + 排队 + 发送三段：AI 调用的 `timeout` 也取剩余预算（最多 60s）；
  发送时把剩余预算当 UI 门的排队上限。
- 任何一段耗尽即作废并记日志（`任务超时未生成` / `任务超时未发出` / `发送排队超时`）。
- 朋友圈同理：一条「赞+评」是一条任务，`moment.task_timeout_seconds`（默认 120s）。
- **合并窗口（debounce）**：客户常分两三条才说完一个意思，逐条发 AI 只会答半句话。
  所以收到消息先攒进该会话缓冲（`collect()`），**静默 `merge_window_seconds`（默认 8s）
  不再来新消息**才合并成**一条**任务投递（`flush()`）；窗口期内每来一条就把计时往后推。
  静态闸门（类型/群/白名单/会话开关）在 `collect()` 判，动态闸门（每小时上限）
  在 `flush()` 那一刻判。
- **合并窗口就是「已处理到哪条」的记录**：一投递就清空缓冲＝把之前收到的消息都标记为
  「已交给 AI」；回复发出后若又有新消息，它们已在新缓冲里，再等一个窗口就回下一轮。
- **旧的 `cooldown_seconds` 已废弃删除**：它按「距上次建任务不足 N 秒就跳过」判，
  会把「回复发出后几秒用户补的一句」直接丢掉；而合并窗口本就保证「用户没停手就不回复」，
  不需要再叠冷却。

**新增/改动的配套（引入多线程必须处理）**：

- `monitor_store._conn()` 的 sqlite 等待从默认 5s 提到 **15s**（`_BUSY_TIMEOUT`）：
  现在有收消息 / AI / 发送多个线程并发读写同一个库。
- `monitor_store.init()` 加**只跑一次**的快速路径（`_init_done` + 锁）：写函数每次
  都会调 `init()`，而它含 10 条 DDL + 一条**全表扫描**的 DELETE，多线程下每次写
  都全表扫一遍会拉长写锁占用、放大撞锁。显式传 `path` 时不走快速路径。
- `_contact_of()` 改用 AI 线程专属的 `WeChatDB` 实例（`_ai_db_obj`），不与主线程的
  收消息轮询共享连接（跨线程共用连接是隐患）。
- worker 内所有异常都被吞在自己循环里（`noqa: BLE001`）——worker 死了整条流水线
  就停了；`_stop_workers()` 在 Ctrl+C 时 `join(timeout=5)` 给正在发送的那条留时间。

命令速查见 COMMANDS.md 2.8。

### 维护任务队列的可观测（reply_task，2026-10-16）

内存队列（待生成 / 待发送）跑在 auto_reply 进程里，与看板是两个进程、看不到。
所以每条任务同步落库 `reply_task` 表当**状态账本**，看板侧栏底部「维护任务」条
（`GET /api/tasks`，只读）显示 `进行中 / 失败 / 作废` 计数，点开看最近 30 条的
状态 + AI 文案 / 失败原因。

状态机：`pending → generating → generated → sending → sent`，任一环节失败或超时
落 `failed` / `expired`。**只读，不做取消/插队。**

- **重启清理**：auto_reply 启动时把上次残留的未完成任务标 `expired`（否则面板
  永远显示假的「进行中」）；**不补跑**（旧回复过时，且可能与新任务重叠）。
- **不自动重试**：回复有时效（deadline 已兜住过期作废），发送不可逆（重试有重复
  发送风险）；失败在面板可见，交人工。
- 账本写失败只记日志、**不影响回复**。

命令速查见 COMMANDS.md 2.9。

## 九、验收清单（按顺序过）

1. `dashboard.py` 起来，浏览器开 http://localhost:5000
2. 跑 `auto_reply.py`（当前 `enabled=true` + `dry_run=true`），日志应显示已开启自动回复的会话数，
   且生成文案但**不真发**；确认无误才用 `--no-dry`
3. 用另一个号给微信发一条消息 → 10 秒内看板出现**对方气泡 + 正文**（`[store] +`）；群消息带「昵称: 」前缀
4. 自己在 PC 微信**手动发**的消息同样上屏（右侧「我」气泡）；程序发的不会双渲染；图片/表情/语音等显示 `[图片 md5=...]` 等占位，不露 XML
   - 不双渲染靠 `send_to_contact` **在按发送键之前**预写 `sent_log`（`monitor_store.add_sent`）：收消息轮询读到这条回执时命中 `was_sent` 即跳过。发送后还要 nap(delay)≈5s 才写看板库，而轮询在另一线程并发跑——若等发送后再写 sent_log，轮询会在这 5s 空窗里把回执当新消息补录一条（2026-10-16 修复）
5. 在看板把目标会话行右侧开关打开（`auto_reply.enabled` 当前已是 `true`，无需改）→
   该会话来消息时 dry-run 日志出现 `[auto] 生成回复 → ...`；一个开关都没开时日志会提示去看板开
6. `--no-dry` 真发成功，看板出现「我」的气泡
7. 合并窗口（2026-10-16 起）：同一客户在 8 秒内连发 3 条 → 日志出现 3 行
   `收到 X 第 N 条（8s 无新消息后合并提交）`，停手 8s 后出现 1 行
   `合并 3 条为一条任务 → X` —— 只发 1 次 AI、只回 1 条（这几条一起进任务账本）。
   停手后又补一句 → 8s 后出现 `合并 1 条为一条任务`（回下一轮）。超过每小时上限
   才会出现「已达每小时上限」跳过
8. 在看板关掉某会话开关 → 该会话再来消息不触发回复（静默跳过）
9. 问「你是机器人吗」→ 回复应坦然承认是 AI 销售助手（合规红线，瞒报不合格），
   承认后拉回价值继续聊正事；问产品问题（如价格）→ 只引用 `knowledge/` 里填了的
   内容，没填的答「我确认一下再回复你」
10. 朋友圈：跑 `moment_ops.py --poll` 后 `--list` 出现新圈；看板「朋友圈」页
    左栏有新圈列表、右栏是「互动管理」面板；`--like <tid> --dry` 显示定位成功；
    面板切真跑后点赞可见，`--unlike` 可撤销
11. 朋友圈自动互动：看板右栏面板给某客户开「赞」开关（模拟模式下终端应出现
    `自动模拟点赞` 日志、不真点）；让该客户现发一条新圈，验证只对开后新到的
    圈生效（历史圈不会被补赞）；评论开关默认关，开前想清楚——不可逆
12. 并发（UI 独占）：两个终端同时跑 `auto_reply.py` 和 `dashboard.py`，
    在看板点「发送」的同时让 auto_reply 也在发消息 → 两边都不该互相打断，
    日志应出现 `[ui] ...：排队 Ns 后取得 UI`；强杀其中一个进程后另一个仍能
    正常发送（Mutex 自愈，日志会出现「已自动接管」）
13. 流水线（2026-10-16 新增）：
    - 让两个客户在同一轮里各来一条消息 → 各自攒批，8s 后各出现一条
      `合并 1 条为一条任务`；AI 线程**连续**生成（第二条 `生成回复` 不必等第一条
      `已发送`）
    - 同一用户连发 3 条（间隔 <8s）→ 只产生 1 条任务（`合并 3 条为一条任务`），
      这 3 条一起进任务账本；没有「冷却中」这类日志了
    - 把 `auto_reply.task_timeout_seconds` 临时改成 5 测试任务级超时：应出现
      `任务超时未生成` 或 `任务超时未发出`，且**不影响后续任务**（流水线继续跑）
    - 朋友圈轮询期间给微信发消息 → 消息仍应被受理（朋友圈不再卡主循环）；
      受理判定＝8s 窗口到期，与朋友圈无关
14. 维护任务队列（2026-10-16 新增）：
    - 看板侧栏底部出现「维护任务」条；auto_reply 未跑时显示「空闲」
    - auto_reply 受理一条任务 → 面板计数「进行中 1」；点开可见该条（客户 + 状态
      徽章，随 pending→generating→generated→sending→sent 变化）
    - 把 `task_timeout_seconds` 临时改成 5 制造超时 → 面板出现红色「失败」或
      黄色「作废」，**原因可见**（如「预算耗尽（未生成）」）
    - 重启 auto_reply（上一次有未完成任务残留）→ 日志出现「上次运行残留 N 条
      未完成任务，已标为作废」，面板不再有永远卡住的「进行中」

## 十、走过的弯路（结论备忘，防回头）

- **多行通知「复制到聊天里却没发送」**（2026-11-10）：人工接管通知是**多行**文案，
  `message.strip()[:16]` 恰好切在换行符上；Windows 剪贴板 Ctrl+A/Ctrl+C 回读
  会把 LF 变成 CRLF，于是「正文有没有粘进输入框」的子串比对失败 → 抛
  `SendAborted` → **不按发送键**，文案一直留在输入框里。单行 AI 回复从不触发。
  修复：`wechat_ui.normalize_probe()` 抹掉全部空白，粘贴回读校验、
  `auto_reply._confirm_sent`、`activate.make_confirm` 三处统一用它。
  安全闸不能顺手拆掉：正文确实没粘进去时仍必须中止、不按键。
  `sent_log` 去重早就有 `_norm_sent` 抹空白，不受影响。
- **接管/取消托管后客户从左侧消失**（2026-11-10）：两半原因 ——
  ① 前端 `ChatView` / 旧版页写死 `filter(c => c.auto_reply)`，只列开着托管的；
  ② `ensure_switch_conversations()` 原来只补 `conv_switch.enabled=1` 的项，
  关闭后就不再兜底会话行。现在后端 `list_conversations()` 返回 `switched`
  （托管过，无论开关）/ `handoff_ts`，占位补齐覆盖关掉的开关并排除 `__` 伪会话
  （朋友圈模拟开关）；前端默认列「托管过的会话」，关掉的仍显示且开关为关，
  带「已转人工 / 已关托管」标签，另有「显示全部会话」可放开。
- **「接管/活动页头像没同步」其实是显示链路断了**（2026-11-10）：`avatars` 表里
  23/23 个候选都有记录、本地也有图片文件，但 `_handoff_candidates()` 只返回
  `conv_id/name/kind`，`ContactPicker` 的 `WbAvatar` 也只传 `name` 不传 `url` →
  永远画首字色块。排查先查库里覆盖率，别一上来改同步逻辑。现在候选带
  `avatar_url`，两个页面的选择器共用 `ContactPicker`：已选对象以 chips 固定在卡片
  顶部（头像 + 类型 + × 移除 + 清空），列表整行可点选；另有
  `POST /api/avatars/sync` 手动触发通讯录 + 头像同步，返回后只局部替换候选列表，
  不整页刷新（否则会冲掉正在编辑的表单和未保存的勾选）。微信没开时返回
  500 + 人话提示，前端专门解析响应体而不是让通用封装抛 `HTTP 500`。
- **「没做向量化，大模型怎么知道知识」是个伪问题**（2026-11-10）：模型从来不
  「知道」知识库，它只在每次请求里看到被拼进 system 的那几段原文，不读文件、
  没有记忆、看不懂 PDF/图片。所以精准度由**检索召回**决定。资料量小（几千字）时
  全量注入的召回率是 100%，比中文 bigram 字面检索更稳；向量化只在「资料大到塞不进
  prompt」时才有价值。上传文件必须转成纯文本并生成顶层 `.md` 镜像，否则模型看不见；
  图片/扫描 PDF 一律标「待补文字」，不能假装已入库。
- DLL 注入/hook：按老架构设计，本机新版微信无本地消息库，架构性错误，勿再投入。
- xlog 解密：TEA 密钥推导错，115 块全失败；PyWxDump 路线：`wxdump info` 报 WeChat No Run。
- UIA 读屏：Qt 自绘层不暴露控件（除非热激活）；读消息最终走 wechatauto-replica 解密本地库。
- **「sender_id == 2 就是自己发的」是错的**（2026-11-06 换设备事故）：sender_id 取的是
  `real_sender_id`，只是**消息所在分片 Name2Id 表的 rowid**。同一台机器上自己账号在
  message_0/1/2.db 的 rowid 分别是 2/4/1，本机实测不同会话里还出现过 1/3/10/24/26。
  换一台设备/换一个账号，编号一变，整套判定就反转，一次踩出三个症状：
  ① 看板自己发的消息跑到左边、对方的跑到右边（role 被写反）；
  ② `collect()` 把自己的消息当成客户新消息 → 无限自动回复（一直重发）；
  ③ 发送校验永远查不到自己那条 → `SendNotVerified` → 任务一直 failed（"落不了库"）。
  正确判据：**sender_username == 本机 wxid**（库已按分片解析好，见 `is_self()`）。
  已写反的历史数据用 `python wx_recv_bridge.py --fix-roles --dry-run` 试算后用
  `--fix-roles` 校正（只改 role，不删消息）。
- **多个 `WeChatDB()` 实例 = `[WinError 5] 拒绝访问`**（2026-11-10 启动事故）：
  `python auto_reply.py` 启动时报
  `os.replace('...contact__contact.db.<pid>.<tid>.tmp', '...contact__contact.db')`
  被拒绝，看 `wechatauto_db/` 里成堆的 `.tmp / -shm / -wal` 残留。两层根因：
  ① `wechatauto` 的解密副本目录 `%TEMP%/wechatauto_db/<账号>/` 是**全进程共享**的，
  微信 WAL 一变就重建并 `os.replace` 覆盖；Windows 下该文件只要被**另一个进程**
  的只读 SQLite 连接持有（`dashboard.py` 后台头像同步线程常驻持有），覆盖必失败。
  ② `_build_lock_for()` 是**实例级**锁，而 `auto_reply.py` 原本给主线程 / AI 线程 /
  发送线程各建了一个实例，同进程内也会并发重建同一份副本、互踩中间产物。
  修复：新增 `db_gate.py`，所有 `WeChatDB()` 收口到 `db_gate.get_db()`——
  **一进程一份私有副本目录**（`proc_<pid>/`，含 stamp / tmp / shm / wal / keys.json）
  + **一进程一个实例**（单例，构建锁统一）+ 命名 Mutex 只保护构造期共享
  `keys.json` 写入 + 预热 `get_self_info()` 时 WinError 5 自愈重试 2 次。
  实测：旧 dashboard 仍占用共享目录时，两个新进程并发启动 + 单进程 8 线程并发
  均成功，各自 workdir 独立、单例唯一。代价仅是每个进程首轮自行解密一遍（秒级）。
  运维注意：**旧代码的 dashboard 必须重启**才会走 `db_gate`；
  遗留的 `proc_*` 目录用 `python -c "import db_gate; print(db_gate.cleanup_stale())"`
  清理（只删 48 小时未修改且非本进程的目录）。
- **`contact` 表 ≠ 通讯录好友表**（2026-11-10）：旧 `sync_contacts()` 只按 username
  形态分类，凡不是 `@chatroom` / `gh_` / 系统号的统统标 `friend`，于是「+ 选择托管」
  的「好友」里混进一堆**只在群聊里出现、没加过好友的陌生人**，主动触达候选池
  也含他们（差点踩「只维护已知客户」的合规红线）。微信 4.x 的
  `contact.local_type` 才是判据：`1`=通讯录好友、`2`=未存通讯录的群聊、
  `3`=**群中陌生人**、`5`=企业微信好友、`6`=陌生企业微信好友
  （chatlog 的 ContactV4 也是 `IsFriend = LocalType != 3`）；
  `delete_flag` 排除已删好友、`verify_flag & 8` 认公众号、`flag & 3` 兜底
  （本机好友 flag 1/3、陌生人 0/4、群 2、两套判据结论一致）。
  修复：`kind` 新增 `stranger`；`GET /api/contacts` 只返回 friend+group
  （还排除本机自己）、前端「好友」chip 严格按 kind；`_handoff_candidates()`
  与 `get_activation_targets()` 都只认 friend。实测本机 56 条 =
  friend 13 / group 11 / official 16 / stranger 11 / system 5，选择器 23 个，
  接管对象 23 个、主动触达候选 4 个均无陌生人。
  误杀兜底：`config.contact_sync.friend_whitelist`（wxid 数组）。
  **升级后必须重启 dashboard.py** —— 旧进程每 10 分钟同步一次会用旧判据把
  kind 覆盖回 friend。

- **`POST /api/send` 不能把 wxid 当搜索词**（2026-11-10 看板手发事故）：
  `conv` 是看板会话 id（wxid / 群号），原来直接喂给 `send_to_contact(name, ...)`
  当 Ctrl+F 搜索词，而微信只认界面上显示的**备注/昵称**，结果搜不到人、正文
  卡在搜索框；顺带没传 `conv_id`，还会把 `conversations.name` 覆盖成一串 wxid。
  现在由 `monitor_store.get_display_name()` 解析（contacts.remark > name > nick
  > conversations.name > 解密库 `get_nickname`），查不到就明确报 400，**绝不退回
  用 wxid 硬搜**；写库始终用真实 wxid（`conv_id=conv`）。首条激活 2026-10-19
  栽在同一个坑，三级回退见 `auto_reply._nick_of()`。

## 十一、风控与合规（始终有效）

- 键盘驱动仍违反微信 ToS，有封号风险；频率闸 + 冷仅是软防护，不是豁免。
- 只维护已知客户；禁止无差别加陌生人、禁止群发骚扰营销。

## 十二、仓库历史重写后：其他机器怎么同步

2026-11-10 清过一次公开历史，所有 commit hash 都变了。已经 clone 过的机器
**不能靠 `git pull` 跟上** —— 新历史不是旧历史的后代。

> **前提**：本项目有大量被 `.gitignore` 的本地运行数据（`wechat_monitor.db`、
> `ai_secrets.json`、`knowledge/`、`wechat_media/`）。**不要重新 clone 覆盖**，
> 也**绝对不要 `git clean -fdx`**，否则这些文件会被删掉。`fetch + reset` 只动
> git 跟踪的文件，本地数据原样保留。

在原来那个 clone 目录里：

```bash
cd <项目根目录>

git fetch origin                     # 只更新远端指针，不动本地分支和工作区
git status -sb                       # 预期显示 diverged
git log --oneline origin/main..HEAD   # 检查本地有没有未推送的提交
```

> ⚠️ **下面这条会永久丢弃本地未提交改动和上面列出的本地提交**，执行前确认没有
> 要保留的东西。有本地提交就先 `git format-patch origin/main..HEAD -o <项目外的目录>`
> 导出，reset 后再 `git am` 应用（注意 patch 里若含敏感内容，等于又复制了一份）。

```bash
git reset --hard origin/main
git log --oneline -1                 # 应与远端一致
git status -sb                       # 干净、且跟踪 origin/main
```

**不要用 `git pull`**：会报 non-fast-forward；若配了 `pull.rebase=true`，还可能把
旧提交重放上去，把已经从历史里删掉的数据重新推回远端。

旧 commit 对象仍会留在本机 `.git/objects` 里（`git log <旧SHA>` 还能翻出来），
`fetch / reset` 不会删它们。要真正清掉：

```bash
git branch -a                        # 有别的分支/标签指向旧 commit 就先删掉
git reflog expire --expire=now --all
git gc --prune=now
```

只要还有一个引用指向旧 commit，`gc` 就清不掉。没有本地运行数据的机器，最省事
的做法是改名旧目录后重新 `git clone`。

**多台机器**：每台各做一次；任何一台在旧历史上 `git push --force` 都会让旧历史
复活。已经 fork 的仓库不受这些命令影响，需要在平台上同步或重建 fork。

> `git filter-repo` 会移除 `origin` remote。重写完的机器要
> `git remote add origin <仓库地址>` + `git fetch origin` +
> `git branch --set-upstream-to=origin/main main`，否则后续 `git push` 会报
> 没有 upstream。
>
> **重写 ≠ 删除**：强推之后，托管平台上的旧 commit 页面 / API / 按旧 SHA fetch
> 仍可能取到旧对象（平台保留不可达对象与缓存）。要彻底消除公开泄露只能删仓库
> 重建或转 private，并联系平台清除缓存对象。
