#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终端单行进度条（给轮询扫描用）。

为什么单独写一个小模块
----------------------
原来 `poll_new()` 每扫一个会话就 print 一行 `[scan] x/y 名字`，25 个会话 ×
每 10 秒一轮 = 日志里每分钟十几行，真正有用的信息被淹掉。这里改成用 `\r`
在同一行原地重绘，像训练进度条那样。

两种环境两种行为（很重要）
------------------------
- **TTY（人盯着终端）**：每扫一个会话重绘一次，结束时把这一行固化成汇总。
- **非 TTY（重定向到 auto_reply.log / 管道）**：`\r` 只会让日志文件里塞满
  控制字符，反而更难读。所以中间过程一次都不写，只在收尾打**一行**汇总。

`echo()` 给扫描循环里的其他日志用：先擦掉进度条 → 打印正常一行 → 立刻重绘，
这样 `[store]` / `[media]` 这类有价值的行不会被进度条吃掉。
"""
import sys
import time


class Progress:
    def __init__(self, label, total, width=22, stream=None, enabled=True):
        self.stream = stream or sys.stdout
        self.label = label
        self.total = max(1, int(total or 1))
        self.width = width
        # 非 TTY 不做原地重绘；close() 仍然会打汇总行
        try:
            isatty = bool(self.stream.isatty())
        except Exception:               # noqa: BLE001
            isatty = False
        self.enabled = bool(enabled) and isatty
        self.done = 0
        self.detail = ""
        self.t0 = time.time()
        self._last_len = 0
        self._closed = False

    def update(self, done=None, detail=None):
        if done is not None:
            self.done = int(done)
        if detail is not None:
            self.detail = str(detail)
        if self.enabled and not self._closed:
            self._draw()

    # ---------- 内部 ----------
    def _line(self):
        pct = min(1.0, max(0.0, self.done / float(self.total)))
        filled = int(round(self.width * pct))
        bar = "█" * filled + "░" * (self.width - filled)
        d = self.detail
        if len(d) > 18:
            d = d[:17] + "…"
        return "\r%s |%s| %d/%d %3d%% · %-18s · %.1fs" % (
            self.label, bar, min(self.done, self.total), self.total,
            int(pct * 100), d, time.time() - self.t0)

    def _draw(self):
        line = self._line()
        # 中文/方块字符在终端里是双宽，len() 会低估；统一多补几个空格擦残影
        pad = max(0, self._last_len - len(line)) + 8
        self.stream.write(line + " " * pad)
        self.stream.flush()
        self._last_len = len(line)

    def _clear(self):
        if self.enabled and self._last_len:
            self.stream.write("\r" + " " * (self._last_len + 8) + "\r")
            self.stream.flush()
            self._last_len = 0

    # ---------- 对外 ----------
    def echo(self, text):
        """在进度条上方正常打一行，然后立刻把进度条画回来。"""
        if self.enabled and not self._closed:
            self._clear()
            print(text, file=self.stream, flush=True)
            self._draw()
        else:
            print(text, file=self.stream, flush=True)

    def close(self, summary=""):
        """固化成一行汇总（TTY 与非 TTY 都会打，日志里每轮只占一行）。"""
        if self._closed:
            return
        self._closed = True
        self._clear()
        if summary:
            print(summary, file=self.stream, flush=True)
