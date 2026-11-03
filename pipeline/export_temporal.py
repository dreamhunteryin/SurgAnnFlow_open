"""temporal 標註匯出（v1.1）：registry annotations → 快照 + 各層 CSV。

欄位定義與一致性檢查共用 export_common（R4）；來源為 registry `annotations` 表
（SurgAnn Annotator 直接寫入），不經過任何外部平台格式轉換。
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone

from . import registry
from .export_common import (ExportOutcome, TEMPORAL_FIELDS, TRIPLET_FIELDS, LAYERS,
                            TRIPLET_LAYER, check_invariants, exports_dir, next_run_id)

# v1.2 追溯 ID 欄（附加於各層 CSV 尾端；annotator_id 改為 annotators 名冊 id，
# 顯示名另出 annotator_name）
ID_FIELDS = ["project_id", "annotator_name"]


def rows_from_registry(conn, project_id: str | None = None,
                       ) -> tuple[dict[str, list[dict]], dict[str, float], list[dict]]:
    """annotations 表 → 各層列 + fps 對照 + 原始列（供快照）。

    project_id 給定時以 annotations.project_id 直查（v1.2 標註按專案隔離）；
    影片範圍（fps 對照）走 project_videos 播放清單（registry.list_videos）。
    """
    fps_by_video = {r["video_id"]: float(r["fps"])
                    for r in registry.list_videos(conn, project_id=project_id)}
    rows: dict[str, list[dict]] = {k: [] for k in (*LAYERS, TRIPLET_LAYER)}
    raw: list[dict] = []
    sql = "SELECT * FROM annotations"
    params: tuple = ()
    if project_id is not None:
        sql += " WHERE project_id = ?"
        params = (project_id,)
    cur = conn.execute(sql + " ORDER BY video_id, layer, start_frame", params)
    for r in cur:
        d = dict(r)
        raw.append(d)
        fps = fps_by_video.get(d["video_id"])
        if fps is None:
            continue
        attrs = json.loads(d["attrs_json"] or "{}")
        base = {
            "video_id": d["video_id"],
            "start_frame": d["start_frame"], "end_frame": d["end_frame"],
            "start_time": round(d["start_frame"] / fps, 6),
            "end_time": round(d["end_frame"] / fps, 6),
            "label": d["label"],
            "annotator_id": d["annotator_id"] or d["annotator"],
            "annotation_id": d["ann_id"], "version": d["updated_at"] or "",
            "project_id": d["project_id"] or "",
            "annotator_name": d["annotator"],
        }
        if d["layer"] == TRIPLET_LAYER:
            rows[TRIPLET_LAYER].append({
                **base,
                "instrument": attrs.get("instrument", ""),
                "verb": attrs.get("verb", ""),
                "target": attrs.get("target", ""),
            })
        elif d["layer"] in LAYERS:
            rows[d["layer"]].append(base)
    return rows, fps_by_video, raw


def run_export(db_path=None, project_id: str | None = None) -> ExportOutcome:
    now = datetime.now(timezone.utc).isoformat()
    registry.init_db(db_path)
    with registry.connect(db_path) as conn:
        rows, fps_by_video, raw = rows_from_registry(conn, project_id=project_id)
        check_invariants(rows, fps_by_video)

        run_id = next_run_id(project_id)
        run_dir = exports_dir(project_id) / run_id
        raw_dir = run_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / "annotations.json"
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=1),
                            encoding="utf-8")

        csv_paths = {}
        for layer in (*LAYERS, TRIPLET_LAYER):
            fields = (TRIPLET_FIELDS if layer == TRIPLET_LAYER
                      else TEMPORAL_FIELDS) + ID_FIELDS
            path = run_dir / f"{layer}.csv"
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(rows[layer])
            csv_paths[layer] = path

        # 快照記錄：每 (video, annotator) 一列
        pairs = sorted({(d["video_id"], d["annotator"]) for d in raw})
        for vid, annotator in pairs:
            registry.add_snapshot(
                conn, snapshot_id=run_id, task_uid=f"annot-{vid}",
                annotator=annotator, raw_json_path=str(raw_path), exported_at=now,
            )
        registry.add_export_run(
            conn, run_id=run_id, run_dir=str(run_dir),
            config_json=json.dumps({
                "source": "surgann-annotator",
                "project_id": project_id,
                "n_annotations": len(raw),
                "videos": sorted({d["video_id"] for d in raw}),
            }),
            created_at=now,
        )

    return ExportOutcome(
        run_id=run_id, run_dir=run_dir,
        n_tasks=len({d["video_id"] for d in raw}), n_annotations=len(raw),
        row_counts={k: len(v) for k, v in rows.items()}, csv_paths=csv_paths,
    )
