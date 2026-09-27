# -*- coding: utf-8 -*-
"""阶段 1 自测：数据层 + 密钥存储 + 元信息读取。

不启动 GUI，纯逻辑验证。可在无人值守下跑。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "OK  " if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 70)
    print("SliceQ 阶段 1 自测")
    print("=" * 70)

    from sliceq import config, secrets, settings, store

    # ── 0. ⚠️ 隔离：把所有落盘位置指向临时目录 ────────────
    #
    # 这一步是**必须**的。本测试原先直接操作生产路径，实测造成了真实破坏：
    #   · `secrets.set_api_key(fake)` 覆盖了用户的真 key
    #   · `secrets.clear_all()` 直接删掉了凭据文件（用户的 key 就此丢失）
    #   · `settings.set("whisper_model", "ggml-base.bin")` 把用户的
    #     large-v3-turbo 降级成 base —— 而且**用户不会知道转录为什么变差**
    #
    # **自测绝不该写生产数据。** 统一走 `sliceq/_testing.py`
    # （那里还隔离了 STYLES_DIR —— 阶段 3 的测试往用户预设目录里
    #   留过垃圾预设，直接出现在用户的下拉框里）。
    from sliceq._testing import assert_isolated, isolate_data_root

    _tmp = isolate_data_root(prefix="sliceq_s1_")
    print(f"\n[0] 隔离：本次全部落在临时目录 {_tmp}")
    check("数据根目录已隔离到临时目录",
          str(config.APP_ROOT).startswith(str(_tmp)), str(config.APP_ROOT))
    check("数据库已隔离（不在用户数据目录）",
          _tmp in config.DB_PATH.parents or config.DB_PATH.parent == _tmp,
          str(config.DB_PATH))
    check("凭据路径已隔离", config.SECRET_PATH.parent == _tmp,
          str(config.SECRET_PATH))
    check("设置路径已隔离", config.SETTINGS_PATH.parent == _tmp,
          str(config.SETTINGS_PATH))
    check("样式预设目录已隔离", config.STYLES_DIR.parent == _tmp,
          str(config.STYLES_DIR))
    assert_isolated()

    # ── 1. 目录 ────────────────────────────────────────
    print("\n[1] 目录与路径")
    config.ensure_dirs()
    check("数据根目录已创建", config.APP_ROOT.exists(), str(config.APP_ROOT))
    check("数据库文件可写", config.DB_DIR.exists())
    check("模型目录已创建", config.MODELS_DIR.exists())

    # ── 2. 建表 ────────────────────────────────────────
    print("\n[2] 数据库建表")
    store.init_db()
    tables = store.table_names()
    for t in ("task", "clip", "export", "subtitle", "marker"):
        check(f"表 {t} 存在", t in tables)

    # ── 3. 任务写入/读取 ───────────────────────────────
    print("\n[3] 任务读写")
    tid = store.create_task(
        name="自测任务", source_path=r"C:\fake\demo.mp4",
        duration_sec=3672.5, width=1920, height=1080,
        fps=30.0, bitrate_kbps=4500, filesize=123456789,
    )
    check("创建任务返回 id", isinstance(tid, int) and tid > 0, f"id={tid}")

    t = store.get_task(tid)
    check("能读回任务", t is not None)
    check("时长字段正确", t and abs(t["duration_sec"] - 3672.5) < 0.01)
    check("分辨率字段正确", t and t["width"] == 1920 and t["height"] == 1080)
    check("初始状态为 imported", t and t["status"] == config.STATUS_IMPORTED)

    store.set_task_status(tid, config.STATUS_READY)
    check("状态可更新",
          store.get_task(tid)["status"] == config.STATUS_READY)

    # ── 4. 预留表实际可用（C 路线专项）─────────────────
    print("\n[4] 预留表 subtitle / marker 实际可写")
    store.add_subtitle(tid, 0, 2000, "这是第一句字幕")
    store.add_subtitle(tid, 2000, 4000, "这是第二句字幕")
    subs = store.list_subtitles(tid)
    check("subtitle 可写入并读回", len(subs) == 2, f"{len(subs)} 条")
    check("subtitle 字段完整",
          subs and subs[0]["text"] == "这是第一句字幕"
          and subs[0]["end_ms"] == 2000)

    store.add_marker(tid, 1500, "候选高光：开场包袱", "CYAN")
    marks = store.list_markers(tid)
    check("marker 可写入并读回", len(marks) == 1)
    check("marker 颜色默认值生效",
          marks and marks[0]["color"] == "CYAN")

    # ── 5. 级联删除 ────────────────────────────────────
    print("\n[5] 级联删除")
    store.delete_task(tid)
    check("任务已删除", store.get_task(tid) is None)
    check("字幕随任务级联删除", len(store.list_subtitles(tid)) == 0)
    check("标记随任务级联删除", len(store.list_markers(tid)) == 0)

    # ── 6. DPAPI 密钥 ──────────────────────────────────
    print("\n[6] API Key 加密存储（DPAPI）")
    if sys.platform != "win32":
        print("  [跳过] 非 Windows 平台")
    else:
        fake = "sk-test-1234567890abcdefghijklmnop"
        secrets.set_api_key(fake)
        check("密钥文件已生成", config.SECRET_PATH.exists())

        raw = config.SECRET_PATH.read_bytes()
        check("落盘内容已加密（不含明文）",
              fake.encode() not in raw, f"{len(raw)} 字节密文")

        check("能解密读回", secrets.get_api_key() == fake)
        check("掩码显示正确", secrets.mask(fake).startswith("sk-tes")
              and "..." in secrets.mask(fake),
              secrets.mask(fake))

        # 验证确实走了 DPAPI（密文长度与明文不同 + 含 DPAPI 特有的随机性）
        secrets.set_api_key(fake)
        raw2 = config.SECRET_PATH.read_bytes()
        check("两次加密结果不同（DPAPI 随机盐）", raw != raw2)

        secrets.clear_all()
        check("可清除", secrets.get_api_key() == "")

    # ── 7. 设置项 ──────────────────────────────────────
    print("\n[7] 偏好设置")
    settings.set("whisper_model", "ggml-base.bin")
    check("设置可读写", settings.get("whisper_model") == "ggml-base.bin")
    check("未知键返回默认值", settings.get("不存在的键", "默认") == "默认")

    # ── 8. ffprobe 可用性 ──────────────────────────────
    print("\n[8] FFmpeg 环境探测")
    from sliceq import ffmpeg_tools
    info = ffmpeg_tools.probe_environment()
    print(f"       状态: {info.get('message')}")
    print(f"       路径: {info.get('ffmpeg')}")

    # ── 9. 元信息读取（用单测素材）──────────────────────
    print("\n[9] 媒体元信息读取")
    from sliceq import ingest
    media = Path(__file__).resolve().parents[1] / "stage0" / "test_media" / "source.mp4"
    if media.exists() and info.get("ffmpeg"):
        meta = ingest.probe_media(media)
        check("能读出时长", bool(meta.get("duration_sec")),
              f"{meta.get('duration_sec')} 秒")
        check("能读出分辨率",
              bool(meta.get("width")) and bool(meta.get("height")),
              f"{meta.get('width')}x{meta.get('height')}")
        check("能读出帧率", bool(meta.get("fps")), f"{meta.get('fps')} fps")
        check("时长格式化正确",
              ingest.format_duration(3672.5) == "1:01:12",
              ingest.format_duration(3672.5))
    else:
        print("  [跳过] 素材或 ffmpeg 不可用")

    # ── 汇总 ───────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
