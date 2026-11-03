"""統一資料集匯出入口（M3）：spatial 標註 + 對應 images 的三種下載格式。

供 SurgAnn Annotator server.py 接線的**跨區契約**：
  - `run_dataset_export` — 全量匯出（spatial + images，三格式）
  - `export_filtered_dataset` — 篩選式下載（scope / temporal_gate /
    include_labels / include_images，回傳 zip 路徑）
每種格式都把「對應 images」與標註一起輸出到 run_dir（可選打包成 zip），
下載即自帶影像、不需回頭找 data/frames/。

複用既有實作（R4，不重寫抽幀 / COCO 組裝 / YOLO 轉換 / MOSaiC 互轉）：
  - yolo   → convert.to_yolo（images/ + labels/ + labels_seg/ + data.yaml 已自帶影像）
  - coco   → export_spatial.{rows_from_registry, spatial_label_order, build_coco,
             check_coco_invariants}；影像以 convert._ensure_image 落地 run_dir/coco/images/
  - mosaic → mosaic_io.export_mosaic（每支影片一份，含 temporal + spatial）；
             該影片 spatial keyframe 影像以 convert._ensure_image 落地 run_dir/{vid}/images/

不變式：coco 影像 time_sec == frame_index / fps（check_coco_invariants）；
所有格式的影像實體必存在（寫檔後重讀自檢），不通過即 fail、不產出殘缺資料集。
"""
from __future__ import annotations

import csv
import json
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import convert, export_spatial, mosaic_io, registry
from .export_common import exports_dir, next_run_id

FORMATS = ("mosaic", "yolo", "coco")
SCOPES = ("all", "spatial", "temporal")


class DatasetExportError(RuntimeError):
    """統一資料集匯出過程中的可預期錯誤。"""


@dataclass
class DatasetExportOutcome:
    """匯出結果。下載路徑取 `output_path`（有 zip 取 zip，否則取目錄）。"""
    fmt: str
    run_dir: Path
    run_id: str = ""
    zip_path: Optional[Path] = None
    n_videos: int = 0
    n_images: int = 0
    n_annotations: int = 0
    manifest: dict = field(default_factory=dict)

    @property
    def output_path(self) -> Path:
        return self.zip_path if self.zip_path is not None else self.run_dir


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------- 各格式 ------

def _export_yolo(run_dir: Path, project_id: Optional[str],
                 videos: Optional[list[str]], db_path, *,
                 ann_ids: Optional[set[str]] = None,
                 include_images: bool = True) -> DatasetExportOutcome:
    """YOLO detection + seg（convert.to_yolo 已自帶 images/）。"""
    m = convert.to_yolo(run_dir, videos=videos, project_id=project_id, db_path=db_path,
                        ann_ids=ann_ids, include_images=include_images)
    with open(run_dir / "images_meta.csv", newline="", encoding="utf-8") as f:
        n_videos = len({row["video_id"] for row in csv.DictReader(f)})
    return DatasetExportOutcome(
        fmt="yolo", run_dir=run_dir, n_videos=n_videos,
        n_images=m["n_images"], n_annotations=m["n_det_boxes"] + m["n_seg_polygons"],
        manifest=m)


def _export_coco(run_dir: Path, project_id: Optional[str],
                 videos: Optional[list[str]], db_path, *,
                 ann_ids: Optional[set[str]] = None,
                 include_images: bool = True) -> DatasetExportOutcome:
    """COCO（重用 export_spatial 組裝）+ 影像落地 run_dir/coco/images/{vid}/。"""
    registry.init_db(db_path)
    coco_dir = run_dir / "coco"
    coco_dir.mkdir(parents=True, exist_ok=True)
    with registry.connect(db_path) as conn:
        vids = convert._load_videos(conn, videos, project_id=project_id)
        rows = [r for r in export_spatial.rows_from_registry(conn, project_id=project_id)
                if r["video_id"] in vids
                and (ann_ids is None or r["ann_id"] in ann_ids)]
        if not rows:
            raise DatasetExportError(
                "此範圍內沒有 spatial 標註，無法輸出 COCO 資料集。")
        labels = export_spatial.spatial_label_order(
            conn, project_id, {r["label"] for r in rows})

    coco = export_spatial.build_coco(rows, vids, labels, project_id=project_id)

    # 影像落地 run_dir/coco/images/{vid}/…，file_name 改為相對 coco/ 的路徑
    # （include_images=False 時只改 file_name 供對位，不抽/不複製影像）
    n_new = 0
    for img in coco["images"]:
        vid, frame = img["video_id"], int(img["frame_index"])
        name = convert._image_name(vid, frame)
        if include_images:
            n_new += convert._ensure_image(
                vids[vid], frame, coco_dir / "images" / vid / name)
        img["file_name"] = f"images/{vid}/{name}"

    coco_path = coco_dir / "annotations.json"
    coco_path.write_text(json.dumps(coco, ensure_ascii=False), encoding="utf-8")
    export_spatial.check_coco_invariants(coco_path, vids)   # time == frame/fps
    if include_images:
        for img in coco["images"]:                          # 影像實體必存在
            if not (coco_dir / img["file_name"]).exists():
                raise DatasetExportError(f"COCO 影像不存在：{img['file_name']}")

    return DatasetExportOutcome(
        fmt="coco", run_dir=run_dir,
        n_videos=len({r["video_id"] for r in rows}),
        n_images=len(coco["images"]) if include_images else 0,
        n_annotations=len(coco["annotations"]),
        manifest={"coco_json": str(coco_path),
                  "images_dir": str(coco_dir / "images") if include_images else None,
                  "classes": labels, "n_new_frames": n_new})


