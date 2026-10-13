#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xlog_decode.py —— 解码微信 (mars xlog) 日志, 提取明文。

原理 (来自腾讯 mars 官方 decode_mars_crypt_log_file.py):
  - 每个日志块首字节是 magic。magic=0x07 表示 TEA 加密 + 压缩。
  - 块头布局 (magic=0x07, crypt_key_len=64):
      [0]      1B  magic (0x07)
      [1:3]    2B  seq (uint16 LE)
      [3]      1B  begin_hour
      [4]      1B  end_hour
      [5:9]    4B  length (uint32 LE) = 加密 payload 长度
      [9:73]  64B  客户端公钥 (X[32] || Y[32])
      [73:..] length 字节 TEA 密文
      [end]   1B  MAGIC_END (0x00)
  - TEA 密钥 = sha512(ECDH(服务端私钥, 客户端公钥))[:16]
    (服务端私钥是 mars 官方解码脚本里硬编码的公开常量)
  - 解密后 zlib 原始解压 (-MAX_WBITS)。

用法:
    pip install cryptography
    python xlog_decode.py                         # 自动找最新的 mm_*.xlog
    python xlog_decode.py "C:\\路径\\mm_20261013.xlog"   # 指定文件
输出: 同目录下的 <原名>.decoded.log, 并打印前 40 行预览。

