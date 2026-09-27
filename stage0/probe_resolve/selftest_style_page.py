# -*- coding: utf-8 -*-
"""阶段 3 自测 —— 字幕样式页与预览（GUI，离屏模式）

跑法：
    QT_QPA_PLATFORM=offscreen python selftest_style_page.py
"""
from __future__ import annotations

import faulthandler
import sys
import traceback
from pathlib import Path

# 原生崩溃（段错误）时打印 Python 栈 —— 否则进程静默消失，什么都看不到
faulthandler.enable()

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from PySide6.QtCore import QTimer                              # noqa: E402
from PySide6.QtWidgets import QApplication                     # noqa: E402

from sliceq import store, subtitle_style as ss                 # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK  ] {name}" + (f"  — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  — {detail}" if detail else ""))
    return ok


def head(t: str) -> None:
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


def main() -> int:
    # ⚠️ 必须声明 global。下面 `_finalize()` 里要读 `FAIL`，而 main 里
    #    还有一处 `FAIL += 1`；main 不声明的话，`FAIL` 就成了 main 的
    #    **局部变量**，嵌套函数读它时它尚未绑定 →
    #    `NameError: cannot access free variable 'FAIL'`。
    #    （这个 bug 潜伏了很久，只在预览回调成功触发 `_finalize()` 时才暴露。）
    global PASS, FAIL

    store.init_db()
    app = QApplication(sys.argv)

    from sliceq.ui.main_window import MainWindow

    win = MainWindow()
    win.resize(1280, 880)
    win.show()
    win.nav.setCurrentRow(2)                 # 切到「字幕样式」
    sp = win.style_page
    app.processEvents()

    # 从干净状态开始：把当前样式重置成出厂值。
    # （本测试会改样式并落盘；上次异常退出可能留下"夜间模式"之类的痕迹）
    ss.save_as_active(ss.builtin_default())
    sp.refresh_all()
    app.processEvents()

    head("[1] 主窗口集成")
    check("侧栏 4 项",
          win.nav.count() == 4,
          str([win.nav.item(i).text() for i in range(win.nav.count())]))
    check("内容页 4 页", win.stack.count() == 4)
    check("已切到样式页", win.stack.currentIndex() == 2)

    head("[2] 控件就位")
    items = [sp.preset_combo.itemText(i) for i in range(sp.preset_combo.count())]
    check("预设下拉非空", bool(items), str(items))
    check("字体下拉已填充", sp._main_font.count() > 50,
          f"{sp._main_font.count()} 个字体")
    check("主字幕字号默认 42", sp._main_size.value() == 42,
          f"实际 {sp._main_size.value()}")
    check("副字幕字号默认 30", sp._sub_size.value() == 30)
    check("主字幕默认色是白字（#FFFFFF）",
          sp._main_color.color() == "#FFFFFF", sp._main_color.color())
    check("副字幕默认色也是白字",
          sp._sub_color.color() == "#FFFFFF", sp._sub_color.color())
    check("布局下拉 2 项", sp.translate_combo.count() == 2)
    check("预览文字 2 项", sp.text_mode_combo.count() == 2)
    check("预览方向 2 项", sp.orient_combo.count() == 2)

    head("[3] 字体缺失必须给出提示（libass 会静默 fallback）")
    sp._main_font.setCurrentText("NoSuchFontXYZ")
    sp._check_font("main")
    app.processEvents()
    check("无效字体时警告可见", sp._main_font_warn.isVisible())
    check("提示里给了替代字体",
          "Microsoft" in sp._main_font_warn.text()
          or "SimHei" in sp._main_font_warn.text(),
          sp._main_font_warn.text()[:56])
    sp._main_font.setCurrentText("SimHei")
    sp._check_font("main")
    app.processEvents()
    check("换有效字体后警告隐藏", not sp._main_font_warn.isVisible())

    head("[4] 改动 → 预览刷新 → 自动保存")
    original = ss.load_preset("default")
    sp._main_size.setValue(56)
    sp._main_color.set_color("#FF3B30")
    sp._on_change()
    check("内存预设已更新",
          sp._preset.main.size == 56 and sp._preset.main.color == "#FF3B30",
          f"size={sp._preset.main.size} color={sp._preset.main.color}")
    check("保存状态提示为未保存", "自动保存" in sp.save_state.text()
          or "改动" in sp.save_state.text(), sp.save_state.text())

    sp._autosave()
    disk = ss.load_preset("default")
    check("已落盘", disk.main.size == 56 and disk.main.color == "#FF3B30",
          f"size={disk.main.size} color={disk.main.color}")
    check("保存状态提示为已保存", "已保存" in sp.save_state.text(),
          sp.save_state.text())

    head("[5] 预览设置切换")
    sp.text_mode_combo.setCurrentIndex(1)
    sp.orient_combo.setCurrentIndex(1)
    app.processEvents()
    check("文字模式切到 short", sp.preview._text_mode == "short")
    check("方向切到 landscape", sp.preview._orientation == "landscape")
    check("画布跟随方向", sp.preview.canvas_size() == (1280, 720),
          str(sp.preview.canvas_size()))

    sp.set_canvas_from_task(406, 720)
    app.processEvents()
    check("画布可跟随素材尺寸", sp.preview.canvas_size() == (406, 720),
          str(sp.preview.canvas_size()))

    head("[6] 预览渲染（异步）")

    result = {"done": False, "has_pixmap": False}

    def on_timeout():
        try:
            pm = sp.preview.view.pixmap()
            result["has_pixmap"] = pm is not None and not pm.isNull()
            result["done"] = True
            check("预览图已渲染出来", result["has_pixmap"])
            check("预览信息含画布尺寸", "406×720" in sp.preview.info.text(),
                  sp.preview.info.text())
            _finalize()
        except BaseException:
            # ⚠️ 回调里抛出的异常**必须自己打出来**。
            #    Qt 会把它吞掉，表现是「测试挂死、没有任何错误信息」——
            #    实测就因为这个 `FAIL` 的 NameError 把 `app.quit()` 拦掉了，
            #    事件循环永不退出，等了 25 分钟才被外部杀掉。
            traceback.print_exc()
            check("预览回调未抛异常", False,
                  traceback.format_exc(limit=1).strip().splitlines()[-1])
        finally:
            app.quit()          # 无论成败，事件循环都必须能退出来

    def _finalize():
        head("[7] 样式新建 / 切换 / 删除")
        newp = sp._preset.copy_as("夜间模式")
        newp.main.color = "#FFD60A"
        ss.save_as_active(newp)
        sp._refresh_preset_combo("夜间模式")
        app.processEvents()
        lst = [sp.preset_combo.itemText(i)
               for i in range(sp.preset_combo.count())]
        check("新建后下拉含新样式", "夜间模式" in lst, str(lst))
        check("下拉切到新样式", sp.preset_combo.currentText() == "夜间模式",
              sp.preset_combo.currentText())

        sp._load_preset(ss.load_preset("夜间模式"), refresh_preview=False)
        check("新样式颜色已加载", sp._main_color.color() == "#FFD60A",
              sp._main_color.color())

        ss.delete_preset("夜间模式")
        sp._refresh_preset_combo("default")
        lst2 = [sp.preset_combo.itemText(i)
                for i in range(sp.preset_combo.count())]
        check("删除后下拉不含它", "夜间模式" not in lst2, str(lst2))

        # 恢复 default 的出厂值，别把测试痕迹留进用户配置
        sp._load_preset(ss.builtin_default(), refresh_preview=False)
        sp._autosave()
        check("default 已恢复出厂值",
              ss.load_preset("default").main.size == 42)

        head("结果")
        print(f"  通过 {PASS} 项，失败 {FAIL} 项")
        app.quit()

    QTimer.singleShot(2500, on_timeout)
    print("  [诊断] 即将进入事件循环", flush=True)
    try:
        code = app.exec()
        print(f"  [诊断] 事件循环返回 code={code}", flush=True)
    except BaseException:
        traceback.print_exc()
        print("  [诊断] 事件循环抛异常", flush=True)
    finally:
        # 不等线程池 —— 环境探测那个 worker 里的 subprocess 在某些受管环境
        # 下会被阻塞，等它会把测试挂在退出阶段。
        win.pool.wait(300)

    if not result["done"]:
        print()
        print("  ⚠️ 预览回调未在超时前触发")
        FAIL += 1

    print(f"  最终：通过 {PASS}，失败 {FAIL}")
    sys.stdout.flush()
    sys.stderr.flush()
    # 跳过解释器关闭时的非 daemon 线程 join（同上原因）
    import os
    os._exit(1 if FAIL else 0)


if __name__ == "__main__":
    sys.exit(main())
