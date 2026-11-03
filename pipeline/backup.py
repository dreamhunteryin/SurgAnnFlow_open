"""surgann backup：registry.db + 可攜標註快照 → backups/，可 rsync。

動 registry schema、大規模重構、任何破壞性操作之前先跑本備份。
registry.db 是標註的唯一來源，沒有第二份資料庫可回頭。
原則：
- 只讀 registry；永不修改（更不碰 data/raw_videos，原始影片唯讀）。
- 標註快照為平台外、可攜的 JSON dump（不依賴 SQLite/平台版本）——第二階段搬 NAS 的保險。
- 絕不刪既有備份；每次備份自成一個時間戳目錄。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config


class BackupError(RuntimeError):
    """備份過程中的可預期錯誤。"""


@dataclass
class BackupOutcome:
    backup_dir: Path
    archive: Path
    table_counts: dict[str, int]
    files: list[str] = field(default_factory=list)
    rsynced: str | None = None


def _dump_sqlite(db_path: Path) -> dict:
    """把 registry 所有表匯成可攜 JSON（table -> list[row dict]）。

    動態列舉 sqlite_master，故後來新增的表（series/pipeline_jobs/deid_map/id_history）
    自動納入。注意：deid_map 含 PHI——backups/ 已 gitignore，
    備份檔不得外流或進版控。
    """
    out: dict[str, list[dict]] = {}
    if not db_path.exists():
        return out
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            out[t] = [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
    finally:
        conn.close()
    return out


def run_backup(label: str | None = None,
               rsync_dest: str | None = None, db_path=None,
               now: datetime | None = None, on_step=None) -> BackupOutcome:
    """執行一次完整備份。回傳 BackupOutcome。"""
    def step(m):
        if on_step:
            on_step(m)

    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M%S")
    name = f"{ts}" + (f"_{label}" if label else "")
    backup_dir = config.BACKUPS_DIR / name
    if backup_dir.exists():
        raise BackupError(f"備份目錄已存在：{backup_dir}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []

    src_db = config.REGISTRY_DB if db_path is None else Path(db_path)

    # 1) registry.db 原檔（SQLite 備援用 backup API 確保一致性快照）
    step("複製 registry.db")
    if src_db.exists():
        _sqlite_safe_copy(src_db, backup_dir / "registry.db")
        files.append("registry.db")

    # 2) 可攜 JSON 快照（平台外、不依賴 SQLite 版本）
    step("匯出可攜標註快照（JSON）")
    dump = _dump_sqlite(src_db)
    (backup_dir / "registry_snapshot.json").write_text(
        json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")
    files.append("registry_snapshot.json")
    table_counts = {t: len(rows) for t, rows in dump.items()}

    # 3) manifest
    # registry schema v1.4 起路徑欄位相對 data 根 → 記下備份當時的 data 根與 schema 版本，
    # 搬機還原時才知道相對路徑要接到哪（不含 PHI；deid_map 內容仍只在 db/json 內）
    schema_version = next((r.get("value") for r in dump.get("schema_meta", [])
                           if r.get("key") == "schema_version"), None)
    manifest = {
        "created_at": (now or datetime.now(timezone.utc)).isoformat(),
        "schema_version": schema_version or "1.3",
        "data_dir": str(config.DATA_DIR),
        "table_counts": table_counts, "files": files,
        "note": "安全備份：動工前的標註快照。還原方式見 INSTALL_agent.md 的備份段。",
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    files.append("manifest.json")

    # 4) 打包 tar.gz（保留目錄，另出壓縮檔便於搬移）
    step("打包 tar.gz")
    archive = config.BACKUPS_DIR / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(backup_dir, arcname=name)

    # 5) rsync（可選；到第二顆磁碟/外接碟）
    rsynced = None
    if rsync_dest:
        step(f"rsync → {rsync_dest}")
        r = subprocess.run(["rsync", "-a", str(archive), rsync_dest],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise BackupError(f"rsync 失敗：{r.stderr.strip()[:300]}")
        rsynced = rsync_dest

    return BackupOutcome(backup_dir=backup_dir, archive=archive,
                         table_counts=table_counts, files=files,
                         rsynced=rsynced)


def _sqlite_safe_copy(src: Path, dst: Path) -> None:
    """用 SQLite backup API 取一致性快照（避免複製到寫入中的檔）。失敗則退回 shutil。"""
    try:
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        d = sqlite3.connect(dst)
        with d:
            s.backup(d)
        s.close(); d.close()
    except sqlite3.Error:
        shutil.copy2(src, dst)
