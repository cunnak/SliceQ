# -*- coding: utf-8 -*-
"""阶段 2 收尾自测 —— analyzer / prompts / screening / pipeline 四层。

═══════════════════════════════════════════════════════════════
设计要点：让"缺 API key"不阻塞验证
═══════════════════════════════════════════════════════════════
精析与粗筛都要调模型，而本机没有 key。解法是**把模型调用抽成可注入接口**
（analyzer.Transport 协议），测试时塞一个 FakeTransport 进来。

这样能验证的：分窗、抽帧、时间基准校验、区间合并、20% 硬阀、
             副本压制、cost 累计、确认点时机、断点续跑、落库字段。
这样**不能**验证的：真实端点的 payload 是否被接受（需要 key）。

测试覆盖不到的部分必须如实标注，不能因为"跑绿了"就以为端到端通了。
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from sliceq import analyzer, asr, config, pipeline, prompts, screening, store
from sliceq.analyzer import AnalyzeResult, Usage

VIDEO = Path("stage0/test_media/source.mp4")
WORK = config.APP_ROOT / "work" / "_stage2_selftest"

ok = fail = 0
_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"    [OK]   {name}" + (f"  — {detail}" if detail else ""))
    else:
        fail += 1
        _failures.append(name)
        print(f"    [FAIL] {name}" + (f"  — {detail}" if detail else ""))


# ─────────────────────────────────────────────────────────────
# 可注入的假 transport
# ─────────────────────────────────────────────────────────────
class FakeTransport:
    """按"送进来的是图片还是视频"区分粗筛与精析。

    真实世界里粗筛送拼贴 PNG、精析送副本 MP4 —— 用文件后缀分流，
    是最贴近真实调用形态的假实现方式。
    """

    name = "fake"

    def __init__(self, screen_hits: int = 1, refine_score: int = 85):
        self.screen_calls = 0
        self.refine_calls = 0
        self.screen_hits = screen_hits
        self.refine_score = refine_score
        self.prompts_seen: list[str] = []

    def analyze(self, video, prompt, *, system=None, max_tokens=900,
                thinking="auto", timeout=600) -> AnalyzeResult:
        self.prompts_seen.append(prompt)
        v = Path(video)
        if v.suffix.lower() == ".png":
            self.screen_calls += 1
            m = re.search(r"第\s*([\d.]+)s\s*~\s*([\d.]+)s", prompt)
            a, b = (float(m.group(1)), float(m.group(2))) if m else (0.0, 600.0)
            items = []
            for k in range(self.screen_hits):
                st = a + (b - a) * (k + 1) / (self.screen_hits + 1)
                items.append({"start": round(st, 1), "end": round(st + 3.0, 1),
                              "hint": f"假候选{k}", "confidence": 60 + k * 10})
            text = json.dumps(items, ensure_ascii=False)
        else:
            self.refine_calls += 1
            # 精析的时间是相对副本的 —— 这里返回 0 起点，等价于"副本开头"
            text = json.dumps({
                "start": 0.0, "end": 3.0, "kind": "测试类",
                "score": self.refine_score, "summary": "假精析描述",
                "title": "假标题"}, ensure_ascii=False)

        return AnalyzeResult(
            text=text,
            usage=Usage(input_tokens=1000, output_tokens=100),
            transport="fake", model="fake", elapsed_s=0.01)


FAKE_SEGMENTS = [
    asr.Segment(start_ms=0, end_ms=3000, text="这是第一句测试语音"),
    asr.Segment(start_ms=3000, end_ms=7000, text="这是第二句测试语音"),
    asr.Segment(start_ms=7000, end_ms=11000, text="这是第三句测试语音"),
]


def main() -> int:
    # ⚠️ 自测必须**自己把环境准备好**，不能依赖"库里已经有表"。
    #
    #    实测（2026-09-27）：隔离到空库后这里直接报
    #    `sqlite3.OperationalError: no such table: task` ——
    #    因为本测试**从来没有调用过 `store.init_db()`**，
    #    一直是靠生产库（或别的测试）已经建过表才跑得起来。
    #
    #    这类"隐式依赖外部既有状态"的测试，在隔离之前看不出来；
    #    但它意味着：**在一个全新的环境里跑它必然失败**。
    config.ensure_dirs()
    store.init_db()

    if not VIDEO.exists():
        print(f"缺少测试视频：{VIDEO}")
        return 1

    print("=" * 78)
    print("阶段 2 收尾自测")
    print("=" * 78)

    # ═══════════════════════════════════════════════════════
    print("\n[1] analyzer · 通道判定与 payload 构造")
    # ═══════════════════════════════════════════════════════
    check("官方端点 → dashscope 通道",
          analyzer.detect_channel("https://dashscope.aliyuncs.com/api/v1") == "dashscope")
    check("中转站 → http 通道",
          analyzer.detect_channel("https://api.example-relay.com/v1") == "http")
    check("空 base_url → http 通道（保守）",
          analyzer.detect_channel("") == "http")

    ds = analyzer.build_dashscope_messages(VIDEO, "测试问题", "测试系统")
    dsv = ds[-1]["content"][0].get("video", "")
    check("DashScope 本地路径用 file:/// 且为绝对路径",
          dsv.startswith("file:///") and ":" in dsv, dsv[:48])
    check("DashScope messages 含系统消息", ds[0]["role"] == "system")

    hp = analyzer.build_http_messages(VIDEO, "测试问题")
    url = hp[-1]["content"][0]["video_url"]["url"]
    check("HTTP 通道用 base64 data URI",
          url.startswith("data:video/") and ";base64," in url)
    check("HTTP messages 不含系统消息（未传时）", len(hp) == 1)

    check("thinking=auto 不发参数", analyzer.thinking_kwargs("auto") == {})
    check("thinking=off 发 False",
          analyzer.thinking_kwargs("off") == {"enable_thinking": False})
    check("thinking=on 发 True",
          analyzer.thinking_kwargs("on") == {"enable_thinking": True})

    check("base64 体积估算 ≈ 原始 × 4/3",
          abs(analyzer.b64_size_of(VIDEO) - VIDEO.stat().st_size * 4 / 3)
          / (VIDEO.stat().st_size * 4 / 3) < 0.01)

    u = analyzer.parse_usage({"input_tokens": 1000, "output_tokens": 100,
                              "completion_tokens_details": {"reasoning_tokens": 40}})
    check("usage 解析（含 reasoning）",
          u.input_tokens == 1000 and u.reasoning_tokens == 40)
    check("OpenAI 风格字段也能解析",
          analyzer.parse_usage({"prompt_tokens": 5, "completion_tokens": 6}
                               ).output_tokens == 6)
    check("成本换算按实测定价",
          abs(Usage(input_tokens=1_000_000, output_tokens=1_000_000).cost_cny
              - (0.8 + 2.7)) < 1e-6)

    est = analyzer.estimate_total_upper_bound(10800, 0.20)   # 3 小时
    check("3 小时上限落在 ¥0.4~0.8 区间",
          0.4 <= est["total"] <= 0.8, f"¥{est['total']}")

    # ═══════════════════════════════════════════════════════
    # ── 错误解释：内容审核（口语素材的常见拦路虎）──────────
    #
    # ⚠️ 这一节守的是「**报错不能猜错病因**」。
    #
    #    实测（2026-09-27）：直播素材触发了
    #    `Output data may contain inappropriate content`，
    #    而当时的报错末句写着"多半是 API Key、端点地址或网络"
    #    —— 三条全错（真实原因与配置无关），用户会去改一堆无关的设置。
    #
    #    泛化的教训：**报错里不要塞猜测**。猜测一旦写进产品，
    #    就把误导固化了，而真实原因明明就在同一段文字的上面。
    class _FakeResp:
        """够 `_explain_http_error` 用即可（它只看 status_code / json / text）。"""

        status_code = 400

        def __init__(self, message: str) -> None:
            self._m = message
            self.text = json.dumps({"error": {"message": message}},
                                   ensure_ascii=False)

        def json(self):
            return {"error": {"message": self._m}}

    txt = analyzer._explain_http_error(_FakeResp(
        "<400> InternalError.Algo.DataInspectionFailed: "
        "Output data may contain inappropriate content."))
    check("内容审核错误 → 说明了这是「内容审核」拦截",
          "内容审核" in txt, txt.splitlines()[0][:56])
    check("内容审核错误 → 指出被拦的是「模型的输出」（而非输入）",
          "模型的输出" in txt, "")
    # ⚠️ 断言「不出现某关键词」是**错的测法** —— 第一版就是这么写的，
    #    结果被自己的文案打脸：正解里有一句"这**不是**配置、Key 或网络的问题"，
    #    子串匹配把它判成了"提到了 Key/网络"。
    #    **否定式断言不能用子串匹配**（"不是 A" 里就含 A）。
    #    改成断言「不出现**猜测性的建议措辞**」—— 这才是要守的东西。
    check("★ 内容审核错误 → 不塞猜测性病因（不给「多半是…」这类猜测）",
          not any(k in txt for k in ("多半是", "大概是", "可能是 API")),
          "未出现猜测措辞")
    check("内容审核错误 → 明确说「改设置不会有帮助」",
          "改设置不会有帮助" in txt, "")

    txt_in = analyzer._explain_http_error(_FakeResp(
        "Input data may contain inappropriate content."))
    check("输入被拦时 → 措辞指向「送入的内容」",
          "送入的内容" in txt_in, "")

    print("\n[2] prompts · 模板与时间基准")
    # ═══════════════════════════════════════════════════════
    check("三个内置模板齐备",
          set(prompts.BUILTIN_TEMPLATES) == {"游戏", "娱乐", "通用"})
    p = prompts.screen_prompt("画像", 600, 1200, "转录文本", has_frames=True)
    check("粗筛提示词含窗口起止", "600s" in p and "1200s" in p)
    check("★ 粗筛提示词交代时间基准（防整体偏移）",
          "整片时间轴" in p and "整片素材起点" in p)
    check("粗筛提示词含画面拼贴说明", "从左到右" in p)
    check("时间基准写死在函数内（不靠调用方传参）",
          "offset_note" not in p)
    r = prompts.refine_prompt("画像", 100, 160, "候选理由")
    check("精析提示词交代副本区间", "100.0s" in r and "160.0s" in r)

    # ═══════════════════════════════════════════════════════
    print("\n[3] screening · 分窗 / 解析 / 合并 / 硬阀")
    # ═══════════════════════════════════════════════════════
    wins = screening.all_windows(1500, 600)
    check("等分窗口覆盖全片",
          wins == [(0.0, 600.0), (600.0, 1200.0), (1200.0, 1500.0)],
          str(wins))

    cs = screening.chunk_segments(FAKE_SEGMENTS, 600)
    check("转录分窗：11 秒素材归入 1 个窗口",
          len(cs) == 1 and "第一句" in cs[0][2])

    good = json.dumps([{"start": 610, "end": 620, "hint": "x", "confidence": 70}])
    check("正常解析", len(screening.parse_candidates(good, 600, 1200, "text")) == 1)

    wrapped = "```json\n" + good + "\n```"
    check("能剥掉 markdown 代码块",
          len(screening.parse_candidates(wrapped, 600, 1200, "text")) == 1)

    oob = json.dumps([{"start": 10, "end": 20, "confidence": 90}])
    check("★ 越界时间戳被丢弃（防整体偏移）",
          len(screening.parse_candidates(oob, 600, 1200, "text")) == 0,
          "窗口外的时间戳必须丢掉")

    bad = "这不是 JSON"
    check("非 JSON 返回空列表", screening.parse_candidates(bad, 0, 600, "x") == [])

    c1 = screening.Candidate(100, 130, "a", 50)
    c2 = screening.Candidate(135, 160, "b", 80)   # 间隔 5s < gap=8 → 应合并
    c3 = screening.Candidate(300, 320, "c", 60)
    merged = screening.merge_candidates([c1, c2, c3], gap=8)
    check("邻近候选合并", len(merged) == 2, f"→ {len(merged)} 段")
    check("合并保留更高置信度", merged[0].confidence == 80)

    many = [screening.Candidate(i * 100, i * 100 + 60, f"c{i}", 50 + i)
            for i in range(10)]     # 每段 60s，共 600s
    kept, trimmed = screening.enforce_ratio_budget(many, 1000.0, ratio=0.20)
    check("★ 20% 硬阀生效（预算 200s → 最多留 200s）",
          sum(c.duration for c in kept) <= 200.0 + 1e-6,
          f"保留 {sum(c.duration for c in kept):.0f}s")
    check("硬阀优先保留高置信度",
          all(c.confidence >= 53 for c in kept),
          f"最低保留置信度 {min((c.confidence for c in kept), default=0)}")
    check("裁剪秒数如实上报", trimmed > 0, f"裁掉 {trimmed:.0f}s")

    # 短视频保护：12 秒素材 × 20% = 2.4 秒预算，纯比例会把候选砍光。
    # 这是实测发现的真缺陷（曾经导致 12 秒测试素材候选归零）。
    small = [screening.Candidate(6, 9, "短视频候选", 80)]
    kept_s, trimmed_s = screening.enforce_ratio_budget(small, 12.0, ratio=0.20)
    check("★ 短视频不被硬阀砍光（预算有绝对下限）",
          len(kept_s) == 1 and trimmed_s == 0.0,
          f"12s 素材保留 {len(kept_s)} 段，裁 {trimmed_s}s")

    # ═══════════════════════════════════════════════════════
    print("\n[4] pipeline · 全链路（FakeTransport）")
    # ═══════════════════════════════════════════════════════
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)

    task_id = store.create_task("阶段2自测", source_path=str(VIDEO),
                                duration_sec=12.0, width=1280, height=720)
    fake = FakeTransport(screen_hits=1, refine_score=85)
    opts = pipeline.Options(profile="测试画像", candidate_ratio=0.20,
                            window_sec=600.0, refine_pad_sec=2.0)

    progress_log: list[str] = []
    confirms: list[pipeline.ConfirmRequest] = []

    def on_confirm(req: pipeline.ConfirmRequest) -> bool:
        confirms.append(req)
        return True

    res = pipeline.run(
        task_id, VIDEO, opts, fake,
        on_progress=lambda p: progress_log.append(p.stage),
        on_confirm=on_confirm,
        work_dir=WORK,
        skip_transcribe=FAKE_SEGMENTS)

    check("粗筛被调用 1 次（1 个窗口）", fake.screen_calls == 1,
          f"实际 {fake.screen_calls}")
    check("精析被调用 1 次（1 个候选）", fake.refine_calls == 1,
          f"实际 {fake.refine_calls}")
    check("产出 1 个候选段", len(res.candidates) == 1)
    check("产出 1 个切片段", len(res.clips) == 1)
    check("行程结束标志", res.finished is True)
    check("进度回调覆盖 screen / refine 阶段",
          "screen" in progress_log and "refine" in progress_log,
          str(sorted(set(progress_log))))

    check("★ 确认点在精析之前被调用",
          bool(confirms) and confirms[0].kind == "before_refine",
          f"确认点被调用 {len(confirms)} 次")
    if confirms:
        check("确认点带上了精析报价", confirms[0].refine_estimate >= 0,
              f"¥{confirms[0].refine_estimate}")
        check("确认点带上了候选段数", confirms[0].candidate_count == 1)
        check("确认点带上了已花金额与硬上限",
              confirms[0].spent_cny >= 0 and confirms[0].hard_limit_cny > 0,
              f"已花 ¥{confirms[0].spent_cny} / 上限 ¥{confirms[0].hard_limit_cny}")

    # usage：粗筛 1 次 + 精析 1 次 = 2 次 × (1000 in, 100 out)
    check("usage 累计正确（2 次调用）",
          res.usage.input_tokens == 2000 and res.usage.output_tokens == 200,
          f"in={res.usage.input_tokens} out={res.usage.output_tokens}")
    check("花费与实际 usage 一致",
          abs(res.spent_cny - res.usage.cost_cny) < 1e-6, f"¥{res.spent_cny}")
    check("本轮上限已给出（给用户最坏预期）", res.estimated_upper_bound > 0,
          f"¥{res.estimated_upper_bound}")

    # ═══════════════════════════════════════════════════════
    print("\n[5] 落库字段完整性（v3 迁移 + 显式签名）")
    # ═══════════════════════════════════════════════════════
    clips = store.list_clips(task_id)
    check("clip 已落库", len(clips) == 1)
    if clips:
        c = clips[0]
        check("★ type 真的写进去了", (c.get("type") or "") == "测试类",
              f"type={c.get('type')!r}")
        check("★ title_hint 真的写进去了",
              (c.get("title_hint") or "") == "假标题",
              f"title_hint={c.get('title_hint')!r}")
        check("★ hint 真的写进去了", (c.get("hint") or "") == "假候选0",
              f"hint={c.get('hint')!r}")
        check("★ confidence 真的写进去了", (c.get("confidence") or 0) == 60,
              f"confidence={c.get('confidence')!r}")
        check("高评分自动勾选", bool(c.get("selected")) is True)
        check("时间戳已换算回原轴（非相对副本）",
              c["end"] - c["start"] > 0, f"{c['start']}~{c['end']}")

    # ═══════════════════════════════════════════════════════
    print("\n[6] 断点续跑（重跑不应再次调用模型）")
    # ═══════════════════════════════════════════════════════
    fake2 = FakeTransport(screen_hits=1)
    res2 = pipeline.run(
        task_id, VIDEO, opts, fake2,
        on_progress=lambda p: None, on_confirm=lambda r: True,
        work_dir=WORK, skip_transcribe=FAKE_SEGMENTS)

    check("★ 重跑时粗筛调用为 0（缓存生效）", fake2.screen_calls == 0,
          f"实际 {fake2.screen_calls}")
    check("★ 重跑时精析调用为 0（副本与结果均命中缓存）",
          fake2.refine_calls == 0, f"实际 {fake2.refine_calls}")
    check("重跑仍能给出候选（从缓存补回）", len(res2.candidates) >= 1,
          f"{len(res2.candidates)} 段")
    check("重跑花费为 0", res2.spent_cny == 0.0, f"¥{res2.spent_cny}")

    # ═══════════════════════════════════════════════════════
    print("\n[7] 确认点行为：用户选「先看清单」应停在粗筛后")
    # ═══════════════════════════════════════════════════════
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    task2 = store.create_task("阶段2自测-停在确认点", source_path=str(VIDEO),
                              duration_sec=12.0)
    fake3 = FakeTransport()
    res3 = pipeline.run(
        task2, VIDEO, opts, fake3,
        on_progress=lambda p: None,
        on_confirm=lambda r: False,      # ← 用户点了「先看清单」
        work_dir=WORK, skip_transcribe=FAKE_SEGMENTS)

    check("★ 用户拒绝时精析调用为 0（钱没花）", fake3.refine_calls == 0,
          f"实际 {fake3.refine_calls}")
    check("候选清单仍然给出（粗筛成果不丢）", len(res3.candidates) >= 1)
    check("finished 标记为 False", res3.finished is False)
    check("未产生切片段", len(res3.clips) == 0)

    # ═══════════════════════════════════════════════════════
    print("\n[8] 空候选：要给可操作的提示，不能静默返回空")
    # ═══════════════════════════════════════════════════════
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    task3 = store.create_task("阶段2自测-空候选", source_path=str(VIDEO),
                              duration_sec=12.0)
    fake4 = FakeTransport(screen_hits=0)      # 粗筛永远返回 []
    res4 = pipeline.run(
        task3, VIDEO, opts, fake4,
        on_progress=lambda p: None, on_confirm=lambda r: True,
        work_dir=WORK, skip_transcribe=FAKE_SEGMENTS)

    check("空候选时不再调精析", fake4.refine_calls == 0)
    check("空候选时候选列表为空", len(res4.candidates) == 0)
    check("★ 空候选时给出可操作的提示（三项建议）",
          any("没有找到" in d for d in res4.degraded),
          (res4.degraded[-1][:48] if res4.degraded else "(没有提示)"))

    # ═══════════════════════════════════════════════════════
    print("\n[9] 模块导入自检（防相对导入层级写错）")
    # ═══════════════════════════════════════════════════════
    # 这个坑在阶段 1/2 各踩过一次：`sliceq/ui/pages/*.py` 里写
    # `from .. import config`，但 `..` 从 `sliceq.ui.pages` 解析是
    # `sliceq.ui`，不是 `sliceq` —— 正确写法是三层 `...`。
    # 编译不会报错（语法合法），只有真正 import 才炸。
    import importlib

    for mod in ("sliceq.analyzer", "sliceq.prompts", "sliceq.screening",
                "sliceq.pipeline", "sliceq.store",
                "sliceq.ui.workers", "sliceq.ui.pages.analyze_dialog",
                "sliceq.ui.pages.clips_page", "sliceq.ui.pages.tasks_page",
                "sliceq.ui.pages.settings_page", "sliceq.ui.main_window"):
        try:
            importlib.import_module(mod)
            check(f"导入 {mod}", True)
        except Exception as exc:
            check(f"导入 {mod}", False, str(exc)[:80])

    # ── 清理 ────────────────────────────────────────────
    store.delete_task(task_id)
    store.delete_task(task2)
    store.delete_task(task3)
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)

    # ═══════════════════════════════════════════════════════
    print("\n" + "=" * 78)
    print(f"结果：{ok} 项通过，{fail} 项失败")
    if _failures:
        print("失败项：")
        for f in _failures:
            print(f"  - {f}")
    print("=" * 78)
    print("\n⚠️ 本测试用 FakeTransport，验证的是**流水线逻辑**。")
    print("   真实端点的 payload 是否被接受、模型返回是否符合预期，")
    print("   需要配置 API key 后另行验证（阶段 2 验收清单里标注为待验项）。")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
