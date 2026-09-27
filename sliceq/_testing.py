# -*- coding: utf-8 -*-
"""自测/探针脚本的数据隔离（仅测试用，产品运行时不涉及）。

⚠️ 为什么必须有这个模块
================================
自测写生产数据在这个项目里造成过一次**真实损失**（2026-09-27）：

    `selftest_stage1.py` 直接操作生产路径
      · `secrets.set_api_key(假的)`   → **覆盖了用户的真 API key**
      · `secrets.clear_all()`         → **删掉了凭据文件**，key 就此丢失
      · `settings.set("whisper_model", "ggml-base.bin")`
                                      → 把 large-v3-turbo **降级成 base**，
                                        而用户不会知道转录为什么突然变差

    `selftest_first_launch.py`
      · `settings.set("ffmpeg_path", "")` → 清掉用户手动指定的 ffmpeg 路径

这类破坏的共同特征：**不报错**，而且**只在很久之后以别的形式暴露**。
（那次是等到另一个依赖 key 的测试报 `AuthError` 才顺藤摸到的。）

## 用法

    from sliceq._testing import isolate_data_root, assert_isolated

    tmp = isolate_data_root()
    assert_isolated()              # 可选，但推荐：把"忘了隔离"变成硬失败
    from sliceq import store
    store.init_db()                # ← 之后一切都在 tmp 里

**必须在 `store.init_db()` / `settings.get()` 之前调用。**

## 为什么是「重定向」而不是「测完恢复」

恢复要写对每一处、且中途不能崩；一旦异常就留下污染，
而污染是**静默**的（用户不会收到通知）。
重定向是从根上让「写生产数据」这件事不可能发生 ——
临时目录在 atexit 时整个删掉，测试写得多脏都不碰用户分毫。

## ★ 哪些隔离、哪些**故意不隔离**

| 路径 | 处理 | 理由 |
|---|---|---|
| `DB_PATH` / `SECRET_PATH` / `SETTINGS_PATH` | **隔离** | 写一次就是真损失（key、模型档位、任务记录）|
| `STYLES_DIR` | **隔离** | 用户手改的字幕预设。阶段 3 的异常注入测试往这里留过两个垃圾预设（`损坏测试`/`类型错误`），**直接出现在用户的下拉框里** |
| `EXPORT_DIR` / `DOWNLOAD_DIR` / `LOGS_DIR` | **隔离** | 测试产物不该混进用户的导出目录 |
| `BGM_DIR` | **隔离** | 用户自备的音乐库，测试别掺和 |
| `JIANYING_DRAFT_ROOT` | **隔离** | 剪映的草稿目录 —— 测试导出草稿会**出现在用户的剪映首页草稿列表里**。要验证真实部署，测试显式传 `draft_root=` |
| `MODELS_DIR` / `BIN_DIR` | **❌ 故意不隔离** | 是**下载缓存**（whisper 1549MB + ffmpeg 245MB）。隔离掉会让依赖模型的测试找不到文件、被迫重新下载 1.8GB；而"下载模型"对用户是**有益**的（他本来就要下），不是破坏 |

> 判据：**写下去会不会损坏用户的既有数据**。
> 模型/ffmpeg 是"没有就补上"，样式/凭据/任务库是"改了就是损坏"。

## ★ 凭据：隔离，但**复制一份**过来

`SECRET_PATH` 被重定向后，需要真实模型调用的测试（`selftest_queue_e2e`、
`selftest_real_e2e`、`selftest_stage2`）会读不到 API key 而直接跑不起来。

所以隔离时把用户的凭据**复制**到临时位置（`with_secrets=True`，默认）：

| | |
|---|---|
| 测试**读** key | ✅ 正常（e2e 能跑真实模型） |
| 测试**写/删** key | ✅ 只影响临时副本，用户那份不动 |

⚠️ 复制到系统临时目录**没有降低安全性**：DPAPI 密文只有同一用户账户
能解密，而 `%APPDATA%\\SliceQ\\credentials.bin` 本来就在同一信任边界内
（同用户的其它程序都读得到）。副本在 atexit 时随目录一起删除。

要测「**没有** key 时的行为」，传 `with_secrets=False`（或复制后
自己 `secrets.clear_all()`，那只清临时副本）。

## ★ settings.json：也**复制**（v2 修正）

第一版**不**复制，理由是"测试断言要确定性，怕用户的配置影响断言"。
**这个判断是错的**，而且当场暴露了后果（2026-09-27）：

    隔离后测试读到的是**默认**端点 `.../api/v1`（原生 SDK 通道），
    而用户在设置里配的是 `.../compatible-mode/v1`（HTTP 通道）。
    两个端点域名相同、**通道不同**：前者要 `dashscope` 包，而它没装
    ⇒ 粗筛一个窗口都没跑成功 ⇒ 端到端测试"0 条切片段"。

也就是说，**测试跑的不是用户真实走的路径**。
而"断言受配置影响"这个担心其实用错了地方 ——
要测"未设置时的默认值"，测试应该自己显式清空，而不是让所有人都
读不到用户配置。

所以现在**复制** `settings.json`：测试看到的就是用户实际在用的端点、
模型、并发度。这与真实运行一致，才是端到端测试该有的样子。

⚠️ 若某个断言确实依赖"出厂默认值"，请在测试里显式
`settings.reset()`（作用于临时副本），不要依赖这里不复制。

## 这个模块解决不了的部分

`MODELS_DIR` / `BIN_DIR` 仍指向生产路径，测试会**读**它们（安全）。
但如果将来要连读也隔离，得另想办法。
"""
from __future__ import annotations

