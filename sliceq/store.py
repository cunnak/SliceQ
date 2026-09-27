# -*- coding: utf-8 -*-
"""SQLite 数据层。

设计要点：
1. **线程安全**：sqlite3 连接不能跨线程共享。这里用 threading.local()
   给每个线程各自一个连接 —— QThreadPool 里的工作线程直接调本模块即可。
2. **WAL 模式**：允许"一个写 + 多个读"并发，避免长任务写库时 UI 读库被阻塞。
3. **版本化迁移**：用 PRAGMA user_version 记录 schema 版本，后续加字段可平滑升级。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import config

_local = threading.local()

# 当前 schema 版本。加字段/加表时 +1，并在 _MIGRATIONS 里补一段。
SCHEMA_VERSION = 5


# ─────────────────────────────────────────────────────────────
# 连接管理
# ─────────────────────────────────────────────────────────────
def _connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    config.ensure_dirs()
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # 读写并发
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _local.conn = conn
    return conn


def close() -> None:
    """关闭当前线程的连接。程序退出时对主线程调用即可。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# ─────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS task (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT    NOT NULL,
  source_path   TEXT,
  source_url    TEXT,
  profile       TEXT,
  user_prompt   TEXT,
  status        TEXT    NOT NULL DEFAULT 'imported',
  duration_sec  REAL,
  width         INTEGER,
  height        INTEGER,
  fps           REAL,
  bitrate_kbps  INTEGER,
  filesize      INTEGER,
  created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_status ON task(status);

