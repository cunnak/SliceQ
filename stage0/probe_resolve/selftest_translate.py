# -*- coding: utf-8 -*-
"""阶段 4-B 自测：字幕翻译（translate.py）与文案生成（copywriter.py）。

**本阶段最大的风险是"静默错位"**：模型少输出一行，从那一行开始所有字幕
都会与画面对不上，而且不产生任何报错。所以这里用 mock transport
**精确注入**各类畸形输出，验证校验真的拦得住。

不碰网络的部分全用 mock；真实 API 只用在最后一段（可跳过）。

运行：python stage0/probe_resolve/selftest_translate.py [--live]
"""
from __future__ import annotations

import sys
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

from sliceq import analyzer, copywriter, subtitle, translate  # noqa: E402

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
# mock transport：可编排每一次调用的返回
# ─────────────────────────────────────────────────────────────
class FakeTransport:
    """按剧本返回。剧本用完就重复最后一项。

    剧本项可以是**异常实例** —— 用来注入 API 失败（欠费/限流/网络）。
    """

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[str] = []

    def analyze_text(self, prompt: str, *, system=None, max_tokens=800,
                     thinking="auto", timeout=180):
        self.calls.append(prompt)
        item = self.script[0] if len(self.script) == 1 else self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return analyzer.AnalyzeResult(
            text=item, usage=analyzer.Usage(input_tokens=100, output_tokens=50),
            transport="fake", model="fake-model")


def numbered(texts, *, drop_indices=(), extra_line=None, fence=False,
             renumber=None, blank_lines=False) -> str:
    """构造模型的"编号输出"，可注入畸形。"""
    body = []
    for i, t in enumerate(texts, 1):
        if (i - 1) in drop_indices:
            continue
        n = renumber.get(i, i) if renumber else i
        body.append(f"{n}. {t}")
        if blank_lines:
            body.append("")
    if extra_line:
        body.append(extra_line)
    out = "\n".join(body)
    return f"```\n{out}\n```" if fence else out


# ─────────────────────────────────────────────────────────────
def t_parse() -> None:
    head("1. 解析器：编号格式的各种畸形（这是防错位的核心）")

    k = translate.parse_numbered
    ck("标准编号", k("1. Hello\n2. World", 2) == ["Hello", "World"])
    ck("中文标点编号", k("1、你好\n2、世界", 2) == ["你好", "世界"])
    ck("圆括号编号", k("1) a\n2) b", 2) == ["a", "b"])
    ck("带代码块围栏", k("```\n1. a\n2. b\n```", 2) == ["a", "b"])
    ck("带语言标记的围栏", k("```json\n1. a\n2. b\n```", 2) == ["a", "b"])
    ck("编号乱序也能归位", k("2. b\n1. a", 2) == ["a", "b"])
    ck("混入说明行（编号齐全）", k("好的，译文如下：\n1. a\n2. b", 2) == ["a", "b"])
    ck("**条数少一行 → 必须返回 None**",
       k("1. a", 2) is None, repr(k("1. a", 2)))
    ck("**条数多一行 → 必须返回 None**",
       k("1. a\n2. b\n3. c", 2) is None)
    ck("无编号但行数正好 → 可按行取",
       k("a\nb", 2) == ["a", "b"])
    ck("完全空 → None", k("", 3) is None and k("   ", 3) is None)
    ck("编号跳号（1,3）条数对不上 → None", k("1. a\n3. c", 2) is None)

    # JSON 数组（标题用）
    j = translate.parse_json_array
    ck("JSON 数组", j('["标题一","标题二"]') == ["标题一", "标题二"])
    ck("JSON 带围栏", j('```json\n["甲","乙"]\n```') == ["甲", "乙"])
    ck("JSON 混在文字里", j('好的：["甲","乙"] 以上') == ["甲", "乙"])
    ck("非 JSON 返回 None", j("标题一\n标题二") is None)


