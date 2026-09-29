# -*- coding: utf-8 -*-
r"""子进程静默自测：所有 `subprocess` 调用都必须走 `sliceq.subproc`。

═══════════════════════════════════════════════════════════════
起因：SliceQ 运行时屏幕上会闪出终端窗口（2026-09-29）
═══════════════════════════════════════════════════════════════
根因（实测，见 `probe_console_flash.py`）：
  父进程是 GUI 子系统（`--windowed` 打包 ⇒ 无控制台）时，启动 **console 子进程**
  （ffmpeg/ffprobe 的 PE `Subsystem=3`）会让 Windows **为它创建一个控制台宿主窗口**。

  | 启动方式                   | 3 次启动中触发窗口 |
  |---------------------------|------------------|
  | 管道 + 不加 flag（原状）     | **3 / 3** ★ 每次都弹 |
  | 管道 + CREATE_NO_WINDOW    | 0 / 3 ✅ |
  | DEVNULL + 不加 flag         | **3 / 3** ★ 光重定向救不了 |

  ⇒ **重定向 ≠ 不弹**，必须显式 `CREATE_NO_WINDOW`。
  ⚠️ 这些窗口一闪而过，**轮询抓不到**（前两版探针因此误判"不弹"），
     只有 `SetWinEventHook` 能看见。

═══════════════════════════════════════════════════════════════
这个自测守什么
═══════════════════════════════════════════════════════════════
**A 静态检查**：`sliceq/` 下不允许出现裸的 `subprocess.run(` / `subprocess.Popen(`。
   本项目已**两次**栽在"在 N 处修了、漏了第 N+1 处"：
     · 子进程管道未排空（editor.py 修了、asr.py 漏了 → 死锁）
     · 进度回调 with_progress（7 处传了 6 处 → 进度条永远不动）
   ⇒ 同一个形状：改法对、覆盖不全。所以改成唯一入口 `sliceq.subproc`，
     再用本检查兜底。将来新增调用点漏了，**测试当场失败**。

**B 行为检查**：`subproc.run` / `subproc.popen` 是否**真的**注入了 `CREATE_NO_WINDOW`；
   以及显式传入的 `creationflags`（`bridge.py` 的 `DETACHED_PROCESS`）**是否被尊重**。

用法：
    python stage0/probe_resolve/selftest_proc_flags.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sliceq import _testing                       # noqa: E402

_TMP = _testing.isolate_data_root("procflags")     # ★ 绝不碰用户真实数据
_testing.assert_isolated()

from sliceq import subproc                           # noqa: E402

PASS, FAIL = [], []


def ck(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    return cond


# ═══════════════════════════════════════════════════════════════
# A. 静态检查
# ═══════════════════════════════════════════════════════════════
BARE = re.compile(r"\bsubprocess\.(run|Popen)\s*\(")

#: 唯一豁免：subproc.py 自己（它就是那层包装）
EXEMPT_NAMES = {"subproc.py"}


def find_bare_calls(sources: dict[str, str]) -> list[tuple[str, int, str]]:
    """返回 [(路径, 行号, 调用形式)]。

    ⚠️ 抽成接收 `{路径: 源码}` 的函数（而不是直接扫盘），
       是为了能喂**构造的**源码做对照实验 —— 见下面 A3。
    """
    bad: list[tuple[str, int, str]] = []
    for path, src in sources.items():
        if Path(path).name in EXEMPT_NAMES:
            continue
        # 跳过注释行与 docstring 里的示例，避免假阳性
        for m in BARE.finditer(src):
            line_no = src[:m.start()].count("\n") + 1
            line = src.split("\n")[line_no - 1].strip()
            if line.startswith("#"):
                continue
            bad.append((path, line_no, m.group(0)))
    return bad


print("\n=== A 静态检查：不得有裸的 subprocess 调用 ===")

prod = {p.as_posix(): p.read_text(encoding="utf-8")
        for p in sorted((ROOT / "sliceq").rglob("*.py"))}
ck("扫到了生产代码文件", len(prod) > 10, f"{len(prod)} 个 .py")

violations = find_bare_calls(prod)
for path, line_no, form in violations:
    print(f"        ★ {path}:{line_no}  {form}")
ck("★ sliceq/ 下没有裸的 subprocess.run / Popen",
   not violations, f"违规 {len(violations)} 处")

# ── A2 反向确认：真的还有 subproc.** 调用（否则说明替换没生效）──
n_proc_calls = sum(len(re.findall(r"\bsubproc\.(run|popen)\(",
                                  src)) for src in prod.values())
ck("★ 确有调用走 subproc.run / subproc.popen",
   n_proc_calls >= 15, f"{n_proc_calls} 处")

# ── A3 ★ 对照组：喂旧写法，检查必须报出来 ─────────────────
# 取自 v0.1.3 的真实代码形状（纯手工构造，等价于当年的写法）
LEGACY_SNIPPET = '''\
def old_style(cmd):
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True)


def old_popen(cmd):
    import subprocess
    return subprocess.Popen(cmd, stdout=subprocess.PIPE)
'''
legacy_hits = find_bare_calls({"fake_old.py": LEGACY_SNIPPET})
ck("★ 对照组：旧写法被检出（证明检查有牙）",
   len(legacy_hits) == 2, f"检出 {len(legacy_hits)} 处（应为 2）")

# 对照组 2：注释掉的不该算
COMMENTED = "# subprocess.run(cmd)  ← 这是注释\nx = 1\n"
ck("对照组：注释行不算违规",
   not find_bare_calls({"fake_c.py": COMMENTED}), "")

# 对照组 3：subproc.py 自身豁免
ck("对照组：subproc.py 自身被豁免",
   not find_bare_calls({"subproc.py": "subprocess.run(args)"}), "")


# ═══════════════════════════════════════════════════════════════
# B. 行为检查：flag 是否真的注入了
# ═══════════════════════════════════════════════════════════════
print("\n=== B 行为检查：creationflags 的真实取值 ===")

_real_run = subprocess.run
_real_popen = subprocess.Popen
_box: dict = {}


def _spy_run(args, **kw):
    _box["run"] = kw.get("creationflags")
    _box["run_kwargs"] = set(kw)
    raise _Stop  # 不真跑


def _spy_popen(args, **kw):
    _box["popen"] = kw.get("creationflags")
    raise _Stop


class _Stop(Exception):
    pass


def probe_flags(fn, *args, **kw) -> int | None:
    """在监控下调用一次 fn，取回它最终传给 subprocess 的 creationflags。"""
    _box.clear()
    if fn is subproc.run:
        subprocess.run = _spy_run
    else:
        subprocess.Popen = _spy_popen
    try:
        fn(*args, **kw)
    except _Stop:
        pass
    except Exception as exc:                     # noqa: BLE001
        _box["err"] = f"{type(exc).__name__}: {exc}"
    finally:
        subprocess.run = _real_run
        subprocess.Popen = _real_popen
    return _box.get("run" if fn is subproc.run else "popen")


f1 = probe_flags(subproc.run, ["x"], capture_output=True)
ck("★ subproc.run 默认注入 CREATE_NO_WINDOW",
   f1 == subproc.CREATE_NO_WINDOW, f"实际 0x{f1:08x}" if f1 is not None else "未注入")

f2 = probe_flags(subproc.popen, ["x"])
ck("★ subproc.popen 默认注入 CREATE_NO_WINDOW",
   f2 == subproc.CREATE_NO_WINDOW, f"实际 0x{f2:08x}" if f2 is not None else "未注入")

DETACHED = 0x00000008 | 0x00000200
f3 = probe_flags(subproc.popen, ["x"], creationflags=DETACHED)
ck("★ 显式 creationflags 被尊重（bridge.py 的 DETACHED 靠它）",
   f3 == DETACHED, f"实际 0x{f3:08x}")

# ⚠️ 断言不要写 `f4 == 0` —— 实现的做法是"flags 为 0 时**不传**该参数"，
#    语义上等价（Windows 的默认值就是 0），所以拿到的是 None 而非 0。
#    之前写死 0 就是"断言比实现严"，属于本项目记过的坑。
f4 = probe_flags(subproc.run, ["x"], creationflags=0)
ck("显式 0 不会被换成 CREATE_NO_WINDOW（可以主动要求不注入）",
   f4 in (0, None), f"实际 {f4!r}")

ck("effective_flags() 三态正确",
   (subproc.effective_flags() == subproc.CREATE_NO_WINDOW
    and subproc.effective_flags(DETACHED) == DETACHED
    and subproc.effective_flags(0) == 0), "")


# ═══════════════════════════════════════════════════════════════
# C. 真跑一次：输出必须完好（CREATE_NO_WINDOW 不得吃掉输出）
# ═══════════════════════════════════════════════════════════════
print("\n=== C 真跑：静默化之后输出仍然完好 ===")

BIG = "import sys; sys.stdout.write('x' * 200000); sys.stderr.write('E' * 200000)"
r = subproc.run([sys.executable, "-c", BIG], stdout=subproc.PIPE,
             stderr=subproc.PIPE, timeout=60)
ck("★ stdout 完整（200,000 字节）", len(r.stdout or b"") == 200000,
   f"{len(r.stdout or b'')} 字节")
ck("★ stderr 完整（200,000 字节）", len(r.stderr or b"") == 200000,
   f"{len(r.stderr or b'')} 字节")
ck("退出码为 0", r.returncode == 0, str(r.returncode))

r2 = subproc.run([sys.executable, "-c",
               "import sys; sys.exit(3)"], capture_output=True, timeout=30)
ck("退出码原样透传（3）", r2.returncode == 3, str(r2.returncode))

# 超时语义没被破坏
try:
    subproc.run([sys.executable, "-c", "import time; time.sleep(30)"],
             capture_output=True, timeout=2)
    ck("★ TimeoutExpired 仍会抛出", False, "没有抛出")
except subproc.TimeoutExpired:
    ck("★ TimeoutExpired 仍会抛出", True, "")
except Exception as exc:                          # noqa: BLE001
    ck("★ TimeoutExpired 仍会抛出", False,
       f"抛的是 {type(exc).__name__}")


# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("  失败项：")
    for n in FAIL:
        print(f"    - {n}")
sys.exit(1 if FAIL else 0)
