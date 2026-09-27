# -*- coding: utf-8 -*-
"""隔离完备性扫描 —— 找出**还在写生产数据**的自测。

## 为什么需要它

判断"某个测试会不会写用户数据"**不能靠读代码**。实测（2026-09-27）：

    `selftest_bridge.py` 里没有一处 `settings.set`，
    但它调用 `bridge.set_manual_path()` —— 而那个产品函数内部
    会 `settings.set("videocaptioner_path", ...)`，
    直接改写了用户的 `settings.json`。

    代码审查时我 grep 的是"测试文件里的直接调用"，因此**整轮排查都漏了它**。
    最后是靠"跑一遍看文件的 mtime 变没变"才逮到。

所以这个工具的做法是：**跑每个测试，跑前跑后比生产数据的指纹**。
不管写入路径有多绕（间接调用、深层封装、第三方库回调），
只要最终动了用户的文件，就会被照出来。

## 指纹包含什么

    · settings.json / credentials.bin / db/sliceq.db 的 md5 + 大小 + mtime
    · styles/ 下的预设文件清单
    · 数据库各表的行数

**md5 与 mtime 都要看**：
    md5 相同但 mtime 变了 → 文件被重写过（内容碰巧一样）。
    这仍然算问题 —— 今天写的是同值，明天就可能是别的值。

## ⚠️ 覆盖范围（本工具的已知边界）

它扫的是 **`selftest_*.py`**（以及 `sliceq/selftest*.py`）。
**`probe_*.py` 不在自动扫描范围内** —— 那些探针大多需要命令行参数
（素材路径、时间段等），不带参数跑会立刻退出，**测不到它真实的写入行为**。

要覆盖探针，需要**带参数**跑一次并人工比指纹，或者
`check_isolation.py <探针名>`（显式指定会尝试不带参跑，可能不可用）。

> 这条边界要如实说，不要因为"grep 看起来没写"就当作已覆盖 ——
> 本轮就是靠 grep 排查的，而 `selftest_bridge` 恰恰靠 grep 查不出来。

## 用法

    python check_isolation.py                 # 跑全部 selftest_*
    python check_isolation.py --skip-heavy    # 跳过 e2e（耗时且会跑真实模型）
    python check_isolation.py selftest_queue selftest_bridge   # 只跑指定的
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]

# 需要真实模型 / 真实长转录的测试，默认跳过（用 --skip-heavy 时）
HEAVY = {
    "selftest_queue_e2e.py",
    "selftest_real_e2e.py",
    "selftest_enhance_real_e2e.py",
    "selftest_bilingual_e2e.py",
    "selftest_asr_e2e.py",
    "selftest_stage2.py",
}


def prod_root() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "SliceQ"


def fingerprint() -> dict:
    """生产数据的指纹。"""
    root = prod_root()
    fp: dict[str, str] = {}

    def sig(p: Path) -> str:
        if not p.exists():
            return "(不存在)"
        return (f"{hashlib.md5(p.read_bytes()).hexdigest()[:10]}"
                f"/{p.stat().st_size}B/{int(p.stat().st_mtime)}")

    for name, p in (("settings.json", root / "settings.json"),
                    ("credentials.bin", root / "credentials.bin"),
                    ("db/sliceq.db", root / "db" / "sliceq.db")):
        fp[name] = sig(p)

    fp["styles/"] = ",".join(sorted(
        p.name for p in (root / "styles").glob("*.json"))) or "(空)"

    db = root / "db" / "sliceq.db"
    if db.exists():
        try:
            con = sqlite3.connect(str(db))
            for t in ("task", "clip", "export", "subtitle", "marker",
                      "transcript_seg"):
                try:
                    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.Error:
                    n = "?"
                fp[f"行数.{t}"] = str(n)
            con.close()
        except sqlite3.Error:
            pass

    # work/ 目录里的残留（测试产物常落这里）
    work = root / "work"
    if work.exists():
        fp["work/"] = ",".join(sorted(p.name for p in work.iterdir()))[:200] \
            or "(空)"
    return fp


def diff(before: dict, after: dict) -> list[str]:
    out = []
    for k in sorted(set(before) | set(after)):
        b, a = before.get(k), after.get(k)
        if b != a:
            out.append(f"{k}: {b} → {a}")
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    skip_heavy = "--skip-heavy" in sys.argv

    cands = sorted(list((PROJ / "stage0" / "probe_resolve").glob("selftest_*.py"))
                   + list((PROJ / "sliceq").glob("selftest*.py")))
    if args:
        want = {a if a.endswith(".py") else a + ".py" for a in args}
        cands = [f for f in cands if f.name in want]
    if skip_heavy:
        cands = [f for f in cands if f.name not in HEAVY]

    print("=" * 78)
    print("隔离完备性扫描 —— 跑测试前后比生产数据指纹")
    print("=" * 78)
    print(f"用户数据根：{prod_root()}")
    print(f"待扫测试  ：{len(cands)} 个"
          + ("（已跳过 e2e）" if skip_heavy else ""))
    print()

    base = fingerprint()
    print("基线：")
    for k, v in base.items():
        print(f"    {k:<22} {v}")
    print()

    dirty: list[tuple[str, list[str]]] = []
    errors: list[str] = []

    for f in cands:
        before = fingerprint()
        t0 = time.time()
        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        r = None
        try:
            r = subprocess.run(
                [sys.executable, str(f)], cwd=str(PROJ), env=env,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=900)
            rc = r.returncode
        except subprocess.TimeoutExpired:
            rc = -9
        dt = time.time() - t0
        after = fingerprint()

        changed = diff(before, after)
        status = "干净" if not changed else "★ 改动了用户数据"
        print(f"  {f.name:<34} rc={rc:<4} {dt:>6.1f}s  {status}")
        if changed:
            dirty.append((f.name, changed))
            for c in changed:
                print(f"        {c}")

        # ⚠️ 失败时必须把输出带出来。
        #    第一版只报 rc，于是"rc=1"完全无法排查 ——
        #    同一个测试手工重跑又是通过的，只能靠猜。
        if rc != 0:
            out = (r.stdout or "") if r is not None else ""
            err = (r.stderr or "") if r is not None else ""
            tail = [ln for ln in out.rstrip().splitlines() if ln.strip()][-6:]
            etail = [ln for ln in err.rstrip().splitlines() if ln.strip()][-4:]
            print(f"        [rc={rc}] 输出尾部：")
            for ln in tail:
                print(f"          | {ln}")
            for ln in etail:
                print(f"          ! {ln}")
        if rc != 0 and not changed:
            errors.append(f.name)

    print()
    print("=" * 78)
    print("汇总")
    print("=" * 78)
    if dirty:
        print(f"  ❌ {len(dirty)} 个测试改动了用户数据：")
        for name, ch in dirty:
            print(f"     · {name}")
            for c in ch:
                print(f"         {c}")
    else:
        print("  ✅ 全部测试都没有改动用户数据 —— 隔离完备")

    # 基线本身也要没变（跑了这么多，最终仍应与最初一致）
    final = fingerprint()
    base_changed = diff(base, final)
    print()
    if base_changed:
        print(f"  ❌ 扫描结束后基线变了（说明有测试改了数据）：")
        for c in base_changed:
            print(f"     {c}")
    else:
        print("  ✅ 扫描结束后基线完全未变")

    if errors:
        print(f"\n  ⚠️ 这些测试脚本自身返回非 0（可能失败，需单独看）：{errors}")

    return 1 if (dirty or base_changed) else 0


if __name__ == "__main__":
    sys.exit(main())
