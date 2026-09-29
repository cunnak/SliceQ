# -*- coding: utf-8 -*-
"""外部工具联动：VideoCaptioner（阶段 4 · F16）。

## 合规边界（GPL-3.0，不可越线）

VideoCaptioner 是 **GPL-3.0**，SliceQ 是 MIT。因此：

    ❌ 不 import 它的模块    ❌ 不复制它的源码
    ❌ 不把它打包进 exe      ❌ 不把它的代码"改写"进我们的代码

    ✅ 文件系统层面交接（把素材/成片放进它的工作目录）
    ✅ 用独立进程启动它（`subprocess.Popen`）

这属于 GPL FAQ 允许的**聚合**（aggregate）——两个独立程序经文件系统
与命令行交换数据，不构成单一作品。`docs/TECH-DESIGN.md` §6.5 有完整说明。

## 实测结论：**它没有 CLI**（2026-09-26，安装于 `C:\\VideoCaptioner`）

    VideoCaptioner.exe     1.6 MB 启动器（pystand 打包器）
    app/                   明文 Python 源码
      ├ common/ components/ core/ thread/ view/   ← 纯 PyQt GUI 结构
      └ 没有任何 argparse / click / sys.argv 入口
    runtime/python.exe     内嵌 Python 运行时
    resource/bin/          **自带 ffmpeg.exe / whisper-cpp.exe /
                            Faster-Whisper-XXL / aria2c.exe / 7z.exe**
    work-dir/<视频名>/     工作产出目录（其下有 subtitle/）

⇒ 所以本模块**不尝试命令行驱动它**，只做四件事：

    检测 → 报告能力 → 引导打开 → 文件交接

> ⚠️ 别再去猜命令行参数了。TASKS 里的原计划（`where` / `pip show` 检测 + 调 CLI）
> 已经证伪：它不是 pip 包、没有 CLI。**继续试只会浪费时间**（与阶段 4-B
> 里"必应带 token 仍然 401"是同一类情况）。

## 检测方式：为什么不用注册表

实测：`reg.exe` 与 COM 实例化（解析 `.lnk`）**都会被安全策略阻止** ——
这在企业/受管机器上很常见，不是本机特例。

而且 **"数据目录存在" ≠ "程序已安装"**：本机就是反例，
`%APPDATA%\\VideoCaptioner` 与桌面快捷方式都在，可执行文件却找不到。

⇒ 探测顺序：**手动路径（用户指定）> 环境变量 > 常见安装位置扫描**。
全部走文件系统，不依赖注册表、不依赖 COM、不运行目标程序。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import subproc, settings

EXE_NAME = "VideoCaptioner.exe"
ENV_VAR = "VIDEOCAPTIONER_PATH"
SETTING_KEY = "videocaptioner_path"

# 常见安装位置（按可能性排序）。
# `C:\VideoCaptioner` 是官方安装包的默认位置，实测本机就在这里。
_COMMON_DIRS = (
    r"C:\VideoCaptioner",
    r"D:\VideoCaptioner",
    r"E:\VideoCaptioner",
    "%LOCALAPPDATA%/Programs/VideoCaptioner",
    "%LOCALAPPDATA%/VideoCaptioner",
    "%APPDATA%/VideoCaptioner",
    "%ProgramFiles%/VideoCaptioner",
    "%ProgramFiles(x86)%/VideoCaptioner",
)


# ─────────────────────────────────────────────────────────────
# 发现
# ─────────────────────────────────────────────────────────────
def get_manual_path() -> str:
    return str(settings.get(SETTING_KEY, "") or "")


def set_manual_path(path: str) -> None:
    settings.set(SETTING_KEY, (path or "").strip())


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(p)).expanduser()


def _candidates() -> list[tuple[str, Path]]:
    """按优先级列出候选 (来源说明, 路径)。"""
    out: list[tuple[str, Path]] = []

    manual = get_manual_path()
    if manual:
        p = _expand(manual)
        # 用户可能填的是目录，也可能是 exe 本身
        out.append(("手动指定", p if p.suffix.lower() == ".exe"
                    else p / EXE_NAME))

    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        p = _expand(env)
        out.append((f"环境变量 {ENV_VAR}", p if p.suffix.lower() == ".exe"
                    else p / EXE_NAME))

    for raw in _COMMON_DIRS:
        out.append(("常见安装位置", _expand(raw) / EXE_NAME))

    return out


def find() -> dict:
    """找 VideoCaptioner。返回一份**能直接展示给用户**的结果。

    {
      ok: bool, path: str, source: str,
      manual_invalid: bool,     # 用户手动填了但没用（必须说出来，见下）
      form: "gui" | "cli" | "unknown",
      bundled_ffmpeg: str,      # 它自带的那份 ffmpeg（可作为兜底提示）
      work_dir: str,
      exe_dir: str,
      error: str,
    }
    """
    info: dict = {"ok": False, "path": "", "source": "", "manual_invalid": False,
                  "form": "unknown", "bundled_ffmpeg": "", "work_dir": "",
                  "exe_dir": "", "error": ""}

    manual = get_manual_path()
    for source, p in _candidates():
        if p.is_file():
            info.update(ok=True, path=str(p), source=source)
            break

    # 手动路径填错时必须说出来 —— 否则用户以为设置生效了。
    # （与 ffmpeg_tools.ffmpeg_source() 里的 manual_invalid 同一个理由）
    #
    # ⚠️ 注意**不能**加 `and not info["ok"]` 这个条件。
    #    踩过：用户填错路径、但自动发现在别处找到了程序时，
    #    早先的写法认为"反正能用"就不报 —— 结果用户填的那个错路径
    #    一直显示在输入框里，他会以为**用的就是那一份**。
    #    实际用的是哪个，必须和用户填了什么分开讲。
    if manual:
        want = _expand(manual)
        if not (want.is_file() or (want / EXE_NAME).is_file()):
            info["manual_invalid"] = True

    if info["ok"]:
        info.update(probe(Path(info["path"])))    # 静态探测形态与附属资源
    return info


def probe(exe: str | Path) -> dict:
    """**静态**探测它的形态（不运行目标程序，避免弹窗与副作用）。

    判断依据是目录结构，不是猜：
      · 有 `app/` 且没有 `cli.py`/`__main__.py` → GUI 结构（无 CLI）
      · 有 `cli.py` / `__main__.py` / 同名 CLI exe → 可能有 CLI
    """
    p = Path(exe)
    base = p.parent
    out: dict = {"form": "unknown", "bundled_ffmpeg": "", "work_dir": "",
                 "exe_dir": str(base)}

    names = {f.name.lower() for f in base.iterdir()} if base.is_dir() else set()
    looks_gui = "app" in names
    looks_cli = bool({"cli.py", "__main__.py", "videocaptioner-cli.exe"} & names)
    if looks_cli:
        out["form"] = "cli"
    elif looks_gui:
        out["form"] = "gui"
    else:
        # 认不出来就按 GUI 处理（保守：GUI 的降级路径是"引导用户手动打开"，
        # 这个降级在任何情况下都不会出错）
        out["form"] = "gui"

    bundled = base / "resource" / "bin" / "ffmpeg.exe"
    if bundled.is_file():
        out["bundled_ffmpeg"] = str(bundled)

    work = base / "work-dir"
    if work.is_dir():
        out["work_dir"] = str(work)
    return out


def has_cli() -> bool:
    """本机这一份是否提供命令行接口。

    ⚠️ 实测为 False（见模块头注释）。保留这个函数是为了**将来**：
       如果官方出了 CLI 版，这里会自动返回 True，调用方无需改动。
       但**不要**据此去猜参数 —— 有 CLI 也得先查清它的参数表。
    """
    info = find()
    return bool(info.get("ok")) and info.get("form") == "cli"


# ─────────────────────────────────────────────────────────────
# 文件交接
# ─────────────────────────────────────────────────────────────
def handoff_folder(video: str | Path, *, info: dict | None = None) -> Path | None:
    """给出某个素材在 VideoCaptioner 里对应的文件夹（不创建、不复制）。

    它的 `work-dir` 按**素材主文件名**分文件夹（实测：
    `work-dir/[未明子]2026年09月10日00时录播/`），所以这里按同样规则推算。
    返回 None 表示还没发现程序或它没有 work-dir。
    """
    info = info or find()
    if not info.get("ok") or not info.get("work_dir"):
        return None
    return Path(info["work_dir"]) / Path(video).stem


def handoff_notes() -> list[str]:
    """给用户看的联动说明（界面直接显示这几行）。"""
    info = find()
    if not info.get("ok"):
        n = ["没有找到 VideoCaptioner。若已安装，可在「设置」里手动指定它的路径。"]
        if info.get("manual_invalid"):
            n.append("⚠️ 你指定的路径里没有 VideoCaptioner.exe，请检查后重填。")
        return n

    n = [f"已找到 VideoCaptioner：{info['path']}（{info['source']}）"]
    if info.get("form") == "gui":
        # ⚠️ 这里是给 QLabel 直接显示的文字，**不要用 Markdown 星号** ——
        #    QLabel 不渲染 Markdown，`**x**` 会把星号原样显示出来。
        n.append("它是图形界面程序，没有命令行接口，"
                 "所以只能由你在它自己的界面里操作（SliceQ 无法代劳）。")
    if info.get("work_dir"):
        n.append(f"它的工作目录：{info['work_dir']}"
                 "（同名素材会出现在这里）")
    if info.get("bundled_ffmpeg"):
        n.append("它自带一份 ffmpeg，"
                 f"位于 {info['bundled_ffmpeg']} —— "
                 "如果 SliceQ 这边缺 ffmpeg，可以在设置里指向它。")
    return n


# ─────────────────────────────────────────────────────────────
# 启动
# ─────────────────────────────────────────────────────────────
def launch(info: dict | None = None) -> tuple[bool, str]:
    """用**独立进程**打开它。返回 (是否成功, 提示)。

    ⚠️ 必须 DETACHED —— 否则 SliceQ 退出时会把它的进程一起带走
       （同一 console group 会被 ^C 波及）。
       这是本项目在 dsh 那套启动方式上踩过的同一个坑。
    """
    info = info or find()
    if not info.get("ok"):
        return False, ("没有找到 VideoCaptioner，无法打开。"
                       "可在「设置」里手动指定它的路径。")
    exe = Path(info["path"])
    try:
        flags = 0
        if sys.platform == "win32":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            flags = 0x00000008 | 0x00000200
        subproc.popen([str(exe)], cwd=str(exe.parent),
                         creationflags=flags, close_fds=True)
        return True, f"已启动 VideoCaptioner（{exe.parent}）"
    except Exception as exc:                     # noqa: BLE001
        return False, f"启动失败：{type(exc).__name__}: {exc}"


def open_work_dir(video: str | Path | None = None,
                  info: dict | None = None) -> tuple[bool, str]:
    """打开它的工作目录（给了 video 就定位到该素材对应的子目录）。

    这是**最实用的一步**：把 SliceQ 的素材/成片路径告诉用户，
    他可以直接在资源管理器里和 VideoCaptioner 的工作目录对照。
    """
    info = info or find()
    if not info.get("ok") or not info.get("work_dir"):
        return False, "没有找到 VideoCaptioner 的工作目录。"
    target = Path(info["work_dir"])
    if video:
        sub = handoff_folder(video, info=info)
        if sub and sub.is_dir():
            target = sub
    try:
        os.startfile(str(target))                # noqa: S606 (Windows 专用)
        return True, f"已打开：{target}"
    except Exception as exc:                     # noqa: BLE001
        return False, f"打开目录失败：{exc}"
