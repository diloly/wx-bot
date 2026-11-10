# WechatBot 命令速查卡

> 全部命令在 Windows 本机执行；键盘发送期间微信需已登录、保持前台、不锁屏。
> 工作目录：`<项目根目录>`（下面都用绝对路径，任意目录可直接粘贴运行）。
>
> ⚠️ **解释器必须用 `python`**：依赖（requests / wechatauto-replica /
> flask 等）都装在这个 conda 环境里。直接敲 `python` 会落到系统 Python，报
> `ModuleNotFoundError: No module named 'requests'`。下文所有 `python` 均指该解释器，
> PowerShell 里可先 `Set-Alias python python`。
>
> **最简启动（只看看板，只读、不碰微信 UI）**：
> ```
> cd <项目根目录>
> & "python" dashboard.py
> ```
> 浏览器打开 http://localhost:5000 （新版 Vue 看板），`/legacy` 是旧版回退。
> 不需要 npm / node：前端构建产物 `static/dist/` 已入库；只有改前端才需要 `npm run build`。

---

## 0. 一次性准备（装好/体检过后可跳过）

```
pip install wechatauto-replica
wechatauto 体检
ollama serve                  :: 确认本地大模型在跑
ollama list                   :: 确认列表里有 gemma（须与 ai_secrets.json 的 model 一致）
```

---

## 1. 日常运行（三个终端）

### 终端 A —— 看板（常开）
```
python <项目根目录>\dashboard.py
```
浏览器打开 http://localhost:5000

### 终端 B —— 收消息（二选一）

B1 只收不发（监控模式）：
```
python <项目根目录>\wx_recv_bridge.py
```

B2 收消息 + AI 自动回复（当前 `config.json` 的 `auto_reply.enabled=false`：只监控上屏、不回复任何人；单聊测满意后把它改 `true` 再跑）：
```
python <项目根目录>\auto_reply.py
python <项目根目录>\auto_reply.py --no-dry      :: 真发（会抢占键鼠焦点，慎用）
```

### 终端 C —— 主动外发（按需）
```
python <项目根目录>\wechat_ui.py --inspect                                       :: 确认微信窗口可被定位
python <项目根目录>\wechat_ui.py --send --template followup --tag old --no-dry   :: 模板群发（按标签）
python <项目根目录>\wechat_ui.py --send --ai --intent "周末问候" --tag vip --no-dry  :: AI 文案外发
python <项目根目录>\wechat_ui.py --schedule --no-dry                             :: 定时任务常驻（config.tasks）
```

---

## 2. 媒体渲染（图片/表情/视频/文件/卡片）

```
:: 历史消息补渲染（一次性，幂等可重跑；旧卡片记录升级也靠它）
python <项目根目录>\wx_recv_bridge.py --media-backfill --depth 50

:: 原图模式：图片强制下载 _h.dat 真原件（清晰，但会驱动微信界面点开气泡/滚动列表）
:: ⚠️ 运行时请勿操作电脑；不想滚界面就加 --no-scroll-original
python <项目根目录>\wx_recv_bridge.py --media-backfill --depth 50 --original
```
新收到的媒体消息由 bridge/auto_reply 自动下载渲染（普通档，不碰界面），无需手动。

---

## 2.5 聊天记录备份（export_history.py）

```
:: 全量导出 JSON 到 backups/history_时间戳.json
python <项目根目录>\export_history.py

python <项目根目录>\export_history.py --user wxid_xxx        :: 只导指定会话（可重复 --user）
python <项目根目录>\export_history.py --limit 500            :: 每会话最多 500 条
```
导出文件含客户聊天内容，属敏感数据：`backups/` 已在 .gitignore，不要外发/截图/传公网。

---

## 2.6 朋友圈（读自动 / 写按开关）

```
:: 读——新圈 + 未读赞/评论通知，自动落库，看板「朋友圈」页展示（左栏列表，点开看详情）
:: 自动：终端 B 跑着 auto_reply.py 就会每约 60s 拉一次，无需额外命令
python <项目根目录>\moment_ops.py --poll      :: 手动立刻拉一轮
python <项目根目录>\moment_ops.py --refresh   :: 手动 UI 刷新（切到朋友圈页并点工具栏「刷新」按钮逼客户端同步，刷完就停在朋友圈页）
python <项目根目录>\moment_ops.py --list      :: 看库里的最近新圈 + 互动通知（拿 tid 用）

:: 写——点赞/评论（默认 dry 只验证「能定位到」，绝不误点；真执行必须 --no-dry）
python <项目根目录>\moment_ops.py --like <tid> --no-dry           :: 真点赞
python <项目根目录>\moment_ops.py --unlike <tid> --no-dry         :: 取消点赞
python <项目根目录>\moment_ops.py --comment <tid> "评论内容" --no-dry  :: 真评论（不可逆）
python <项目根目录>\moment_ops.py --ai-comment <tid>              :: 小光按销售人设生成评论文案（只生成不发送）
```
写操作也可以在看板「朋友圈」页点按钮触发。

