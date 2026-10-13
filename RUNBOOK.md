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
3. 编辑 `config.json`：
   - `ai.model` = `gemma`（须与 `ollama list` 输出一致）
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

## 四、自动回复安全闸门（config.auto_reply）

| 闸门 | 默认 | 作用 |
|---|---|---|
| `enabled` | false | 总开关：false=只监控上屏不回复（当前状态）；测满意再开 |
| `dry_run` | true | 只打印不真发；`--no-dry` 才发 |
| `reply_types` | ["文本"] | 只回文本，图片/语音不回（type 是中文字符串） |
| `allow_groups` | false | 群消息不自动回 |
| `contacts` | [] | 空=全部会话；填了=白名单（name 或 wxid） |
| `cooldown_seconds` | 30 | 同一会话回复冷却 |
| `max_replies_per_hour` | 20 | 全局每小时上限（防风控） |
| FAILSAFE | 开 | 鼠标甩到屏幕左上角立即急停 |

## 五、验收清单（按顺序过）

1. `dashboard.py` 起来，浏览器开 http://localhost:5000
2. 跑 `auto_reply.py`（当前 enabled=false），日志应显示「仅收消息监控上屏，不回复任何人」
3. 用另一个号给微信发一条消息 → 10 秒内看板出现**对方气泡 + 正文**（`[store] +`）；群消息带「昵称: 」前缀
4. 自己在 PC 微信**手动发**的消息同样上屏（右侧「我」气泡）；程序发的不会双渲染；图片/表情/语音等显示 `[图片 md5=...]` 等占位，不露 XML
5. 单聊确认无误后，把 `config.json` 的 `auto_reply.enabled` 改 `true`，重跑 → dry-run 日志出现 `[auto] 生成回复 → ...`
6. `--no-dry` 真发成功，看板出现「我」的气泡
7. 连发多条 → 日志出现「冷却中/已达上限」跳过，闸门生效

## 六、已作废组件（勿再投入）

- `wechat_hook/` — DLL hook 路线（按老架构设计，本机无本地消息库，架构性错误）
- `xlog_decode.py` — xlog 解密（TEA 密钥推导错，115 块全失败，兔子洞）
- `ingest.py` / `auto_sync.py` / `config.sync` — PyWxDump 路线（wxdump info 报 WeChat No Run）
- `ui_dump*.py` / `wxauto_test.py` — UIA 探测遗留（结论：Qt 自绘层不暴露控件，除非热激活）

## 七、风控与合规（始终有效）

- 键盘驱动仍违反微信 ToS，有封号风险；频率闸 + 冷仅是软防护，不是豁免。
- 只维护已知客户；禁止无差别加陌生人、禁止群发骚扰营销。