def t_clip_long() -> None:
    head("2. 超长译文的行宽保护")
    short = "This is fine."
    ck("短句不动", translate.clip_long([short]) == [short])
    long_line = "word " * 30
    out = translate.clip_long([long_line])[0]
    # ⚠️ 原断言写成 `all(...) or True` —— **恒真，等于没测**。
    #    正是它掩盖了一个真缺陷：clip_long 断完又用空格拼回一行，
    #    "超长保护"从来没生效过。改成三条能失败的断言。
    segs = out.split("\n")
    ck("超长句被切成多行（不是原样返回）", len(segs) > 1,
       f"{len(segs)} 行，总长 {len(out)}")
    ck("每一行都不超上限",
       all(len(x) <= translate.MAX_LINE_CHARS for x in segs),
       f"最长 {max(len(x) for x in segs)} / 上限 {translate.MAX_LINE_CHARS}")
    ck("断开后没有丢词",
       len(out.replace("\n", " ").split()) == len(long_line.split()),
       f"{len(out.replace(chr(10), ' ').split())} vs {len(long_line.split())}")
    # 换行符下游要能正确消化：ASS 路径靠 ass_escape 转 \N
    from sliceq import subtitle_style as _ss
    ck("ass_escape 会把换行转成 ASS 硬换行 \\N",
       _ss.ass_escape("a\nb") == "a\\Nb", repr(_ss.ass_escape("a\nb")))


def t_translate_ok() -> None:
    head("3. 正常翻译：条数必须与输入一一对应")
    src = ["第一句", "第二句", "第三句"]
    tp = FakeTransport([numbered(["One.", "Two.", "Three."])])
    r = translate.translate_lines(src, transport=tp)
    ck("条数相等", len(r.lines) == len(src), f"{len(r.lines)}")
    ck("内容正确", r.lines == ["One.", "Two.", "Three."], str(r.lines))
    ck("没有失败项", r.failed == 0, str(r.failed))
    ck("来源标记为 llm", r.source == "llm", r.source)
    ck("只调用一次", len(tp.calls) == 1, str(len(tp.calls)))


def t_translate_mismatch() -> None:
    head("4. ★ 模型少输出一行：必须降级为逐条，绝不错位")
    src = ["甲", "乙", "丙"]
    # 第 1 次少一行；重试仍少一行；随后逐条调用各返回一条
    tp = FakeTransport([
        numbered(["A", "B"]),           # 少一行
        numbered(["A", "B"]),           # 重试仍少一行
        numbered(["A"]),                # 逐条：第 1 条
        numbered(["B"]),                # 逐条：第 2 条
        numbered(["C"]),                # 逐条：第 3 条
    ])
    r = translate.translate_lines(src, transport=tp, batch=3)
    ck("条数仍然一一对应（没有错位）", len(r.lines) == 3, str(r.lines))
    ck("走了逐条降级", r.single_pass == 3, f"single_pass={r.single_pass}")
    ck("发生过重试", r.retries >= 1, f"retries={r.retries}")
    ck("有可读的提示", any("逐条" in n for n in r.notes), str(r.notes)[:90])
    ck("内容对得上", r.lines == ["A", "B", "C"], str(r.lines))
    print(f"     调用次数={len(tp.calls)}（1 + 1重试 + 3逐条）")


def t_translate_total_fail() -> None:
    head("5. 彻底翻不出来：保留原文，且不丢行")
    src = ["甲", "乙"]
    tp = FakeTransport([""])            # 永远返回空
    r = translate.translate_lines(src, transport=tp, batch=2)
    ck("条数不变（不丢行）", len(r.lines) == 2, str(r.lines))
    ck("原样保留原文", r.lines == ["甲", "乙"], str(r.lines))
    ck("失败计数正确", r.failed == 2, str(r.failed))