**互动管理面板（看板「朋友圈」页右栏默认视图）**：
- **「模拟 / 真跑」开关**：默认模拟——点赞/评论只验证「能定位到目标圈」，不真点不发；演练无误后关掉它切真跑。这是朋友圈独立开关，与 `auto_reply.py --no-dry`（消息回复）互不影响。
- **「自动互动（按客户）」**：每个客户「赞 / 评」两个小开关，默认全关。打开后该客户**之后新发**的圈自动点赞 / 自动评论（文案由小光按销售人设生成）。只对开后新到的圈生效，不补赞旧圈；开了开关的客户即使暂时没有新圈也会留在面板里。
- **放行标准就是开关本身**：2026-10-16 起移除 `config.moment.watch` 白名单——按客户开关即人工确认，不再有第二道名单闸（此前「开了开关却被白名单静默跳过」的困惑不再存在）。

配置在 `config.json` 的 `moment` 块：

| 键 | 含义 |
|---|---|
| `max_actions_per_day` | 每日点赞+评论总上限。**0 = 不限次数**（当前默认 0）。2026-10-16 用户取消了固定 10 次——正常人朋友圈大一点也不止 10 个，节奏控制交给 `moment.refresh_interval`/`rhythm` 连发冷却。填正数即恢复硬上限 |
| `ai_comment` | 是否允许用 AI 生成评论 |
| `refresh_interval` | 隔多少秒用 UI 路线刷新一次朋友圈（默认 180；**0=关闭自动刷新**，只读库里已有的数据） |
| `refresh_dwell` | 每次刷新在朋友圈页停留几秒等客户端同步（默认 4） |

**关于「实时」（2026-10-16 实测结论）**：纯解密库读**做不到实时**——微信 PC 只在用户点朋友圈页工具栏的「刷新」按钮时才把新圈同步进本地库（光切页停留不够，时间线仍是旧数据）。现在 auto_reply 按 `refresh_interval` 定期自动切到朋友圈页并**点「刷新」**催客户端同步，把「实时」从「手动」变成「最长差一个刷新周期」。刷新会短暂抢占焦点（几秒），介意就调大 interval 或设 0 后自己偶尔点开一下朋友圈。

**自动流程的步骤逻辑（2026-10-16 二次重构，用户拍板）**：定时刷新（切朋友圈页 → 点「刷新」→ 停留）→ **停在朋友圈页**读库 → 有开关的新圈直接在此定位点赞 → **全程不回聊天页**。唯一的「回聊天页」发生在发消息前一步：`send_to_contact` 在 Ctrl+F 之前自带切页，**只有真收到聊天消息、要发消息时才走**，所以既没有多余操作，微信停在朋友圈页也不影响发消息。聊天消息入库走解密库轮询、不需要刷新，与朋友圈流程完全解耦。

写操作三道闸：dry-run（默认模拟）→ 每日上限（`max_actions_per_day`，**0=不限**）→ 连发冷却；另有两个前置条件——① 库的 UIA 导航要求微信主窗口在前台，写操作前会自动把微信提前台（已在前台则跳过；找不到窗口会重试几次）；② 必须停在朋友圈页，写操作前会自动确认（被切回过聊天就重新导航，`SwitchToMoments` 已在页面时是空转）。

**光标轨迹全程拟人（2026-10-16）**：库的点赞链路里「点『…』」「点浮层『赞/评论』」「滚时间线前的落位」原本是 `pyautogui` 的瞬时/直线移动（脚本签名）；`moment_ops._human_cursor()` 会在一次写操作内临时把这些接管成 `rhythm.move_to`（二次贝塞尔 + 末段减速），用完还原。所以点赞时能看到光标**平滑划过去**而不是闪现。

**排障**：开了自动开关却没反应？看 auto_reply 终端——① 开关只对开后**新到**的圈生效，历史圈不会补赞，让客户现发一条测；② 定位失败会打原因（曾出现「滚轮落点不属于微信 (0,0)」= 页面不在朋友圈，现已自动导航修复）；③ 注意朋友圈 dry-run 与 `auto_reply.py --no-dry`（消息回复）是两个独立开关。

---

## 2.7 并发：抢 UI 只有一个出路——排队（ui_gate.py）

微信 PC **只有一份 UI**（一个主窗口、一份键鼠焦点）。会抢前台的动作只有这四类：键盘发送、朋友圈刷新、点赞/评论、页面导航。它们必须全机串行，否则互相打断——历史事故：`Ctrl+F` 发进看板浏览器；朋友圈刷新到一半被消息发送切回聊天页，定位在聊天页白跑（日志 `滚轮落点不属于微信 (0,0)`）。

`ui_gate.py` 把这件事收敛成一条规则：**同一时刻只有一个操作能碰微信，其余排队等，不丢弃**。

| 谁 | 什么时候抢 UI | 单次持有时长（约） |
|---|---|---|
| `auto_reply.py` 发 AI 回复 | 收到消息且该会话开关打开时 | 4–8s |
| `auto_reply.py` 朋友圈刷新 | 每 `moment.refresh_interval` 秒 | 8–15s（含 dwell） |
| `auto_reply.py` 自动点赞/评论 | 开关客户发新圈时，**逐条**抢 | 3–8s / 条 |
| 看板「发送」按钮 | 点击时 | 4–8s |
| 看板点赞/评论按钮 | 点击时 | 3–8s |

- **跨进程**：`dashboard.py` 与 `auto_reply.py` 是两个进程，排的是**同一队**（Windows 命名互斥体）。持锁进程崩溃 / 被 Ctrl+C 时，系统直接把锁交给下一个等待者——不会留下解不开的死锁。
- **AI 请求不占 UI**：生成文案是网络请求，全部在门外做完再进门发送；朋友圈自动互动也是先把一批文案生成好、再逐条进门执行。否则一个慢请求会把整队卡住。
- **逐条而非整批**：自动点赞一批 10 条是「一条一进门」，中间让出 UI，消息回复能插进来排队——不会因为一批点赞把微信独占一两分钟。

