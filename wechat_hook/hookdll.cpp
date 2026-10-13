// hookdll.cpp —— 注入到微信后: hook SQLite 写入, 通过命名管道把"收消息"明文回传 Python
//
// 编译 (MSVC):
//     cl /O2 /EHsc /LD hookdll.cpp /link user32.lib advapi32.lib /out:hookdll.dll
// 编译 (MinGW-w64):
//     g++ -shared -static hookdll.cpp -o hookdll.dll -luser32 -ladvapi32
//
// 本文件不依赖 MinHook, 自己实现 x64 inline hook (覆盖目标前 14 字节为绝对跳转)。
//
// 思路 (无需 x64dbg 逆向任何微信内部函数):
//   微信收到消息后, 一定会把明文写进本地 SQLite 库 (Msg.db / MicroMsg.db ...)。
//   写入走 sqlite3 的公开 API: prepare -> bind -> step。我们 hook 这一串,
//   在 sqlite3_step 实际执行 "INSERT" 时, 把 SQL + 绑定参数 (消息正文/发送者)
//   通过命名管道回传给 Python, 再写进 monitor_store, 看板自动刷新。
//   SQLite 加解密发生在 pager 层, sqlite3 API 这层看到的是明文, 与微信版本无关。
//
//   注: 若目标进程把 SQLite 静态编进主模块且未导出符号, GetProcAddress 会拿不到,
//   此时 DllMain 会把导出表里含 "sqlite3" 的符号名全部 pipe 出来 (见 [export] 行),
//   据此再补签名扫描即可, 不必上 x64dbg 手逆。

#include <windows.h>
#include <tlhelp32.h>
#include <cstdio>
#include <cstring>
#include <cctype>
#include <string>
#include <vector>
#include <map>

static HANDLE g_pipe = INVALID_HANDLE_VALUE;
static CRITICAL_SECTION g_cs;

// ---------------- 简易 x64 inline hook ----------------
// 覆盖目标函数头部 14 字节为: FF 25 00 00 00 00 + 8字节绝对地址 (jmp [rip])
// trampoline = 原14字节 + jmp 回 target+14, 供原函数继续调用。
// 限制: 简化版假定目标头部 14 字节可整体搬移 (多数函数成立)。
//       严谨实现需反汇编处理 rip 相对指令, 此处用于打通链路。
struct HookCtx {
    BYTE* target = nullptr;
    BYTE  orig[14] = { 0 };
    BYTE* trampoline = nullptr;
};

static BYTE* BuildTrampoline(BYTE* target, const BYTE* saved, size_t n) {
    BYTE* t = (BYTE*)VirtualAlloc(nullptr, n + 14, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!t) return nullptr;
    memcpy(t, saved, n);
    t[n] = 0xFF; t[n + 1] = 0x25;            // jmp [rip]
    *(DWORD*)(t + n + 2) = 0;
    *(BYTE**)(t + n + 6) = target + n;        // 跳回 target+n
    return t;
}

static bool InstallHook(HookCtx& h, BYTE* target, BYTE* replacement) {
    h.target = target;
    DWORD oldProtect = 0;
    if (!VirtualProtect(target, 14, PAGE_EXECUTE_READWRITE, &oldProtect)) return false;
    memcpy(h.orig, target, 14);
    h.trampoline = BuildTrampoline(target, h.orig, 14);
    if (!h.trampoline) { VirtualProtect(target, 14, oldProtect, &oldProtect); return false; }
    BYTE jmp[14] = { 0xFF, 0x25, 0x00, 0x00, 0x00, 0x00 };
    *(BYTE**)(jmp + 6) = replacement;
    memcpy(target, jmp, 14);
    VirtualProtect(target, 14, oldProtect, &oldProtect);
    return true;
}

// ---------------- 管道回传 ----------------
static void PipeSend(const char* msg) {
    if (g_pipe == INVALID_HANDLE_VALUE) return;
    DWORD w = 0;
    WriteFile(g_pipe, msg, (DWORD)strlen(msg), &w, nullptr);
    WriteFile(g_pipe, "\n", 1, &w, nullptr);   // 行分隔, 方便 Python 按行读
}