import atexit
import shutil
import tempfile
import threading
from pathlib import Path

# 被重定向的 config 属性（改名时这里要一起改）
#
# ⚠️ MODELS_DIR / BIN_DIR **不在列表里**，是刻意的 —— 见模块头注释的表格。
_PATH_ATTRS = (
    "APP_ROOT",
    "LOGS_DIR", "DB_DIR", "DOWNLOADS_DIR",
    "EXPORT_DIR", "STYLES_DIR", "STYLE_ASSETS_DIR", "BGM_DIR",
    "JIANYING_DRAFT_ROOT",
    "DB_PATH", "SECRET_PATH", "SETTINGS_PATH",
)

_ISOLATED_ROOT: Path | None = None
_ORIGINAL: dict[str, object] = {}


def isolate_data_root(prefix: str = "sliceq_test_",
                      with_secrets: bool = True,
                      with_settings: bool = True) -> Path:
    """把所有落盘位置指向一个临时目录，返回该目录。

    幂等：重复调用返回同一个目录（嵌套调用不会把外层换掉）。

    默认会把用户的**凭据**与**设置**各复制一份到临时位置：
    · 凭据 —— 需要真实模型调用的测试才能跑；对 key 的写入只落在副本上
    · 设置 —— 测试看到的就是用户实际在用的端点/模型/并发度，
              而不是出厂默认值（见模块头注释「v2 修正」）

    要测"出厂默认配置"下的行为，传 `with_settings=False`
    （或在隔离后自己 `settings.reset()`）。

    ⚠️ 必须在 `store.init_db()` 之前调用 —— 之后调只是换个路径，
       已经建立的连接还挂在旧库上。
    """
    global _ISOLATED_ROOT
    if _ISOLATED_ROOT is not None:
        return _ISOLATED_ROOT

    from . import config, settings, store

    tmp = Path(tempfile.mkdtemp(prefix=prefix)).resolve()

    for attr in _PATH_ATTRS:
        _ORIGINAL[attr] = getattr(config, attr, None)
    config.APP_ROOT = tmp
    config.LOGS_DIR = tmp / "logs"
    config.DB_DIR = tmp / "db"
    config.DOWNLOADS_DIR = tmp / "downloads"
    config.EXPORT_DIR = tmp / "export"
    config.STYLES_DIR = tmp / "styles"
    config.STYLE_ASSETS_DIR = tmp / "styles" / "assets"
    config.BGM_DIR = tmp / "bgm"
    # 剪映草稿目录：测试往里写的草稿会出现在用户剪映首页，必须隔离
    config.JIANYING_DRAFT_ROOT = tmp / "jianying_draft"
    config.DB_PATH = config.DB_DIR / "sliceq.db"
    config.SECRET_PATH = tmp / "credentials.bin"
    config.SETTINGS_PATH = tmp / "settings.json"

    # MODELS_DIR / BIN_DIR 保持指向生产：它们是下载缓存（1.8GB），
    # 隔离掉会让依赖模型的测试被迫重新下载。详见模块头注释的表格。
    _ORIGINAL["MODELS_DIR"] = config.MODELS_DIR
    _ORIGINAL["BIN_DIR"] = config.BIN_DIR

    # ── 凭据：复制一份到临时位置 ──────────────────────────
    # 不复制的话，需要真实模型调用的测试（queue_e2e / real_e2e / stage2）
    # 会因为读不到 key 直接跑不起来 —— 实测踩过。
    #
    # 复制的是 DPAPI 密文，同用户账户能解密；安全性没有降低
    # （生产那份本来就在同一信任边界内可读），副本随临时目录一起删。
    for attr, flag in (("SECRET_PATH", with_secrets),
                       ("SETTINGS_PATH", with_settings)):
        if not flag:
            continue
        src = _ORIGINAL.get(attr)
        try:
            if src and Path(str(src)).exists():
                shutil.copy2(str(src), getattr(config, attr))
        except OSError:
            # 复制失败（权限/文件锁）不该让测试起不来 ——
            # 缺 key 的测试会自己报"未配置 API key"，
            # 缺设置的测试会退回出厂默认值。
            pass

    # ── 丢掉缓存，否则仍连在旧库/旧设置上 ──────────────
    # store 用 threading.local 存连接：换一个空的就能断开旧库。
    if hasattr(store, "_local"):
        store._local = threading.local()
    # settings 用模块级 dict 缓存整个文件
    if hasattr(settings, "_cache"):
        settings._cache = None

    config.ensure_dirs()

    # 只在"确实在系统临时目录下"时才注册删除 —— 万一上面的重定向被改坏了，
    # 这一条能挡住"把用户数据目录整个删掉"这种最坏情况。
    if _under_temp(tmp):
        atexit.register(shutil.rmtree, tmp, ignore_errors=True)

    _ISOLATED_ROOT = tmp
    return tmp


