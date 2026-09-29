"""读 PE 头判断可执行文件是 GUI 还是 console 子系统 —— 零侵入。

为什么这个判据有用
------------------
Windows 只有 **console 子系统**（Subsystem=3）的程序才会让系统创建/需要一个
控制台对象。GUI 子系统（Subsystem=2）程序不会。

所以：**SliceQ 启动一个 Subsystem=3 的 exe，且不给它重定向输出、也不给
CREATE_NO_WINDOW 时，才是"可能弹黑框"的组合。**

这条判据不需要真的启动任何程序，因此没有副作用（不用去开 VideoCaptioner
再关掉它）。

（顺带一提：PE32/PE32+ 的 Subsystem 字段都在 Optional Header 偏移 68，
所以两种位数可以共用一段解析代码。）
"""
from __future__ import annotations

import struct
from pathlib import Path

SUBSYS = {
    0: "UNKNOWN",
    1: "NATIVE",
    2: "★ GUI（不会弹终端）",
    3: "★ CONSOLE（可能弹终端）",
    5: "OS/2 CUI",
    7: "POSIX CUI",
    9: "Windows CE GUI",
    10: "EFI APPLICATION",
    14: "EFI ROM",
    16: "EFI RUNTIME",
}

MACHINE = {0x014c: "x86", 0x8664: "x64", 0xAA64: "ARM64"}


def pe_info(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            data = fh.read(4096)
    except OSError as exc:
        return {"path": str(path), "error": f"读不到：{exc}"}

    if len(data) < 0x40 or data[:2] != b"MZ":
        return {"path": str(path), "error": "不是 PE 文件（无 MZ）"}

    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew + 24 > len(data):
        # 表头可能在 4KB 之后，补读
        try:
            with open(path, "rb") as fh:
                fh.seek(e_lfanew)
                head = fh.read(128)
        except OSError as exc:
            return {"path": str(path), "error": str(exc)}
    else:
        head = data[e_lfanew:e_lfanew + 128]

    if head[:4] != b"PE\0\0":
        return {"path": str(path), "error": "无 PE 签名"}

    machine = struct.unpack_from("<H", head, 4)[0]
    opt = 24                                   # COFF header 20 + 签名 4
    magic = struct.unpack_from("<H", head, opt)[0]
    subsystem = struct.unpack_from("<H", head, opt + 68)[0]
    return {
        "path": str(path),
        "arch": MACHINE.get(machine, hex(machine)),
        "magic": hex(magic),
        "bits": "PE32+" if magic == 0x20B else "PE32",
        "subsystem_id": subsystem,
        "subsystem": SUBSYS.get(subsystem, f"其他({subsystem})"),
    }


def main() -> None:
    import os

    targets: list[Path] = []

    # SliceQ 自己
    targets.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "SliceQ"
                   / "SliceQ.exe")
    # ffmpeg
    targets.append(Path(os.environ["APPDATA"]) / "SliceQ" / "bin"
                   / "ffmpeg.exe")
    targets.append(Path(os.environ["APPDATA"]) / "SliceQ" / "bin"
                   / "ffprobe.exe")
    # VideoCaptioner（bridge 启动的那个）
    vc = Path(r"C:\VideoCaptioner")
    if vc.is_dir():
        for p in sorted(vc.glob("*.exe")):
            targets.append(p)
        for p in sorted(vc.glob("app/*.exe")):
            targets.append(p)
        for p in sorted(vc.glob("app/*/python*.exe")):
            targets.append(p)

    seen = set()
    print(f"{'文件':<52s} {'架构':6s} {'子系统'}")
    print("-" * 96)
    for t in targets:
        if not t.exists():
            print(f"{str(t)[-50:]:<52s} {'-':6s} （不存在）")
            continue
        if str(t) in seen:
            continue
        seen.add(str(t))
        info = pe_info(t)
        if "error" in info:
            print(f"{t.name:<52s} {'-':6s} {info['error']}")
        else:
            print(f"{t.name:<52s} {info['arch']:<6s} {info['subsystem']}")
            if info["subsystem_id"] == 3:
                print(f"{'':<52s} {'':6s}   ↳ 路径: {t}")


if __name__ == "__main__":
    main()
