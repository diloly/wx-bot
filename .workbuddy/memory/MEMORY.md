# 项目长期笔记

## WechatBot 项目
- 目标：个人微信自动化「加好友 + 定时发信息维护客户」。
- 已确认路线：**个人微信账号，不使用企业微信**。
- 技术底座：**PC 端键盘驱动（非注入）**，不用 wcferry（用户认为其注入 DLL 易封号）。
  关键事实（2026-10-12 实测）：用户装的微信 PC 是 **Qt 渲染版**（窗口类 `Qt51514QWindowIcon`、子窗口 `MMUIRenderSubWindowHW`），**不暴露 Windows 原生控件**，故 uiautomation/pywinauto 按名找控件这条路**走不通**（ui_dump.txt 仅 3 行）。已改为只给前台窗口发快捷键（Ctrl+F 搜索→Ctrl+V 粘贴名字→Enter 进会话→Ctrl+V 粘贴消息→Enter 发送），用 ctypes 定位窗口、pyautogui 发键、pyperclip 粘贴中文。
- 安卓 uiautomator2 / iOS Appium+WDA 为备选（Android 暴露真实 View 树，UIA 类方案在安卓反而可行）。
- AI 外发内容生成：ai_client.py（OpenAI 兼容接口，provider 可切 ollama/deepseek/openai/qwen，默认 ollama；API key 走 config 或 env WECHAT_AI_API_KEY）。范围仅「AI 生成外发文案」：给定 intent + 联系人(name/tags/可选 note) 调 LLM 出文案，复用键盘发送。未做「读入站消息自动回」（Qt 微信读不了聊天内容，需 OCR/本地库，与用户非注入偏好冲突）。
- **客户端架构（2026-10-13 实测，权威）**：用户当前装的是**新版 Weixin（RadiumWMPF 架构）**，不是老版 `WeChat.exe`。证据链：`Weixin.exe` 仅 **3MB**，加载的是 VLC 播放器插件 + `mmmojo_64.dll`(Mojo IPC) + `wxplayer.dll`，是**外壳/宿主进程**；真正聊天 UI 跑在 `WeChatAppEx.exe`（Chromium webview 运行时，进程列表里一堆）。磁盘上**没有**经典 `Msg.db` 本地 SQLite 消息库；消息走 webview 渲染 + 服务端拉取，本地仅有 `xwechat\radium\web\profiles\*\IndexedDB`(LevelDB) 缓存。
- **重要结论**：之前「hook SQLite 收消息」整套方案是按**老版 WeChat.exe 架构**设计的，对本机**架构上走不通**——本机消息不走本地 SQLite，`sqlite3_step` 不是消息存储层，且当初注入的 `Weixin.exe` 是**错进程（外壳）**。该路线作废。
- **读消息新候选（待验证，优先）**：Chromium webview 暴露**无障碍树**（与老 Qt 版「ui_dump 仅 3 行」不同），故 **UIA/无障碍读屏** 重新成为可行候选（非侵入、无 hook、无逆向）。下一步：写 `ui_dump2.py`，在用户聚焦某聊天窗口时 dump 前台窗口 a11y 树，确认消息文本可读 + 设计增量去重。另备选项：解析 `xwechat\log\mm_*.xlog`（自定义日志格式，需先解 xlog）、或 CDP 注入 webview（需改启动参数、侵入高）。
- config 联系人支持可选 `note` 字段（背景备注，喂给 LLM 做个性化）。定时任务支持 `ai:true` + `intent` 字段。
- 网页看板：dashboard.py（Flask，左会话列表 + 右仿微信气泡，2s 轮询刷新）；数据底座 monitor_store.py（wechat_monitor.db，收发同库）。收件由 ingest.py 从 PyWxDump 解密后的微信本地库批量导入；发件由键盘驱动实时写回。auto_sync.py 常驻每 60s 重新解密 + 导入，实现「零手动」刷新。ingest 已做去重（微信 MsgSvrID 或内容 hash）+ 跳过 role=me（避免与我方实时记录重复），重复导入不翻倍。
- 已知硬限制：无官方 API；**PC 加好友受限**（Qt 版无可靠键盘流程发好友申请，加好友需回手机端）；每日加人软上限约 20–30；行为风控仍在（与方式无关，靠限速+SOP）。
- 合规：仅维护已知客户，禁止无差别加陌生人/骚扰营销。
- 合规：仅维护已知客户，禁止无差别加陌生人/骚扰营销。
