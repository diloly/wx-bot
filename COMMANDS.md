# WechatBot 命令速查卡

> 全部命令在 Windows 本机执行；键盘发送期间微信需已登录、保持前台、不锁屏。
> 工作目录：`<项目根目录>`（下面都用绝对路径，任意目录可直接粘贴运行）。

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

## 3. 调试 / 验证

```
python <项目根目录>\wx_recv_bridge.py --once        :: 只轮询一轮后退出（快速看全量结果）
python <项目根目录>\auto_reply.py --interval 5      :: 自定义轮询间隔（秒）
python -u <项目根目录>\wx_recv_bridge.py            :: 无缓冲输出（排查"终端卡住"观感）
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
| 图片/卡片不显示 | 跑一次 `--media-backfill`；看终端 `[media] 下载失败` 行 |
| 只看到自己的消息 | 首轮没跑完就被 Ctrl+C 了（自测会话排最前）；重跑等 `[poll]` 汇总行出现 |

---

风控与合规：键盘驱动仍违反微信 ToS，有封号风险；频率闸（`cooldown_seconds` / `max_replies_per_hour`）只是软防护。只维护已知客户，禁止无差别加陌生人、群发骚扰。