def _export_mosaic(run_dir: Path, project_id: Optional[str],
                   videos: Optional[list[str]], db_path, *,
                   ann_ids: Optional[set[str]] = None,
                   include_images: bool = True) -> DatasetExportOutcome:
    """每支影片一份 MOSaiC JSON（temporal + spatial）+ spatial keyframe 影像。"""
    registry.init_db(db_path)
    with registry.connect(db_path) as conn:
        vids = convert._load_videos(conn, videos, project_id=project_id)
        # 範圍內「有標註」的影片才輸出（temporal 或 spatial 皆算；
        # ann_ids 給定時只算被選中的標註；project 走 annotations.project_id 直查）
        ann_sql = "SELECT ann_id, video_id FROM annotations"
        ann_params: tuple = ()
        if project_id is not None:
            ann_sql += " WHERE project_id = ?"
            ann_params = (project_id,)
        ann_vids = sorted(
            {r["video_id"] for r in conn.execute(ann_sql, ann_params)
             if ann_ids is None or r["ann_id"] in ann_ids} & set(vids))
        # 每支影片的 spatial keyframe（供補抽影像）
        spatial_rows = [r for r in export_spatial.rows_from_registry(
            conn, project_id=project_id)
            if r["video_id"] in vids
            and (ann_ids is None or r["ann_id"] in ann_ids)]
    if not ann_vids:
        raise DatasetExportError("此範圍內沒有任何標註，無法輸出 MOSaiC 資料集。")

    frames_by_vid: dict[str, set[int]] = {v: set() for v in ann_vids}
    for r in spatial_rows:
        for kp in r["keypoints"]:
            frames_by_vid.setdefault(r["video_id"], set()).add(int(kp["frame"]))

    n_images = n_annotations = 0
    files: list[str] = []
    for vid in ann_vids:
        vdir = run_dir / vid
        vdir.mkdir(parents=True, exist_ok=True)
        doc = mosaic_io.export_mosaic(vid, db_path=db_path, ann_ids=ann_ids,
                                      project_id=project_id)  # temporal + spatial
        json_path = vdir / f"{vid}_mosaic.json"
        json_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        json.loads(json_path.read_text(encoding="utf-8"))     # parse 自檢
        n_annotations += len(doc["annotations"])
        files.append(str(json_path))
        if not include_images:
            continue
        for frame in sorted(frames_by_vid.get(vid, set())):
            name = convert._image_name(vid, frame)
            out_path = vdir / "images" / name
            convert._ensure_image(vids[vid], frame, out_path)
            if not out_path.exists():                         # 影像實體必存在
                raise DatasetExportError(f"MOSaiC 影像不存在：{out_path}")
            n_images += 1

    return DatasetExportOutcome(
        fmt="mosaic", run_dir=run_dir, n_videos=len(ann_vids),
        n_images=n_images, n_annotations=n_annotations,
        manifest={"videos": ann_vids, "files": files})


# ------------------------------------------------------------- 主入口 ------

def _zip_dir(run_dir: Path) -> Path:
    """把 run_dir 打包成同名 zip（供下載）。回傳 zip 路徑。"""
    base = str(run_dir)
    shutil.make_archive(base, "zip", root_dir=str(run_dir.parent),
                        base_dir=run_dir.name)
    return Path(base + ".zip")


