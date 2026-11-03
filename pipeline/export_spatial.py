"""spatial 標註匯出（v1.2）：registry annotations(layer='spatial') →
全保真 spatial.json + keypoint frames 補抽幀（purpose=manual）+ COCO。

與 export_temporal 同源（registry 直讀，不經平台）；COCO 每張 image 注入
metadata（video_id/frame_index/time_sec/fps）供回溯來源影片。

不變式：每張 image 的 time_sec 必 == frame_index / fps；
COCO 寫檔後重讀自我檢查，不通過即 fail。
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config, registry
from .export_common import exports_dir, next_run_id
from .ingest import _require


class SpatialExportError(RuntimeError):
    """spatial 匯出過程中的可預期錯誤。"""


# ------------------------------------------------------------- 幾何工具 ----

def bounding_box(shape: str, points: list[float]) -> list[float]:
    """各 shape → COCO 外接 bbox [x, y, w, h]（point 為 [x, y, 0, 0]）。"""
    if shape == "bbox":
        x1, y1, x2, y2 = points
        x, y = min(x1, x2), min(y1, y2)
        return [x, y, abs(x2 - x1), abs(y2 - y1)]
    if shape == "point":
        return [points[0], points[1], 0.0, 0.0]
    if shape == "ellipse":
        cx, cy, rx, ry = points
        return [cx - rx, cy - ry, 2 * rx, 2 * ry]
    if shape in ("polygon", "polyline"):
        xs, ys = points[0::2], points[1::2]
        return [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]
    raise SpatialExportError(f"未知 shape：{shape}")


def shoelace_area(points: list[float]) -> float:
    """polygon [x1,y1,...] → 面積（shoelace 公式，頂點自動閉合）。"""
    xs, ys = points[0::2], points[1::2]
    n = len(xs)
    s = sum(xs[i] * ys[(i + 1) % n] - xs[(i + 1) % n] * ys[i] for i in range(n))
    return abs(s) / 2.0


# --------------------------------------------------------- frames 補抽幀 ----

def extract_frame(std_path: Path, fps: float, frame: int, out_path: Path) -> None:
    """ffmpeg 精準抽出單一 frame（-ss frame/fps → 該 frame 的 PTS）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-ss", f"{frame / fps:.6f}", "-i", str(std_path),
           "-frames:v", "1", "-q:v", "2", str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out_path.exists():
        raise SpatialExportError(
            f"ffmpeg 抽幀失敗（{out_path.name}）：\n{proc.stderr[-500:]}")


def ensure_keyframe_images(conn, videos: dict[str, dict],
                           needed: set[tuple[str, int]]) -> int:
    """補抽缺少的 keypoint frame JPEG，並以 purpose='manual' upsert 進 frames 表。

    回傳新抽出的張數。已存在的 JPEG 不重抽（原始影片唯讀不受影響，R9）。
    """
    n_new = 0
    for vid, frame in sorted(needed):
        v = videos[vid]
        out_path = config.frame_path(vid, frame)
        if not out_path.exists():
            std_path = config.to_abs(v["std_path"])   # v1.4：registry 存相對 data 根
            if not std_path or not std_path.exists():
                raise SpatialExportError(f"標準化影片不存在：{std_path}")
            extract_frame(std_path, v["fps"], frame, out_path)
            n_new += 1
            conn.execute(
                "INSERT OR REPLACE INTO frames "
                "(video_id, frame_index, time_sec, path, purpose) "
                "VALUES (?, ?, ?, ?, 'manual')",
                (vid, frame, round(frame / v["fps"], 6), config.to_rel(out_path)),
            )
    return n_new


# ------------------------------------------------------------- COCO 組裝 ----

