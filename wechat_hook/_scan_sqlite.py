#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描 Weixin.exe, 确认 SQLite 版本并尝定位 sqlite3_step (分析用, 不进产物)."""
import re, sys

PATH = r"C:\Program Files\Tencent\Weixin\Weixin.exe"

data = open(PATH, "rb").read()
print(f"[*] file size = {len(data)} bytes")

# 1) SQLite 版本串候选
vers = set()
for m in re.finditer(rb"3\.\d{1,2}\.\d{1,3}", data):
    s = m.group().decode()
    vers.add(s)
print("[*] SQLite 版本串候选:", sorted(vers)[:20])

# 2) SQLite 标志性字符串 (确认 SQLite 在场 + 定位 .rdata 区域)
markers = [b"SQLite format 3", b"sqlite_version", b"not an error",
           b"malformed database schema", b"no such table: ",
           b"near \x00\x25\x73\x00: syntax error", b"database disk image is malformed",
           b"SQLite version 3."]
print("[*] 标志字符串命中:")
for mk in markers:
    idx = data.find(mk)
    print(f"    {mk!r:50} -> {hex(idx) if idx>=0 else 'NOT FOUND'}")

# 3) 看 VDBE_MAGIC_RUN 之类常量? 跳过, 先给版本.
print("[*] done")