def run_dataset_export(fmt: str, project_id: Optional[str] = None,
                       videos: Optional[list[str]] = None, db_path=None,
                       make_zip: bool = False) -> DatasetExportOutcome:
    """統一資料集匯出：spatial 標註 + 對應 images，三格式擇一。

    參數：
      fmt         "mosaic" | "yolo" | "coco"
      project_id  限定專案（專案 id/編碼；None = 全 registry）
      videos      限定影片清單（None = 範圍內全部）
      db_path     registry.db 路徑（None = 預設）
      make_zip    True 時把 run_dir 另打包成 {run_dir}.zip

    回傳 DatasetExportOutcome（run_id / run_dir / zip_path / 統計 / manifest）；
    下載路徑取 `outcome.output_path`（有 zip 取 zip，否則取目錄）。
    """
    if fmt not in FORMATS:
        raise DatasetExportError(f"未知格式 '{fmt}'（可用：{', '.join(FORMATS)}）")
    registry.init_db(db_path)
    run_id = next_run_id(project_id)
    run_dir = exports_dir(project_id) / run_id   # data/exports/{pid}/{run_id}/
    run_dir.mkdir(parents=True, exist_ok=True)

    if fmt == "yolo":
        outcome = _export_yolo(run_dir, project_id, videos, db_path)
    elif fmt == "coco":
        outcome = _export_coco(run_dir, project_id, videos, db_path)
    else:
        outcome = _export_mosaic(run_dir, project_id, videos, db_path)
    outcome.run_id = run_id

    with registry.connect(db_path) as conn:
        registry.add_export_run(
            conn, run_id=run_id, run_dir=str(run_dir),
            config_json=json.dumps({
                "kind": "dataset", "format": fmt, "project_id": project_id,
                "videos": videos, "n_videos": outcome.n_videos,
                "n_images": outcome.n_images, "n_annotations": outcome.n_annotations,
            }),
            created_at=_now_iso())

    if make_zip:
        outcome.zip_path = _zip_dir(run_dir)
    return outcome


# ------------------------------------------------- 篩選式下載（契約）------

def _resolve_label_items(conn, project_id: str, items) -> set[tuple[str, str]]:
    """label 名稱或 label_id 清單 → {(layer, name)}；任一查無即 ValueError。

    名稱同名跨層（少見）時全數納入；label_id 精準對到單一 (layer, name)。
    """
    known = registry.list_labels(conn, project_id)
    by_id = {r["label_id"]: r for r in known}
    by_name: dict[str, list] = {}
    for r in known:
        by_name.setdefault(r["name"], []).append(r)

    out: set[tuple[str, str]] = set()
    for item in items:
        if item in by_id:
            r = by_id[item]
            out.add((r["layer"], r["name"]))
        elif item in by_name:
            out.update((r["layer"], r["name"]) for r in by_name[item])
        else:
            raise ValueError(
                f"專案「{project_id}」找不到 label「{item}」（名稱與 label_id 皆查無）。")
    return out