def build_coco(rows: list[dict], videos: dict[str, dict],
               labels: list[str], project_id: str | None = None) -> dict:
    """spatial 列 → COCO dict（info / images / categories / annotations）。

    images[] 每個唯一 (video_id, keypoint frame) 一張，注入回溯 metadata；
    annotations[] 每個標註 × keypoint 一筆，extra 帶齊追溯 ID
    （ann_id/ann_uid、video_id、project_id、annotator_id、annotator_name）；
    info 記 project_id（COCO 允許額外 key，v1.2 供後處理按專案/影片/標註者篩選）。
    """
    cat_id = {name: i + 1 for i, name in enumerate(labels)}
    categories = [{"id": i + 1, "name": name, "supercategory": ""}
                  for i, name in enumerate(labels)]

    pairs = sorted({(r["video_id"], kp["frame"])
                    for r in rows for kp in r["keypoints"]})
    images, image_id = [], {}
    for i, (vid, frame) in enumerate(pairs, start=1):
        v = videos[vid]
        image_id[(vid, frame)] = i
        images.append({
            "id": i,
            "file_name": f"{vid}/{vid}_f{frame:07d}.jpg",
            "width": v["width"], "height": v["height"],
            "video_id": vid, "frame_index": frame,
            "time_sec": round(frame / v["fps"], 6), "fps": v["fps"],
        })

    annotations, aid = [], 0
    for r in rows:
        if r["label"] not in cat_id:
            raise SpatialExportError(
                f"label「{r['label']}」不在類別清單內"
                f"（ann_id={r['ann_id']}）。")
        shape = r["shape"]
        for kp in r["keypoints"]:
            pts = [float(p) for p in kp["points"]]
            bbox = [round(c, 3) for c in bounding_box(shape, pts)]
            if shape == "bbox":
                area = bbox[2] * bbox[3]
            elif shape == "polygon":
                area = shoelace_area(pts)
            elif shape == "point":
                area = 0.0
            else:  # ellipse / polyline：外接 bbox 面積
                area = bbox[2] * bbox[3]
            aid += 1
            ann = {
                "id": aid,
                "image_id": image_id[(r["video_id"], kp["frame"])],
                "category_id": cat_id[r["label"]],
                "bbox": bbox,
                "area": round(area, 3),
                "iscrowd": 0,
                "extra": {"shape": shape, "points": pts,
                          "ann_uid": r["ann_id"],
                          "ann_id": r["ann_id"],
                          "video_id": r["video_id"],
                          "project_id": r.get("project_id"),
                          "annotator_id": r.get("annotator_id"),
                          "annotator_name": r.get("annotator"),
                          "interpolate": r["interpolate"]},
            }
            if shape == "polygon":
                ann["segmentation"] = [pts]
            annotations.append(ann)

    return {"info": {"project_id": project_id,
                     "source": "surgann-registry"},
            "images": images, "categories": categories,
            "annotations": annotations}


def check_coco_invariants(coco_path: Path, videos: dict[str, dict]) -> None:
    """重讀寫出的 COCO，驗證每張 image time_sec == frame_index / fps。"""
    coco = json.loads(coco_path.read_text(encoding="utf-8"))
    for img in coco.get("images", []):
        v = videos.get(img.get("video_id"))
        if v is None:
            raise SpatialExportError(
                f"一致性檢查失敗：image 無法回溯 video_id：{img}")
        if abs(img["time_sec"] - img["frame_index"] / v["fps"]) > 1e-6:
            raise SpatialExportError(
                f"一致性檢查失敗：time_sec != frame_index/fps：{img}")


# ------------------------------------------------------------- 主流程 ------

@dataclass
class SpatialExportOutcome:
    run_id: str
    run_dir: Path
    n_videos: int = 0
    n_annotations: int = 0      # spatial 標註列數
    n_images: int = 0           # COCO images（唯一 keypoint frames）
    n_coco_annotations: int = 0  # 標註 × keypoint
    n_new_frames: int = 0       # 本次補抽的 JPEG 張數


def rows_from_registry(conn, project_id: str | None = None) -> list[dict]:
    """annotations(layer='spatial') → 全保真列（attrs 解開）。

    project_id 給定時以 annotations.project_id 直查（v1.2 標註按專案隔離）。
    每列帶齊追溯 ID：ann_id / video_id / project_id / annotator_id（+ 顯示名）。
    """
    rows = []
    sql = "SELECT * FROM annotations WHERE layer = 'spatial'"
    params: tuple = ()
    if project_id is not None:
        sql += " AND project_id = ?"
        params = (project_id,)
    cur = conn.execute(sql + " ORDER BY video_id, start_frame, end_frame", params)
    for r in cur:
        attrs = json.loads(r["attrs_json"] or "{}")
        rows.append({
            "ann_id": r["ann_id"], "video_id": r["video_id"],
            "project_id": r["project_id"], "annotator_id": r["annotator_id"],
            "label": r["label"],
            "start_frame": r["start_frame"], "end_frame": r["end_frame"],
            "shape": attrs.get("shape"),
            "interpolate": bool(attrs.get("interpolate", False)),
            "keypoints": attrs.get("keypoints", []),
            "annotator": r["annotator"],
            "created_at": r["created_at"], "updated_at": r["updated_at"],
        })
    return rows


