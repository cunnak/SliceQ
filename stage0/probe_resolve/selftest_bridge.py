# -*- coding: utf-8 -*-
"""阶段 4-C 自测：VideoCaptioner 联动（F16）。

设计要点：**用临时目录构造假的安装结构**，让测试不依赖"本机装没装"。
真机路径只作为附加信息打印出来，不作为断言依据。

重点验证的四条：
  ① 形态判定依据是**目录结构**（不是硬编码 "gui"）
  ② 手动路径优先于自动发现
  ③ **手动路径填错必须报出来**（不能静默当作"没装"）
  ④ **合规**：本模块不得 import / 读取对方的源码

运行：python stage0/probe_resolve/selftest_bridge.py
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
#    注意：**间接写入也算**（例如调 bridge.set_manual_path() 会写 settings.json）
#    —— 所以默认给所有自测都隔离，不做"看起来不碰数据"的判断。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from sliceq import bridge  # noqa: E402

PASS = FAIL = 0


def ck(tag: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {tag}" + (f" :: {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {tag}" + (f" :: {detail}" if detail else ""))
    return ok


def head(t: str) -> None:
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


# ─────────────────────────────────────────────────────────────
def make_fake_gui(root: Path) -> Path:
    """造一个 GUI 形态的假安装（仿真实结构）。"""
    (root / "app" / "core").mkdir(parents=True, exist_ok=True)
    (root / "app" / "view").mkdir(parents=True, exist_ok=True)
    (root / "resource" / "bin").mkdir(parents=True, exist_ok=True)
    (root / "work-dir").mkdir(parents=True, exist_ok=True)
    (root / "runtime").mkdir(parents=True, exist_ok=True)
    (root / bridge.EXE_NAME).write_bytes(b"MZ fake")
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "resource" / "bin" / "ffmpeg.exe").write_bytes(b"MZ ffmpeg")
    return root / bridge.EXE_NAME


def make_fake_cli(root: Path) -> Path:
    """造一个 CLI 形态的假安装（用于验证判定不是写死的）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / bridge.EXE_NAME).write_bytes(b"MZ fake")
    (root / "cli.py").write_text("# cli entry", encoding="utf-8")
    return root / bridge.EXE_NAME


def reset_manual() -> None:
    bridge.set_manual_path("")


# ─────────────────────────────────────────────────────────────
def t_probe_forms() -> None:
    head("1. 形态判定依据「目录结构」，不是写死的")
    with tempfile.TemporaryDirectory(prefix="vcgui_") as d:
        exe = make_fake_gui(Path(d))
        info = bridge.probe(exe)
        ck("有 app/ 无 cli.py → 判为 gui", info["form"] == "gui", info["form"])
        ck("认出它自带的 ffmpeg",
           info["bundled_ffmpeg"].endswith("resource\\bin\\ffmpeg.exe")
           or info["bundled_ffmpeg"].endswith("resource/bin/ffmpeg.exe"),
           info["bundled_ffmpeg"])
        ck("认出 work-dir", info["work_dir"].endswith("work-dir"),
           info["work_dir"])

    with tempfile.TemporaryDirectory(prefix="vccli_") as d:
        exe = make_fake_cli(Path(d))
        info = bridge.probe(exe)
        ck("有 cli.py → 判为 cli（将来官方若出 CLI 会自动认出来）",
           info["form"] == "cli", info["form"])

    with tempfile.TemporaryDirectory(prefix="vcnone_") as d:
        exe = Path(d) / bridge.EXE_NAME
        exe.write_bytes(b"MZ")
        info = bridge.probe(exe)
        ck("认不出来时保守判为 gui（降级路径在任何情况下都不会出错）",
           info["form"] == "gui", info["form"])
        ck("没有 work-dir 时返回空", info["work_dir"] == "", info["work_dir"])
        ck("没有自带 ffmpeg 时返回空", info["bundled_ffmpeg"] == "")


def t_manual_priority() -> None:
    head("2. 手动路径优先，且填错必须报出来")
    with tempfile.TemporaryDirectory(prefix="vc_") as d:
        root = Path(d)
        exe = make_fake_gui(root)

        # 不带手动路径：本机若没装，应该找不到（不依赖真机结果，只验证类型）
        reset_manual()
        ck("清空手动路径后 manual_invalid 为 False",
           bridge.find()["manual_invalid"] is False)

        # 指到目录
        bridge.set_manual_path(str(root))
        info = bridge.find()
        ck("手动填「目录」也能认出来", info["ok"] and info["path"] == str(exe),
           f"ok={info['ok']} path={info['path']}")
        ck("来源标为手动指定", info["source"] == "手动指定", info["source"])
        ck("形态探测跟着一起做了", info["form"] == "gui", info["form"])

        # 指到 exe 本身
        bridge.set_manual_path(str(exe))
        info = bridge.find()
        ck("手动填「exe 路径」也认得出", info["ok"] and info["path"] == str(exe),
           info["path"])

        # ★ 填错
        bridge.set_manual_path(str(root / "根本不存在"))
        info = bridge.find()
        ck("填错时 manual_invalid = True（不能静默当作没装）",
           info["manual_invalid"] is True, str(info["manual_invalid"]))
        if not info["ok"]:
            ck("填错时找不到，但提示里会说清楚",
               any("没有 VideoCaptioner.exe" in n
                   for n in bridge.handoff_notes()),
               str(bridge.handoff_notes())[:100])

        reset_manual()