// ---------------- 工具 ----------------
static std::string WToUtf8(const wchar_t* w, int lenChars) {
    if (!w || lenChars == 0) return "";
    if (lenChars < 0) lenChars = (int)wcslen(w);
    int n = WideCharToMultiByte(CP_UTF8, 0, w, lenChars, nullptr, 0, nullptr, nullptr);
    if (n <= 0) return "";
    std::string s(n, 0);
    WideCharToMultiByte(CP_UTF8, 0, w, lenChars, &s[0], n, nullptr, nullptr);
    return s;
}

static bool IcContains(const std::string& hay, const char* needle) {
    std::string low = hay;
    for (auto& c : low) c = (char)::tolower((unsigned char)c);
    return strstr(low.c_str(), needle) != nullptr;
}

// ---------------- SQLite API 类型 ----------------
// x64 下单调用约定, cdecl/stdcall 一致, 用默认即可。
typedef int (*PFN_prepare16_v2)(void* db, const void* zSql, int nSql, void** ppStmt, const void** pzTail);
typedef int (*PFN_step)(void* stmt);
typedef int (*PFN_bind_text16)(void* stmt, int iCol, const void* zText, int nText, void(*xDel)(void*));
typedef int (*PFN_bind_text)(void* stmt, int iCol, const void* zText, int nText, void(*xDel)(void*));

static PFN_prepare16_v2 g_origPrepare = nullptr;
static PFN_step         g_origStep    = nullptr;
static PFN_bind_text16  g_origBind16  = nullptr;
static PFN_bind_text    g_origBind    = nullptr;

static HookCtx g_hkPrepare, g_hkStep, g_hkBind16, g_hkBind;

// 每个被 prepare 出来的 INSERT 语句: 记录 SQL + 按列收集绑定文本
struct StmtInfo { std::string sql; std::vector<std::string> binds; };
static std::map<void*, StmtInfo> g_stmts;   // key = sqlite3_stmt*

// ---------------- 示例 hook: MessageBoxW (心跳/链路自检) ----------------
typedef int (WINAPI* PFN_MessageBoxW)(HWND, LPCWSTR, LPCWSTR, UINT);
static PFN_MessageBoxW g_origMessageBoxW = nullptr;
static HookCtx g_hkMsg;

static int WINAPI HookedMessageBoxW(HWND h, LPCWSTR text, LPCWSTR cap, UINT type) {
    char buf[1024];
    snprintf(buf, sizeof(buf), "[hook] MessageBox text=%ls", text ? text : L"");
    PipeSend(buf);
    return g_origMessageBoxW(h, text, cap, type);
}

// ---------------- SQLite hooks ----------------
static int HookedPrepare16(void* db, const void* zSql, int nSql, void** ppStmt, const void** pzTail) {
    int r = g_origPrepare(db, zSql, nSql, ppStmt, pzTail);
    if (ppStmt && *ppStmt && zSql) {
        std::string sql = WToUtf8((const wchar_t*)zSql, nSql < 0 ? -1 : nSql / 2);
        if (IcContains(sql, "insert")) {
            EnterCriticalSection(&g_cs);
            if (g_stmts.size() > 4096) g_stmts.clear();   // 防泄漏兜底
            g_stmts[*ppStmt] = StmtInfo{sql, {}};
            LeaveCriticalSection(&g_cs);
        }
    }
    return r;
}

static int HookedBind16(void* stmt, int iCol, const void* zText, int nText, void(*xDel)(void*)) {
    if (zText) {
        std::string t = WToUtf8((const wchar_t*)zText, nText < 0 ? -1 : nText / 2);
        if (!t.empty()) {
            EnterCriticalSection(&g_cs);
            auto it = g_stmts.find(stmt);
            if (it != g_stmts.end()) {
                if ((int)it->second.binds.size() < iCol) it->second.binds.resize(iCol);
                it->second.binds[iCol - 1] = t;
            }
            LeaveCriticalSection(&g_cs);
        }
    }
    return g_origBind16(stmt, iCol, zText, nText, xDel);
}

static int HookedBind(void* stmt, int iCol, const void* zText, int nText, void(*xDel)(void*)) {
    if (zText) {
        int n = (nText < 0) ? (int)strlen((const char*)zText) : nText;
        std::string t((const char*)zText, n);
        if (!t.empty()) {
            EnterCriticalSection(&g_cs);
            auto it = g_stmts.find(stmt);
            if (it != g_stmts.end()) {
                if ((int)it->second.binds.size() < iCol) it->second.binds.resize(iCol);
                it->second.binds[iCol - 1] = t;
            }
            LeaveCriticalSection(&g_cs);
        }
    }
    return g_origBind(stmt, iCol, zText, nText, xDel);
}