def _under_temp(path: Path) -> bool:
    """路径是否在系统临时目录下。"""
    try:
        temp_root = Path(tempfile.gettempdir()).resolve()
        path.relative_to(temp_root)
        return True
    except (ValueError, OSError):
        return False


def assert_isolated() -> None:
    """确认落盘位置确实被隔离了；没有就抛异常。

    推荐每个自测在 `isolate_data_root()` 后调一次 ——
    它把「忘了隔离」从**静默写用户数据**变成**测试当场失败**。
    """
    from . import config

    root = Path(str(config.APP_ROOT)).resolve()
    if not _under_temp(root):
        raise AssertionError(
            f"数据根目录未隔离：{root}\n"
            f"请在本测试开头调用 sliceq._testing.isolate_data_root() —— "
            f"自测绝不该写生产数据（本项目因此丢过一次用户的 API key）。")

    strays = []
    for attr in ("DB_PATH", "SECRET_PATH", "SETTINGS_PATH"):
        p = Path(str(getattr(config, attr))).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            strays.append(f"{attr}={p}")
    if strays:
        raise AssertionError(
            "这些落盘位置逃出了隔离目录：\n  " + "\n  ".join(strays))


def is_isolated() -> bool:
    """当前是否已隔离（只判断，不抛异常）。"""
    return _ISOLATED_ROOT is not None


def isolated_root() -> Path | None:
    return _ISOLATED_ROOT
