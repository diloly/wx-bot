// injector.cpp —— 把 hookdll.dll 注入到目标进程 (WeChat.exe)
//
// 编译 (MSVC):
//     cl /O2 /EHsc injector.cpp /link user32.lib advapi32.lib /out:injector.exe
// 编译 (MinGW-w64):
//     g++ -municode -static injector.cpp -o injector.exe -luser32 -ladvapi32
//     注意: 入口是 wmain, 必须加 -municode 否则链接器找不到入口会报 WinMain 未定义
//
// 用法:
//     injector.exe WeChat.exe path\to\hookdll.dll
//
// 说明: 本注入器用最基础的 CreateRemoteThread+LoadLibraryW。
//       它"能用但特征明显"(CreateRemoteThread 是 EDR/反注入常见检测点)。
//       自研的价值就在这里: 你之后可以把注入换成更隐蔽的方式
//       (APC 注入 / SetWindowsHookEx / 或微信自己的线程里 LoadLibrary)，
//       本文件只是先把整条链路打通。

#include <windows.h>
#include <tlhelp32.h>
#include <stdio.h>

static DWORD FindProcessId(const wchar_t* exeName) {
    PROCESSENTRY32W pe = { sizeof(pe) };
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return 0;
    DWORD pid = 0;
    if (Process32FirstW(snap, &pe)) {
        do {
            if (_wcsicmp(pe.szExeFile, exeName) == 0) { pid = pe.th32ProcessID; break; }
        } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
    return pid;
}

int wmain(int argc, wchar_t** argv) {
    if (argc < 3) {
        wprintf(L"usage: injector.exe <WeChat.exe> <path\\to\\hookdll.dll>\n");
        return 1;
    }
    const wchar_t* exe = argv[1];
    const wchar_t* dll = argv[2];

    DWORD pid = FindProcessId(exe);
    if (!pid) {
        wprintf(L"[!] 找不到进程 %s，请确认微信已登录并前台运行\n", exe);
        return 1;
    }

    HANDLE hProc = OpenProcess(PROCESS_ALL_ACCESS, FALSE, pid);
    if (!hProc) {
        wprintf(L"[!] OpenProcess 失败, err=%lu (可能是权限不足, 用管理员运行)\n", GetLastError());
        return 1;
    }

    SIZE_T pathLen = (wcslen(dll) + 1) * sizeof(wchar_t);
    LPVOID remoteMem = VirtualAllocEx(hProc, NULL, pathLen, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!remoteMem) {
        wprintf(L"[!] VirtualAllocEx 失败\n");
        CloseHandle(hProc);
        return 1;
    }
    SIZE_T written = 0;
    WriteProcessMemory(hProc, remoteMem, dll, pathLen, &written);

    HMODULE kernel32 = GetModuleHandleW(L"kernel32.dll");
    LPTHREAD_START_ROUTINE loadLib = (LPTHREAD_START_ROUTINE)GetProcAddress(kernel32, "LoadLibraryW");

    HANDLE hThread = CreateRemoteThread(hProc, NULL, 0, loadLib, remoteMem, 0, NULL);
    if (!hThread) {
        wprintf(L"[!] CreateRemoteThread 失败, err=%lu\n", GetLastError());
        VirtualFreeEx(hProc, remoteMem, 0, MEM_RELEASE);
        CloseHandle(hProc);
        return 1;
    }
    WaitForSingleObject(hThread, INFINITE);
    wprintf(L"[+] 注入完成, 目标进程 pid=%lu\n", pid);

    // DLL 里 DllMain 会自动起管道 + hook；注入成功即代表链路已通
    CloseHandle(hThread);
    VirtualFreeEx(hProc, remoteMem, 0, MEM_RELEASE);
    CloseHandle(hProc);
    return 0;
}