**日志怎么看**：

| 日志 | 含义 |
|---|---|
| `[ui] 发送给 XX：排队 3.2s 后取得 UI` | 正常排队，说明锁在干活 |
| `[ui] 检测到上一个进程异常退出留下的锁，已自动接管` | 上个进程被强杀过，已自愈 |
| `等待微信 UI 空闲超时` | 排队等太久了（见 2.8 的任务超时） |

配置在 `config.json`：

| 键 | 含义 |
|---|---|
| `ui.lock_timeout_seconds` | 没带任务预算时的兜底排队上限（默认 180） |

自动回复与自动点赞的排队上限默认**不用**这个值，而是用各自**任务的剩余时间预算**（见 2.8）——这样一条任务不会因为前面排了别人就无限期挂着。看板手动按钮没有任务概念，用 `ui.lock_timeout_seconds`，超时返回明确报错，可以再点一次。

---

## 2.8 任务流水线与任务级超时（auto_reply.py）

**流水线（2026-10-16 起）**：四个线程各司其职，谁都不等谁。

| 线程 | 干什么 |
|---|---|
| 主线程 | 轮询收消息 → 判定闸门 → 建任务入队（**立刻返回，不等 AI**） |
| AI 线程 | 串行消费待生成队列，**一条 AI 请求完成就立刻发起下一条**，不等上一条发出去 |
| 发送线程 | 独立消费待发送队列，在 UI 门内逐条发出 |
| 朋友圈线程 | 定期刷新朋友圈 + 按开关自动点赞/评论（不再卡住收消息） |

为什么生成与发送要拆开：发送要抢微信 UI（几秒）。原来「生成 A → 发送 A → 生成 B」里，这几秒白白占着 AI 的等待窗口。现在 AI 只管生成、发送只管发。**顺序仍有保证**——AI 线程单线程、按 FIFO 消费，产出的发送队列顺序就是入队顺序。

**任务级超时**：一条「回复任务」＝**一个用户的一次回复**（不是一条消息、也不是一次 AI 请求），从受理到消息发出算一条，带自己的时间预算 `auto_reply.task_timeout_seconds`（默认 120s）。预算覆盖 AI 生成 + 排队 + 发送三段，任何一段耗尽就作废并记日志。朋友圈同理：一条「赞+评」是一条任务。

| 配置（config.json） | 含义 |
|---|---|
| `auto_reply.task_timeout_seconds` | 单条回复任务的时间预算（默认 120） |
| `auto_reply.queue_max` | 待生成/待发送队列上限（默认 200），满了拒收新任务并记日志 |
| `moment.task_timeout_seconds` | 单条朋友圈互动任务的时间预算（默认 120） |

**合并窗口（debounce，2026-10-16 起，替代旧的 `cooldown_seconds`）**：客户常分两三条才说完一个意思，逐条发 AI 只会答半句话。所以收到消息**先不建任务**——攒进该会话的合并缓冲，**静默 `auto_reply.merge_window_seconds`（默认 8s）不再来新消息**，才把这几条**拼成一条任务**交给 AI。要点：

- 窗口期内每来一条就把计时往后推（所以「手快每隔 5s 发一条」也会一直等，直到停手 8s）；
- 这几条一起进任务账本（看板可见），AI 拿到的是完整一段意思；
- 这就是「已处理到哪条」的记录：一提交就清空缓冲；**回复发出后又来消息** → 进新缓冲 → 再等一个窗口回下一轮；
- 旧的 `cooldown_seconds`（按「距上次建任务不足 N 秒就跳过」判）**已废弃**：它会把「回复发出后几秒用户补的一句」直接丢掉，而合并窗口已保证「用户没停手就不回复」，无需再叠冷却。

**日志顺序**：`收到 X 第 N 条（8s 无新消息后合并提交）` → `合并 N 条为一条任务 → X（预算 120s，待生成队列 N 条）` → `生成回复 → X` → `发送 X（后面还排着 N 条）`。超时会是 `任务超时未生成` / `任务超时未发出` / `发送排队超时`。

**线程与 COM（2026-10-16）**：三个 worker 线程都由 `_worker_entry` 统一入口启动，入口里先给**本线程**做 CoInitialize（`_thread_com_init`）。原因：UIAutomation 是 COM 组件，Windows 要求每个使用它的线程各自初始化；发送（切回聊天页）与朋友圈（控件树）都会碰 UIA，此前它们跑在主线程时被顺手初始化过，搬进独立线程后就报了 `尚未调用 CoInitialize`——那一次定位/切页其实是**静默失败**的。线程退出时 `Uninitialize`。

---

## 2.9 维护任务队列的可观测（看板「维护任务」，2026-10-16）

`auto_reply.py` 的两个内存队列（待生成 / 待发送）**只是内存**——它和看板是两个进程，看板看不到。所以每条任务同时落库到 `reply_task` 表，作为**状态账本**：

- 内存 `queue.Queue` 仍是真正的工作队列（worker 靠它阻塞取值、保证 FIFO）；
- `reply_task` 表只负责「看得见」：看板侧栏底部「维护任务」条 + 点开看最近 30 条。

**状态机**（一条回复任务的完整生命周期）：

