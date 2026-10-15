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
      → 键盘驱动（Ctrl+F 搜 → 粘贴名字 → Enter → 粘贴消息 → Enter）
      → 写回 monitor_store(role=me) → 看板上屏
```

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
python <项目根目录>\dashboard.py
```

**终端 B — 收消息 + AI 自动回复（二选一）**
```
# B1 只收不发（监控模式）
python <项目根目录>\wx_recv_bridge.py

# B2 自动回复闭环。当前 config auto_reply.enabled=false：
#     只收消息监控上屏，不回复任何人。单聊测满意、群控确认后，
#     再把 config.json 的 auto_reply.enabled 改 true：
python <项目根目录>\auto_reply.py                # dry-run 观察
python <项目根目录>\auto_reply.py --no-dry       # 真发（抢焦点，慎用）
```

**终端 C — 主动外发（按需）**
```
python <项目根目录>\wechat_ui.py --inspect
python <项目根目录>\wechat_ui.py --send --template followup --tag old --no-dry
python <项目根目录>\wechat_ui.py --send --ai --intent "周末问候" --tag vip --no-dry
python <项目根目录>\wechat_ui.py --schedule --no-dry
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
  python <项目根目录>\wx_recv_bridge.py --media-backfill --depth 50
  ```
- **原图模式**（`--original`，仅限手动回填）：图片强制下载 `_h.dat` 真原件（清晰），
  但会**驱动微信界面**（点开气泡、滚动消息列表）——运行时勿操作电脑；
  不想滚界面加 `--no-scroll-original`。轮询路径只走本地解密档，绝不碰界面。
- 群聊发送者显示名：**微信群备注优先于昵称**（「某总」优于「本名」），
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
python <项目根目录>\export_history.py                    :: 全量导出 backups/history_时间戳.json
python <项目根目录>\export_history.py --user wxid_xxx    :: 只导指定会话
python <项目根目录>\export_history.py --limit 500        :: 每会话上限
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

## 八、验收清单（按顺序过）

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

## 九、走过的弯路（结论备忘，防回头）

- DLL 注入/hook：按老架构设计，本机新版微信无本地消息库，架构性错误，勿再投入。
- xlog 解密：TEA 密钥推导错，115 块全失败；PyWxDump 路线：`wxdump info` 报 WeChat No Run。
- UIA 读屏：Qt 自绘层不暴露控件（除非热激活）；读消息最终走 wechatauto-replica 解密本地库。

## 十、风控与合规（始终有效）

- 键盘驱动仍违反微信 ToS，有封号风险；频率闸 + 冷仅是软防护，不是豁免。
- 只维护已知客户；禁止无差别加陌生人、禁止群发骚扰营销。