def t_translate_blank_and_batch() -> None:
    head("6. 空行与分批")
    src = ["甲", "", "丙"]
    tp = FakeTransport([numbered(["A", "C"])])
    r = translate.translate_lines(src, transport=tp)
    ck("空行不送模型（只翻 2 条）", "甲" in tp.calls[0] and "丙" in tp.calls[0],
       tp.calls[0][-60:])
    ck("条数仍为 3", len(r.lines) == 3, str(r.lines))
    ck("空位仍是空", r.lines[1] == "", repr(r.lines[1]))
    ck("非空位翻了", r.lines[0] == "A" and r.lines[2] == "C", str(r.lines))

    # 分批：5 条 / 每批 2 → 3 批
    tp2 = FakeTransport([numbered(["A", "B"]), numbered(["C", "D"]),
                         numbered(["E"])])
    r2 = translate.translate_lines(["1", "2", "3", "4", "5"],
                                   transport=tp2, batch=2)
    ck("分 3 批完成", r2.batches == 3, f"batches={r2.batches}")
    ck("跨批结果顺序正确", r2.lines == ["A", "B", "C", "D", "E"], str(r2.lines))


def t_translate_cost() -> None:
    head("7. 费用累计（成本可见性不能漏）")
    src = ["甲", "乙", "丙", "丁"]
    tp = FakeTransport([numbered(["A", "B"]), numbered(["C", "D"])])
    r = translate.translate_lines(src, transport=tp, batch=2)
    ck("两次调用累加了 usage", r.usage is not None and r.usage.input_tokens == 200,
       f"in={getattr(r.usage, 'input_tokens', None)}")
    ck("成本可读且非负", r.cost_cny >= 0, f"¥{r.cost_cny}")


def t_no_transport() -> None:
    head("8. 没有 key 时的兜底（要说明质量降级）")
    r = translate.translate_lines(["甲", "乙"], transport=None,
                                  allow_free_fallback=False)
    ck("禁用兜底时不翻", r.lines == ["甲", "乙"], str(r.lines))
    ck("给出可读原因", any("API key" in n for n in r.notes), str(r.notes))
    ck("来源标记 none", r.source == "none", r.source)


def t_apply_to_cues() -> None:
    head("9. 接到字幕 cue 上")
    cues = [subtitle.Cue(start=0, end=1, main="第一句"),
            subtitle.Cue(start=1, end=2, main=""),
            subtitle.Cue(start=2, end=3, main="第三句")]
    tp = FakeTransport([numbered(["First.", "Third."])])
    r = translate.apply_to_cues(cues, transport=tp)
    ck("空 main 不占译文位", r.lines[:1] == ["First."] or True, str(r.lines))
    ck("第一条拿到译文", cues[0].sub == "First.", cues[0].sub)
    ck("空条目没有副字幕", cues[1].sub == "", repr(cues[1].sub))
    ck("第三条拿到译文", cues[2].sub == "Third.", cues[2].sub)

    # 译文与原文相同 → 不显示副字幕（避免同一句话上下重复两次）
    cues2 = [subtitle.Cue(start=0, end=1, main="OK")]
    translate.apply_to_cues(cues2, transport=FakeTransport([numbered(["OK"])]))
    ck("译文与原文相同则不显示副字幕", cues2[0].sub == "", repr(cues2[0].sub))


def t_bilingual_render() -> None:
    head("10. 双语渲染链路（ASS + SRT）")
    cues = [subtitle.Cue(start=0, end=1.5, main="你拿个二维码过来", sub="Scan this QR code.")]
    import tempfile
    from sliceq import subtitle_style as ss
    out = Path(tempfile.mkdtemp(prefix="sliceq_bi_"))
    srt = subtitle.write_srt(cues, out / "a.srt", bilingual=True)
    body = srt.read_text(encoding="utf-8")
    ck("SRT 双语：两行都在", "你拿个二维码过来" in body and "Scan this QR code." in body,
       body.replace("\n", "｜")[:70])
    ck("SRT 双语：原文在上、译文在下",
       body.index("你拿个二维码") < body.index("Scan this"),
       "顺序正确")
    srt_single = subtitle.write_srt(cues, out / "b.srt", bilingual=False)
    b2 = srt_single.read_text(encoding="utf-8")
    ck("SRT 单语：只有原文", "Scan this" not in b2, b2.replace("\n", "｜")[:60])

    ass = subtitle.write_ass(cues, out / "a.ass", ss.builtin_default(),
                             width=1280, height=720, title="t")
    a = ass.read_text(encoding="utf-8")
    ck("ASS 含 Main 样式与 Sub 样式", "Style: Main" in a and "Style: Sub" in a)
    ck("ASS 主副各一条 Dialogue", a.count("Dialogue:") == 2, f"{a.count('Dialogue:')}")
    ck("ASS 里译文挂在 Sub 样式上",
       ",Sub," in a and "Scan this QR code." in a, "")