```
pending（已受理）→ generating（AI 生成中）→ generated（待发送）
                 → sending（发送中）→ sent（已发送）
任一环节失败/超时 → failed（失败）或 expired（作废，预算耗尽/UI 排队超时/重启残留）
```

**在哪看**：`dashboard.py` 侧栏底部「维护任务」条，显示 `进行中 N · 失败 N · 作废 N`；点击展开最近 30 条（状态徽章 + 客户 + 时间 + AI 文案/失败原因）。后端 `GET /api/tasks`（只读）。**不做取消/插队**——只看，不控。

**重启清理**：`auto_reply.py` 启动时把上次运行残留的未完成任务标为 `expired`（原因「进程重启，任务作废」）。内存队列随进程一起没了，不清理的话看板会永远显示一堆假的「进行中」。**不做补跑**——旧回复已过时，补跑还可能与新收消息的任务重叠（同一客户被回两次）。

**失败不自动重试**（2026-10-16 定）：回复有时效，`task_timeout_seconds` 已兜住「过期即作废」；发送不可逆，自动重试有重复发送风险。失败在面板上直接可见，交人工判断。

**账本写失败不影响业务**：`_task_open` / `_task_set` 内部吞异常只记日志——它只管可观测，不能因为它坏了就不回消息。

---

## 2.10 批量加好友（看板操作，2026-10-19）

**推荐路径：全在看板里做**，不用敲命令。打开 http://localhost:5000 →
左侧底部「加好友」条 → 展开：

1. **勾选「自动加好友」**（开关落库，`auto_reply` 每 20 秒读一次，**不用重启**）；
2. **上传 CSV** 或**直接粘贴**（一行一个手机号/微信号）；
3. 面板里直接看：今日 x/10、排队 N、已通过 N、每条的状态与原因。

命令行只留两个用途：

```
python friend_add.py --list               :: 终端看队列与统计
python friend_add.py --one 13800138000    :: 手工加一个（调试，不受限速）
```

**CSV 列**：`keyword`（必填，手机号或微信号）、`name`、`note`、`source`。
`note` 不是可选项——首条激活话术的唯一信息源，没它 AI 只能写套话。
粘贴时不带表头也认（每行一个号码，或 `keyword,name,note,source` 逗号分隔）。

**默认节奏**：每日 10 条、间隔 8~15 分钟随机、9:00–21:00、周末额度 ×0.5。
改默认值改 `config.json` 的 `add_friend`；**临时停/开用看板开关**。
`add_friend.enabled` 只是「库里没设过时的默认值」——看板点过之后以库里为准。

**它怎么工作**：`auto_reply.py` 起第 4 个 worker（`add-friend-worker`），
到点取一条 `pending` → UI 门内执行「搜索 → 网络查找 → 添加到通讯录 → 填入 → 确定」
→ **点完就走，不等对方通过**，继续下一条。

**状态**（`friend_task` 表）：
```
pending（排队） → sent（申请已发出） → passed（对方通过）
                    ├→ expired（7 天未通过，启动时清理）
                    └→ rejected（对方拒绝）
pending → already（对方已在通讯录，跳过）/ failed（同一号码累计 3 次技术失败）
```

**「未通过」不是失败**：状态停在 `sent`，不占队列、不阻塞。
`failed` 只表示这一次技术执行挂了（搜不到人/找不到按钮/界面异常）；
同一号码累计 3 次 `failed` 才落终态。**连续 3 条都技术失败 → 当天停手**（大概率被限流/改版）。

**通过检测**（不需要 UIA 轮询「新的朋友」页）：
库里的系统消息文案命中 `我通过了你的朋友验证请求` / `你已添加了` / `以上是打招呼的消息`，
且该会话在「发申请之前没有任何消息」→ 判定为刚通过的新好友（匹配回 `sent` 的名单项）。

**首条激活（= 自动自我介绍）**——谁会收到、什么时候收到：

| 场景 | 会不会发 | 触发点 |
|---|---|---|
| 加好友申请被通过（在名单里） | 会，随机等 60~120s 后发**一条** | `_maybe_friend_pass()` 排期 |
| 已通过但首条从没真正发出（历史漏发/发送失败） | 会，启动时**补排**一次 | `_backfill_pass_events()` 第二步 |
| 通过后客户自己先开口了 | **不发**，取消排期，交给正常自动回复 | `collect()` → `_cancel_first_touch` |
| 手动加的人 / 来源不明（名单里匹配不上） | **不发** | 只认 `status='sent'` 的名单项 |
| 会话自动回复开关关着 | 不发（跳过，日志有「首条激活跳过」） | `_tick_first_touch()` |

- 一批人同时通过时按 `ft_delay_min~max` **依次错峰**，不挤在同一分钟（避免群发观感）。
- 通过时会**自动打开该会话的自动回复开关**（否则客户主动说话也没人接）。
- **系统消息不进 AI 缓冲**——「我通过了你的朋友验证请求」在库里 raw type 是「文本」，
  不拦的话 AI 会去回这句话。
- 只发一条，不追问；客户不回就停在那，后续交给「主动触达」（`outreach`）。

**自我介绍写什么**（改文案不用动代码）：

- 正文在 `config.json` → `add_friend.first_touch_intent`（当前已填，2026-11-06 版）；
  留空则退回 `auto_reply.py` 的 `_first_touch_intent()` 内置兜底，两处口径一致。
