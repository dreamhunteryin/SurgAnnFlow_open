"""Metadata Registry（SQLite，標註與影片中繼資料的單一事實來源）。

標註介面只是工作桌；registry + 匯出快照才是資料資產。
schema 刻意保持簡單、標準 SQL，設計為可直接遷移 PostgreSQL。

registry schema v1.4（2026-11-02）：版本記在 schema_meta 表（key='schema_version'）；
PATH_COLUMNS 列出的 6 個欄位存相對 data 根的 POSIX 路徑（見 config.to_rel / to_abs）。
"""
from __future__ import annotations

import json
import random
import sqlite3
import string
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from . import config

DEFAULT_PROJECT_ID = "default"
LABEL_LAYERS = ("phase", "action", "event", "triplet", "spatial")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id       TEXT PRIMARY KEY,   -- slug，如 default
    name             TEXT,
    project_type     TEXT,               -- temporal / spatial / mixed
    annotation_group TEXT,               -- Roboflow 式：這個專案在標什麼
    extra_json       TEXT,               -- 專案層設定（如 triplet 三選單 taxonomy）
    created_at       TEXT
);

-- 每專案獨立 taxonomy（取代全域 labels_custom.yaml；yaml 僅作為 default 專案種子）
CREATE TABLE IF NOT EXISTS labels (
    label_id   TEXT PRIMARY KEY,         -- uuid
    project_id TEXT,
    layer      TEXT,                     -- phase/action/event/triplet/spatial
    name       TEXT,
    color      TEXT,                     -- #hex；空 = 前端自動配色
    extra_json TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_labels_project_layer_name
    ON labels(project_id, layer, name);
CREATE TABLE IF NOT EXISTS videos (
    video_id      TEXT PRIMARY KEY,
    source_path   TEXT,
    std_path      TEXT,
    fps           REAL NOT NULL,          -- 標準化後實測值（必須 = 25.0）
    n_frames      INTEGER,
    duration_sec  REAL,
    width         INTEGER,
    height        INTEGER,
    sha256_source TEXT,
    sha256_std    TEXT,
    status        TEXT,                   -- ingested / verified / published
    ingested_at   TEXT,
    group_name    TEXT,                   -- 影片分組（landing 頁分區；預設 default）
    series        TEXT                    -- 手術類型/系列（如 ABCD、EFGH）
);

-- v1.2 播放清單模式：影片入庫一次，專案引用不複製（多對多）。
-- videos.project_id 欄位已停用（僅保留避免 SQLite drop column 麻煩），一律走此表。
CREATE TABLE IF NOT EXISTS project_videos (
    project_id TEXT NOT NULL,
    video_id   TEXT NOT NULL,
    added_at   TEXT NOT NULL,
    PRIMARY KEY (project_id, video_id)
);

-- v1.2 標註者名冊：annotator_id 為連號 slug（u001, u002, …）
CREATE TABLE IF NOT EXISTS annotators (
    annotator_id TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS frames (
    video_id    TEXT,
    frame_index INTEGER,
    time_sec    REAL,
    path        TEXT,
    purpose     TEXT,                     -- sample_5s / sample_30s / keyframe / manual
    PRIMARY KEY (video_id, frame_index, purpose),
    FOREIGN KEY (video_id) REFERENCES videos(video_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    task_uid          TEXT PRIMARY KEY,
    platform          TEXT,               -- 保留欄位，現無寫入者
    platform_project_id INTEGER,
    platform_task_id  INTEGER,
    video_id          TEXT,
    task_type         TEXT,               -- phase/action/triplet/event/bbox/seg/point/review
    assignee          TEXT,
    status            TEXT,
    created_at        TEXT,
    FOREIGN KEY (video_id) REFERENCES videos(video_id)
);

CREATE TABLE IF NOT EXISTS annotation_snapshots (
    snapshot_id   TEXT,
    task_uid      TEXT,
    annotator     TEXT,
    raw_json_path TEXT,
    exported_at   TEXT
);

CREATE TABLE IF NOT EXISTS export_runs (
    run_id      TEXT PRIMARY KEY,
    run_dir     TEXT,
    config_json TEXT,
    created_at  TEXT
);

-- v1.3：series 資產層（入庫分群，非標註專案）。
-- series_id 為隨機四碼大寫英文（^[A-Z]{4}$）；影片數量＝COUNT 衍生值，不落欄位。
-- 遷移後 videos.series 欄位改存 series_id（不再是自由文字）。
CREATE TABLE IF NOT EXISTS series (
    series_id    TEXT PRIMARY KEY,        -- 隨機四碼大寫英文，產碼查重
    name         TEXT UNIQUE NOT NULL,    -- 顯示名（如 SERIES_1），可改名（ID 不動）
    institution  TEXT,                    -- 機構名稱（管理者可編輯，可空）
    surgery_type TEXT,                    -- 手術類別（管理者可編輯，可空）
    note         TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL            -- 加/移除影片、改 metadata 時更新
);

-- v1.3：上傳入庫 pipeline 工作佇列（單 worker serial queue）
CREATE TABLE IF NOT EXISTS pipeline_jobs (
    job_id         TEXT PRIMARY KEY,      -- uuid
    patient_folder TEXT NOT NULL,         -- _incoming 下的相對路徑
    series_id      TEXT NOT NULL,
    project_id     TEXT,                  -- 可選：完成後順掛標註專案
    stage          TEXT NOT NULL,         -- merge|oob|cut_encode|verify|register|done
    status         TEXT NOT NULL,         -- queued|running|awaiting_qc|done|failed|cancelled
    progress       REAL DEFAULT 0,        -- 0–100 全程進度（stage 權重＋ffmpeg -progress）
    error          TEXT,
    params_json    TEXT,
    artifacts_json TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- v1.3：videoID ↔ 原始影片對照（PHI 收斂處）。★ API 永不回傳本表內容。
CREATE TABLE IF NOT EXISTS deid_map (
    video_id             TEXT PRIMARY KEY,
    series_id            TEXT NOT NULL,
    original_folder      TEXT NOT NULL,   -- 原始病人資料夾名（PHI）
    original_files_json  TEXT NOT NULL,   -- [{name,order,size,sha256,duration}]
    surgery_date         TEXT,            -- 來源推斷（PHI）
    merged_duration      REAL,
    oob_segments_json    TEXT,            -- [(start,end)…] 已切除段
    oob_csv_path         TEXT,
    pipeline_params_json TEXT,            -- 模型版本/threshold/編碼參數
    job_id               TEXT,
    created_at           TEXT NOT NULL
);

-- v1.3：ID 變更史（改 ID 為罕見管理操作；已匯出資料集靠此回溯）
CREATE TABLE IF NOT EXISTS id_history (
    old_id     TEXT,
    new_id     TEXT,
    entity     TEXT,                      -- video / series
    reason     TEXT,
    changed_at TEXT
);

-- v1.1：自建 annotator 的 temporal 標註。時間永遠由 frame 推導，不落地秒數。
CREATE TABLE IF NOT EXISTS annotations (
    ann_id      TEXT PRIMARY KEY,          -- uuid
    video_id    TEXT NOT NULL,
    layer       TEXT NOT NULL,             -- phase / action / event / triplet
    label       TEXT NOT NULL,
    start_frame INTEGER NOT NULL,          -- 0-based, inclusive
    end_frame   INTEGER NOT NULL,          -- inclusive；point-like = start_frame
    attrs_json  TEXT,                      -- triplet: {instrument, verb, target}
    annotator   TEXT NOT NULL,             -- 顯示名（歷史欄位；正式身分見 annotator_id）
    created_at  TEXT,
    updated_at  TEXT,
    project_id   TEXT,                     -- v1.2 標註按專案隔離（必帶；migration 回填舊資料）
    annotator_id TEXT,                     -- v1.2 → annotators.annotator_id（必帶；migration 回填）
    FOREIGN KEY (video_id) REFERENCES videos(video_id)
);
CREATE INDEX IF NOT EXISTS idx_annotations_video ON annotations(video_id);

-- registry schema v1.4（2026-11-02）：可查詢的 schema 版本紀錄（key/value）。
-- key='schema_version' → '1.3'（舊庫、路徑欄位仍為絕對路徑，待跑 migration）| '1.4'。
-- 注意：此為「registry schema 版本」，與平台程式版本號是兩回事。
CREATE TABLE IF NOT EXISTS schema_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
"""

# registry schema 版本（只在 schema 或欄位語意改變時遞增；變更須附 migration — 契約檔案 #1）
SCHEMA_VERSION = "1.4"
LEGACY_SCHEMA_VERSION = "1.3"   # 無 schema_meta 紀錄的既有庫視為此版

# schema v1.4：以下 6 個欄位存「相對 data 根的 POSIX 路徑」（data 根外保留絕對）。
# 寫入一律經 config.to_rel()（本模組的寫入 helper 已自動套用），讀出一律經 config.to_abs()。
PATH_COLUMNS: tuple[tuple[str, str], ...] = (
    ("videos", "std_path"),
    ("videos", "source_path"),
    ("frames", "path"),
    ("annotation_snapshots", "raw_json_path"),
    ("export_runs", "run_dir"),
    ("deid_map", "oob_csv_path"),
)
_PATH_FIELDS: dict[str, frozenset[str]] = {}
for _t, _c in PATH_COLUMNS:
    _PATH_FIELDS[_t] = _PATH_FIELDS.get(_t, frozenset()) | {_c}


def _rel_fields(table: str, fields: dict) -> dict:
    """把 fields 中屬於該表路徑欄位的值轉成 v1.4 儲存形式（config.to_rel；冪等）。"""
    cols = _PATH_FIELDS.get(table, frozenset())
    return {k: (config.to_rel(v) if k in cols else v) for k, v in fields.items()}


def _resolve(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path else config.REGISTRY_DB


@contextmanager
def connect(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """開啟連線（啟用外鍵、Row 存取）。以 context manager 保證 commit/close。"""
    path = _resolve(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Optional[Path] = None) -> Path:
    """建立所有資料表（冪等；已存在不動）。回傳 db 路徑。"""
    path = _resolve(db_path)
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _init_schema_version(conn)
        _seed_default_project(conn)
    return path


# ---------------------------------------------------------- schema_meta -----

def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO schema_meta (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, _now_iso()))


def get_schema_version(conn: sqlite3.Connection) -> str:
    """回傳 registry schema 版本字串（'1.3' / '1.4'）。無紀錄視為 1.3（舊庫）。"""
    try:
        return get_meta(conn, "schema_version") or LEGACY_SCHEMA_VERSION
    except sqlite3.OperationalError:   # 尚未 init_db（無 schema_meta 表）
        return LEGACY_SCHEMA_VERSION


def _init_schema_version(conn: sqlite3.Connection) -> None:
    """首次建立 schema_meta 紀錄（冪等；已有紀錄不動）。

    - 全新庫（6 個路徑欄位所在的表皆無資料）→ 直接記 SCHEMA_VERSION（1.4）。
    - 既有庫 → 記 1.3：路徑仍是絕對值，需跑
      `python -m pipeline.migrations.m20261102_relative_paths`（先 surgann backup，R13）才升 1.4。
    """
    if get_meta(conn, "schema_version") is not None:
        return
    tables = {t for t, _ in PATH_COLUMNS}
    has_data = any(conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in tables)
    set_meta(conn, "schema_version", LEGACY_SCHEMA_VERSION if has_data else SCHEMA_VERSION)


def _migrate(conn: sqlite3.Connection) -> None:
    """輕量欄位遷移（既有 DB 補新欄位，冪等）。

    v1.2 播放清單模式：
    - videos.series（一次性以檔名前綴回填）
    - annotations.project_id / annotator_id（回填舊資料；新寫入由 add_annotation 強制）
    - project_videos 由既有 videos.project_id 一次性播種；之後 videos.project_id 停用
      （欄位保留不刪，但 registry 內不再讀它）。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    if "group_name" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN group_name TEXT")
    if "project_id" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN project_id TEXT")

    # --- videos.series（一次性：新增欄位當下才做檔名啟發式回填）---
    if "series" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN series TEXT")
        for r in conn.execute(
                "SELECT video_id, source_path FROM videos WHERE series IS NULL").fetchall():
            src = r["source_path"]
            if not src:
                continue
            stem = Path(src).stem
            series = stem.split("_", 1)[0] if "_" in stem else stem
            conn.execute("UPDATE videos SET series = ? WHERE video_id = ?",
                         (series, r["video_id"]))

    # --- annotations.project_id / annotator_id ---
    ann_cols = {r["name"] for r in conn.execute("PRAGMA table_info(annotations)")}
    first_pv_migration = "project_id" not in ann_cols
    if first_pv_migration:
        conn.execute("ALTER TABLE annotations ADD COLUMN project_id TEXT")
    if "annotator_id" not in ann_cols:
        conn.execute("ALTER TABLE annotations ADD COLUMN annotator_id TEXT")

    # --- project_videos 播種（一次性：僅在剛加 annotations.project_id 的那次執行）---
    # 之後 remove_video_from_project 的移除必須「留得住」，不能被重複播種蓋回去。
    if first_pv_migration:
        conn.execute(
            "INSERT OR IGNORE INTO project_videos (project_id, video_id, added_at) "
            "SELECT project_id, video_id, "
            "       COALESCE(ingested_at, ?) FROM videos WHERE project_id IS NOT NULL",
            (_now_iso(),))

    # --- 回填（每次都跑，冪等且自癒：容忍過渡期舊 server 寫入的缺 ID 列）---
    conn.execute(
        "UPDATE annotations SET project_id = COALESCE("
        "  (SELECT project_id FROM videos WHERE videos.video_id = annotations.video_id),"
        "  ?) WHERE project_id IS NULL", (DEFAULT_PROJECT_ID,))
    names = [r["annotator"] for r in conn.execute(
        "SELECT DISTINCT annotator FROM annotations WHERE annotator_id IS NULL")]
    for raw in names:
        name = (raw or "").strip() or "default"
        aid = get_or_create_annotator(conn, name)
        if (raw or "").strip():
            conn.execute(
                "UPDATE annotations SET annotator_id = ? "
                "WHERE annotator_id IS NULL AND annotator = ?", (aid, raw))
        else:
            conn.execute(
                "UPDATE annotations SET annotator_id = ? WHERE annotator_id IS NULL "
                "AND (annotator IS NULL OR TRIM(annotator) = '')", (aid,))

    conn.execute("CREATE INDEX IF NOT EXISTS idx_annotations_video_project "
                 "ON annotations(video_id, project_id)")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_default_project(conn: sqlite3.Connection) -> None:
    """首次遷移：建立 default 專案並把 yaml taxonomy 種入 labels 表（冪等）。

    - labels_temporal.yaml 的 phase/action/event + triplet segment_label（顏色沿用
      colors 區塊）與 labels_spatial.yaml 的 labels（含顏色）→ labels 表。
    - triplet 的 instrument/verb/target 三選單存 projects.extra_json。
    - 既有 labels_custom.yaml（手動新增的 label）一併併入，之後以 DB 為準。
    """
    n = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
    if n == 0:
        temporal = config.load_config("labels_temporal.yaml")
        spatial = config.load_config("labels_spatial.yaml")
        colors = temporal.get("colors") or {}
        trip = temporal.get("triplet") or {}
        extra = {"triplet": {k: trip.get(k, []) for k in ("instrument", "verb", "target")}}
        create_project(conn, project_id=DEFAULT_PROJECT_ID, name="Default",
                       project_type="mixed", annotation_group="surgery",
                       extra_json=json.dumps(extra, ensure_ascii=False))

        custom_path = config.CONFIGS_DIR / "labels_custom.yaml"
        custom = {}
        if custom_path.exists():
            import yaml
            custom = yaml.safe_load(custom_path.read_text(encoding="utf-8")) or {}

        for layer in ("phase", "action", "event"):
            names = list(temporal.get(layer) or [])
            names += [x for x in (custom.get(layer) or []) if x not in names]
            for name in names:
                add_label(conn, DEFAULT_PROJECT_ID, layer, name,
                          color=colors.get(f"{layer}/{name}"))
        seg = trip.get("segment_label", "Triplet")
        add_label(conn, DEFAULT_PROJECT_ID, "triplet", seg,
                  color=colors.get(f"triplet/{seg}"))
        spatial_labels = list(spatial.get("labels") or [])
        for l in (custom.get("spatial") or []):
            if isinstance(l, dict) and not any(x["name"] == l["name"] for x in spatial_labels):
                spatial_labels.append(l)
        for l in spatial_labels:
            add_label(conn, DEFAULT_PROJECT_ID, "spatial", l["name"],
                      color=l.get("color"))
    # v1.2 起不再回填 videos.project_id（欄位停用）：
    # 影片與專案的關係一律由 project_videos 表維護（ingest --project / API 加入清單）。


# ------------------------------------------------------------- projects -----

def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM projects ORDER BY created_at, project_id"))


def get_project(conn: sqlite3.Connection, project_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM projects WHERE project_id = ?",
                        (project_id,)).fetchone()


def create_project(conn: sqlite3.Connection, project_id: str, name: str,
                   project_type: str, annotation_group: str = "",
                   extra_json: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO projects (project_id, name, project_type, annotation_group, "
        "extra_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, name, project_type, annotation_group, extra_json,
         datetime.now(timezone.utc).isoformat()),
    )


# --------------------------------------------------------------- labels -----

def list_labels(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    """依插入順序回傳（rowid），確保 taxonomy 順序穩定。"""
    return list(conn.execute(
        "SELECT * FROM labels WHERE project_id = ? ORDER BY rowid", (project_id,)))


def add_label(conn: sqlite3.Connection, project_id: str, layer: str, name: str,
              color: Optional[str] = None, extra_json: Optional[str] = None) -> str:
    if layer not in LABEL_LAYERS:
        raise ValueError(f"未知 layer：{layer}")
    label_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO labels (label_id, project_id, layer, name, color, extra_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (label_id, project_id, layer, name, color, extra_json),
    )
    return label_id


def get_label(conn: sqlite3.Connection, label_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM labels WHERE label_id = ?",
                        (label_id,)).fetchone()


def label_usage(conn: sqlite3.Connection, project_id: str, layer: str,
                name: str) -> int:
    """該 label 在此專案內被標註使用的筆數（刪除前檢查用）。"""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM annotations "
        "WHERE layer = ? AND label = ? AND project_id = ?",
        (layer, name, project_id)).fetchone()["n"]


def update_label(conn: sqlite3.Connection, label_id: str,
                 name: Optional[str] = None, color: Optional[str] = None,
                 updated_at: Optional[str] = None) -> bool:
    """改名/改色。改名會連動更新此專案影片的既有標註（label 即類別名）。"""
    row = get_label(conn, label_id)
    if row is None:
        return False
    new_name = name if name is not None else row["name"]
    new_color = color if color is not None else row["color"]
    conn.execute("UPDATE labels SET name = ?, color = ? WHERE label_id = ?",
                 (new_name, new_color, label_id))
    if new_name != row["name"]:
        conn.execute(
            "UPDATE annotations SET label = ?, updated_at = ? "
            "WHERE layer = ? AND label = ? AND project_id = ?",
            (new_name, updated_at or datetime.now(timezone.utc).isoformat(),
             row["layer"], row["name"], row["project_id"]))
    return True


def delete_label(conn: sqlite3.Connection, label_id: str,
                 delete_annotations: bool = False) -> int:
    """刪 label；delete_annotations=True 時連同此專案內使用它的標註。回傳受影響標註數。"""
    row = get_label(conn, label_id)
    if row is None:
        return 0
    n = label_usage(conn, row["project_id"], row["layer"], row["name"])
    if delete_annotations and n:
        conn.execute(
            "DELETE FROM annotations WHERE layer = ? AND label = ? AND project_id = ?",
            (row["layer"], row["name"], row["project_id"]))
    conn.execute("DELETE FROM labels WHERE label_id = ?", (label_id,))
    return n


# ------------------------------------- project_videos（v1.2 播放清單模式）----

def add_video_to_project(conn: sqlite3.Connection, project_id: str,
                         video_id: str) -> None:
    """把影片加入專案播放清單（冪等；不複製影片本體）。"""
    conn.execute(
        "INSERT OR IGNORE INTO project_videos (project_id, video_id, added_at) "
        "VALUES (?, ?, ?)", (project_id, video_id, _now_iso()))


def remove_video_from_project(conn: sqlite3.Connection, project_id: str,
                              video_id: str) -> bool:
    """只把影片移出專案清單，**不動標註**（要連刪標註由呼叫端確認後另呼叫
    delete_annotations）。回傳是否有列被刪。"""
    cur = conn.execute(
        "DELETE FROM project_videos WHERE project_id = ? AND video_id = ?",
        (project_id, video_id))
    return cur.rowcount > 0


def video_projects(conn: sqlite3.Connection, video_id: str) -> list[str]:
    """此影片被哪些專案引用（播放清單反查）。"""
    return [r["project_id"] for r in conn.execute(
        "SELECT project_id FROM project_videos WHERE video_id = ? "
        "ORDER BY added_at, project_id", (video_id,))]


def set_video_project(conn: sqlite3.Connection, video_id: str, project_id: str) -> None:
    """DEPRECATED（v1.2）：改用 add_video_to_project()。

    保留簽名避免舊呼叫點瞬間爆炸；內部已改寫 project_videos（不再寫
    videos.project_id）。語意差異：不會把影片移出其他專案。
    """
    add_video_to_project(conn, project_id, video_id)


# ------------------------------------------------------------- series -------
# v1.3：series 為資產層獨立表。videos.series 欄位存 series_id。

def _new_series_code(conn: sqlite3.Connection) -> str:
    """產生未使用的隨機四碼大寫英文 seriesID（查重重抽）。"""
    for _ in range(1000):
        code = "".join(random.choices(string.ascii_uppercase, k=4))
        if conn.execute("SELECT 1 FROM series WHERE series_id = ?",
                        (code,)).fetchone() is None:
            return code
    raise RuntimeError("seriesID 產碼失敗：重試 1000 次仍碰撞")


def create_series(conn: sqlite3.Connection, name: str,
                  institution: Optional[str] = None,
                  surgery_type: Optional[str] = None,
                  note: Optional[str] = None) -> str:
    """建立 series（隨機四碼大寫英文 ID，查重）。回傳 series_id。

    name 唯一；重複時由 UNIQUE 約束丟 IntegrityError（呼叫端處理）。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("create_series：name 必填")
    series_id = _new_series_code(conn)
    now = _now_iso()
    conn.execute(
        "INSERT INTO series (series_id, name, institution, surgery_type, note, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (series_id, name, institution, surgery_type, note, now, now))
    return series_id


def get_series(conn: sqlite3.Connection, series_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM series WHERE series_id = ?",
                        (series_id,)).fetchone()


def list_series_detail(conn: sqlite3.Connection) -> list[dict]:
    """series 表全列（含衍生影片數 n_videos = COUNT(videos.series=series_id)）。"""
    return [dict(r) for r in conn.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM videos v WHERE v.series = s.series_id) "
        "AS n_videos FROM series s ORDER BY s.created_at, s.series_id")]


def update_series(conn: sqlite3.Connection, series_id: str,
                  name: Optional[str] = None,
                  institution: Optional[str] = None,
                  surgery_type: Optional[str] = None,
                  note: Optional[str] = None) -> bool:
    """更新 series metadata（僅帶入的欄位會改）；維護 updated_at。回傳是否有列被更新。"""
    fields: dict[str, str] = {}
    if name is not None:
        fields["name"] = name.strip()
        if not fields["name"]:
            raise ValueError("update_series：name 不可為空")
    if institution is not None:
        fields["institution"] = institution
    if surgery_type is not None:
        fields["surgery_type"] = surgery_type
    if note is not None:
        fields["note"] = note
    if not fields:
        return False
    fields["updated_at"] = _now_iso()
    sets = ", ".join(f"{c} = ?" for c in fields)
    cur = conn.execute(f"UPDATE series SET {sets} WHERE series_id = ?",
                       [*fields.values(), series_id])
    return cur.rowcount > 0


def touch_series(conn: sqlite3.Connection, series_id: str) -> None:
    """加/移除影片時更新 updated_at。"""
    conn.execute("UPDATE series SET updated_at = ? WHERE series_id = ?",
                 (_now_iso(), series_id))


def next_video_id(conn: sqlite3.Connection, series_id: str) -> str:
    """回傳該 series 的下一個 videoID：{series_id}_{NNNN}（每 series 續號）。"""
    if get_series(conn, series_id) is None:
        raise ValueError(f"next_video_id：series 不存在：{series_id}")
    prefix = f"{series_id}_"
    n = conn.execute(
        "SELECT COALESCE(MAX(CAST(SUBSTR(video_id, ?) AS INTEGER)), 0) AS n "
        "FROM videos WHERE video_id GLOB ?",
        (len(prefix) + 1, prefix + "[0-9][0-9][0-9][0-9]")).fetchone()["n"]
    return f"{prefix}{n + 1:04d}"


def add_id_history(conn: sqlite3.Connection, old_id: str, new_id: str,
                   entity: str, reason: str) -> None:
    """記一筆 ID 變更史（entity: video / series）。"""
    conn.execute(
        "INSERT INTO id_history (old_id, new_id, entity, reason, changed_at) "
        "VALUES (?, ?, ?, ?, ?)", (old_id, new_id, entity, reason, _now_iso()))


def set_video_series(conn: sqlite3.Connection, video_id: str, series: str) -> None:
    conn.execute("UPDATE videos SET series = ? WHERE video_id = ?",
                 (series, video_id))


def list_series(conn: sqlite3.Connection) -> list[dict]:
    """回傳 [{series, n_videos}, ...]（series 為 NULL 的影片不列）。"""
    return [{"series": r["series"], "n_videos": r["n"]} for r in conn.execute(
        "SELECT series, COUNT(*) AS n FROM videos "
        "WHERE series IS NOT NULL AND series != '' "
        "GROUP BY series ORDER BY series")]


def upsert_deid_map(conn: sqlite3.Connection, **fields) -> None:
    """以 video_id 為主鍵 upsert 一筆去識別化對照（★ API 永不回傳本表內容）。"""
    if "created_at" not in fields:
        fields["created_at"] = _now_iso()
    fields = _rel_fields("deid_map", fields)   # oob_csv_path → 相對 data 根（v1.4）
    cols = list(fields.keys())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "video_id")
    conn.execute(
        f"INSERT INTO deid_map ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(video_id) DO UPDATE SET {updates}",
        [fields[c] for c in cols])


# ------------------------------------------- pipeline_jobs（v1.3 佇列）------
# 最小存取層：worker/API 一律經由這些 helpers 讀寫（params/artifacts 的
# JSON 序列化與狀態機邏輯在 pipeline/jobs.py，此處只管列存取）。

def insert_pipeline_job(conn: sqlite3.Connection, **fields) -> None:
    """新增一筆 pipeline job（created_at/updated_at 未給時自動補）。"""
    fields.setdefault("created_at", _now_iso())
    fields.setdefault("updated_at", fields["created_at"])
    cols = list(fields.keys())
    conn.execute(
        f"INSERT INTO pipeline_jobs ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        [fields[c] for c in cols])


def get_pipeline_job(conn: sqlite3.Connection, job_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM pipeline_jobs WHERE job_id = ?",
                        (job_id,)).fetchone()


def list_pipeline_jobs(conn: sqlite3.Connection, status: Optional[str] = None,
                       limit: Optional[int] = None,
                       oldest_first: bool = False) -> list[sqlite3.Row]:
    """列 jobs。UI 預設新到舊；worker 認領佇列用 oldest_first=True（FIFO）。"""
    sql = "SELECT * FROM pipeline_jobs"
    params: list = []
    if status is not None:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created_at " + ("ASC" if oldest_first else "DESC")
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return list(conn.execute(sql, params))


def update_pipeline_job(conn: sqlite3.Connection, job_id: str, **fields) -> bool:
    """更新指定欄位（自動補 updated_at）。回傳是否有列被更新。"""
    fields.setdefault("updated_at", _now_iso())
    sets = ", ".join(f"{c} = ?" for c in fields)
    cur = conn.execute(f"UPDATE pipeline_jobs SET {sets} WHERE job_id = ?",
                       [*fields.values(), job_id])
    return cur.rowcount > 0


def delete_pipeline_job(conn: sqlite3.Connection, job_id: str) -> bool:
    """刪除 job 列（工作目錄清理由 jobs.delete_job 負責）。回傳是否有列被刪。"""
    cur = conn.execute("DELETE FROM pipeline_jobs WHERE job_id = ?", (job_id,))
    return cur.rowcount > 0


def set_group(conn: sqlite3.Connection, video_id: str, group_name: str) -> None:
    conn.execute("UPDATE videos SET group_name = ? WHERE video_id = ?",
                 (group_name, video_id))


def get_video(video_id: str, conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,))
    return cur.fetchone()


def upsert_video(conn: sqlite3.Connection, **fields) -> None:
    """以 video_id 為主鍵 upsert 一筆影片記錄。std_path / source_path 自動轉 v1.4 相對路徑。"""
    fields = _rel_fields("videos", fields)
    cols = list(fields.keys())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "video_id")
    sql = (
        f"INSERT INTO videos ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(video_id) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [fields[c] for c in cols])


def replace_frames(conn: sqlite3.Connection, video_id: str, rows: Iterable[dict]) -> int:
    """重寫某影片的 frames 抽樣點（先刪後插，確保重跑冪等）。回傳寫入列數。"""
    conn.execute("DELETE FROM frames WHERE video_id = ?", (video_id,))
    rows = [_rel_fields("frames", dict(r)) for r in rows]   # path → 相對 data 根（v1.4）
    conn.executemany(
        "INSERT INTO frames (video_id, frame_index, time_sec, path, purpose) "
        "VALUES (:video_id, :frame_index, :time_sec, :path, :purpose)",
        rows,
    )
    return len(rows)


def list_videos(conn: sqlite3.Connection, project_id: Optional[str] = None,
                series: Optional[str] = None) -> list[sqlite3.Row]:
    """列影片。project_id 過濾走 project_videos（播放清單）；series 過濾走 videos.series。"""
    sql = "SELECT v.* FROM videos v"
    where, params = [], []
    if project_id is not None:
        sql += " JOIN project_videos pv ON pv.video_id = v.video_id"
        where.append("pv.project_id = ?")
        params.append(project_id)
    if series is not None:
        where.append("v.series = ?")
        params.append(series)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return list(conn.execute(sql + " ORDER BY v.video_id", params))


def upsert_task(conn: sqlite3.Connection, **fields) -> None:
    """以 task_uid 為主鍵 upsert 一筆任務記錄（平台任務 ↔ registry 對應）。"""
    cols = list(fields.keys())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "task_uid")
    sql = (
        f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(task_uid) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [fields[c] for c in cols])


def find_task(conn: sqlite3.Connection, platform: str, video_id: str,
              task_type: str) -> Optional[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM tasks WHERE platform = ? AND video_id = ? AND task_type = ?",
        (platform, video_id, task_type),
    )
    return cur.fetchone()


def add_snapshot(conn: sqlite3.Connection, **fields) -> None:
    """記錄一次標註快照（每次匯出的原始 JSON 存檔 → versioning）。
    raw_json_path 自動轉 v1.4 相對路徑。"""
    fields = _rel_fields("annotation_snapshots", fields)
    cols = list(fields.keys())
    conn.execute(
        f"INSERT INTO annotation_snapshots ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        [fields[c] for c in cols],
    )


def add_export_run(conn: sqlite3.Connection, run_id: str, run_dir: str,
                   config_json: str, created_at: str) -> None:
    """記錄一次匯出 run。run_dir 自動轉 v1.4 相對路徑（呼叫端給絕對或相對皆可）。"""
    run_dir = config.to_rel(run_dir)
    conn.execute(
        "INSERT INTO export_runs (run_id, run_dir, config_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (run_id, run_dir, config_json, created_at),
    )


def latest_export_run(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM export_runs ORDER BY created_at DESC LIMIT 1")
    return cur.fetchone()


# ----------------------------------------------- annotators（v1.2 名冊）-----

def list_annotators(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM annotators ORDER BY annotator_id"))


def get_or_create_annotator(conn: sqlite3.Connection, name: str) -> str:
    """依名字取（或建）annotator，回傳連號 slug id（u001, u002, …）。"""
    name = (name or "").strip() or "default"
    row = conn.execute("SELECT annotator_id FROM annotators WHERE name = ?",
                       (name,)).fetchone()
    if row is not None:
        return row["annotator_id"]
    n = conn.execute(
        "SELECT COALESCE(MAX(CAST(SUBSTR(annotator_id, 2) AS INTEGER)), 0) AS n "
        "FROM annotators WHERE annotator_id GLOB 'u[0-9]*'").fetchone()["n"]
    annotator_id = f"u{n + 1:03d}"
    conn.execute(
        "INSERT INTO annotators (annotator_id, name, created_at) VALUES (?, ?, ?)",
        (annotator_id, name, _now_iso()))
    return annotator_id


# ------------------------------------------------- annotations（v1.1）----

def list_annotations(conn: sqlite3.Connection, video_id: str,
                     project_id: Optional[str] = None) -> list[sqlite3.Row]:
    """列某影片標註；給 project_id 時只回該專案的（v1.2 按專案隔離）。"""
    if project_id is None:
        return list(conn.execute(
            "SELECT * FROM annotations WHERE video_id = ? "
            "ORDER BY layer, start_frame, end_frame", (video_id,)))
    return list(conn.execute(
        "SELECT * FROM annotations WHERE video_id = ? AND project_id = ? "
        "ORDER BY layer, start_frame, end_frame", (video_id, project_id)))


def get_annotation(conn: sqlite3.Connection, ann_id: str,
                   project_id: Optional[str] = None) -> Optional[sqlite3.Row]:
    """取單筆標註；給 project_id 時附帶專案歸屬檢查（不符 → None，
    供 API 以 404 回應、不洩漏其他專案標註的存在性）。"""
    if project_id is None:
        return conn.execute(
            "SELECT * FROM annotations WHERE ann_id = ?", (ann_id,)).fetchone()
    return conn.execute(
        "SELECT * FROM annotations WHERE ann_id = ? AND project_id = ?",
        (ann_id, project_id)).fetchone()


def insert_annotation(conn: sqlite3.Connection, **fields) -> None:
    """低階 raw insert（舊介面，保留相容）。新程式請改用 add_annotation()：
    v1.2 起每筆標註必須帶 project_id 與 annotator_id。"""
    cols = list(fields.keys())
    conn.execute(
        f"INSERT INTO annotations ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        [fields[c] for c in cols],
    )


def add_annotation(conn: sqlite3.Connection, *, video_id: str, project_id: str,
                   annotator_id: str, layer: str, label: str,
                   start_frame: int, end_frame: int,
                   attrs_json: Optional[str] = None,
                   annotator: Optional[str] = None,
                   ann_id: Optional[str] = None,
                   created_at: Optional[str] = None,
                   updated_at: Optional[str] = None) -> str:
    """新增一筆標註（v1.2 契約：project_id 與 annotator_id 必填）。回傳 ann_id。

    annotator（顯示名）未給時自動由 annotators 名冊反查。
    """
    if not project_id:
        raise ValueError("add_annotation：project_id 必填（標註按專案隔離）")
    if not annotator_id:
        raise ValueError("add_annotation：annotator_id 必填")
    if annotator is None:
        row = conn.execute("SELECT name FROM annotators WHERE annotator_id = ?",
                           (annotator_id,)).fetchone()
        annotator = row["name"] if row else annotator_id
    ann_id = ann_id or str(uuid.uuid4())
    now = _now_iso()
    conn.execute(
        "INSERT INTO annotations (ann_id, video_id, project_id, annotator_id, "
        "layer, label, start_frame, end_frame, attrs_json, annotator, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ann_id, video_id, project_id, annotator_id, layer, label,
         start_frame, end_frame, attrs_json, annotator,
         created_at or now, updated_at or now))
    return ann_id


def count_annotations(conn: sqlite3.Connection, video_id: str,
                      project_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM annotations WHERE video_id = ? AND project_id = ?",
        (video_id, project_id)).fetchone()["n"]


def delete_annotations(conn: sqlite3.Connection, video_id: str,
                       project_id: str) -> int:
    """刪除某影片在某專案內的全部標註（「移出專案並連同刪除」用；破壞性，
    呼叫端必須先取得使用者確認 — R9）。回傳刪除筆數。"""
    cur = conn.execute(
        "DELETE FROM annotations WHERE video_id = ? AND project_id = ?",
        (video_id, project_id))
    return cur.rowcount


def update_annotation(conn: sqlite3.Connection, ann_id: str, **fields) -> bool:
    """更新指定欄位；回傳是否有列被更新。"""
    sets = ", ".join(f"{c} = ?" for c in fields)
    cur = conn.execute(
        f"UPDATE annotations SET {sets} WHERE ann_id = ?",
        [*fields.values(), ann_id],
    )
    return cur.rowcount > 0


def delete_annotation(conn: sqlite3.Connection, ann_id: str,
                      project_id: Optional[str] = None) -> bool:
    """刪單筆標註；給 project_id 時只刪屬於該專案的（不符 → False）。"""
    if project_id is None:
        cur = conn.execute("DELETE FROM annotations WHERE ann_id = ?", (ann_id,))
    else:
        cur = conn.execute(
            "DELETE FROM annotations WHERE ann_id = ? AND project_id = ?",
            (ann_id, project_id))
    return cur.rowcount > 0


def count_frames(conn: sqlite3.Connection, video_id: str) -> dict[str, int]:
    cur = conn.execute(
        "SELECT purpose, COUNT(*) AS n FROM frames WHERE video_id = ? GROUP BY purpose",
        (video_id,),
    )
    return {r["purpose"]: r["n"] for r in cur}