static int HookedStep(void* stmt) {
    int r = g_origStep(stmt);
    EnterCriticalSection(&g_cs);
    auto it = g_stmts.find(stmt);
    if (it != g_stmts.end()) {
        std::string sql   = it->second.sql;
        std::string binds = "";
        for (size_t i = 0; i < it->second.binds.size(); i++) {
            if (i) binds += " || ";
            binds += it->second.binds[i];
        }
        g_stmts.erase(it);
        LeaveCriticalSection(&g_cs);
        std::string msg = "[MSG] SQL=" + sql + " BINDS=" + binds;
        PipeSend(msg.c_str());
    } else {
        LeaveCriticalSection(&g_cs);
    }
    return r;
}

// ---------------- 导出表自诊 + 模块发现 ----------------
// 枚举模块导出表里含 "sqlite3" 的符号, 顺便打印 [mod] 头。返回是否含任意 sqlite3 符号。
static bool EnumSqliteExports(HMODULE mod, const char* tag) {
    ULONG_PTR base = (ULONG_PTR)mod;
    IMAGE_DOS_HEADER* dos = (IMAGE_DOS_HEADER*)base;
    if (!dos || dos->e_magic != IMAGE_DOS_SIGNATURE) return false;
    IMAGE_NT_HEADERS* nt = (IMAGE_NT_HEADERS*)(base + dos->e_lfanew);
    if (!nt || nt->Signature != IMAGE_NT_SIGNATURE) return false;
    DWORD expVA = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXPORT].VirtualAddress;
    if (!expVA) return false;
    IMAGE_EXPORT_DIRECTORY* exp = (IMAGE_EXPORT_DIRECTORY*)(base + expVA);
    DWORD* names = (DWORD*)(base + exp->AddressOfNames);
    DWORD* funcs = (DWORD*)(base + exp->AddressOfFunctions);
    WORD*  ords  = (WORD*)(base + exp->AddressOfNameOrdinals);
    bool has = false;
    for (DWORD i = 0; i < exp->NumberOfNames; i++) {
        const char* name = (const char*)(base + names[i]);
        if (name && strstr(name, "sqlite3")) {
            if (!has) {
                char hdr[256];
                snprintf(hdr, sizeof(hdr), "[mod] %s @ %p exports=%lu", tag, (void*)mod, exp->NumberOfNames);
                PipeSend(hdr);
                has = true;
            }
            BYTE* addr = (BYTE*)(base + funcs[ords[i]]);
            char buf[256];
            snprintf(buf, sizeof(buf), "[export] %s!%s @ %p", tag, name, addr);
            PipeSend(buf);
        }
    }
    return has;
}

// 扫描主模块映像, 找 SQLite 版本串 (如 "3.39.4"), 方便后续按版本定位 sqlite3_step
static void ScanSqliteVersionString() {
    HMODULE exe = GetModuleHandleA(NULL);
    if (!exe) return;
    ULONG_PTR base = (ULONG_PTR)exe;
    IMAGE_DOS_HEADER* dos = (IMAGE_DOS_HEADER*)base;
    if (!dos || dos->e_magic != IMAGE_DOS_SIGNATURE) return;
    IMAGE_NT_HEADERS* nt = (IMAGE_NT_HEADERS*)(base + dos->e_lfanew);
    if (!nt || nt->Signature != IMAGE_NT_SIGNATURE) return;
    DWORD size = nt->OptionalHeader.SizeOfImage;
    const BYTE* img = (const BYTE*)base;
    int found = 0;
    for (DWORD i = 0; i + 8 < size && found < 10; i++) {
        if (img[i] == '3' && img[i+1] == '.' && isdigit(img[i+2])) {
            DWORD j = i + 3;
            while (j < size && isdigit(img[j])) j++;
            if (j < size && img[j] == '.' && isdigit(img[j+1])) {
                DWORD k = j + 2;
                while (k < size && isdigit(img[k])) k++;
                int len = (int)(k - i);
                if (len >= 5) {
                    std::string s((const char*)(img + i), len);
                    char buf[256];
                    snprintf(buf, sizeof(buf), "[sqlite-version] %s @ +0x%X", s.c_str(), i);
                    PipeSend(buf);
                    found++;
                    i = k;   // 跳过已匹配, 避免重复
                    continue;
                }
            }
        }
    }
    if (found == 0) PipeSend("[sqlite-version] 未找到明显版本串");
}

