# 微信客户维护机器人 — 完整运行路径

> 2026-10-13 整理。环境：Weixin 4.1.13.65（RadiumWMPF 架构）+ Python 3.13。
> 路线：收 = 本地数据库解密（只读）；发 = 键盘驱动（非注入）；AI = Ollama 本地模型。

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

1. `pip install wechatauto-replica`（已完成）
2. `wechatauto 体检`（已完成：数据库密钥 20/20、图片密钥 OK；控件树缺失只影响 GUI 发送，不影响本路径）
3. 编辑 `ai_secrets.json`（**AI 配置统一在这一个文件**，含 api_key，不要外发/截图；已加入 .gitignore）：
   - `provider` / `model`：默认 `ollama` + `gemma`（须与 `ollama list` 输出一致）
   - 接自定义/云端大模型时：`provider` 设 `custom`，`base_url` 写全端点（**自带 `/v1`，代码不拼**），`model` 写该服务实际支持的模型名
   - `system_prompt` / `temperature` / `max_tokens`：可选，不填用代码内置默认
   编辑 `config.json`：
   - `auto_reply` 块：白名单 `contacts`、频率闸 `cooldown_seconds` / `max_replies_per_hour`
   - `contacts` 可加 `"wxid"` 字段做精确匹配（可选）
4. 确认 Ollama 在跑：`ollama serve` + `ollama list` 里有 gemma

## 三、日常运行（三个终端）

**终端 A — 看板（常开）**
```
python E:\WorkBuddyData\WechatBot\dashboard.py
```

**终端 B — 收消息 + AI 自动回复（二选一）**
```
# B1 只收不发（监控模式）
python E:\WorkBuddyData\WechatBot\wx_recv_bridge.py

# B2 自动回复闭环。当前 config auto_reply.enabled=false：
#     只收消息监控上屏，不回复任何人。单聊测满意、群控确认后，
#     再把 config.json 的 auto_reply.enabled 改 true：
python E:\WorkBuddyData\WechatBot\auto_reply.py                # dry-run 观察
python E:\WorkBuddyData\WechatBot\auto_reply.py --no-dry       # 真发（抢焦点，慎用）
```

**终端 C — 主动外发（按需）**
```
python E:\WorkBuddyData\WechatBot\wechat_ui.py --inspect
python E:\WorkBuddyData\WechatBot\wechat_ui.py --send --template followup --tag old --no-dry
python E:\WorkBuddyData\WechatBot\wechat_ui.py --send --ai --intent "周末问候" --tag vip --no-dry
python E:\WorkBuddyData\WechatBot\wechat_ui.py --schedule --no-dry
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
  python E:\WorkBuddyData\WechatBot\wx_recv_bridge.py --media-backfill --depth 50
  ```
- **原图模式**（`--original`，仅限手动回填）：图片强制下载 `_h.dat` 真原件（清晰），
  但会**驱动微信界面**（点开气泡、滚动消息列表）——运行时勿操作电脑；
  不想滚界面加 `--no-scroll-original`。轮询路径只走本地解密档，绝不碰界面。
- 群聊发送者显示名：**微信群备注优先于昵称**（「马总」优于「马彬」），
  群成员表 5 分钟缓存（`_group_member_name`），刚改的备注下一轮生效。
- **卡片仿微信四段式**：标题（2 行截断）+ 大小/描述 + 右侧类型图标
  （PDF 红 / W 蓝 / X 绿；app 分享用首字母占位）+ 底栏来源（微信电脑版 / 千问APP / 腾讯会议）；
  文件卡点击即下载，链接卡新窗口打开。旧卡片记录重跑一次 `--media-backfill` 即升级为新版式。
- 已知限制：语音（SILK）浏览器不能播，仍显示占位；`.wxgf` 动画表情容器
  不能直接渲染（下载成功也显示占位）；卡片封面/app 图标走 CDN 下载，失败则用类型图标/首字母占位。

## 四、每会话自动回复开关（默认全关）

**核心规则：只有开关打开的会话才会触发 AI 自动回复，默认全部关闭。**

- **在哪管**：看板（`dashboard.py`）左侧会话列表，**每一行右侧一个小开关**，点一下即开/关，
  存 `wechat_monitor.db` 的 `conv_switch` 表，重启不丢。
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
| `rhythm` | natural | 拟人节流档位（natural/calm/fast/off）：两次发送最小间隔 + 连发 6 条强制冷却 30–75s。与 cooldown/每小时上限叠加取严；状态落盘 `~/.wechatauto/rhythm.json`，auto_reply 与 wechat_ui 手动外发共享节流计数 |
| `cooldown_seconds` | 30 | 同一会话回复冷却 |
| `max_replies_per_hour` | 20 | 全局每小时上限（防风控） |
| FAILSAFE | 开 | 鼠标甩到屏幕左上角立即急停 |

## 六、聊天记录备份（export_history.py）

```
python E:\WorkBuddyData\WechatBot\export_history.py                    :: 全量导出 backups/history_时间戳.json
python E:\WorkBuddyData\WechatBot\export_history.py --user wxid_xxx    :: 只导指定会话
python E:\WorkBuddyData\WechatBot\export_history.py --limit 500        :: 每会话上限
```

- wechatauto 的 `export_history` 封装；输出含**客户聊天内容**，属敏感数据：
  `backups/` 已在 .gitignore，不要外发/截图/传公网。
- 与 `wechat_monitor.db`（收发同库、含程序发送记录）互补：这是微信侧的原始底档。

## 七、销售人设与产品知识库（AI 回复的「大脑」）

