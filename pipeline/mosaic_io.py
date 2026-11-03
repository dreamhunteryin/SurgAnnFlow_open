"""MOSaiC 標註 JSON 互轉（Phase B）。

MOSaiC schema（MOSaiC 標註平台匯出的 JSON）：
頂層 {annotations, labels, edges}；annotation 有
type(bounding_box/polygon/polyline/point/temporal), startFrame, numberFrames,
labelId, label:{name}, shapes:[{frame, shape}]（temporal 的 shapes=null）。
座標 x,y 為 0–100 正規化百分比；我方 registry 存像素。

對映規則（單一事實來源 = registry annotations 表）：
- 座標：MOSaiC 百分比 ↔ 像素（× video width/height ÷ 100）。
- temporal：start_frame=startFrame、end_frame=startFrame+numberFrames-1；
  layer 由 labels 表反查（label 名已存在於 phase/action/event/triplet 層則沿用），
  否則落到 default_temporal_layer。
- spatial：bounding_box↔bbox（[x1,y1,x2,y2]）、polygon↔polygon、
  polyline↔polyline、point↔point；每個 shapes[].frame ↔ 一個 keypoint。
  ellipse 為我方獨有 → 匯出時降階為外接 bounding_box（有損，見 spec）。
- round-trip：import 時把 MOSaiC 的 id/labelId 存進 attrs_json["mosaic"]，
  export 時優先還原；triplet 的 instrument/verb/target 進 description（JSON）。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import registry
from .export_spatial import bounding_box

TEMPORAL_LAYERS = ("phase", "action", "event", "triplet")

# MOSaiC type ↔ 內部 attrs.shape
MOSAIC_TO_SHAPE = {"bounding_box": "bbox", "polygon": "polygon",
                   "polyline": "polyline", "point": "point"}
SHAPE_TO_MOSAIC = {v: k for k, v in MOSAIC_TO_SHAPE.items()}
SHAPE_TO_MOSAIC["ellipse"] = "bounding_box"   # 降階：橢圓 → 外接框（有損）


class MosaicError(RuntimeError):
    """MOSaiC 互轉過程中的可預期錯誤。"""


# ------------------------------------------------------------ 座標換算 -----

def _pct_to_px(x: float, y: float, w: int, h: int) -> tuple[float, float]:
    return x * w / 100.0, y * h / 100.0


def _px_to_pct(x: float, y: float, w: int, h: int) -> tuple[float, float]:
    return x * 100.0 / w, y * 100.0 / h


def _shape_to_points(mtype: str, shape, w: int, h: int) -> list[float]:
    """MOSaiC shapes[].shape → 內部 keypoint points（像素，flat list）。"""
    if mtype == "bounding_box":
        px, py = _pct_to_px(shape["position"]["x"], shape["position"]["y"], w, h)
        bw, bh = _pct_to_px(shape["width"], shape["height"], w, h)
        return [px, py, px + bw, py + bh]                 # [x1,y1,x2,y2]
    if mtype == "point":
        return list(_pct_to_px(shape["x"], shape["y"], w, h))
    if mtype in ("polygon", "polyline"):
        pts: list[float] = []
        for p in shape:
            pts.extend(_pct_to_px(p["x"], p["y"], w, h))
        return pts
    raise MosaicError(f"未知 MOSaiC type：{mtype}")


def _points_to_shape(shape_name: str, points: list[float], w: int, h: int):
    """內部 keypoint points（像素）→ MOSaiC shapes[].shape（百分比）。"""
    if shape_name in ("bbox", "ellipse"):
        # ellipse 先取外接框再輸出（bounding_box 語義）
        x, y, bw, bh = bounding_box(shape_name, points)
        px, py = _px_to_pct(x, y, w, h)
        pw, ph = _px_to_pct(bw, bh, w, h)
        return {"position": {"x": px, "y": py}, "width": pw, "height": ph}
    if shape_name == "point":
        px, py = _px_to_pct(points[0], points[1], w, h)
        return {"x": px, "y": py}
    if shape_name in ("polygon", "polyline"):
        return [{"x": xp, "y": yp}
                for xp, yp in (_px_to_pct(points[i], points[i + 1], w, h)
                               for i in range(0, len(points), 2))]
    raise MosaicError(f"未知內部 shape：{shape_name}")


# ------------------------------------------------------------- import ------

@dataclass
class MosaicImportOutcome:
    video_id: str
    n_total: int = 0
    n_temporal: int = 0
    n_spatial: int = 0
    by_type: dict = field(default_factory=dict)
    n_labels_seeded: int = 0
    ann_ids: list[str] = field(default_factory=list)


def _infer_temporal_layer(conn, name: str, default: str,
                          project_id: Optional[str] = None) -> str:
    """label 名已存在於 registry labels 的 temporal 層 → 沿用該層。

    優先看目標專案的 taxonomy，查無再退回全庫。
    """
    if project_id is not None:
        row = conn.execute(
            "SELECT layer FROM labels WHERE name = ? AND project_id = ? "
            "AND layer IN (?, ?, ?, ?) ORDER BY rowid LIMIT 1",
            (name, project_id, *TEMPORAL_LAYERS)).fetchone()
        if row:
            return row["layer"]
    row = conn.execute(
        "SELECT layer FROM labels WHERE name = ? AND layer IN (?, ?, ?, ?) "
        "ORDER BY rowid LIMIT 1", (name, *TEMPORAL_LAYERS)).fetchone()
    return row["layer"] if row else default


def _seed_label(conn, project_id: str, layer: str, name: str,
                color: Optional[str]) -> int:
    """label 不在目標專案 taxonomy 內則補種（冪等）。回傳新增數 0/1。"""
    row = conn.execute(
        "SELECT 1 FROM labels WHERE project_id = ? AND layer = ? AND name = ?",
        (project_id, layer, name)).fetchone()
    if row:
        return 0
    registry.add_label(conn, project_id, layer, name, color=color)
    return 1


def _triplet_attrs_from_description(desc) -> dict:
    """description 若是 triplet JSON（import↔export round-trip）則還原。"""
    if not desc:
        return {}
    try:
        d = json.loads(desc)
    except (TypeError, ValueError):
        return {}
    if isinstance(d, dict) and {"instrument", "verb", "target"} & set(d):
        return {k: d.get(k, "") for k in ("instrument", "verb", "target")}
    return {}


def import_mosaic(json_path, video_id: str, db_path=None,
                  annotator: str = "mosaic-import",
                  default_temporal_layer: str = "event",
                  seed_labels: bool = True,
                  project_id: Optional[str] = None) -> MosaicImportOutcome:
    """讀 MOSaiC JSON → 轉為內部 annotations 寫入 registry（指定 db_path）。

    video_id 必須已存在於 videos 表（需要 width/height 做座標換算）。

    追溯 ID：來源 JSON 的 extra.project_id / extra.annotator（我方
    export_mosaic round-trip 產物）優先讀回；缺欄時 project 用 CLI 指定的
    project_id（再缺退 default），annotator 用參數顯示名並經
    get_or_create_annotator 建冊。寫入一律走 registry.add_annotation
    （project_id / annotator_id 必填契約），並把影片加進該專案播放清單。
    """
    json_path = Path(json_path)
    if default_temporal_layer not in TEMPORAL_LAYERS:
        raise MosaicError(f"default_temporal_layer 必須是 {TEMPORAL_LAYERS} 之一")
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    m_anns = doc.get("annotations") or []
    label_colors = {l.get("name"): l.get("color") for l in (doc.get("labels") or [])}

    registry.init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    fallback_project = project_id or registry.DEFAULT_PROJECT_ID
    out = MosaicImportOutcome(video_id=video_id)
    with registry.connect(db_path) as conn:
        v = registry.get_video(video_id, conn)
        if v is None:
            raise MosaicError(f"registry 查不到 video_id={video_id}，"
                              f"請先 ingest 或建立影片記錄。")
        if registry.get_project(conn, fallback_project) is None:
            raise MosaicError(f"registry 查不到專案 {fallback_project}，"
                              f"請先建立專案或改用 -P 指定。")
        w, h = int(v["width"]), int(v["height"])
        if not w or not h:
            raise MosaicError(f"video {video_id} 缺 width/height，無法換算座標。")

        annotator_ids: dict[str, str] = {}   # 顯示名 → annotator_id（建冊快取）

        for a in m_anns:
            extra = a.get("extra") or {}     # 我方 export round-trip 帶的追溯 ID
            ann_project = extra.get("project_id") or fallback_project
            ann_annotator = extra.get("annotator") or annotator
            if ann_annotator not in annotator_ids:
                annotator_ids[ann_annotator] = registry.get_or_create_annotator(
                    conn, ann_annotator)
            mtype = a.get("type")
            label_name = (a.get("label") or {}).get("name") or f"label_{a.get('labelId')}"
            start = int(a["startFrame"])
            end = start + int(a.get("numberFrames") or 1) - 1
            mosaic_meta = {"id": a.get("id"), "labelId": a.get("labelId")}

            if mtype == "temporal":
                layer = _infer_temporal_layer(conn, label_name,
                                              default_temporal_layer,
                                              project_id=ann_project)
                attrs: dict = {"mosaic": mosaic_meta}
                if layer == "triplet":
                    attrs.update(_triplet_attrs_from_description(a.get("description")))
            elif mtype in MOSAIC_TO_SHAPE:
                layer = "spatial"
                shape_name = MOSAIC_TO_SHAPE[mtype]
                keypoints = []
                for s in (a.get("shapes") or []):
                    keypoints.append({
                        "frame": int(s["frame"]),
                        "points": _shape_to_points(mtype, s["shape"], w, h),
                    })
                if not keypoints:
                    raise MosaicError(
                        f"MOSaiC annotation id={a.get('id')}（{mtype}）沒有 shapes。")
                keypoints.sort(key=lambda k: k["frame"])
                # 以 keypoints 實際範圍為準（與 startFrame/numberFrames 一致）
                start = min(start, keypoints[0]["frame"])
                end = max(end, keypoints[-1]["frame"])
                attrs = {"shape": shape_name,
                         "interpolate": len(keypoints) > 1,
                         "keypoints": keypoints,
                         "mosaic": mosaic_meta}
            else:
                raise MosaicError(f"未知 MOSaiC type：{mtype}（id={a.get('id')}）")

            if seed_labels:
                out.n_labels_seeded += _seed_label(
                    conn, ann_project, layer, label_name,
                    label_colors.get(label_name))

            ann_id = registry.add_annotation(
                conn, video_id=video_id, project_id=ann_project,
                annotator_id=annotator_ids[ann_annotator],
                layer=layer, label=label_name,
                start_frame=start, end_frame=end,
                attrs_json=json.dumps(attrs, ensure_ascii=False),
                annotator=ann_annotator,
                created_at=a.get("createdAt") or now,
                updated_at=a.get("updatedAt") or now,
            )
            registry.add_video_to_project(conn, ann_project, video_id)
            out.ann_ids.append(ann_id)
            out.n_total += 1
            out.by_type[mtype] = out.by_type.get(mtype, 0) + 1
            if layer == "spatial":
                out.n_spatial += 1
            else:
                out.n_temporal += 1
    return out


# ------------------------------------------------------------- export ------

def export_mosaic(video_id: str, db_path=None,
                  ann_ids: Optional[set] = None,
                  project_id: Optional[str] = None) -> dict:
    """registry annotations（單一影片）→ MOSaiC 結構 dict。

    像素 → 0–100 百分比；end_frame → numberFrames = end - start + 1。
    import 時保留的 mosaic.id/labelId 會優先還原（round-trip 盡量無損）。
    ann_ids 給定時只輸出該子集（篩選式下載，dataset_export 注入）；
    project_id 給定時只輸出該專案的標註（annotations.project_id 直查）。
    每筆 annotation 的 extra 帶齊追溯 ID（additive，不破壞 MOSaiC 相容欄位）。
    """
    registry.init_db(db_path)
    with registry.connect(db_path) as conn:
        v = registry.get_video(video_id, conn)
        if v is None:
            raise MosaicError(f"registry 查不到 video_id={video_id}。")
        w, h = int(v["width"]), int(v["height"])
        rows = [r for r in registry.list_annotations(conn, video_id,
                                                     project_id=project_id)
                if ann_ids is None or r["ann_id"] in ann_ids]
        label_colors = {r["name"]: r["color"] for r in conn.execute(
            "SELECT name, color FROM labels")}

    annotations: list[dict] = []
    label_meta: dict[str, dict] = {}   # name -> {"types": set, "labelId": int|None}
    next_ann_id = 1
    for r in rows:
        attrs = json.loads(r["attrs_json"] or "{}")
        mosaic_meta = attrs.get("mosaic") or {}
        start, end = int(r["start_frame"]), int(r["end_frame"])
        name = r["label"]
        description = None

        if r["layer"] == "spatial":
            shape_name = attrs.get("shape")
            if shape_name not in SHAPE_TO_MOSAIC:
                raise MosaicError(
                    f"無法輸出 shape={shape_name}（ann_id={r['ann_id']}）。")
            mtype = SHAPE_TO_MOSAIC[shape_name]
            shapes = [{"frame": int(kp["frame"]),
                       "shape": _points_to_shape(shape_name,
                                                 [float(p) for p in kp["points"]],
                                                 w, h)}
                      for kp in attrs.get("keypoints", [])]
            if not shapes:
                raise MosaicError(f"spatial 標註沒有 keypoints（ann_id={r['ann_id']}）。")
        else:
            mtype = "temporal"
            shapes = None
            if r["layer"] == "triplet":
                trip = {k: attrs.get(k, "") for k in ("instrument", "verb", "target")}
                if any(trip.values()):
                    description = json.dumps(trip, ensure_ascii=False)

        meta = label_meta.setdefault(name, {"types": set(), "labelId": None})
        meta["types"].add(mtype)
        if mosaic_meta.get("labelId") is not None:
            meta["labelId"] = mosaic_meta["labelId"]

        annotations.append({
            "id": mosaic_meta.get("id", next_ann_id),
            "createdAt": r["created_at"],
            "updatedAt": r["updated_at"],
            "type": mtype,
            "description": description,
            "numberFrames": end - start + 1,
            "startFrame": start,
            "labelId": None,            # 回填於下方
            "label": {"name": name},
            "shapes": shapes,
            "extra": {"ann_uid": r["ann_id"], "ann_id": r["ann_id"],
                      "layer": r["layer"],
                      "annotator": r["annotator"],
                      "annotator_id": r["annotator_id"],
                      "project_id": r["project_id"],
                      "video_id": video_id},
        })
        next_ann_id += 1

    # labels：沿用 import 保留的 labelId，否則指派穩定序號（避免衝突）
    used = {m["labelId"] for m in label_meta.values() if m["labelId"] is not None}
    seq = 1
    labels = []
    for name in sorted(label_meta):
        meta = label_meta[name]
        if meta["labelId"] is None:
            while seq in used:
                seq += 1
            meta["labelId"] = seq
            used.add(seq)
        labels.append({
            "id": meta["labelId"],
            "name": name,
            "color": label_colors.get(name),
            "types": sorted(meta["types"]),
            "form": None,
        })
    label_id_of = {name: m["labelId"] for name, m in label_meta.items()}
    for a in annotations:
        a["labelId"] = label_id_of[a["label"]["name"]]

    return {"annotations": annotations, "labels": labels, "edges": []}