static void InstallSqliteHooks(BYTE* pPrepare, BYTE* pStep, BYTE* pBind16, BYTE* pBind) {
    if (InstallHook(g_hkPrepare, pPrepare, (BYTE*)HookedPrepare16))
        g_origPrepare = (PFN_prepare16_v2)g_hkPrepare.trampoline;
    if (InstallHook(g_hkStep, pStep, (BYTE*)HookedStep))
        g_origStep = (PFN_step)g_hkStep.trampoline;
    if (pBind16 && InstallHook(g_hkBind16, pBind16, (BYTE*)HookedBind16))
        g_origBind16 = (PFN_bind_text16)g_hkBind16.trampoline;
    if (pBind && InstallHook(g_hkBind, pBind, (BYTE*)HookedBind))
        g_origBind = (PFN_bind_text)g_hkBind.trampoline;
}

// 枚举当前进程所有模块, 在导出表里找 sqlite3_* 并装 hook。
static void DiscoverSqliteAndHook() {
    bool hooked = false;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, GetCurrentProcessId());
    if (snap != INVALID_HANDLE_VALUE) {
        MODULEENTRY32 me; me.dwSize = sizeof(me);
        if (Module32First(snap, &me)) {
            do {
                HMODULE m = me.hModule;
                if (EnumSqliteExports(m, me.szModule)) {
                    BYTE* pPrepare = (BYTE*)GetProcAddress(m, "sqlite3_prepare16_v2");
                    BYTE* pStep    = (BYTE*)GetProcAddress(m, "sqlite3_step");
                    BYTE* pBind16  = (BYTE*)GetProcAddress(m, "sqlite3_bind_text16");
                    BYTE* pBind    = (BYTE*)GetProcAddress(m, "sqlite3_bind_text");
                    if (pPrepare && pStep && !hooked) {
                        InstallSqliteHooks(pPrepare, pStep, pBind16, pBind);
                        hooked = true;
                    }
                }
            } while (Module32Next(snap, &me));
        }
        CloseHandle(snap);
    }
    if (hooked) {
        PipeSend("[+] SQLite hook 已安装 (prepare/step/bind)");
    } else {
        PipeSend("[!] 没有任何模块导出 sqlite3_* (疑似静态编入主模块, 需要特征码扫描定位)");
        ScanSqliteVersionString();
    }
}

// ---------------- DLL 入口 ----------------
BOOL APIENTRY DllMain(HMODULE hModule, DWORD ul_reason_for_call, LPVOID lpReserved) {
    if (ul_reason_for_call == DLL_PROCESS_ATTACH) {
        InitializeCriticalSection(&g_cs);

        // 1. 起命名管道服务端 (Python bridge 作为客户端连接)
        g_pipe = CreateNamedPipeW(
            L"\\\\.\\pipe\\wechat_hook_pipe",
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE,
            1, 8192, 8192, 0, nullptr);
        if (g_pipe != INVALID_HANDLE_VALUE) ConnectNamedPipe(g_pipe, nullptr);

        PipeSend("[+] hookdll 已加载 (SQLite 收消息捕获模式)");

        // 2. 枚举进程所有模块的导出表找 sqlite3_*, 找到即装 hook; 否则扫版本串兜底
        DiscoverSqliteAndHook();

        // 4. 心跳 hook (验证链路; 微信弹框时会在 bridge 出现 [hook] 行)
        HMODULE u = GetModuleHandleW(L"user32.dll");
        if (u) {
            BYTE* target = (BYTE*)GetProcAddress(u, "MessageBoxW");
            if (target) {
                if (InstallHook(g_hkMsg, target, (BYTE*)HookedMessageBoxW))
                    g_origMessageBoxW = (PFN_MessageBoxW)g_hkMsg.trampoline;
            }
        }
    } else if (ul_reason_for_call == DLL_PROCESS_DETACH) {
        if (g_pipe != INVALID_HANDLE_VALUE) {
            DisconnectNamedPipe(g_pipe);
            CloseHandle(g_pipe);
            g_pipe = INVALID_HANDLE_VALUE;
        }
        DeleteCriticalSection(&g_cs);
    }
    return TRUE;
}