- 三句结构：**①自报身份**（小光 + 中照金智 + A站干什么的，一句话）→
  **②说明来意**（优先吃名单里的 `note`/`source`，没有就泛到行业，不编对方信息）→
  **③给价值点 + 低压钩子**（「需要的话我发份介绍给您看看」），总长 80 字内。
- 三条红线：**卖 SaaS 工具不是接设计项目**（不说“帮您做设计/代出方案”）；
  不报价/不承诺效果与中标率/不催单；**不主动说自己是 AI**（问起才承认）。
- 参考话术（AI 会照意思改写，不照抄）：
  「您好，我是中照金智的小光，我们做 AI 照明设计工具 A站，主要帮设计师快速出照明效果图和方案汇报物料。看您这边做照明，需要的话我发份介绍给您看看？」
- 身份字段统一在 `config.json` → `ai`（`persona/company/product/positioning`），
  日常回复和首条自我介绍共用，改一处全局生效。

---

## 2.11 消息页只列已托管的会话（2026-11-06）

以前左栏把**所有会话**都列出来（几十上百个），既看不出哪些真在托管，也没法用。
现在：

- **「消息」页只显示已开启托管的会话**，每行右侧开关点一下即关，关掉立刻从列表消失
  （聊天记录不受影响）。
- **开新的会话**：点搜索框右侧的 **「+」** → 弹层里挑：
  - 搜索框输**名字 / 备注 / wxid** 过滤；
  - 分类 chips：`全部` /`群聊 N` /`私聊 N`（内置，按 conv_id 判定）；
  - `#标签 N`：来自 `config.json` 的 `contacts`（`name` 或 `wxid` 匹配 + `tags`），
    和 auto_reply 用的是同一份配置，改完刷新页面即生效；
  - 点一行 → 开启托管，该会话立刻出现在左侧列表；`Esc` 或点遮罩关闭弹层。
- 接口：`GET /api/conversations` 每项新增 `tags`（数组）；开关仍走 `POST /api/switch`。

**头像（2026-11-06）**：微信 `contact` 表里有 `small_head_url`（`wx.qlogo.cn` 的 CDN
地址），实测**不需要登录态就能 GET**，所以走「库取 URL → 下载落地 → 本地返回」，
不截屏、不调客户端、不碰 UI 锁。

- **落地**：`wechat_media/avatars/<URL 的 md5>.<ext>`，登记在 `wechat_monitor.db` 的
  `avatars` 表（`conv_id / file / src / ts`）。换头像 URL 就变，天然可更新。
- **同步时机**：`dashboard.py` 启动同步一次、之后每 10 分钟一轮；`wx_recv_bridge.py`
  启动时也同步一次。首轮 51 个头像约 13 秒，之后几乎零开销（无变化直接跳过）。
- **渲染**：左栏列表、`+` 选择器、会话头部；消息气泡**只在私聊**显示双方头像
  （群聊只有一张群头像，一人一个反而错位）。图片加载失败自动回退「名字首字」色块，不留破图。
- **接口**：`GET /api/avatar/<conv_id>`（404 = 没头像，前端兜底）；
  `/api/conversations` 每项带 `avatar_url`（含 `?v=<ts>` 缓存键）和 `me`（本机 wxid）。

**通讯录（2026-11-09；2026-11-10 修正好友判据）**：左栏和选择器原来只看
`conversations`（聊过的会话），加了好友却从没聊过的人根本不存在于看板。现在
`wx_recv_bridge.sync_contacts()` 把微信 `contact.db` 全量同步进 `contacts` 表，
`kind` = friend / group / official / system / **stranger**。
看板用 `GET /api/contacts` 取全量（合并会话状态、托管开关、头像），
选择器里「未聊过」的会打标记。同步时机与头像相同（启动 + 每 10 分钟）。

**「好友」怎么判**（旧版只按 username 形态，把群成员全标成 friend，选择器里混进一堆
没加过好友的人，主动触达甚至可能给他们发消息）：微信 4.x `contact.local_type`——
`1`=通讯录好友、`2`=未存通讯录的群聊、`3`=**群中的陌生人**、`5`=企业微信好友、
`6`=陌生企业微信好友；`delete_flag` 排除已删好友，`verify_flag & 8` 认公众号，
`flag & 3` 兜底（好友 flag 1/3，陌生人 0/4，群 2）。本机实测 56 条 =
friend 13 / group 11 / official 16 / stranger 11 / system 5。
- `GET /api/contacts` 只返回 **friend + group**（还排除本机自己）；前端「好友」chip
  严格按 kind，不再有「公众号」分类。
- `_handoff_candidates()`（接管通知对象 / 活动触达对象）同样只认 friend + group；
  `get_activation_targets()` 的 revive 和 icebreak 两条 SQL 都用 `kind='friend'`。
- 判据跨版本漂移时，把误判的 wxid 填进 `config.contact_sync.friend_whitelist`
  手动拉回好友。
- **升级后必须重启 dashboard.py**：旧进程每 10 分钟仍会用旧判据跑一次同步，
  把 `kind` 覆盖回 friend。

---

## 2.12 主动触达 / 存量激活（2026-11-09 重做）

两套入口，**同一份规则、同一份额度**（实现在 `outreach_core.py`，别再各写一份）：

| 入口 | 何时用 | 怎么开 |
|---|---|---|
| **常驻触达**（auto_reply 后台线程） | 日常。每 5 分钟巡检一次，命中就发一条 | `config.outreach.enabled=true`（默认已开），**不用敲命令** |
| **手动激活**（activate.py） | 临时想多发几条、或先看文案 | 敲命令 |