def spatial_label_order(conn, project_id: str | None,
                        labels_in_use: set[str]) -> list[str]:
    """COCO 類別順序：registry labels（spatial 層，rowid 序）優先，未知者字典序附後。

    專案 taxonomy（labels 表）是事實來源；yaml 只是 default 專案的種子。
    """
    if project_id is not None:
        cur = conn.execute(
            "SELECT name FROM labels WHERE layer = 'spatial' AND project_id = ? "
            "ORDER BY rowid", (project_id,))
    else:
        cur = conn.execute(
            "SELECT DISTINCT name FROM labels WHERE layer = 'spatial' ORDER BY rowid")
    known = [r["name"] for r in cur]
    ordered = list(dict.fromkeys(known))
    ordered += sorted(labels_in_use - set(ordered))
    return ordered


def run_export(db_path=None, project_id: str | None = None) -> SpatialExportOutcome:
    """完整匯出：spatial.json → 補抽幀 → COCO → 不變式檢查 → registry 記錄。"""
    _require("ffmpeg")
    now = datetime.now(timezone.utc).isoformat()
    registry.init_db(db_path)
    with registry.connect(db_path) as conn:
        videos = {r["video_id"]: dict(r)
                  for r in registry.list_videos(conn, project_id=project_id)}
        rows = rows_from_registry(conn, project_id=project_id)
        if not rows:
            raise SpatialExportError(
                "registry 內沒有 spatial 標註，請先在 SurgAnn Annotator 標註"
                "（layer='spatial'）。")
        for r in rows:
            if r["video_id"] not in videos:
                raise SpatialExportError(
                    f"registry 查不到 video_id={r['video_id']}"
                    f"（ann_id={r['ann_id']}）。")
            if r["shape"] not in ("bbox", "point", "ellipse", "polygon",
                                  "polyline") or not r["keypoints"]:
                raise SpatialExportError(
                    f"attrs_json 結構異常（ann_id={r['ann_id']}）："
                    f"shape={r['shape']}, keypoints={len(r['keypoints'])}")

        run_id = next_run_id(project_id)
        run_dir = exports_dir(project_id) / run_id
        coco_dir = run_dir / "coco"
        coco_dir.mkdir(parents=True, exist_ok=True)

        # 1) 全保真 dump
        spatial_path = run_dir / "spatial.json"
        spatial_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

        # 2) keypoint frames 補抽（purpose='manual' 入 frames 表）
        needed = {(r["video_id"], int(kp["frame"]))
                  for r in rows for kp in r["keypoints"]}
        n_new = ensure_keyframe_images(conn, videos, needed)

        # 3) COCO + 不變式自我檢查（類別以 registry labels 表為準）
        labels = spatial_label_order(conn, project_id,
                                     {r["label"] for r in rows})
        coco = build_coco(rows, videos, labels, project_id=project_id)
        coco_path = coco_dir / "spatial.json"
        coco_path.write_text(json.dumps(coco, ensure_ascii=False),
                             encoding="utf-8")
        check_coco_invariants(coco_path, videos)

        # 4) registry 記錄：每 (video, annotator) 一列快照 + export run
        pairs = sorted({(r["video_id"], r["annotator"]) for r in rows})
        for vid, annotator in pairs:
            registry.add_snapshot(
                conn, snapshot_id=run_id, task_uid=f"annot-spatial-{vid}",
                annotator=annotator, raw_json_path=str(spatial_path),
                exported_at=now,
            )
        registry.add_export_run(
            conn, run_id=run_id, run_dir=str(run_dir),
            config_json=json.dumps({
                "source": "surgann-annotator-spatial",
                "project_id": project_id,
                "n_annotations": len(rows),
                "n_images": len(coco["images"]),
                "videos": sorted({r["video_id"] for r in rows}),
            }),
            created_at=now,
        )

    return SpatialExportOutcome(
        run_id=run_id, run_dir=run_dir,
        n_videos=len({r["video_id"] for r in rows}),
        n_annotations=len(rows),
        n_images=len(coco["images"]),
        n_coco_annotations=len(coco["annotations"]),
        n_new_frames=n_new,
    )