### 三层 prompt 结构

```
system = 身份纪律（被问才承认AI，没问不主动提）+ 销售人设 + 聊天风格 + 产品知识（检索注入）+ 输出纪律
user   = 客户称呼/标签/背景备注 + 本次沟通意图 + 最近对话上下文
```

- **身份纪律**（内置模板，合规红线）：AI 生成内容须可识别身份——被问到
  「你是机器人吗/是AI吗」**必须坦然承认**（示例话术在模板里），不冒充真人、
  不编造真人状态（不说自己在外跑客户/开会）；**客户没问就不主动提**；
  承认后拉回价值（响应快、资料准），把 AI 身份说成优势。
- **人设可配**：`config.json` 的 `ai.persona`（默认「小光」）和 `ai.product`（默认「空间设计 SaaS 平台」）。
- **完全接管 prompt**：`ai_secrets.json` 写 `system_prompt`（非空即整体覆盖内置模板；
  当前已清空走模板）。非保密的 prompt 推荐写在 `config.json` 的 `ai.system_prompt`。

### 产品知识库 knowledge/

- **一个 `.md` 文件 = 一个主题**，`## 小节` 是检索单元；`_` 开头是说明文件不进库。
- **检索注入**：客户消息进来时，按「客户原话 + 沟通意图 + 客户标签」二元切词，
  取相关度最高的 `knowledge.top_k`（默认 3）个小节注入 system prompt。
  **相关度为 0 的小节不注入**——闲聊时不硬塞产品资料。
- **改完即生效**：按 mtime 自动重载，不用重启 `auto_reply.py`。
- **防编造**：prompt 明令资料里没有的（价格细节/合同/上线时间）统一答
  「我确认一下再回复你」，不许编功能、编价格、编案例。
- 初始模板 `knowledge/产品总览.md` 里全是「待填」，**填成真实内容前 AI 只会按
  「需人工确认」话术应对产品问题**——别让它拿空资料瞎说。
- 拓展路线（front-matter 标签过滤 → 向量检索 → 资料库批量入库 → 缺口反馈闭环）
  见 `knowledge/_说明.md`，按需逐步上，不要一步到位。

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

写操作三道闸（默认最严）：① dry-run（看板「互动管理」面板开关
`__moment_dry__`，默认模拟）；② 每日上限 `max_actions_per_day`（默认 10）；
③ 库内置 rhythm 连发冷却。定位不到/超上限一律拒绝。
**放行标准 = 开关 / 人工点击本身**：2026-10-16 移除 `config.moment.watch`
白名单——按客户开关就是人工确认；白名单是早于开关设计的第二道闸，属冗余，
还造成过「开了开关却被静默跳过」的困惑。自动模式同样只认开关。

配置在 `config.json` 的 `moment` 块（max_actions_per_day / ai_comment）；
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
- **同一用户连发多条只算一条任务**：第一条在 `submit()` 里受理的瞬间就更新同会话
  冷却窗口（`cooldown_seconds`，默认 30s），同一轮里后面的消息自然被挡掉。闸门与
  计数全部在**投递时**判完（不放到发送成功后），否则同一轮里同一用户连发 3 条会
  全部通过、排成 3 条任务。

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

## 九、验收清单（按顺序过）

1. `dashboard.py` 起来，浏览器开 http://localhost:5000
2. 跑 `auto_reply.py`（当前 enabled=false），日志应显示「仅收消息监控上屏，不回复任何人」
3. 用另一个号给微信发一条消息 → 10 秒内看板出现**对方气泡 + 正文**（`[store] +`）；群消息带「昵称: 」前缀
4. 自己在 PC 微信**手动发**的消息同样上屏（右侧「我」气泡）；程序发的不会双渲染；图片/表情/语音等显示 `[图片 md5=...]` 等占位，不露 XML
5. 单聊确认无误后，把 `config.json` 的 `auto_reply.enabled` 改 `true`，重跑 → dry-run 日志出现「已开启自动回复的会话 N 个」；在看板把目标会话行右侧开关打开 → 该会话来消息时 dry-run 日志出现 `[auto] 生成回复 → ...`
6. `--no-dry` 真发成功，看板出现「我」的气泡
7. 连发多条 → 日志出现「冷却中/已达上限」跳过，闸门生效
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
    - 让两个客户在同一轮里各来一条消息 → 应出现两条 `已受理`，AI 线程**连续**
      生成（第二条 `生成回复` 不必等第一条 `已发送`）
    - 同一用户连发 3 条 → 只出现 1 条 `已受理`，另两条日志为「冷却中」
    - 把 `auto_reply.task_timeout_seconds` 临时改成 5 测试任务级超时：应出现
      `任务超时未生成` 或 `任务超时未发出`，且**不影响后续任务**（流水线继续跑）
    - 朋友圈轮询期间给微信发消息 → 消息仍应在 10 秒内被受理（朋友圈不再卡主循环）

## 十、走过的弯路（结论备忘，防回头）

- DLL 注入/hook：按老架构设计，本机新版微信无本地消息库，架构性错误，勿再投入。
- xlog 解密：TEA 密钥推导错，115 块全失败；PyWxDump 路线：`wxdump info` 报 WeChat No Run。
- UIA 读屏：Qt 自绘层不暴露控件（除非热激活）；读消息最终走 wechatauto-replica 解密本地库。

## 十一、风控与合规（始终有效）

- 键盘驱动仍违反微信 ToS，有封号风险；频率闸 + 冷仅是软防护，不是豁免。
- 只维护已知客户；禁止无差别加陌生人、禁止群发骚扰营销。