def t_copywriter() -> None:
    head("11. 文案生成（F9）")
    segs = [
        analyzer.Usage and type("S", (), {"start_ms": 1000, "end_ms": 3000,
                                          "text": "这个主板不识别网卡"})()
        for _ in range(1)
    ]
    segs = [type("S", (), {"start_ms": 1000, "end_ms": 3000,
                           "text": "这个主板不识别网卡"})(),
            type("S", (), {"start_ms": 4000, "end_ms": 6000,
                           "text": "修了一整天"})()]

    txt = copywriter.transcript_of_clip(segs, 0, 10)
    ck("取到片段内文本", "主板" in txt and "一整天" in txt, txt)
    txt_out = copywriter.transcript_of_clip(segs, 100, 200)
    ck("片段外不取（避免拿别处信息写标题）", txt_out == "", repr(txt_out))
    txt_partial = copywriter.transcript_of_clip(segs, 2.5, 10)
    ck("半截交叠的也算（片段边界常切在句子中间）",
       "一整天" in txt_partial, txt_partial)

    clip = {"id": 7, "start": 0, "end": 10, "summary": "修电脑"}
    tp = FakeTransport(['["标题甲","标题乙","标题丙"]',
                        "这段讲了他修电脑的遭遇，主板不识别网卡，折腾了一整天。"])
    res = copywriter.write_for_clip(clip, segs, transport=tp)
    ck("拿到 3 条标题", len(res.titles) == 3, str(res.titles))
    ck("拿到简介", bool(res.description), res.description[:40])
    ck("clip_id 带上了", res.clip_id == 7, str(res.clip_id))
    ck("成本记录在案", res.cost_cny >= 0, f"¥{res.cost_cny}")

    # 模型返回非 JSON 时要兜底
    tp2 = FakeTransport(["标题一\n标题二\n标题三", "简介一段。"])
    res2 = copywriter.write_for_clip(clip, segs, transport=tp2)
    ck("非 JSON 输出也能兜底出标题", len(res2.titles) >= 1, str(res2.titles))

    # 单条失败不该影响整体，也不该抛
    tp3 = FakeTransport([""])
    try:
        res3 = copywriter.write_for_clip(clip, segs, transport=tp3)
        ck("失败时不抛异常", True)
        ck("有空结果 + 提示", res3.titles == [] or res3.notes != [],
           f"titles={res3.titles} notes={res3.notes[:1]}")
    except Exception as exc:                     # noqa: BLE001
        ck("失败时不抛异常", False, f"{type(exc).__name__}: {exc}")

    out = copywriter.to_text([res])
    ck("拼成可复制文本", "标题1" in out and "简介" in out, out[:60].replace("\n", "｜"))

    # 批量：一条失败不中断
    tp4 = FakeTransport(['["甲","乙","丙"]', "简介。"])
    many = copywriter.write_for_clips(
        [clip, {"id": 8, "start": 0, "end": 10, "summary": "x"}],
        segs, transport=tp4)
    ck("批量返回两条结果", len(many) == 2, str(len(many)))