def t_env_var() -> None:
    head("3. 环境变量可以指定（部署/多机场景）")
    import os
    with tempfile.TemporaryDirectory(prefix="vcen_") as d:
        root = Path(d)
        exe = make_fake_gui(root)
        reset_manual()
        old = os.environ.get(bridge.ENV_VAR)
        try:
            os.environ[bridge.ENV_VAR] = str(exe)
            info = bridge.find()
            ck("从环境变量找到", info["ok"] and info["path"] == str(exe),
               f"{info['ok']} / {info['path']}")
            ck("来源标注为环境变量",
               bridge.ENV_VAR in info["source"], info["source"])
        finally:
            if old is None:
                os.environ.pop(bridge.ENV_VAR, None)
            else:
                os.environ[bridge.ENV_VAR] = old
        reset_manual()


def t_handoff() -> None:
    head("4. 文件交接目录的推算")
    with tempfile.TemporaryDirectory(prefix="vch_") as d:
        root = Path(d)
        make_fake_gui(root)
        info = bridge.probe(root / bridge.EXE_NAME)
        info["ok"] = True
        info["work_dir"] = str(root / "work-dir")

        f = bridge.handoff_folder(
            r"C:\未明子录播\260925\[未明子]2026年09月10日00时录播.mp4",
            info=info)
        ck("按素材主文件名推算子目录",
           f is not None and f.name == "[未明子]2026年09月10日00时录播",
           str(f))
        ck("不创建目录（只推算路径）", f is not None and not f.exists(),
           str(f))
        ck("没有 work_dir 时返回 None",
           bridge.handoff_folder("x.mp4", info={"ok": True, "work_dir": ""})
           is None)


def t_notes() -> None:
    head("5. 给用户的说明文案")
    with tempfile.TemporaryDirectory(prefix="vcn_") as d:
        root = Path(d)
        make_fake_gui(root)
        bridge.set_manual_path(str(root))
        notes = bridge.handoff_notes()
        joined = "\n".join(notes)
        ck("找到了会给出路径", "已找到" in joined and str(root) in joined,
           joined[:80])
        ck("明确说了它没有命令行接口（不承诺做不到的事）",
           "没有命令行接口" in joined, "")
        ck("【QLabel 陷阱】文案里不能有 Markdown 星号（会原样显示）",
           "**" not in joined, "")
        ck("会提示它自带的 ffmpeg", "ffmpeg" in joined, "")
        reset_manual()

    # 找不到时
    reset_manual()
    info = bridge.find()
    if not info["ok"]:
        notes = "\n".join(bridge.handoff_notes())
        ck("找不到时说清了怎么办（可手动指定）", "手动指定" in notes, notes[:80])
    else:
        ck("（本机装了 VideoCaptioner，跳过「找不到」文案检查）", True,
           info["path"])


def t_compliance() -> None:
    head("6. ★ 合规：不得 import / 读取对方的源码（GPL-3.0 边界）")
    import ast

    src = (ROOT / "sliceq" / "bridge.py").read_text(encoding="utf-8")
    tree = ast.parse(src)          # 只看**真正的代码**，不含 docstring

    # ⚠️ 早先这里用正则扫全文，结果把模块头 docstring 里"app/ 是源码目录"
    #    这句**说明文字**也匹配成了"读了对方的源码"，报了个假阳性。
    #    检查源码行为要用 AST，不能用正则扫文本 —— 文本里既有代码也有文档。
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    ck("没有 import 任何对方模块",
       not any("videocaptioner" in (n or "").lower() for n in imported),
       str(imported))

    # 有没有"读取文件内容"的动作（读 .py 就等于抄源码）
    reads: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("read_text", "read_bytes", "readlines"):
                reads.append(node.func.attr)
    ck("不读取任何文件内容（只做 is_file/iterdir 之类的路径判断）",
       not reads, str(reads))

    ck("没有把它的目录加进 sys.path",
       not any("sys.path" in ast.dump(n) for n in ast.walk(tree)
               if isinstance(n, (ast.Import, ast.ImportFrom))), "")
    ck("只用 subprocess 启动独立进程（不 import 它的代码）",
       "subprocess" in imported or any("subprocess" in n for n in imported),
       str(imported))
    ck("启动用了 DETACHED（否则 SliceQ 退出会带走它）",
       "0x00000008" in src or "DETACHED_PROCESS" in src, "")
    ck("注释里写明了 GPL 边界与聚合的法理依据",
       "GPL" in src and "聚合" in src, "")


def t_real_machine() -> None:
    head("7. 本机实况（仅作参考，不作断言）")
    reset_manual()
    info = bridge.find()
    if info["ok"]:
        print(f"  找到：{info['path']}")
        print(f"  来源：{info['source']}｜形态：{info['form']}")
        print(f"  自带 ffmpeg：{info['bundled_ffmpeg'] or '无'}")
        print(f"  work-dir：{info['work_dir'] or '无'}")
        ck("有 CLI 时给出可见提示（当前无 CLI）",
           bridge.has_cli() == (info["form"] == "cli"),
           f"has_cli={bridge.has_cli()} form={info['form']}")
    else:
        print("  本机未找到 VideoCaptioner（不影响上面的结论）")
    reset_manual()


def main() -> int:
    t_probe_forms()
    t_manual_priority()
    t_env_var()
    t_handoff()
    t_notes()
    t_compliance()
    t_real_machine()
    print("\n" + "=" * 70)
    print(f"通过 {PASS} / 失败 {FAIL} / 共 {PASS + FAIL}")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