### 常驻触达：全部走 config.json 的 `outreach` 块

```json
"outreach": {
  "enabled": true,                 // 总开关（看板也能开关，5 分钟内生效）
  "daily_limit": 0,                 // 0=不设每日上限（靠下面的间隔节流）
  "per_run_limit": 10,              // activate.py 单次最多几条，防手滑
  "interval_min_seconds": 600,     // 两条之间最少隔 10 分钟
  "interval_max_seconds": 1800,    // 最多隔 30 分钟
  "work_start_hour": 8,            // 只在 8:00-21:00 发（夜间绝不发）
  "work_end_hour": 21,
  "silent_days": 7,                // 多久没聊算「长久没聊」
  "max_days": 0,                   // 0=不限；设 60 就只碰 60 天内聊过的
  "min_gap_days": 7,               // 同一个人 7 天内不重复碰（2026-11-10 改为 7 天 1 次）
  "cycle_days": 0,                 // 0=不启用；设 7 则整体每 7 天只跑一轮（更狠的全局降频）
  "max_per_contact": 3,            // 同一个人累计最多被自动碰 3 次，之后不再碰
  "lookback_days": 180,            // 拼「上次聊到」时往回看多久
  "include_new_contacts": false,   // 加了好友从没聊过的，要不要破冰（默认否）
  "require_switch": false          // true=只碰看板上开了托管的会话
}
```

**改完必须重启 auto_reply**（config 只在启动时读；`enabled` 例外，看板开关走库）。

### 手动激活：参数默认全取 config，不用手写

```bash
python activate.py                        # 只列候选（零成本，不调 AI）
python activate.py --preview              # 生成话术给你看（会调 AI，不发送）
python activate.py --send                 # 按 config 的条数真发
python activate.py --send --limit 3       # 临时改条数
python activate.py --send --icebreak      # 临时把「没聊过」的一起破冰
```

取值优先级：**命令行 > config.json > 代码兜底**，所以日常都不用带参数。

### 两类话术（自动判断）

- **唤回（revive）**：聊过、现在沉默 → 承接上次聊到的**具体内容**（提示词禁止
  「最近怎么样」这类空话）；取不到可承接内容就跳过，不发空泛问候；
- **破冰（icebreak）**：加了好友从没聊过 → 自我介绍 + 价值点 + 低压钩子。

### 安全闸门（全过才发）

只碰真正的好友（`contacts.kind='friend'`，排除群/公众号/系统号/自己）→ 最后一条
距今 ≥ `silent_days` → 同一人 `min_gap_days` 内没被碰过 → 累计 < `max_per_contact`
→（`daily_limit>0` 时）当天 ≤ `daily_limit`，**默认 0 = 不限**
→ 只在 `work_start_hour~work_end_hour` → 两条之间随机等 `interval_min/max_seconds`。

不设日上限之后，节流全靠「10~30 分钟一条 + 8-21 点窗口 + 每会话 7 天冷却 /
累计 3 次」这三道闸；`cycle_days` 可再加一层全局周期。13 小时窗口理论最多约
26~78 条，实际候选通常远少于此。

> **「7 天 1 次」的口径（2026-11-10）**：默认指**每个客户 7 天内最多被碰 1 次**
> （`min_gap_days=7`，由 14 改来）。如果你要的是「整体每 7 天只发一轮」这种更
> 保守的临时降频，把 `cycle_days` 设成 7 即可，主进程会在上次真实触达后
> 7 天内不再碰任何人；两个闸同时开着取更严的那个。

**发完之后**：自动打开该会话的托管开关（否则客户回你没人接），`--no-switch` 可关。
发送结果三态与自动回复一致：回读确认到才记 `sent`，否则记 `failed` 不重试。

---

## 2.13 人工接管（客户有意向 → 通知真人，2026-11-10）

激活/触达发出去之后，客户回的话如果露出明确意向（问报价、要资料、要试用、
想合作、约会议……），机器人继续自动聊容易把线索聊丢。链路是：

```
客户有意向 → 给选定的【群聊 + 私聊（真人）】各发一条内部通知
           → 关掉该客户会话的自动回复（真人在微信里接上）
           → 本次不生成 AI 回复，不抢真人的话
           → 落 handoff_log，同一会话冷却期内不重复通知
```

**去哪配**：看板第 5 个 tab 「人工接管」。勾选通知对象（通讯录里的好友 + 群聊，
可多选、可搜索、可按群聊/私聊过滤），调好参数点「保存配置」——配置写进
`app_setting.handoff_config`，主进程每条消息都重读，**不用重启**。

`config.json` 的 `handoff` 块是默认值，看板保存过的字段优先：

| 字段 | 默认 | 含义 |
|---|---|---|
| `enabled` | true | 总开关 |
| `targets` | `[]` | 通知对象；没配就不触发（不报错、照常自动回复） |
| `watch_scope` | activated | 只判定「近期被激活过」的会话；`all`=所有客户回复 |
| `watch_days` | 7 | 激活后多少天内的回复还在观察窗口 |
| `detector` | ai | `ai`=独立轻量二分类 / `keywords`=纯规则零成本 |
| `cooldown_hours` | 24 | 同一客户多久内只通知一次 |
| `pause_auto_reply` | true | 通知后关掉该会话自动回复 |
| `skip_ai_reply` | true | 本次不自动回客户，留给真人 |

