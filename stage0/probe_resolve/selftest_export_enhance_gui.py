# -*- coding: utf-8 -*-
"""阶段 4-A 自测 —— 导出对话框的「成片增强」区（GUI，离屏模式）。

离屏模式点不了按钮、看不到弹窗，所以这里验的是**控件状态与参数装配**：
用户在下拉里选了什么，最终有没有正确地变成 `EnhanceOptions`。
真正的 ffmpeg 行为由 selftest_enhance.py 覆盖。

跑法：
    QT_QPA_PLATFORM=offscreen python selftest_export_enhance_gui.py
"""
from __future__ import annotations

import faulthandler
import sys
import tempfile
from pathlib import Path

faulthandler.enable()

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from PySide6.QtWidgets import QApplication                     # noqa: E402

from sliceq import enhance, secrets, settings, store                 # noqa: E402

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
    store.init_db()
    app = QApplication(sys.argv)

    from sliceq.ui.pages.export_dialog import ExportDialog

    # 建一个**带转录数据**的任务 —— 否则 burn_available=False，
    # 所有模式提示都会被"没有转录文本"的警告接管，测不到正常分支。
    tid = store.create_task("GUI自测_有字幕", source_path="", width=640, height=360)

    class _Seg:
        def __init__(self, a, b, t):
            self.start_ms, self.end_ms, self.text = a, b, t

    store.save_transcript_segs(tid, [_Seg(0, 1500, "第一句"), _Seg(1500, 3000, "第二句")])
    task = store.get_task(tid)
    clips = [{"id": 1, "start": 0.0, "end": 2.0, "title_hint": "测试片段"}]

    head("1. 增强区默认必须等价于「不做增强」")
    dlg = ExportDialog(task=task, clips=clips)
    app.processEvents()
    opts = dlg._current_enhance()
    check("默认 EnhanceOptions 是 no-op（不改变阶段 3 的老行为）",
          opts.is_noop(), f"fade_in={opts.fade_in} fade_out={opts.fade_out} bgm='{opts.bgm}'")
    check("默认不配乐", opts.bgm == "", repr(opts.bgm))
    check("默认配乐音量 -15dB", opts.bgm_volume_db == -15.0, str(opts.bgm_volume_db))
    check("默认让位强度 = 标准档",
          dlg.duck_combo.currentData() == enhance.DUCK_LEVEL_DEFAULT,
          str(dlg.duck_combo.currentData()))
    check("默认片头淡入 = 不做", float(dlg.fade_in_combo.currentData()) == 0.0)
    check("默认片尾淡出 = 不做（增强必须是显式加法）",
          float(dlg.fade_out_combo.currentData()) == 0.0,
          str(dlg.fade_out_combo.currentData()))
    check("默认不勾封面", not dlg.cover_check.isChecked())
    check("有转录数据时「烧录字幕」可用", dlg.burn_check.isEnabled())

    head("2. 改控件 → 立刻反映到 EnhanceOptions")
    dlg.fade_in_combo.setCurrentIndex(1)          # 0.3
    dlg.bgm_vol_combo.setCurrentIndex(0)          # -6dB
    app.processEvents()
    o = dlg._current_enhance()
    check("片头淡入已生效", o.fade_in == 0.3, str(o.fade_in))
    check("配乐音量已生效", o.bgm_volume_db == -6.0, str(o.bgm_volume_db))
    check("不再 no-op", not o.is_noop())

    head("3. 让位强度四档都要能落到参数上")
    seen = []
    for i in range(dlg.duck_combo.count()):
        dlg.duck_combo.setCurrentIndex(i)
        app.processEvents()
        o = dlg._current_enhance()
        key = dlg.duck_combo.currentData()
        seen.append((key, o.duck, o.duck_threshold, o.duck_ratio))
    check("四档齐全", len(seen) == 4, str([s[0] for s in seen]))
    off = [s for s in seen if s[0] == "off"][0]
    check("「不压低」档 duck=False", off[1] is False, str(off))
    vals = [s[3] for s in seen if s[0] != "off"]
    check("三档的 ratio 不全相同（确实是不同强度）",
          len(set(vals)) > 1 or len(set(s[2] for s in seen if s[0] != "off")) > 1,
          str([(s[0], s[2], s[3]) for s in seen]))
    dlg.duck_combo.setCurrentIndex(
        list(enhance.DUCK_LEVELS).index(enhance.DUCK_LEVEL_DEFAULT))
    app.processEvents()

    head("4. 快速模式 + 增强 → 必须提示会升档")
    dlg.rb_fast.setChecked(True)
    dlg.burn_check.setChecked(False)              # 排除烧字幕那条理由
    app.processEvents()
    note = dlg.mode_note.text()
    check("提示里说明了增强导致的升档", "滤镜" in note or "精确" in note, note[:80])
    dlg.rb_exact.setChecked(True)
    app.processEvents()
    check("只剩「没烧字幕」的常规提示（不再是升档理由）",
          "滤镜" not in dlg.mode_note.text()
          and "srt" in dlg.mode_note.text().lower(),
          dlg.mode_note.text()[:70])
    dlg.burn_check.setChecked(True)
    app.processEvents()
    check("两个都恢复后提示清空", dlg.mode_note.text() == "",
          repr(dlg.mode_note.text()[:60]))

    head("5. 烧字幕 + 增强同时触发升档时，两条理由都要说")
    dlg.rb_fast.setChecked(True)
    dlg.burn_check.setChecked(True)
    app.processEvents()
    note = dlg.mode_note.text()
    check("烧字幕的理由在", "字幕" in note, note[:100])
    check("增强的理由也在", "滤镜" in note or "配乐" in note or "淡入" in note,
          note[:100])
    # 恢复
    dlg.rb_exact.setChecked(True)
    dlg.burn_check.setChecked(False)
    dlg.fade_in_combo.setCurrentIndex(0)
    app.processEvents()

    head("6. BGM 列表与提示")
    check("下拉至少含「不配乐」一项", dlg.bgm_combo.count() >= 1,
          str([dlg.bgm_combo.itemText(i) for i in range(dlg.bgm_combo.count())]))
    check("第一项是「不配乐」", dlg.bgm_combo.itemData(0) == "",
          repr(dlg.bgm_combo.itemData(0)))
    items = enhance.list_bgm()
    if not items:
        check("库为空时给出解释性提示（而不是空白）",
              "空" in dlg.enh_note.text() and "不内置" in dlg.enh_note.text(),
              dlg.enh_note.text()[:70])
    else:
        check("库里已有音乐时提示说明当前选择",
              dlg.enh_note.text() != "" or dlg.bgm_combo.count() > 1,
              f"{len(items)} 首")

    head("7. 运行中要禁用全部增强控件（否则参数会被中途改掉）")
    dlg._set_running(True)
    app.processEvents()
    widgets = [dlg.bgm_combo, dlg.bgm_vol_combo, dlg.duck_combo,
               dlg.fade_in_combo, dlg.fade_out_combo, dlg.cover_check,
               dlg.bgm_dir_btn]
    check("增强控件全部禁用", all(not w.isEnabled() for w in widgets))
    dlg._set_running(False)
    app.processEvents()
    check("恢复后全部可用", all(w.isEnabled() for w in widgets))

    head("8. 没有转录数据时：警告必须可见，禁用在导出结束后仍要保住")
    tid2 = store.create_task("GUI自测_无字幕", source_path="")
    d2 = ExportDialog(task=store.get_task(tid2), clips=clips)
    app.processEvents()
    check("无转录 → 烧字幕被禁用", not d2.burn_check.isEnabled())
    check("警告文本确实显示出来（没被常规提示覆盖）",
          "转录" in d2.mode_note.text(), d2.mode_note.text()[:70])
    d2._set_running(True)
    d2._set_running(False)
    app.processEvents()
    check("导出结束不会把业务层的禁用冲掉（回归点）",
          not d2.burn_check.isEnabled())
    d2.fade_in_combo.setCurrentIndex(2)
    app.processEvents()
    check("无字幕任务仍可配乐/淡入淡出", not d2._current_enhance().is_noop())
    d2.close()

    head("9. 双语字幕控件（阶段 4-B）")
    check("默认不勾双语（增强是显式加法）", not dlg.bi_check.isChecked())
    langs = [dlg.lang_combo.itemData(i) for i in range(dlg.lang_combo.count())]
    check("语言列表不含中文（当译文没意义）", "zh" not in langs, str(langs))
    check("默认译文语言是英语", dlg.lang_combo.currentData() == "en",
          str(dlg.lang_combo.currentData()))
    check("未勾选时没有说明文字", dlg.bi_note.text() == "",
          repr(dlg.bi_note.text()[:40]))
    dlg.bi_check.setChecked(True)
    app.processEvents()
    note = dlg.bi_note.text()
    check("勾选后给出说明", bool(note), note[:60])
    check("说明里提到了费用（BYOK 必须先讲钱）",
          "费用" in note or "账单" in note, note[:90])
    if secrets.get_api_key():
        check("已配 key 时提示正常流程（不报错）", "⚠️" not in note,
              note[:50])
    else:
        check("未配 key 时明确说无法翻译",
              "key" in note.lower() or "⚠️" in note, note[:60])
    dlg.bi_check.setChecked(False)
    app.processEvents()

    head("10. 文案对话框（阶段 4-B · F9）")
    from sliceq.ui.pages.copy_dialog import CopyDialog
    cd = CopyDialog(task=task, clips=clips)
    app.processEvents()
    check("文案对话框可构建", cd is not None)
    check("说明里写明会调模型并注明费用",
          "费用" in cd.summary.text() or "账单" in cd.summary.text(),
          cd.summary.text()[:80])
    check("复制按钮初始禁用（没内容可复制）", not cd.copy_btn.isEnabled())
    cd.text.setPlainText("【片段 1】\n  标题1：测试")
    app.processEvents()
    cd.copy_btn.setEnabled(True)
    cd._copy_all()
    app.processEvents()
    check("复制后状态更新",
          "剪贴板" in cd.status.text(), cd.status.text()[:40])
    cd.close()

    dlg.close()
    # 清掉测试任务，别污染用户的库
    head("11. 导出内容（阶段 5）：三项复选 + 联动置灰")
    d = ExportDialog(task=task, clips=clips)
    app.processEvents()
    check("默认勾「成片片段」（不改变既有行为）", d.clips_check.isChecked())
    check("默认不勾剪映草稿", not d.jy_check.isChecked())
    check("默认不勾达芬奇时间线", not d.dv_check.isChecked())
    check("勾着成片时，切片模式可用", d.rb_exact.isEnabled())
    check("勾着成片时，增强控件可用", d.bgm_combo.isEnabled())

    # ── 取消成片、改勾草稿 → 成片相关的控件必须一起置灰 ──
    d.clips_check.setChecked(False)
    d.jy_check.setChecked(True)
    app.processEvents()
    check("★ 取消成片后，切片模式置灰", not d.rb_exact.isEnabled())
    check("★ 取消成片后，增强控件置灰", not d.bgm_combo.isEnabled())
    check("★ 取消成片后，「烧录字幕」置灰", not d.burn_check.isEnabled())
    check("草稿复选仍可用", d.jy_check.isEnabled())
    check("成片复选仍可用（能改回来）", d.clips_check.isEnabled())
    note = d.content_note.text()
    check("★ 提示里写明「字幕样式不会跟随导出」", "字幕样式" in note,
          note[:78])
    check("提示里说明了要在目标软件里手动完成最后一步", "手动" in note, "")

    # ── 三项全不勾 → 必须明确警告 ──
    d.jy_check.setChecked(False)
    app.processEvents()
    check("★ 三项全不勾时给出明确警告",
          "不会有任何产出" in d.content_note.text(),
          d.content_note.text()[:60])

    # ── 勾回成片 → 控件恢复 ──
    d.clips_check.setChecked(True)
    app.processEvents()
    check("重新勾上成片后，切片模式恢复可用", d.rb_exact.isEnabled())
    check("重新勾上成片后，增强控件恢复可用", d.bgm_combo.isEnabled())
    check("三项全勾时不报警告", "不会有任何产出" not in d.content_note.text())

    # ── 剪映版本提示（本机 11.5.3 已验证 → 不该报未验证）──
    d.jy_check.setChecked(True)
    d.clips_check.setChecked(False)
    app.processEvents()
    note2 = d.content_note.text()
    check("已验证版本不误报「兼容性未验证」",
          "兼容性未经验证" not in note2, note2[:70])
    d.close()

    try:
        store.delete_task(tid)
        store.delete_task(tid2)
    except BaseException:                          # noqa: BLE001
        pass
    print()
    print("=" * 72)
    print(f"通过 {PASS} / 失败 {FAIL} / 共 {PASS + FAIL}")
    print("=" * 72)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
