# WechatBot 命令速查卡

> 全部命令在 Windows 本机执行；键盘发送期间微信需已登录、保持前台、不锁屏。
> 工作目录：`E:\WorkBuddyData\WechatBot`（下面都用绝对路径，任意目录可直接粘贴运行）。

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
python E:\WorkBuddyData\WechatBot\dashboard.py
```
浏览器打开 http://localhost:5000

### 终端 B —— 收消息（二选一）

B1 只收不发（监控模式）：
```
python E:\WorkBuddyData\WechatBot\wx_recv_bridge.py
```

B2 收消息 + AI 自动回复（当前 `config.json` 的 `auto_reply.enabled=false`：只监控上屏、不回复任何人；单聊测满意后把它改 `true` 再跑）：
```
python E:\WorkBuddyData\WechatBot\auto_reply.py
python E:\WorkBuddyData\WechatBot\auto_reply.py --no-dry      :: 真发（会抢占键鼠焦点，慎用）
```

### 终端 C —— 主动外发（按需）
```
python E:\WorkBuddyData\WechatBot\wechat_ui.py --inspect                                       :: 确认微信窗口可被定位
python E:\WorkBuddyData\WechatBot\wechat_ui.py --send --template followup --tag old --no-dry   :: 模板群发（按标签）
python E:\WorkBuddyData\WechatBot\wechat_ui.py --send --ai --intent "周末问候" --tag vip --no-dry  :: AI 文案外发
python E:\WorkBuddyData\WechatBot\wechat_ui.py --schedule --no-dry                             :: 定时任务常驻（config.tasks）
```

---

## 2. 媒体渲染（图片/表情/视频/文件/卡片）

```
:: 历史消息补渲染（一次性，幂等可重跑；旧卡片记录升级也靠它）
python E:\WorkBuddyData\WechatBot\wx_recv_bridge.py --media-backfill --depth 50

:: 原图模式：图片强制下载 _h.dat 真原件（清晰，但会驱动微信界面点开气泡/滚动列表）
:: ⚠️ 运行时请勿操作电脑；不想滚界面就加 --no-scroll-original
python E:\WorkBuddyData\WechatBot\wx_recv_bridge.py --media-backfill --depth 50 --original
```
新收到的媒体消息由 bridge/auto_reply 自动下载渲染（普通档，不碰界面），无需手动。

---

## 2.5 聊天记录备份（export_history.py）

```
:: 全量导出 JSON 到 backups/history_时间戳.json
python E:\WorkBuddyData\WechatBot\export_history.py

python E:\WorkBuddyData\WechatBot\export_history.py --user wxid_xxx        :: 只导指定会话（可重复 --user）
python E:\WorkBuddyData\WechatBot\export_history.py --limit 500            :: 每会话最多 500 条
```
导出文件含客户聊天内容，属敏感数据：`backups/` 已在 .gitignore，不要外发/截图/传公网。

---

## 2.6 朋友圈（读自动 / 写按开关）

```
:: 读——新圈 + 未读赞/评论通知，自动落库，看板「朋友圈」页展示（左栏列表，点开看详情）
:: 自动：终端 B 跑着 auto_reply.py 就会每约 60s 拉一次，无需额外命令
python E:\WorkBuddyData\WechatBot\moment_ops.py --poll      :: 手动立刻拉一轮
python E:\WorkBuddyData\WechatBot\moment_ops.py --refresh   :: 手动 UI 刷新（切到朋友圈页并点工具栏「刷新」按钮逼客户端同步，刷完就停在朋友圈页）
python E:\WorkBuddyData\WechatBot\moment_ops.py --list      :: 看库里的最近新圈 + 互动通知（拿 tid 用）

:: 写——点赞/评论（默认 dry 只验证「能定位到」，绝不误点；真执行必须 --no-dry）
python E:\WorkBuddyData\WechatBot\moment_ops.py --like <tid> --no-dry           :: 真点赞
python E:\WorkBuddyData\WechatBot\moment_ops.py --unlike <tid> --no-dry         :: 取消点赞
python E:\WorkBuddyData\WechatBot\moment_ops.py --comment <tid> "评论内容" --no-dry  :: 真评论（不可逆）
python E:\WorkBuddyData\WechatBot\moment_ops.py --ai-comment <tid>              :: 小光按销售人设生成评论文案（只生成不发送）
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

## 3. 调试 / 验证

```
python E:\WorkBuddyData\WechatBot\wx_recv_bridge.py --once        :: 只轮询一轮后退出（快速看全量结果）
python E:\WorkBuddyData\WechatBot\auto_reply.py --interval 5      :: 自定义轮询间隔（秒）
python -u E:\WorkBuddyData\WechatBot\wx_recv_bridge.py            :: 无缓冲输出（排查"终端卡住"观感）
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
| 只看到自己的消息 | 首轮没跑完就被 Ctrl+C 了（自测会话排最前）；重跑等 `[poll]` 汇总行出现 |

---

风控与合规：键盘驱动仍违反微信 ToS，有封号风险；频率闸（`merge_window_seconds` 合并窗口 / `max_replies_per_hour` / `rhythm` 拟人节流）只是软防护。只维护已知客户，禁止无差别加陌生人、群发骚扰。
