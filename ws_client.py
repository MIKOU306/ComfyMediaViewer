# -*- coding: utf-8 -*-
"""极简 WebSocket 客户端(仅用于 CDP Runtime.evaluate),纯 stdlib,零依赖。"""
import base64
import json
import os
import re
import socket
import struct


def ws_connect(url: str, timeout: float = 10):
    """ws://host:port/path 握手,返回 socket。"""
    m = re.match(r"ws://([^:/]+):(\d+)(/.*)", url)
    if not m:
        raise RuntimeError("bad ws url: " + url)
    host, port, path = m.group(1), int(m.group(2)), m.group(3)
    s = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    s.sendall(req.encode("ascii"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("ws handshake closed")
        buf += chunk
    status = buf.split(b"\r\n", 1)[0]
    if b"101" not in status:
        raise RuntimeError("ws handshake refused: " + status.decode("ascii", "replace"))
    s.settimeout(30)
    return s


def _send_frame(sock, opcode: int, payload: bytes = b""):
    """底层发送（客户端帧必须带掩码）。"""
    mask = os.urandom(4)
    hdr = bytearray([0x80 | opcode])  # FIN + opcode
    n = len(payload)
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126)
        hdr += struct.pack(">H", n)
    else:
        hdr.append(0x80 | 127)
        hdr += struct.pack(">Q", n)
    hdr += mask
    sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def ws_send(sock, payload: str):
    _send_frame(sock, 0x1, payload.encode("utf-8"))


def ws_close(sock):
    """礼貌关闭：发 close 帧再关 socket，错误一律吞掉。"""
    try:
        _send_frame(sock, 0x8)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise RuntimeError("ws closed")
        buf += c
    return buf


def ws_recv(sock) -> bytes:
    """读一条完整消息：自动回 ping 的 pong、拼接分帧续帧、对端 close 抛异常。"""
    msg = bytearray()
    while True:
        h1, h2 = _recv_exact(sock, 2)
        fin = h1 & 0x80
        op = h1 & 0x0F
        ln = h2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", _recv_exact(sock, 2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", _recv_exact(sock, 8))[0]
        key = _recv_exact(sock, 4) if (h2 & 0x80) else b""
        data = _recv_exact(sock, ln)
        if key:
            data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
        if op == 0x8:                      # close：对端主动断开
            raise RuntimeError("ws closed by peer")
        if op == 0x9:                      # ping → 回 pong 后等下一条
            _send_frame(sock, 0xA, data)
            continue
        if op == 0xA:                      # pong → 忽略
            continue
        if op in (0x0, 0x1, 0x2):          # 续帧 / 文本 / 二进制
            msg += data
            if fin:
                return bytes(msg)
            continue
        # 保留 opcode：协议不允许，丢弃这段继续等


_REQ_ID = [0]


def cdp_call(sock, method: str, params: dict = None, timeout: float = 60) -> dict:
    """通用 CDP 调用：发送任意方法并等待对应 id 的响应。"""
    _REQ_ID[0] += 1
    rid = _REQ_ID[0]
    ws_send(sock, json.dumps({"id": rid, "method": method,
                              "params": params or {}}))
    sock.settimeout(timeout)
    while True:
        raw = ws_recv(sock)
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if msg.get("id") == rid:
            return msg


def cdp_eval(sock, expression: str, timeout: float = 60) -> dict:
    """发送 Runtime.evaluate(awaitPromise) 并返回该 id 的结果。"""
    return cdp_call(sock, "Runtime.evaluate",
                    {"expression": expression, "awaitPromise": True,
                     "returnByValue": True}, timeout=timeout)