def _select_annotations(conn, project_id: str, *, scope: str,
                        temporal_gate, include_labels) -> list[dict]:
    """篩選前置步驟：套 scope / include_labels / temporal_gate，回傳選中的標註列。

    temporal_gate 判定（僅作用於 spatial 標註）：spatial 標註的
    [start_frame, end_frame] 與該 temporal label 任一標註的 [start, end] 有交集
    即入選（單 keyframe 標註 start==end，等同「時間點落在範圍內」；
    instant 型 temporal 標註 start==end，同 frame 即命中）。
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT ann_id, video_id, layer, label, start_frame, end_frame "
        "FROM annotations WHERE project_id = ? "        # v1.2 按專案直查
        "ORDER BY video_id, layer, start_frame", (project_id,))]

    gate_iv: Optional[dict[str, list[tuple[int, int]]]] = None
    if temporal_gate is not None:
        gates = ([temporal_gate] if isinstance(temporal_gate, str)
                 else list(temporal_gate))
        gate_pairs = {(layer, name) for layer, name
                      in _resolve_label_items(conn, project_id, gates)
                      if layer != "spatial"}
        if not gate_pairs:
            raise ValueError(
                f"temporal_gate「{'、'.join(map(str, gates))}」不是 temporal label"
                f"（phase/action/event/triplet），無法作為時間範圍篩選條件。")
        gate_iv = {}
        for r in rows:
            if (r["layer"], r["label"]) in gate_pairs:
                gate_iv.setdefault(r["video_id"], []).append(
                    (int(r["start_frame"]), int(r["end_frame"])))

    keep_pairs = (_resolve_label_items(conn, project_id, include_labels)
                  if include_labels else None)

    selected: list[dict] = []
    for r in rows:
        if scope == "spatial" and r["layer"] != "spatial":
            continue
        if scope == "temporal" and r["layer"] == "spatial":
            continue
        if keep_pairs is not None and (r["layer"], r["label"]) not in keep_pairs:
            continue
        if gate_iv is not None and r["layer"] == "spatial":
            s, e = int(r["start_frame"]), int(r["end_frame"])
            if not any(max(s, gs) <= min(e, ge)
                       for gs, ge in gate_iv.get(r["video_id"], [])):
                continue
        selected.append(r)
    return selected


def export_filtered_dataset(db_path, project_id, out_dir, *,
                            fmt,
                            include_images: bool,
                            scope,
                            temporal_gate=None,
                            include_labels=None) -> Path:
    """篩選式資料集下載（跨區契約，供 annotator server.py 接線）。

    參數：
      db_path         registry.db 路徑（None = 預設）
      project_id      專案編碼（必填，查無即 ValueError）
      out_dir         匯出根目錄（如 config.EXPORTS_DIR）；v1.2 起 zip 實際
                      落 out_dir/{project_id}/（按專案分層，與 run 目錄同層）
      fmt             'mosaic' | 'coco' | 'yolo'
      include_images  False = 只匯出 label（不抽/不附 images）
      scope           'all' | 'spatial' | 'temporal'（依 layer 篩）
      temporal_gate   temporal label 名稱或 label_id，可為單一字串或清單
                      （多選＝聯集）；只在 scope='spatial' 有效：只匯出落在
                      任一 gate label 任一標註 [start, end] 內的 spatial 標註
      include_labels  label 名稱或 label_id 清單；None = 全部

    篩選為前置步驟：先選出符合條件的標註集合（ann_id），再注入既有
    MOSaiC / COCO / YOLO 匯出路徑（R4 不重複實作）。參數不合法一律
    raise ValueError（中文訊息，server 端轉 4xx）。

    回傳打包好的 zip 檔完整路徑
    （out_dir/{project_id}/filtered_{fmt}_{時間戳}_{短碼}.zip；
    zip 內為同名頂層目錄，結構同 run_dataset_export 各格式）。
    """
    if fmt not in FORMATS:
        raise ValueError(f"未知格式「{fmt}」（可用：{', '.join(FORMATS)}）。")
    if scope not in SCOPES:
        raise ValueError(f"未知 scope「{scope}」（可用：{', '.join(SCOPES)}）。")
    if temporal_gate is not None and not isinstance(temporal_gate, str):
        temporal_gate = list(temporal_gate) or None   # 空清單視同未指定
    if temporal_gate is not None and scope != "spatial":
        raise ValueError("temporal_gate 只能搭配 scope='spatial'"
                         "（用 temporal 標註的時間範圍篩 spatial 標註）。")
    if fmt in ("coco", "yolo") and scope == "temporal":
        raise ValueError(f"{fmt} 格式只支援 spatial 標註，"
                         f"scope='temporal' 沒有可匯出的內容。")

    registry.init_db(db_path)
    with registry.connect(db_path) as conn:
        if registry.get_project(conn, project_id or "") is None:
            raise ValueError(f"找不到專案「{project_id}」。")
        selected = _select_annotations(
            conn, project_id, scope=scope,
            temporal_gate=temporal_gate, include_labels=include_labels)
    if not selected:
        raise ValueError("依此篩選條件沒有任何標註可匯出，請放寬條件。")
    if fmt in ("coco", "yolo") and not any(
            r["layer"] == "spatial" for r in selected):
        raise ValueError(f"篩選結果沒有 spatial 標註，無法輸出 {fmt} 資料集。")
    ann_ids = {r["ann_id"] for r in selected}

    out_dir = Path(out_dir) / project_id      # v1.2 按專案分層
    out_dir.mkdir(parents=True, exist_ok=True)
    run_name = (f"filtered_{fmt}_"
                f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:6]}")
    run_dir = out_dir / run_name
    run_dir.mkdir(parents=True, exist_ok=False)
    try:
        if fmt == "yolo":
            outcome = _export_yolo(run_dir, project_id, None, db_path,
                                   ann_ids=ann_ids, include_images=include_images)
        elif fmt == "coco":
            outcome = _export_coco(run_dir, project_id, None, db_path,
                                   ann_ids=ann_ids, include_images=include_images)
        else:
            outcome = _export_mosaic(run_dir, project_id, None, db_path,
                                     ann_ids=ann_ids, include_images=include_images)
        zip_path = _zip_dir(run_dir)
    except Exception:
        shutil.rmtree(run_dir, ignore_errors=True)   # 不留殘缺產物
        raise
    shutil.rmtree(run_dir, ignore_errors=True)       # 只留 zip，不佔雙倍空間

    with registry.connect(db_path) as conn:
        registry.add_export_run(
            conn, run_id=run_name, run_dir=str(zip_path),
            config_json=json.dumps({
                "kind": "dataset-filtered", "format": fmt,
                "project_id": project_id, "scope": scope,
                "include_images": include_images,
                "temporal_gate": temporal_gate,
                "include_labels": list(include_labels) if include_labels else None,
                "n_videos": outcome.n_videos, "n_images": outcome.n_images,
                "n_annotations": outcome.n_annotations,
                "n_selected": len(ann_ids),
            }, ensure_ascii=False),
            created_at=_now_iso())
    return zip_path
