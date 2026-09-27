# SliceQ · Live Stream Clip Workbench

**English** | [中文](README.md)

> Turn a multi-hour stream recording into publishable clips — highlights found, cut,
> subtitled — plus an editable timeline (CapCut draft / DaVinci Resolve OTIO) to keep refining.

Built on multimodal LLMs (Qwen-Omni family) · Windows desktop app · MIT licensed · **Bring Your Own Key**

**Version**: `v0.1.0` (stages 0–5 complete, usable)

> ⚠️ **Please read the [Disclaimer](#disclaimer) first.** Only process footage you have the
> rights to use. Copyright and platform-compliance responsibility for the resulting clips
> rests with the user.

---

## The problem it solves

Out of a 3-hour stream, maybe 15 minutes is actually worth publishing. Watching it all takes
3 hours; then you still have to cut, subtitle, and import into an editor — a full day of work.

SliceQ automates the **first half**:

```
recording → transcribe → find highlights → cut + subtitle → clips / timeline draft
            (local, free)  (multimodal)     (FFmpeg)        (keep refining)
```

**It does not replace your editor.** It gets you to the starting line of fine-tuning;
the finishing touches still happen in CapCut or DaVinci Resolve.

---

## Features

| Area | What it does |
|---|---|
| **Import** | Drag & drop local video; URLs (Bilibili etc.) via yt-dlp |
| **Transcription** | FFmpeg's built-in whisper — **fully local, zero API cost**; hallucination cleanup |
| **Coarse screening** | Compresses hours of video into dozens of candidate windows using text signals |
| **Fine analysis** | Multimodal model watches and listens, picks highlights, writes reasons and titles |
| **Cost visibility** | Estimate up front → a single confirmation point after screening → post-hoc reconciliation |
| **Cutting** | One MP4 per candidate, optional fade, music auto-ducking, automatic cover frame |
| **Subtitles** | ASS styling (font / color / outline / bilingual), plus SRT output |
| **Copywriting** | Title and description text per clip |
| **Batching** | Concurrent candidates; multiple recordings queued serially |
| **Export** | CapCut draft / DaVinci OTIO / fallback package |

---

## Quick start

### Option 1: Download (recommended)

1. Grab `SliceQ.exe` from [Releases](https://github.com/cunnak/SliceQ/releases)
2. Double-click. **No Python required**
3. First launch walks you through three setup steps

> Windows may warn about an unknown publisher — that's normal for an unsigned binary.
> Choose "More info → Run anyway".

### Option 2: From source (developers)

```bash
pip install -r requirements.txt
python -m sliceq
```

Requires Python ≥ 3.11 (developed on 3.13).

---

## First-launch setup

| # | Item | Notes |
|---|---|---|
| 1 | **API Key** | Multimodal analysis needs a DashScope (Aliyun Bailian) key — see below |
| 2 | **FFmpeg** | Checked on the settings page. If missing or lacking the whisper filter, click "Download" (~180 MB) |
| 3 | **whisper model** | Pick a size (recommended `large-v3-turbo`, ~1.6 GB), fetched from a mirror |

> ⚠️ **First launch downloads roughly 1.8 GB** (FFmpeg + model). These are **not bundled**
> into the exe (see [Third-party components](#third-party-components)), and live under
> `%APPDATA%\SliceQ\` — downloaded once.
>
> Transcription is **entirely local**: once set up, cutting costs nothing extra.

---

## Getting an API Key

1. Open the [Aliyun Bailian console](https://bailian.console.aliyun.com/) and sign in
2. Activate the model service when prompted (there is a free tier for personal use)
3. In the model marketplace, confirm `qwen3.8-omni-flash` is available (this version uses it)
4. Avatar → **API-KEY** → create → copy
5. In SliceQ: **Settings** → paste → save

> 🔒 The key is encrypted with **Windows DPAPI** (`%APPDATA%\SliceQ\credentials.bin`),
> bound to your Windows account, and cannot be decrypted on another machine.
> It is only ever sent to the endpoint you configure. Nothing is phoned home.

---

## Cost

**Transcription is free.** The only paid step is fine analysis.

| Item | Order of magnitude |
|---|---|
| Fine analysis, per candidate | around **¥0.5** (calibrated against real footage) |
| Full pipeline, 3-hour stream | a small fraction of per-minute services |
| Cutting / subtitles / export | **¥0** (local FFmpeg) |

The app shows an estimated ceiling before starting and **pauses for your confirmation**
once screening finishes — so you can see the candidate count and cost before committing.

---

## Export channels

Three independent checkboxes:

| Channel | Output | Next step |
|---|---|---|
| **Clips** | One MP4 per candidate (+ SRT) | Publish directly |
| **CapCut draft** | Video + subtitle + title tracks | Keep editing in CapCut |
| **DaVinci timeline** | `.otio` + parallel `.srt` + `导入说明.txt` | Keep editing in Resolve |

### ⚠️ CapCut: you must trigger the final render yourself

SliceQ only writes a **draft** into CapCut's draft folder. Since CapCut 7, external
processes cannot trigger an export — **click render in CapCut yourself**.

### ⚠️ DaVinci Resolve: two things you must know

The export includes an `导入说明.txt` (import guide, in Chinese). The two essentials:

1. **"Media not found" on first import is expected.** `.otio` records cut points only,
   not video. Click Yes and point it at the source folder.
2. **Import subtitles with "Insert Selected Subtitles to Timeline Using Timecode" —
   do not drag the `.srt` onto the timeline.** Measured: dragging makes Resolve lay them
   out from frame 0, **losing the first cue's offset**, shifting every subtitle
   about 0.5 s earlier. No error is reported.

### Subtitle styling is intentionally not carried over

Styles you set in SliceQ do **not** transfer to the export channels — CapCut uses its own
defaults; Resolve uses the parallel SRT. The two styling systems differ too much to map
faithfully. This is stated in three places: the export dialog, the result text, and the import guide.

---

## Where data lives

Everything under `%APPDATA%\SliceQ\`:

```
SliceQ/
├── db/sliceq.db          tasks, clips, subtitles, markers
├── credentials.bin       API key (DPAPI encrypted)
├── settings.json         preferences
├── logs/sliceq.log       rolling log — check this when something goes wrong
├── bin/                  downloaded FFmpeg
├── models/               whisper models
├── downloads/            videos imported by URL
├── styles/               subtitle style presets
├── bgm/                  background music (bring your own)
└── export/               default export location
```

**Uninstall = delete this folder.** The app itself is a single portable executable.

---

## Third-party components

This project is MIT licensed. The following are either **not bundled** or used under
their own compatible licenses:

| Component | License | How it's used |
|---|---|---|
| **FFmpeg** | **GPL** (gyan.dev build) | **Not bundled.** Downloaded by the user on first run and invoked via `subprocess` as a **separate program** — an aggregate, which the GPL FAQ permits alongside MIT code |
| **whisper models** | MIT (OpenAI) | Not bundled, downloaded at runtime |
| `pyJianYingDraft` | Apache-2.0 | Library, CapCut draft generation |
| `OpenTimelineIO` | Apache-2.0 | Library, `.otio` generation |
| `PySide6-Essentials` | LGPL-3.0 | Library (Qt) |
| `yt-dlp` / `requests` / `Pillow` / `py7zr` | respective OSS licenses | Libraries |

**Fonts**: no fonts are redistributed. Subtitle rendering uses fonts already installed
on your system. If you use a particular typeface in an exported clip, verify its license yourself.

**Music**: no background music is bundled (audio licensing can't be handled the way font
licensing can). The `bgm` folder is for tracks you have the rights to.

---

## Disclaimer

1. **Rights**: only process footage you own, have licensed, or that the platform permits
   you to remix.
2. **Responsibility**: copyright and compliance responsibility for the resulting clips
   rests with **you**. This project does not adjudicate that.
3. **Platform rules**: follow each platform's rules on remixing, reposting, and commercial use.
4. **Deliberately not implemented**: this project does **not** implement any capability to
   bypass platform verification, mass-register accounts, auto-publish, or evade copyright
   detection. Nor will it help circumvent model-provider safety review.
5. **No warranty**: provided "as is", with no warranty of output quality or fitness for purpose.

---

## Development

```
sliceq/                application code
├── config.py          all paths and constants (nothing hardcoded elsewhere)
├── secrets.py         API key (Windows DPAPI)
├── store.py           SQLite data layer
├── ffmpeg_tools.py    FFmpeg locate / verify / download
├── asr.py             local transcription and hallucination cleanup
├── screening.py       coarse screening
├── analyzer.py        multimodal fine analysis
├── editor.py          cutting
├── subtitle.py        ASS subtitles + timeline remapping
├── exporter.py        three export channels
└── ui/                PySide6 interface

docs/                  PRD / tech design / task breakdown
reports/               measured evidence per stage
stage0/probe_resolve/  probes and self-tests (all acceptance criteria live here)
```

**Self-tests** (no GUI):

```bash
# Use the project's own interpreter. Self-tests isolate user data —
# they will not touch your API key or settings.
python stage0/probe_resolve/selftest_exporter.py
```

Status and roadmap: [`docs/TASKS.md`](docs/TASKS.md).

---

## License

[MIT](LICENSE) © 2026 cunnak
