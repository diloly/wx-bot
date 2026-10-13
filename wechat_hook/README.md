# wechat_hook —— 自研微信 PC 注入/hook 框架（非 wcferry）

自己搓轮子：注入 DLL + x64 inline hook 微信内部函数，把消息明文通过命名管道回传
Python，再接回现有看板 / AI 发送逻辑。**不依赖任何第三方微信逆向库。**

---

## 为什么自研而不是用 wcferry
- wcferry 是一个**已被广泛标记**的特征化 DLL，注入即触发客户端反作弊的
  "第三方模块"检测，这正是它易封号的根因。
- 自研让你**完全控制检测面**：注入方式、加载时机、常驻 artifacts、字符串特征
  全都自己定，可以把"被识别为外挂"的概率压到最低（但仍非零，见风险）。

---

## 目录
- `injector.cpp` 注入器（CreateRemoteThread + LoadLibraryW，最基础，可换更隐蔽方式）
- `hookdll.cpp`  被注入的 DLL：自写 x64 inline hook + 命名管道回传
- `bridge.py`    Python 侧：连管道、读回传数据

---

## 编译（需要 C/C++ 工具链，二选一）
### MSVC (Visual Studio 自带)
```
cl /O2 /EHsc injector.cpp /link user32.lib advapi32.lib /out:injector.exe
cl /O2 /EHsc /LD hookdll.cpp /link user32.lib advapi32.lib /out:hookdll.dll
```
### MinGW-w64
```
g++ injector.cpp -o injector.exe -static -luser32 -ladvapi32
g++ -shared -static hookdll.cpp -o hookdll.dll -luser32 -ladvapi32
```

---

## 跑通链路（SQLite 收消息捕获，无需 x64dbg）
1. 打开微信 PC（Qt 版 `Weixin.exe`），登录，前台运行。
2. 启动 Python 桥（等连接 + 自动写库）：
   ```
   python bridge.py
   ```
3. 另开终端注入 DLL：
   ```
   injector.exe Weixin.exe C:\完整路径\hookdll.dll
   ```
4. bridge 会先收到：
   ```
   [+] hookdll 已加载 (SQLite 收消息捕获模式)
   [export] Weixin.exe!sqlite3_step @ 0x...
   [+] SQLite hook 已安装 (prepare/step/bind)
   ```
   - 若看到 `[export] Weixin.exe!sqlite3_step`，说明 SQLite 从主模块导出，hook 直接生效。
   - 若 **没有** `[export] sqlite3_*` 行、且最后一行是 `[!] 未解析到 sqlite3_step`，
     说明本机微信把 SQLite 静态编进主模块且未导出 → 需要补一段「特征码扫描」定位
     `sqlite3_step`（见下方"静态链接兜底"），这一步仍需你本机做，但只是填一个字节特征，
     不必跟调用栈手逆解密回调。
5. 让微信收到一条测试消息（自己用另一个号发一条）。bridge 出现：
   ```
   [MSG] SQL=INSERT INTO ChatMsg(...) BINDS=wxid_xxx||你好...
   [store] + [wxid_xxx] 你好...
   ```
   看板 http://localhost:5000 对应会话即出现这条 `them` 消息，收信息链路打通。

> 收到 `[MSG]` 后，bridge 已自动按「发送者 wxid / 群 id」归到对应会话、正文取最长文本字段。
> 若归类或正文不对（schema 因微信版本而异），把 `recv_raw.log` 里的 SQL 贴给我，
> 我把 `handle_msg` 的解析按真实列顺序调准即可，无需改 C++。

---

## 静态链接兜底（仅当上面没有 `[export] sqlite3_*` 时才需要）
若微信未导出 sqlite3 符号，hookdll 当前会报告「未解析到」。补法（在你本机）：
1. 用 x64dbg 在 `Weixin.exe` 里搜 `sqlite3_step` 的字节特征（SQLite 该函数的 prologue
   跨版本较稳定），把特征码填进 hookdll.cpp 的 `ScanForSqlite()`（已留 TODO 位置），
   扫到地址后照常 InstallHook。这一步只是"找函数地址"，不需要理解消息结构体。
2. 或者：装一个会把 SQLite 单独放 `sqlite3.dll` 的微信版本/补丁，hookdll 会优先走
   `sqlite3.dll` 导出，零改动生效。

---

## 规避检测（方向性，行为层；不提供具体反调试 bypass）
- **注入方式**：CreateRemoteThread 特征明显，可换 APC 注入 / 线程劫持 /
  利用微信自身加载时机，降低被反注入扫描命中的概率。
- **常驻特征**：DLL 文件名、管道名 `wechat_hook_pipe`、导出表、字符串，都要改成不显眼的。
- **行为节奏**：hook 内只做"拷贝 + 回传"，不要在回调里做重活；发送节奏用现有
  `send_delay_seconds` 限速，模拟人。
- **最小化 footprint**：按需加载、用完可卸载，避免长期常驻可疑模块。

---

## 风险与边界
- 仍违反《微信软件许可及服务协议》，账号仍可能被限制——自研只是把"被识别概率"降低，
  不是免疫。
- 逆向 / hook 需要你本机动态调试能力（x64dbg/IDA），偏移随微信版本变，我无法凭空给出。
- 合规底线：只维护已知客户，禁止无差别加陌生人 / 群发骚扰。
- 本框架仅做"读取明文 + 回传"，不内置任何绕过反调试的具体实现。