**判定**：`ai` 走 `ai_client.classify()`——不套销售话术、不查知识库、`temperature=0`，
只要一行 `YES|原因`；模型调不通自动降级关键词，绝不把异常抛给回复主流程。
关键词模式拒否词（「不需要」「没兴趣」…）优先于意向词，宁可漏判也不误报。

**通知走的是自动回复同一条发送队列**：UI 独占门、三态回读校验、任务账本全都现成。
`kind='handoff'` 不进 `count_outreach_since`，不占主动触达额度；看板上标「接管通知」。

**合规边界**：默认只对已经被主动激活过的**已知客户**做判定，不是全量聊天内容扫描；
通知只发给你自己勾的内部群/同事。改 `watch_scope=all` 前先确认这是你要的。
**群聊一律不触发**接管（一句 @ 就关掉整个群的自动回复、还往内部群发通知，动静太大）。

接口：`GET/POST /api/handoff/config`、`GET /api/handoff/log`。

---

## 2.14 终端输出：scan 进度条（2026-11-10）

原来每扫一个会话打一行 `[scan] x/y 名字`，加上每轮 `[poll]`，日志每分钟十几行。
现在 `progress_bar.Progress`：

- **终端（TTY）**：`\r` 同一行原地重绘 `scan |████░░░| 12/25 48% · 名字 · 0.3s`，
  收尾固化成一行 `[poll] 扫描 25 会话…新增 N 条`；
- **重定向到日志 / 管道**：中间过程一次都不写，每轮只留一行汇总（`\r` 写进文件
  只会塞满控制字符，更难读）。

循环里 `[store]` / `[media]` 这类有价值的行走 `bar.echo()`：先擦进度条 → 正常打一行
→ 立刻重绘，不会被吃掉。进度条只在 `poll_new(progress=True)` 时启用。

---

## 2.15 活动触达（固定文案 + 指定时间 + 指定对象，2026-11-10）

和 AI 主动触达**不是一回事**，别混：

| | AI 主动触达 | 活动触达 |
|---|---|---|
| 内容 | 模型按上次聊天现写 | **人在看板里写死**，不走 AI |
| 时间 | 8-21 点按沉默/冷却自动挑 | **人指定某个时刻** |
| 对象 | 符合条件的沉默客户 | **人从通讯录勾选**，群聊/私聊都行 |
| 次数 | 常驻、反复 | 一次性，发完收口 |

**怎么用**：看板第 6 个 tab「活动触达」→ 写正文、选 `datetime-local` 时间、勾选对象
→ 创建。到点由 `auto_reply` 的 `campaign-worker` 自动发（**看板只存配置，不碰 UI**，
否则会和主进程抢微信焦点）。

**安全阀**：

- 对象只允许来自通讯录真实存在的好友/群聊（服务端按 conv_id 校验，前端传假的会被丢掉）；
- 一次最多 200 个对象、正文 2000 字；
- **过点宽限**：默认 60 分钟内补发，超时直接标 `expired` 不发——避免隔夜开机突然群发；
- 状态机 `scheduled → running → sent/partial`（条件 UPDATE 抢占，重启不会重投）；
- **同一活动始终只有 1 条在途**，20s 巡检一次逐条投递，几十个对象也不会把
  自动回复的发送队列堵死；
- 每条走 UI 独占门 + 三态回读校验，结果逐个回写 `campaign.result`，失败不自动重试
  （重复打扰客户比漏发一条更糟）；
- 只有 `scheduled` 能「立即发送 / 取消」，任何状态都能删记录。

`auto_reply` 用 `--no-dry` 跑才会真发；dry-run 下会走完整流程但 `send_to_contact`
不落键盘动作，账本仍记成功，联调时注意区分。

接口：`GET/POST /api/campaigns`、`POST /api/campaigns/<id>/send-now`、
`POST /api/campaigns/<id>/cancel`、`DELETE /api/campaigns/<id>`。

---

## 2.16 前端工程：Vue 3 + Element Plus（2026-11-10）

看板从「单文件 HTML + 手写 CSS + 原生 JS」换成前后端源码分离：

| 位置 | 作用 |
|---|---|
| `frontend/` | Vue 3 + Element Plus 源码（Vite，`<script setup>`） |
| `frontend/src/api.js` | 所有 `/api/*` 调用集中封装 |
| `frontend/src/App.vue` | 导航、亮暗主题、2 秒轮询中枢 |
| `frontend/src/views/` | 6 个页面：Chat / Moment / Friends / Tasks / Handoff / Campaign |
| `static/dist/` | 构建产物（app.js / app.css / index.html），**已入库** |
| `templates/index.html` | 旧版单文件看板，保留作回退 |

- **运行时不依赖 node / CDN**：`python dashboard.py` 直接托管 `static/dist/`；
  产物文件名固定不带 hash，每次 build 覆盖同名文件。
- **改前端**：`cd frontend && npm install && npm run build`，不用重启 Flask
  （静态文件实时读盘，浏览器强刷一次即可）。开发模式 `npm run dev` 会把
  `/api`、`/media` 代理到 5000 端口，支持热更新。
- **回退**：访问 `/legacy` 就是旧看板；`static/dist/index.html` 不存在时
  根路由也会自动回退到旧页面。
- **亮暗主题**：右上角开关，偏好存 localStorage；Element Plus dark css-vars
  加自定义 `--wb-*` 设计变量。