CREATE TABLE IF NOT EXISTS clip (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  start       REAL    NOT NULL,
  end         REAL    NOT NULL,
  type        TEXT,
  score       INTEGER,
  summary     TEXT,
  title_hint  TEXT,
  transcript  TEXT,
  selected    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_clip_task ON clip(task_id);

CREATE TABLE IF NOT EXISTS export (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  kind        TEXT    NOT NULL,          -- mp4 / srt / otio / jianying
  path        TEXT    NOT NULL,
  created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_export_task ON export(task_id);
"""

# v2：C 路线专项 —— 字幕轨与高光标记的预留表
# ⚠️ v1 的字幕虽然只走 SRT，但这两张表必须现在就建好。
#    否则 v2 做三端可编辑时间线时，要推翻时间线数据模型重做。
#    （用户已确认愿意为 v2 预留，见 TECH-DESIGN §6.3.4）
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS subtitle (
  cue_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  start_ms    INTEGER NOT NULL,          -- 相对成片起点，零基
  end_ms      INTEGER NOT NULL,
  text        TEXT    NOT NULL,
  style_hint  TEXT,                      -- 预留给 v2 的样式描述（JSON）
  created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subtitle_task ON subtitle(task_id);

CREATE TABLE IF NOT EXISTS marker (
  marker_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  at_ms       INTEGER NOT NULL,          -- 相对成片起点，零基
  name        TEXT    NOT NULL,          -- 高光说明
  color       TEXT    NOT NULL DEFAULT 'CYAN',
  created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marker_task ON marker(task_id);
"""

# v3：clip 表补两列，记录"粗筛为什么选中它"。
#
# 这两列在 pipeline 里本来就要写，v1/v2 的 schema 却漏了 ——
# 而 add_clip 用 `**kw` 把不认识的键**静默吞掉**，所以不会报错，
# 只是数据无声消失。是"不报错的 bug"的典型，故单独记一笔。
_SCHEMA_V3 = """
ALTER TABLE clip ADD COLUMN hint TEXT;
ALTER TABLE clip ADD COLUMN confidence INTEGER;
"""

# v4：转录分段落库。
#
# 为什么必须存：导出时要生成字幕（烧录 + SRT），而字幕的原料是
# **转录分段**。分段原先只在 asr 的分片缓存文件里，导出侧拿不到 ——
# 要么重新转录一次（3 小时素材要 20 分钟，不可接受），
# 要么就做不出字幕。
#
# 与 subtitle 表的区别：
#   transcript_seg  = 转录的**原始**输出（句级，源视频时间轴）
#   subtitle        = 切分/重映射后的**成品**字幕（成片时间轴）
# 两者语义不同，混用会让"重映射"这一步失去可核对的原点。
_SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS transcript_seg (
    seg_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    idx        INTEGER NOT NULL,
    start_ms   INTEGER NOT NULL,
    end_ms     INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tseg_task ON transcript_seg(task_id, idx);
"""

# ── v5：给 transcript_seg 补外键 ────────────────────────────
#
# 建这张表时漏了 `REFERENCES task(id) ON DELETE CASCADE`（其它 4 张表都有）。
# 后果不是"数据多一点"这么轻 ——
#
#   ① **隐私**：用户删掉任务（意图是"这些素材我不要了"），
#      素材里说过的话**还留在库里**。实测删 14 个任务后仍有 659 行孤儿。
#   ② 数据无上限膨胀：转录是几 MB 文本，每次分析都写。
#
# SQLite 不支持 ALTER TABLE 加外键，只能重建表；
# 重建时顺手把已经产生的孤儿行删掉。
#
# ⚠️ 复制时用 `WHERE task_id IN (SELECT id FROM task)` 过滤，
#    否则新表会因外键违例而插不进去（外键在 _connect 里是打开的）。
_SCHEMA_V5 = """
CREATE TABLE transcript_seg_v5 (
    seg_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    start_ms   INTEGER NOT NULL,
    end_ms     INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    created_at TEXT
);

INSERT INTO transcript_seg_v5
    (seg_id, task_id, idx, start_ms, end_ms, text, created_at)
SELECT seg_id, task_id, idx, start_ms, end_ms, text, created_at
FROM transcript_seg
WHERE task_id IN (SELECT id FROM task);

DROP TABLE transcript_seg;
ALTER TABLE transcript_seg_v5 RENAME TO transcript_seg;
CREATE INDEX IF NOT EXISTS idx_tseg_task ON transcript_seg(task_id, idx);
"""


_MIGRATIONS: dict[int, str] = {
    1: _SCHEMA_V1,
    2: _SCHEMA_V2,
    3: _SCHEMA_V3,
    4: _SCHEMA_V4,
    5: _SCHEMA_V5,
}


def init_db() -> None:
    """建表 / 升级 schema。启动时调用一次。"""
    conn = _connect()
    cur = conn.execute("PRAGMA user_version")
    current = int(cur.fetchone()[0])

    if current >= SCHEMA_VERSION:
        return

    for ver in range(current + 1, SCHEMA_VERSION + 1):
        sql = _MIGRATIONS.get(ver)
        if sql:
            conn.executescript(sql)
        conn.execute(f"PRAGMA user_version={ver}")
    conn.commit()


# ─────────────────────────────────────────────────────────────
# 通用小工具
# ─────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rows(sql: str, args: Iterable[Any] = ()) -> list[dict]:
    cur = _connect().execute(sql, tuple(args))
    return [dict(r) for r in cur.fetchall()]


def _one(sql: str, args: Iterable[Any] = ()) -> dict | None:
    rows = _rows(sql, args)
    return rows[0] if rows else None


# ─────────────────────────────────────────────────────────────
# task
# ─────────────────────────────────────────────────────────────
def create_task(
    name: str,
    source_path: str | None = None,
    source_url: str | None = None,
    duration_sec: float | None = None,
    width: int | None = None,
    height: int | None = None,
    fps: float | None = None,
    bitrate_kbps: int | None = None,
    filesize: int | None = None,
) -> int:
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO task
           (name, source_path, source_url, status, duration_sec,
            width, height, fps, bitrate_kbps, filesize, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (name, source_path, source_url, config.STATUS_IMPORTED,
         duration_sec, width, height, fps, bitrate_kbps, filesize, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_tasks() -> list[dict]:
    return _rows("SELECT * FROM task ORDER BY id DESC")


def get_task(task_id: int) -> dict | None:
    return _one("SELECT * FROM task WHERE id=?", (task_id,))


def set_task_status(task_id: int, status: str) -> None:
    conn = _connect()
    conn.execute("UPDATE task SET status=? WHERE id=?", (status, task_id))
    conn.commit()


def rename_task(task_id: int, name: str) -> None:
    conn = _connect()
    conn.execute("UPDATE task SET name=? WHERE id=?", (name, task_id))
    conn.commit()


def delete_task(task_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM task WHERE id=?", (task_id,))
    conn.commit()


# ─────────────────────────────────────────────────────────────
# clip
# ─────────────────────────────────────────────────────────────
def add_clip(task_id: int, start: float, end: float, *,
             type: str = "", score: int = 0, summary: str = "",
             title_hint: str = "", transcript: str = "",
             hint: str = "", confidence: int = 0,
             selected: bool = False) -> int:
    """写入一个候选切片。

    ⚠️ 签名必须是**显式**的，不能再用 `**kw` 静默吞键。
       之前的写法在传错键名时不报错、数据只是无声消失
       （实测：`kind=` / `title=` 都写不进表）。显式签名让这类错误
       立刻变成 TypeError，而不是变成"用户发现某列一直是空的"。
    """
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO clip
           (task_id, start, end, type, score, summary, title_hint,
            transcript, hint, confidence, selected)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (task_id, start, end, type or None, int(score or 0),
         summary or None, title_hint or None, transcript or None,
         hint or None, int(confidence or 0),
         1 if selected else 0),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_clips(task_id: int) -> list[dict]:
    return _rows("SELECT * FROM clip WHERE task_id=? ORDER BY start", (task_id,))


def set_clip_selected(clip_id: int, selected: bool) -> None:
    conn = _connect()
    conn.execute("UPDATE clip SET selected=? WHERE id=?",
                 (1 if selected else 0, clip_id))
    conn.commit()


def clear_clips(task_id: int) -> int:
    """清掉某任务的全部候选切片，返回删除条数。

    "重新分析"必须先调它 —— 否则新结果会和旧结果堆在同一张表里，
    用户看到的是**两次结果的叠加**（时间点重复、评分混杂），
    而且看不出哪里不对。
    """
    conn = _connect()
    cur = conn.execute("DELETE FROM clip WHERE task_id=?", (task_id,))
    conn.commit()
    return int(cur.rowcount)


# ─────────────────────────────────────────────────────────────
# export
# ─────────────────────────────────────────────────────────────
def add_export(task_id: int, kind: str, path: str) -> int:
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO export (task_id, kind, path, created_at) VALUES (?,?,?,?)",
        (task_id, kind, path, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_exports(task_id: int) -> list[dict]:
    return _rows("SELECT * FROM export WHERE task_id=? ORDER BY id DESC",
                 (task_id,))


# ─────────────────────────────────────────────────────────────
# 预留表的读写（v2 会用，现在提供接口便于测试表结构）
# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# 转录分段（导出时生成字幕要用）
# ─────────────────────────────────────────────────────────────
def save_transcript_segs(task_id: int, segs: Iterable[Any]) -> int:
    """整批写入某任务的转录分段（先清掉旧的），返回写入条数。

    接受任何带 `start_ms` / `end_ms` / `text` 属性的对象 —— `asr.Segment`
    直接可用，不必在调用处做转换。

    ⚠️ 重跑分析时会覆盖。这是有意的：转录结果跟着任务走，
       留多份只会让"这份是哪次跑的"变得说不清。
    """
    rows = [(task_id, i, int(getattr(s, "start_ms")),
             int(getattr(s, "end_ms")), str(getattr(s, "text", "")))
            for i, s in enumerate(segs)]
    conn = _connect()
    conn.execute("DELETE FROM transcript_seg WHERE task_id=?", (task_id,))
    if rows:
        now = _now()
        conn.executemany(
            "INSERT INTO transcript_seg "
            "(task_id, idx, start_ms, end_ms, text, created_at) "
            "VALUES (?,?,?,?,?,?)",
            [(t, i, a, b, x, now) for (t, i, a, b, x) in rows])
    conn.commit()
    return len(rows)


def list_transcript_segs(task_id: int) -> list[dict]:
    return _rows(
        "SELECT seg_id, idx, start_ms, end_ms, text "
        "FROM transcript_seg WHERE task_id=? ORDER BY idx", (task_id,))


def add_subtitle(task_id: int, start_ms: int, end_ms: int, text: str,
                 style_hint: str | None = None) -> int:
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO subtitle (task_id, start_ms, end_ms, text,
                                 style_hint, created_at)
           VALUES (?,?,?,?,?,?)""",
        (task_id, start_ms, end_ms, text, style_hint, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_subtitles(task_id: int) -> list[dict]:
    return _rows("SELECT * FROM subtitle WHERE task_id=? ORDER BY start_ms",
                 (task_id,))


def add_marker(task_id: int, at_ms: int, name: str,
               color: str = "CYAN") -> int:
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO marker (task_id, at_ms, name, color, created_at)
           VALUES (?,?,?,?,?)""",
        (task_id, at_ms, name, color, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_markers(task_id: int) -> list[dict]:
    return _rows("SELECT * FROM marker WHERE task_id=? ORDER BY at_ms",
                 (task_id,))


# ─────────────────────────────────────────────────────────────
# 诊断
# ─────────────────────────────────────────────────────────────
def table_names() -> list[str]:
    return [r["name"] for r in
            _rows("SELECT name FROM sqlite_master WHERE type='table' "
                  "ORDER BY name")]


def db_path() -> Path:
    return config.DB_PATH
