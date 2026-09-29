"""统一的子进程入口 —— 同时解决「GUI 进程启动 console 程序会弹终端」。


## 为什么必须有它

Windows 上，父进程若是 **GUI 子系统**（PyInstaller `--windowed` 打出来的
`SliceQ.exe` 就是，`SliceQ.spec: console=False`），**它没有控制台**。
此时启动一个 **console 子进程**（`ffmpeg.exe` / `ffprobe.exe` 的 PE
`Subsystem=3` 都是 console），系统会**为它创建一个控制台宿主窗口**。

实测（Windows 11，2026-09-29，`stage0/probe_resolve/probe_console_flash.py`）：

| 启动方式                      | 3 次启动中触发窗口 |
|------------------------------|------------------|
| 管道 + 不加 flag（**原状**）    | **3 / 3** ★ 每次都弹 |
| 管道 + `CREATE_NO_WINDOW`     | **0 / 3** ✅ |
| 管道 + `DETACHED_PROCESS`     | 0 / 3 ✅ |
| `DEVNULL` + 不加 flag          | **3 / 3** ★ 光重定向救不了 |

窗口类名是 **`CASCADIA_HOSTING_WINDOW_CLASS`** —— Win11 的控制台宿主
（Windows Terminal 的内核窗口）。这正是用户看到的"弹出的终端"。

⚠️ 这些窗口**一闪而过**：普通轮询（80ms 一次、2 秒后采样）**完全抓不到**，
   只有 `SetWinEventHook(EVENT_OBJECT_CREATE)` 能捕到。
   前两版探针就是因为用轮询而误判成"不弹"。

⚠️ **重定向 ≠ 不弹**。`capture_output=True` / `DEVNULL` 都不能避免，
   必须显式告诉 Windows「别分配控制台」。
   （本项目 19 处调用原本全是重定向的，照样会弹。）


## 为什么是"统一入口"而不是"逐处加个参数"

本项目已 **两次** 栽在"在 N 处修了、漏了第 N+1 处"：

  · 子进程管道未排空 —— `editor.py::_run_ffmpeg` 修过，`asr.py` 漏了 ⇒ 死锁
  · 进度回调 `with_progress=True` —— 7 处调用点传了 6 处 ⇒ 进度条永远不动

同样的形状：**改法对、覆盖不全**。所以这里改成唯一入口，再由
`stage0/probe_resolve/selftest_proc_flags.py` 做**静态检查**：
`sliceq/` 下不允许直接出现 `subprocess.run(` / `subprocess.Popen(`。
将来新增调用点漏掉，测试会当场抓住，而不是等用户来报。


## 用法

    from . import subproc
    r = subproc.run([ff, "-version"], capture_output=True, text=True)
    p = subproc.popen(args, stdout=subproc.PIPE, stderr=subproc.PIPE)

需要**别的**创建方式时显式传 `creationflags=`（会被尊重，不会被覆盖）：
`bridge.py` 启动 VideoCaptioner 要 `DETACHED_PROCESS`，就是这种情况。
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Sequence

#: Windows：不给新进程分配控制台（= 屏幕上不弹终端）
#:
#: ⚠️ 不用 `subprocess.CREATE_NO_WINDOW`，因为该常量**只在 Windows 存在**，
#:    在别的平台 import 就会炸。这里自己定义，跨平台安全。
CREATE_NO_WINDOW = 0x08000000

# ── 常用常量的转发（调用方不必再 import subprocess）────────────
PIPE = subprocess.PIPE
DEVNULL = subprocess.DEVNULL
STDOUT = subprocess.STDOUT
CompletedProcess = subprocess.CompletedProcess
TimeoutExpired = subprocess.TimeoutExpired
CalledProcessError = subprocess.CalledProcessError


def effective_flags(explicit: int | None = None) -> int:
    """决定最终的 `creationflags`。

    - 非 Windows         → 0（该参数在别的平台根本不存在）
    - Windows + 未指定   → `CREATE_NO_WINDOW`（默认静默）
    - Windows + 显式指定 → **尊重调用方**（例如 `DETACHED_PROCESS`）
    """
    if os.name != "nt":
        return 0
    if explicit is None:
        return CREATE_NO_WINDOW
    return explicit


def _apply(kwargs: dict[str, Any]) -> dict[str, Any]:
    flags = effective_flags(kwargs.pop("creationflags", None))
    if os.name == "nt" and flags:
        kwargs["creationflags"] = flags
    return kwargs


def run(args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run` 的静默版（默认不给子进程分配控制台）。"""
    return subprocess.run(args, **_apply(kwargs))


def popen(args: Sequence[str], **kwargs: Any) -> subprocess.Popen:
    """`subprocess.Popen` 的静默版。"""
    return subprocess.Popen(args, **_apply(kwargs))
