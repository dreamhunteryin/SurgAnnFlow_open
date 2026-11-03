"""registry schema v1.3 → v1.4 遷移（2026-11-02）。

把 6 個路徑欄位（registry.PATH_COLUMNS）由絕對路徑改存「相對 data 根的 POSIX 路徑」：
  videos.std_path / videos.source_path / frames.path /
  annotation_snapshots.raw_json_path / export_runs.run_dir / deid_map.oob_csv_path

規則（每個非空值）：
  1. 絕對且在 data 根內            → 相對（config.to_rel）
  2. 絕對、在 --legacy-data-root 內，且換到現行 data 根後檔案「確實存在」
                                     → 相對（修復掛載點漂移，如磁碟重新掛載後名稱變成 /media/<user>/Disk1/... 的舊值）
  3. 絕對、在 data 根外             → 原樣保留（data 根外保留絕對路徑）
  4. 相對值：
     - 庫版本 < 1.4（schema v1.3 語意＝相對 repo 根，例 `data/exports/...`）→ 以 PROJECT_ROOT 解析後套 1–3；
       但若「相對 data 根」解讀下檔案已存在，視為已是 schema v1.4 值、不動
     - 庫版本 = 1.4 → 已是 schema v1.4 值，不動
最後 schema_meta.schema_version = '1.4'。

冪等：重跑只會把殘留的絕對路徑（如過渡期舊程式寫入者）再轉一次，已相對者不動。
單一 transaction；--dry-run 只報告不寫入。遷移前後印各表 COUNT 與「to_abs 後檔案存在」對帳。

執行（先跑 surgann backup：動 DB 前先備份）：
    python -m pipeline.migrations.m20261102_relative_paths --dry-run
    python -m pipeline.migrations.m20261102_relative_paths [--legacy-data-root /舊/掛載點/data ...]

過渡期退路（非破壞性，可再正向重跑）：`--to-absolute` 把相對值以 to_abs 展回絕對路徑、
版本記回 1.3——只給「仍在跑 schema v1.3 舊程式的服務」暫時救急用，消費端都改走 helper 後不應再用。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path, PurePosixPath
from typing import Optional

from .. import config, registry


def _legacy_abs(value: str) -> Path:
    """schema v1.3 語意解讀：絕對原樣；相對＝相對 repo 根（舊 writer 以 repo 根為 cwd 寫入）。"""
    p = Path(value)
    return p if p.is_absolute() else config.PROJECT_ROOT / p


def convert_value(value: Optional[str], legacy: bool,
                  legacy_roots: list[Path]) -> tuple[Optional[str], str]:
    """回傳 (新值, 分類)。分類：empty / rel_kept / to_rel / legacy_root / outside。"""
    if value is None or value == "":
        return value, "empty"
    p = Path(value)
    if not p.is_absolute():
        if not legacy:
            return value, "rel_kept"
        if (config.DATA_DIR / p).exists():          # 已是相對 data 根（且確實存在）
            return PurePosixPath(*p.parts).as_posix(), "rel_kept"
        p = config.PROJECT_ROOT / p                 # schema v1.3：相對 repo 根
    new = config.to_rel(p)
    if not Path(new).is_absolute():
        return new, ("to_rel" if new != value else "rel_kept")
    for root in legacy_roots:
        try:
            rel = Path(os.path.abspath(p)).relative_to(os.path.abspath(root)).as_posix()
        except ValueError:
            continue
        if (config.DATA_DIR / rel).exists():
            return rel, "legacy_root"
    return new, "outside"


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    """各資料表筆數（schema_meta 為本遷移自身寫入的版本紀錄，不列入對帳）。"""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND name != 'schema_meta' ORDER BY name")]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def _exist_report(conn: sqlite3.Connection, resolver) -> dict[str, tuple[int, int]]:
    """每個路徑欄位：(非空值數, resolver 解析後檔案存在數)。"""
    out = {}
    for t, c in registry.PATH_COLUMNS:
        vals = [r[0] for r in conn.execute(f"SELECT {c} FROM {t}") if r[0]]
        out[f"{t}.{c}"] = (len(vals), sum(1 for v in vals if resolver(v).exists()))
    return out


def migrate(db_path: Optional[Path] = None, dry_run: bool = False,
            legacy_roots: Optional[list[Path]] = None, log=print) -> dict:
    """執行遷移；回傳摘要 dict（給測試/證據用）。"""
    legacy_roots = [Path(r) for r in (legacy_roots or [])]
    db = registry.init_db(db_path)   # 確保 schema_meta 表存在（舊庫會被記為 1.3）
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        version_before = registry.get_schema_version(conn)
        legacy = version_before != registry.SCHEMA_VERSION
        log(f"registry：{db}")
        log(f"data 根：{config.DATA_DIR}（來源：{config.DATA_DIR_SOURCE}）")
        log(f"schema 版本（遷移前）：{version_before}"
            + ("（相對值以 repo 根解讀）" if legacy else "（已是 schema v1.4，只轉殘留絕對值）"))
        counts_before = _counts(conn)
        exist_before = _exist_report(conn, _legacy_abs if legacy else config.to_abs)

        stats: dict[str, dict[str, int]] = {}
        outside: list[str] = []
        conn.execute("BEGIN")
        for t, c in registry.PATH_COLUMNS:
            st = stats.setdefault(f"{t}.{c}", {})
            rows = conn.execute(f"SELECT rowid, {c} FROM {t}").fetchall()
            for r in rows:
                new, kind = convert_value(r[1], legacy, legacy_roots)
                st[kind] = st.get(kind, 0) + 1
                if kind == "outside":
                    outside.append(f"{t}.{c}: {r[1]}")
                if new != r[1]:
                    conn.execute(f"UPDATE {t} SET {c} = ? WHERE rowid = ?", (new, r[0]))
        registry.set_meta(conn, "schema_version", registry.SCHEMA_VERSION)
        registry.set_meta(conn, "paths_relative_to", "data_dir")

        counts_after = _counts(conn)
        exist_after = _exist_report(conn, config.to_abs)
        if dry_run:
            conn.rollback()
            log("（--dry-run：已回滾，未寫入）")
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log("\n欄位轉換統計（empty=空值 / to_rel=轉相對 / legacy_root=舊掛載點修復 / "
        "rel_kept=原已相對 / outside=data 根外保留絕對）：")
    for k, st in stats.items():
        log(f"  {k:36s} " + "  ".join(f"{a}={b}" for a, b in sorted(st.items())))
    if outside:
        log(f"\ndata 根外保留絕對路徑（{len(outside)} 筆）：")
        for o in outside:
            log(f"  {o}")
    log("\n各表 COUNT（前 → 後）：")
    for t in sorted(set(counts_before) | set(counts_after)):
        b, a = counts_before.get(t), counts_after.get(t)
        log(f"  {t:22s} {b} → {a}" + ("" if b == a else "  ✗ 不一致"))
    log("\n檔案存在對帳（非空值數 / 存在數；前＝schema v1.3 解讀，後＝to_abs）：")
    for k in exist_before:
        (nb, eb), (na, ea) = exist_before[k], exist_after[k]
        flag = "" if (nb == na and ea >= eb) else "  ✗ 退化"
        log(f"  {k:36s} {nb}/{eb} → {na}/{ea}{flag}")

    ok = counts_before == counts_after and all(
        exist_before[k][0] == exist_after[k][0] and exist_after[k][1] >= exist_before[k][1]
        for k in exist_before)
    log(f"\n結果：{'OK' if ok else '對帳失敗'}；schema 版本 → "
        f"{registry.SCHEMA_VERSION if not dry_run else version_before + '（dry-run 未變）'}")
    return {"ok": ok, "version_before": version_before, "stats": stats,
            "counts_before": counts_before, "counts_after": counts_after,
            "exist_before": exist_before, "exist_after": exist_after,
            "outside": outside, "dry_run": dry_run}


def to_absolute(db_path: Optional[Path] = None, dry_run: bool = False, log=print) -> dict:
    """退路：schema v1.4 相對值 → 絕對（config.to_abs），schema_version 記回 1.3。冪等、單一 transaction。"""
    db = registry.init_db(db_path)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    n = 0
    try:
        before = _counts(conn)
        conn.execute("BEGIN")
        for t, c in registry.PATH_COLUMNS:
            for r in conn.execute(f"SELECT rowid, {c} FROM {t}").fetchall():
                v = r[1]
                if v and not Path(v).is_absolute():
                    conn.execute(f"UPDATE {t} SET {c} = ? WHERE rowid = ?",
                                 (str(config.to_abs(v)), r[0]))
                    n += 1
        registry.set_meta(conn, "schema_version", registry.LEGACY_SCHEMA_VERSION)
        ok = before == _counts(conn)
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log(f"--to-absolute：{n} 筆相對值展回絕對（data 根 {config.DATA_DIR}）；COUNT {'一致' if ok else '不一致'}"
        + ("（dry-run 已回滾）" if dry_run else "；schema_version → 1.3"))
    return {"ok": ok, "converted": n, "dry_run": dry_run}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="registry schema v1.3 → v1.4：路徑欄位改相對 data 根")
    ap.add_argument("--db", type=Path, default=None, help="registry.db 路徑（預設 config.REGISTRY_DB）")
    ap.add_argument("--dry-run", action="store_true", help="只報告不寫入")
    ap.add_argument("--legacy-data-root", type=Path, action="append", default=[],
                    help="舊 data 根（掛載點漂移前的路徑，可多次給）；"
                         "其下的值若在現行 data 根下確實存在則轉相對")
    ap.add_argument("--to-absolute", action="store_true",
                    help="過渡期退路：相對值展回絕對、版本記回 1.3（之後可再正向重跑）")
    args = ap.parse_args(argv)
    if args.to_absolute:
        return 0 if to_absolute(args.db, dry_run=args.dry_run)["ok"] else 1
    out = migrate(args.db, dry_run=args.dry_run, legacy_roots=args.legacy_data_root)
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
