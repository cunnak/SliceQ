# -*- coding: utf-8 -*-
"""转录分片缓存 + 「重新分析」的清理范围（v0.1.3）。

背景（2026-09-28 发现）：
  用户跑一次 84 分钟素材的分析后，想"换一版筛选结果"重新看候选。
  两条路都要重付**约 40 分钟的转录**：
    ① `asr.transcribe_chunk()` 里 `dest.unlink(missing_ok=True)` ——
       **转录结果从来就不是缓存**，每次都被删掉重跑；
    ② GUI 的「重新分析」调 `pipeline.clear_cache()` → `rmtree(work)`
       —— 连 audio.wav / chunk_*.speech.wav 一起没。
  而转录**本地免费但极慢**，与"筛选强度 / 需求描述"完全无关。

本测试守四件事：
  ① 参数一致时，分片结果**必须被复用**（且完全不碰音频文件）
  ② 参数变了（换模型/语言/GPU）必须**重跑**，不能拿旧结果糊弄
  ③ 老缓存（无指纹文件）仍可用（否则用户升级后反而更慢）
  ④ `clear_cache` 默认**保留转录**、只清候选与精析产物
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sliceq import _testing                                   # noqa: E402

_testing.isolate_data_root("asrcache")
_testing.assert_isolated()

from sliceq import asr, asr_models, config, pipeline          # noqa: E402

PASS = FAIL = 0


def ck(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK]   {name}" + (f"　—　{detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"　—　{detail}" if detail else ""))


def head(t: str) -> None:
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


def write_jsonl(p: Path, segs: list[tuple[int, int, str]]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps({"start": a, "end": b, "text": t})
                           for a, b, t in segs) + "\n", encoding="utf-8")


work = Path(_testing.isolated_root()) / "work"          # type: ignore[arg-type]
work.mkdir(parents=True, exist_ok=True)

# ⚠️ 用**真实模型文件**（MODELS 目录按设计是共享的）。
#    缓存命中路径需要它存在 —— 模型不存在会先抛"模型不存在"，
#    就测不到缓存逻辑了。
model = asr_models.model_path()
print(f"模型文件: {model.name}  存在={model.exists()}")
if not model.exists():
    print("模型不在，无法测缓存路径；退出")
    raise SystemExit(2)

# ─────────────────────────────────────────────────────────
head("1. 参数一致 ⇒ 复用缓存，且**完全不碰音频**")
# ─────────────────────────────────────────────────────────
# ★ 判据很干净：传一个**不存在的音频路径**。
#   走缓存 ⇒ 不会调用 ffmpeg ⇒ 正常返回；
#   没走缓存 ⇒ ffmpeg 打不开文件 ⇒ 抛异常。
MISSING_AUDIO = work / "根本不存在的音频.wav"
dest = work / "chunk_0000.speech.jsonl"
write_jsonl(dest, [(0, 1200, "甲"), (1200, 2600, "乙")])
fp = asr._chunk_fingerprint(model, "zh", True)
dest.with_suffix(dest.suffix + ".meta").write_text(fp, encoding="utf-8")

try:
    got = asr.transcribe_chunk(MISSING_AUDIO, dest, model=model, language="zh",
                               use_gpu=True)
    reused_ok = [s.text for s in got] == ["甲", "乙"]
except BaseException as exc:                                  # noqa: BLE001
    reused_ok = False
    print(f"     抛异常：{type(exc).__name__}: {str(exc)[:90]}")
ck("★ 缓存命中时复用结果（未触碰不存在的音频）", reused_ok)

# ─────────────────────────────────────────────────────────
head("2. 参数变了 ⇒ 必须重跑（不能拿旧结果糊弄）")
# ─────────────────────────────────────────────────────────
for label, kwargs in (
    ("换语言", dict(language="en", use_gpu=True)),
    ("换 GPU 开关", dict(language="zh", use_gpu=False)),
):
    write_jsonl(dest, [(0, 1200, "旧结果")])
    dest.with_suffix(dest.suffix + ".meta").write_text(fp, encoding="utf-8")
    try:
        asr.transcribe_chunk(MISSING_AUDIO, dest, model=model, **kwargs)
        ran = False                      # 没抛 ⇒ 复用了旧结果（错）
    except BaseException:                # noqa: BLE001
        ran = True                       # 抛 ⇒ 真的去跑了 ffmpeg（对）
    ck(f"★ {label} 时判定缓存失效（确实尝试重跑）", ran)

# 换模型（模拟同名不同内容）—— 用 fake 文件验证指纹能区分
fake_model = work / "fake-model.bin"
fake_model.write_bytes(b"x" * 1000)
fp_a = asr._chunk_fingerprint(fake_model, "zh", True)
fake_model.write_bytes(b"x" * 2000)          # 内容变了 ⇒ size 变 ⇒ 指纹变
fp_b = asr._chunk_fingerprint(fake_model, "zh", True)
ck("★ 模型文件变化 ⇒ 指纹变化", fp_a != fp_b, f"{fp_a[:28]}… vs {fp_b[:28]}…")
ck("模型不变 ⇒ 指纹稳定",
   asr._chunk_fingerprint(fake_model, "zh", True) == fp_b)

# ─────────────────────────────────────────────────────────
head("3. 老缓存（无指纹文件）仍可用")
# ─────────────────────────────────────────────────────────
# 升级到 v0.1.3 时，用户手上已有的 chunk_*.speech.jsonl **没有 .meta**。
# 若判为"不可用"，升级后的第一次重跑反而更慢 —— 与修这个缺陷的初衷相反。
write_jsonl(dest, [(0, 900, "老缓存内容")])
meta = dest.with_suffix(dest.suffix + ".meta")
if meta.exists():
    meta.unlink()
try:
    got = asr.transcribe_chunk(MISSING_AUDIO, dest, model=model, language="zh",
                               use_gpu=True)
    legacy_ok = [s.text for s in got] == ["老缓存内容"]
except BaseException as exc:                                  # noqa: BLE001
    legacy_ok = False
    print(f"     抛异常：{type(exc).__name__}: {str(exc)[:90]}")
ck("★ 老缓存按可用处理（用户升级不倒退）", legacy_ok)

# 空文件 / 只有空白 ⇒ 视为没有缓存
dest.write_text("", encoding="utf-8")
ck("空文件不算有效缓存", asr._cached_chunk(dest, fp) is None)
dest.unlink()
ck("文件不存在 ⇒ 没有缓存", asr._cached_chunk(dest, fp) is None)

# ─────────────────────────────────────────────────────────
head("4. clear_cache：默认保留转录，只清候选与精析产物")
# ─────────────────────────────────────────────────────────
video = Path("某素材.mp4")
w = config.APP_ROOT / "work" / video.stem
w.mkdir(parents=True, exist_ok=True)
KEEP = ["audio.wav", "chunk_0000.wav", "chunk_0000.speech.wav",
        "chunk_0000.speech.jsonl", "chunk_0000.speech.jsonl.meta"]
CLEAR_FILES = ["screen.jsonl"]
CLEAR_DIRS = ["refine", "frames"]

for name in KEEP:
    (w / name).write_bytes(b"keep")
for name in CLEAR_FILES:
    (w / name).write_bytes(b"clear")
for name in CLEAR_DIRS:
    (w / name).mkdir(parents=True, exist_ok=True)
    (w / name / "x").write_bytes(b"clear")

pipeline.clear_cache(video)                     # 默认 keep_transcript=True
kept = [n for n in KEEP if (w / n).exists()]
gone_f = [n for n in CLEAR_FILES if not (w / n).exists()]
gone_d = [n for n in CLEAR_DIRS if not (w / n).exists()]
print(f"     保留：{kept}")
print(f"     已删：{gone_f + gone_d}")
ck("★ 转录相关产物全部保留（audio / chunk / speech / jsonl / meta）",
   len(kept) == len(KEEP), f"{len(kept)}/{len(KEEP)}")
ck("★ 粗筛缓存 screen.jsonl 被清（「换一版结果」本就该重跑粗筛）",
   len(gone_f) == len(CLEAR_FILES), str(gone_f))
ck("★ refine / frames 被清", len(gone_d) == len(CLEAR_DIRS), str(gone_d))

# 显式要求全清时，连转录一起删
pipeline.clear_cache(video, keep_transcript=False)
ck("★ keep_transcript=False ⇒ 整个 work 目录清空（留给换模型的场合）",
   not w.exists() or not any(w.iterdir()), str(list(w.iterdir()) if w.exists() else []))

ck("对不存在的素材调用不报错（幂等）",
   (lambda: (pipeline.clear_cache(Path("从来没有过的素材.mp4")), True)[1])())

print("\n" + "=" * 72)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
raise SystemExit(1 if FAIL else 0)