- 原生版那些 `pickerSig` / `convSig` / `momentSigs` 手搓 diff 全部删除：
  轮询只改响应式数据，Vue 自己更新 DOM，不会打断输入和滚动位置。
- 接口**一个都没改**：`/api/messages` 仍返回裸数组、`card` 仍是 JSON 字符串、
  `/api/friends/import` 仍同时支持 multipart 与 JSON。

---

## 3. 调试 / 验证

```
python <项目根目录>\wx_recv_bridge.py --once        :: 只轮询一轮后退出（快速看全量结果）
python <项目根目录>\auto_reply.py --interval 5      :: 自定义轮询间隔（秒）
python -u <项目根目录>\wx_recv_bridge.py            :: 无缓冲输出（排查"终端卡住"观感）
python -c "import db_gate; d=db_gate.get_db(); print(d.workdir)"  :: 只读验证解密库能否初始化，打印本进程私有副本目录
python -c "import db_gate; print(db_gate.cleanup_stale())"       :: 清理其他进程遗留 48h+ 的解密副本目录
```

---

## 4. 停止

所有终端 `Ctrl+C`。键盘发送进行中，把鼠标甩到屏幕左上角可立即急停（FAILSAFE）。

---

## 5. 排障速查

| 现象 | 先看这个 |
|---|---|
| 终端像卡住没输出 | 不是卡死：首轮要全量解密全部数据库（分钟级、一次性）。看 `[scan] n/总数 名字` / `[poll]` 进度行；或加 `python -u` |
| 看板没有消息 | ① dashboard 是否在跑；② bridge 终端有没有 `[store] +` 行；③ 启动时的库存统计 `{'me': x, 'them': y}` |
| AI 不回复 | ① `config.json` → `auto_reply.enabled` 是否 `true`；② 看板会话列表**每行右侧开关**是否打开（默认全关）；③ `ai_secrets.json` 的 base_url/api_key；④ 群消息默认不回（`allow_groups=false`） |
| 发送慢/日志有「拟人节流等待」 | 正常：wechatauto rhythm 拟人节流（两次发送最小间隔 + 连发 6 条强制冷却 30–75s）。想调小就改 `config.json` → `auto_reply.rhythm`（natural/calm/fast/off，默认 natural） |
| 群里显示的是昵称不是备注 | 群成员表 5 分钟缓存；刚改的备注下一轮生效 |
| 发送失败 | 微信在前台且未最小化；先跑 `wechat_ui.py --inspect` 能列出窗口 |
| 日志有 `[ui] ...：排队 Ns 后取得 UI` | 正常：微信 UI 同一时刻只服务一个操作，在排队（详见 2.7） |
| 日志有 `等待微信 UI 空闲超时` / `本条未发出` | 有操作把微信占了超过 `ui.lock_timeout_seconds`；先查是不是某个操作卡住了（详见 2.7） |
| 启动时出现 `尚未调用 CoInitialize` / `Can not load UIAutomationCore.dll` | UIAutomation 是 COM，**每个用它的线程都要自己 CoInitialize**。2026-10-16 已在 worker 线程入口统一初始化（`_worker_entry` + `_thread_com_init`）。若仍出现，说明有新的、没走 worker 入口的线程在碰微信 UI——按同样的方式给那个线程初始化即可 |
| 图片/卡片不显示 | 跑一次 `--media-backfill`；看终端 `[media] 下载失败` 行 |
| 看板里**自己发的消息在左边、对方的在右边**，同时发送一直失败/一直重发 | 换设备后 `sender_id` 编号变了（它不是固定 2）。先 `python wx_recv_bridge.py --fix-roles --dry-run` 试算，确认后去掉 `--dry-run` 校正历史数据；代码已改为按 `sender_username` 判定，重启 bridge + auto_reply 即生效。详见 RUNBOOK「十、走过的弯路」 |
| 只看到自己的消息 | 首轮没跑完就被 Ctrl+C 了（自测会话排最前）；重跑等 `[poll]` 汇总行出现 |
| 「+」里出现没加过好友、只在群里见过的人 | `contact.db` 的 contact 表混着群中陌生人（`local_type=3`）。新版 `sync_contacts` 会标 `stranger` 并从选择器/接管/触达里排除；**确认 dashboard.py 已重启到新版**，否则旧进程每 10 分钟同步一次会把 kind 覆盖回去。手动校验：`python -c "import monitor_store,collections; print(collections.Counter(r['kind'] for r in monitor_store.list_contacts()))"` |
| 某个确实是好友的人被排除了 | 把他的 wxid 填进 `config.json` → `contact_sync.friend_whitelist`，重启 dashboard 后同步一次 |
| 启动报 `WeChatDB 初始化失败 ... [WinError 5] 拒绝访问` | 多个进程/多个实例抢同一份解密副本。确认 `dashboard.py` 与 `auto_reply.py` 都重启到新版 `db_gate`；先 `python -c "import db_gate; d=db_gate.get_db(); print(d.workdir)"` 单测能否初始化，再关掉全部 python 进程、清掉 `%TEMP%\wechatauto_db\<账号>\` 下残留的 `.tmp* / -shm / -wal` 重启。详见 RUNBOOK「十、走过的弯路」 |

---

风控与合规：键盘驱动仍违反微信 ToS，有封号风险；频率闸（`merge_window_seconds` 合并窗口 / `max_replies_per_hour` / `rhythm` 拟人节流）只是软防护。只维护已知客户，禁止无差别加陌生人、群发骚扰。