注意: 本脚本只读 xlog 文件, 不碰微信进程、不注入、不改任何东西。
若解出来是乱码, 说明本机微信换了服务端密钥或偏移, 把输出贴回即可继续调。
"""
import os
import sys
import glob
import struct
import zlib
import hashlib
import argparse

try:
    from cryptography.hazmat.primitives.asymmetric import ec
except ImportError:
    print("[!] 需要 cryptography: 运行  pip install cryptography")
    sys.exit(1)

# ---- mars xlog magic ----
MAGIC_COMPRESS_START2 = 0x07          # TEA 加密 + 压缩 (本机文件类型)
MAGIC_NO_COMPRESS_START = 0x03
MAGIC_NO_COMPRESS_START1 = 0x06
MAGIC_NO_COMPRESS_NO_CRYPT_START = 0x08
MAGIC_COMPRESS_START = 0x04
MAGIC_COMPRESS_START1 = 0x05
MAGIC_COMPRESS_NO_CRYPT_START = 0x09
MAGIC_END = 0x00

# 腾讯 mars 官方解码脚本里硬编码的服务端私钥 (secp256k1, 32 字节)
SERVER_PRIV_KEY_HEX = "145aa7717bf9745b91e9569b80bbf1eedaa6cc6cd0e26317d810e35710f44cf8"

_op = 0xFFFFFFFF
_delta = 0x9E3779B9
_priv_int = int(SERVER_PRIV_KEY_HEX, 16)
_server_priv = ec.derive_private_key(_priv_int, ec.SECP256K1())

_key_cache = {}


def derive_tea_key(pubkey64):
    """pubkey64: 64 字节 (X[32]||Y[32])。返回 16 字节 TEA 密钥。"""
    if pubkey64 in _key_cache:
        return _key_cache[pubkey64]
    px = pubkey64[:32]
    py = pubkey64[32:64]
    x_int = int.from_bytes(px, "big")
    y_int = int.from_bytes(py, "big")
    client_pub = ec.EllipticCurvePublicNumbers(x_int, y_int, ec.SECP256K1()).public_key()
    shared_x = _server_priv.exchange(ec.ECDH(), client_pub)  # 32 字节 x 坐标
    tea_key = hashlib.sha512(shared_x).digest()[:16]
    _key_cache[pubkey64] = tea_key
    return tea_key


def tea_decipher(v, k):
    """mars 自定义 TEA 解密 (16 轮, s 从 delta<<4 递减)。v,k 各 8/16 字节。"""
    v0, v1 = struct.unpack("=LL", v[0:8])
    k1, k2, k3, k4 = struct.unpack("=LLLL", k[0:16])
    s = (_delta << 4) & _op
    for _ in range(16):
        v1 = (v1 - (((v0 << 4) + k3) ^ (v0 + s) ^ ((v0 >> 5) + k4))) & _op
        v0 = (v0 - (((v1 << 4) + k1) ^ (v1 + s) ^ ((v1 >> 5) + k2))) & _op
        s = (s - _delta) & _op
    return struct.pack("=LL", v0, v1)


def tea_decrypt(data, k):
    n = (len(data) // 8) * 8
    out = b""
    for i in range(0, n, 8):
        out += tea_decipher(data[i:i + 8], k)
    out += data[n:]
    return out


def is_good_block(buf, off, count):
    """复刻官方 IsGoodLogBuffer: 校验块结构合法性。"""
    if off == len(buf):
        return True
    magic = buf[off]
    if magic in (MAGIC_NO_COMPRESS_START, MAGIC_COMPRESS_START, MAGIC_COMPRESS_START1):
        ckl = 4
    elif magic in (MAGIC_COMPRESS_START2, MAGIC_NO_COMPRESS_START1,
                   MAGIC_NO_COMPRESS_NO_CRYPT_START, MAGIC_COMPRESS_NO_CRYPT_START):
        ckl = 64
    else:
        return False
    headerLen = 1 + 2 + 1 + 1 + 4 + ckl
    if off + headerLen + 1 + 1 > len(buf):
        return False
    length = struct.unpack_from("I", buf, off + headerLen - 4 - ckl)[0]
    if off + headerLen + length + 1 > len(buf):
        return False
    if buf[off + headerLen + length] != MAGIC_END:
        return False
    if count <= 1:
        return True
    return is_good_block(buf, off + headerLen + length + 1, count - 1)


def get_log_start(buf, count):
    off = 0
    while off < len(buf):
        m = buf[off]
        if m in (MAGIC_NO_COMPRESS_START, MAGIC_NO_COMPRESS_START1, MAGIC_COMPRESS_START,
                 MAGIC_COMPRESS_START1, MAGIC_COMPRESS_START2, MAGIC_COMPRESS_NO_CRYPT_START,
                 MAGIC_NO_COMPRESS_NO_CRYPT_START):
            if is_good_block(buf, off, count):
                return off
        off += 1
    return -1


def decode_block(buf, off, out):
    """解码单个块, 返回下一个块偏移; -1 表示结束/出错。"""
    if off >= len(buf):
        return -1
    magic = buf[off]
    if magic in (MAGIC_NO_COMPRESS_START, MAGIC_COMPRESS_START, MAGIC_COMPRESS_START1):
        ckl = 4
    elif magic in (MAGIC_COMPRESS_START2, MAGIC_NO_COMPRESS_START1,
                   MAGIC_NO_COMPRESS_NO_CRYPT_START, MAGIC_COMPRESS_NO_CRYPT_START):
        ckl = 64
    else:
        return -1

    headerLen = 1 + 2 + 1 + 1 + 4 + ckl
    if off + headerLen + 1 > len(buf):
        return -1
    length = struct.unpack_from("I", buf, off + headerLen - 4 - ckl)[0]
    payload = bytes(buf[off + headerLen: off + headerLen + length])

    try:
        if magic == MAGIC_COMPRESS_START2:
            tea_key = derive_tea_key(bytes(buf[off + 9: off + 9 + 64]))
            dec = tea_decrypt(payload, tea_key)
            try:
                dec = zlib.decompressobj(-zlib.MAX_WBITS).decompress(dec)
            except Exception as e:
                out.extend(("[!] zlib 解压失败(可能密钥/偏移不对): %s\n" % str(e)).encode("utf-8", "ignore"))
                return off + headerLen + length + 1
        elif magic in (MAGIC_COMPRESS_START, MAGIC_COMPRESS_NO_CRYPT_START):
            dec = zlib.decompressobj(-zlib.MAX_WBITS).decompress(payload)
        elif magic == MAGIC_COMPRESS_START1:
            # 分段压缩
            tmp = bytearray()
            rest = payload
            while len(rest) > 0:
                single = struct.unpack_from("H", rest, 0)[0]
                tmp.extend(rest[2: single + 2])
                rest = rest[single + 2:]
            dec = zlib.decompressobj(-zlib.MAX_WBITS).decompress(bytes(tmp))
        elif magic in (MAGIC_NO_COMPRESS_START, MAGIC_NO_COMPRESS_START1,
                       MAGIC_NO_COMPRESS_NO_CRYPT_START):
            dec = payload  # 未压缩
        else:
            return off + headerLen + length + 1
    except Exception as e:
        out.extend(("[!] 解码异常: %s\n" % str(e)).encode("utf-8", "ignore"))
        return off + headerLen + length + 1

    out.extend(dec)
    return off + headerLen + length + 1


def decode_file(path, outpath):
    with open(path, "rb") as f:
        buf = bytearray(f.read())
    start = get_log_start(buf, 2)
    if start == -1:
        print("[!] 没找到任何合法 xlog 块 (首字节不是已知 magic)")
        return False
    out = bytearray()
    pos = start
    n = 0
    while True:
        nxt = decode_block(buf, pos, out)
        if nxt == -1:
            break
        pos = nxt
        n += 1
        if pos >= len(buf):
            break
    with open(outpath, "wb") as f:
        f.write(out)
    print("[+] 解码块数: %d, 明文大小: %d 字节" % (n, len(out)))
    print("[+] 输出: %s" % outpath)
    # 预览前 40 行
    try:
        text = out.decode("utf-8", "ignore")
    except Exception:
        text = out.decode("gbk", "ignore")
    lines = text.splitlines()
    print("[*] 前 40 行预览:")
    for ln in lines[:40]:
        print("    " + ln[:200])
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=None, help="xlog 路径; 缺省自动找最新 mm_*.xlog")
    args = ap.parse_args()

    if args.path:
        files = [args.path]
    else:
        base = os.path.join(os.environ.get("APPDATA", ""), "Tencent", "xwechat", "log")
        files = sorted(glob.glob(os.path.join(base, "mm_*.xlog")),
                       key=os.path.getmtime, reverse=True)
        if not files:
            print("[!] 没找到 mm_*.xlog, 请手动指定路径")
            sys.exit(1)
    target = files[0]
    print("[*] 解码: %s" % target)
    outpath = target + ".decoded.log"
    decode_file(target, outpath)


if __name__ == "__main__":
    main()