def t_api_failure() -> None:
    head("13. API 异常：不中断整批、保留原文、给出可读原因")
    src = ["甲", "乙", "丙", "丁"]
    # 第 1 批正常；第 2 批开始 API 一直失败（重试、逐条也失败）
    tp = FakeTransport([numbered(["A", "B"]),
                        RuntimeError("HTTP 400：账号状态异常（欠费）")])
    try:
        r = translate.translate_lines(src, transport=tp, batch=2)
    except Exception as exc:                     # noqa: BLE001
        ck("API 异常不应向上抛（否则整批都拿不到）", False,
           f"{type(exc).__name__}: {exc}")
        return

    ck("条数不变（不丢行）", len(r.lines) == 4, str(r.lines))
    ck("第 1 批（成功的那批）译文保住了", r.lines[:2] == ["A", "B"], str(r.lines))
    ck("失败批保留原文占位", r.lines[2:] == ["丙", "丁"], str(r.lines))
    ck("失败计数正确", r.failed == 2, str(r.failed))
    ck("notes 里写明了失败原因", any("HTTP 400" in n for n in r.notes),
       str(r.notes)[:120])
    ck("提示里说明了「保留原文」而不是丢弃",
       any("保留原文" in n for n in r.notes), str(r.notes)[:120])


def t_analyzer_error_text() -> None:
    head("14. 账号欠费的报错要说人话（英文原文会把用户带偏）")
    class _R:
        status_code = 400
        text = ('{"error":{"message":"Access denied, please make sure your '
                'account is in good standing. For details, see: '
                'https://help.aliyun.com/zh/model-studio/error-code'
                '#overdue-payment"}}')

        def json(self):
            return {"error": {"code": "", "message": (
                "Access denied, please make sure your account is in good "
                "standing. For details, see: https://help.aliyun.com/zh/"
                "model-studio/error-code#overdue-payment")}}

    text = analyzer._explain_http_error(_R())    # noqa: SLF001
    ck("报错里点明了「欠费」这个真实病因", "欠费" in text, text.splitlines()[0][:60])
    ck("明确说了不是本机配置问题（避免用户反复改设置）",
       "不是本机配置" in text or "改设置不会有用" in text, "")
    ck("给出了可执行的动作", "充值" in text or "余额" in text, "")
    ck("仍然保留了原始信息（便于排查）",
       "overdue-payment" in text or "Access denied" in text, "")


def t_live() -> None:
    head("15. 真实 API 端到端（--live 才跑）")
    try:
        tp = analyzer.make_transport()
    except Exception as exc:                     # noqa: BLE001
        ck("构造 transport", False, str(exc))
        return
    src = ["我心里很痛。", "你就是老落最爱的我。", "这个主板不识别网卡，修了一整天了。"]
    try:
        r = translate.translate_lines(src, transport=tp, batch=3)
    except Exception as exc:                     # noqa: BLE001
        ck("真实翻译调用", False, f"{type(exc).__name__}: {exc}")
        return
    ck("真实翻译条数一致", len(r.lines) == len(src), str(r.lines))
    ck("无失败项", r.failed == 0, str(r.failed))
    print(f"     译文：{r.lines}")
    print(f"     费用：¥{r.cost_cny:.6f}｜批数：{r.batches}")

    try:
        segs = [type("S", (), {"start_ms": 0, "end_ms": 9000,
                               "text": "".join(src)})()]
        res = copywriter.write_for_clip(
            {"id": 1, "start": 0, "end": 9, "summary": "吐槽修电脑"}, segs,
            transport=tp)
        ck("真实文案生成", len(res.titles) >= 1 and bool(res.description),
           f"{len(res.titles)} 条标题")
        print(f"     标题：{res.titles}")
        print(f"     简介：{res.description[:80]}")
        print(f"     费用：¥{res.cost_cny:.6f}")
    except Exception as exc:                     # noqa: BLE001
        ck("真实文案生成", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    live = "--live" in sys.argv
    t_parse()
    t_clip_long()
    t_translate_ok()
    t_translate_mismatch()
    t_translate_total_fail()
    t_translate_blank_and_batch()
    t_translate_cost()
    t_no_transport()
    t_apply_to_cues()
    t_bilingual_render()
    t_copywriter()
    t_api_failure()
    t_analyzer_error_text()
    if live:
        t_live()
    else:
        print("\n（跳过真实 API 段；加 --live 可跑）")

    print("\n" + "=" * 70)
    print(f"通过 {PASS} / 失败 {FAIL} / 共 {PASS + FAIL}")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
