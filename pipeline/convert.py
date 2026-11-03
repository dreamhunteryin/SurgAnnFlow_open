"""label 格式轉換引擎（Phase B）：registry annotations（單一事實來源）→
YOLO / frame-label pairs / clip-label pairs。

- 既有 COCO 匯出在 export_spatial，不在此重寫（單一實作）；
  本模組沿用其座標與回溯慣例：影像檔名 {video_id}_f{frame:07d}.jpg、
  sidecar 欄位 video_id/frame_index/time_sec/fps。
- 只讀 registry，不寫 frames 表 / export_runs（避免與 UI/CLI 寫入路徑衝突）；
  抽出的 JPG 一律就近放 run_dir 內。
- 不變式：所有輸出寫檔後重讀自檢 time == frame / fps。
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional

import yaml

from . import config, derive, registry
from .export_spatial import bounding_box, extract_frame, rows_from_registry
from .ingest import _require

PAIR_FIELDS = ["video_id", "image_path", "frame_index", "time_sec", "fps",
               "label_type", "label", "annotator",
               "project_id", "annotator_id"]
CLIP_FIELDS = ["video_id", "start_frame", "end_frame", "start_time", "end_time",
               "fps", "layer", "label", "annotator",
               "ann_id", "project_id", "annotator_id", "clip_path"]
# 每張圖可能由多筆標註（多專案/多標註者）貢獻 → 多值以 "|" 串接
IMAGE_META_FIELDS = ["video_id", "frame_index", "time_sec", "fps", "image",
                     "project_id", "annotator_id", "annotator_name"]

TEMPORAL_LAYERS = ("phase", "action", "event", "triplet")


class ConvertError(RuntimeError):
    """格式轉換過程中的可預期錯誤。"""


# ------------------------------------------------------------ 共用工具 -----

def _image_name(video_id: str, frame: int) -> str:
    """與 export_spatial / sample 的抽幀命名一致（單一實作）。"""
    return f"{video_id}_f{frame:07d}.jpg"


def _load_videos(conn, videos: Optional[Iterable[str]] = None,
                 project_id: Optional[str] = None) -> dict[str, dict]:
    all_v = {r["video_id"]: dict(r)
             for r in registry.list_videos(conn, project_id=project_id)}
    if videos is None:
        return all_v
    missing = [v for v in videos if v not in all_v]
    if missing:
        where = f"專案 {project_id}" if project_id else "registry"
        raise ConvertError(f"{where} 查不到 video_id：{missing}")
    return {k: all_v[k] for k in videos}


def _ensure_image(video: dict, frame: int, out_path: Path) -> int:
    """確保 out_path 存在：已存在→跳過；FRAMES_DIR 有快取→複製；否則 ffmpeg 抽。

    回傳本次新抽出的張數（0/1）。原始影片唯讀不受影響。
    """
    if out_path.exists():
        return 0
    cached = config.FRAMES_DIR / video["video_id"] / out_path.name
    if cached.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached, out_path)
        return 0
    std_path = config.to_abs(video["std_path"])   # schema v1.4：registry 存相對 data 根
    if not std_path or not std_path.exists():
        raise ConvertError(f"標準化影片不存在：{std_path}")
    extract_frame(std_path, float(video["fps"]), frame, out_path)
    return 1


def _check_time_invariant(rows: Iterable[dict], frame_key: str, time_key: str,
                          fps_key: str = "fps", where: str = "") -> None:
    """每列 time == frame / fps（重讀後自檢，不通過即 fail）。"""
    for r in rows:
        f, t, fps = int(r[frame_key]), float(r[time_key]), float(r[fps_key])
        if abs(t - f / fps) > 1e-6:
            raise ConvertError(
                f"一致性檢查失敗（{where}）：{time_key} != {frame_key}/fps：{dict(r)}")


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _reread_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ----------------------------------------------------------------- YOLO ----

def _clamp01(v: float) -> float:
    return min(1.0, max(0.0, v))


def _yolo_class_order(conn, labels_in_use: set[str],
                      project_id: Optional[str] = None) -> list[str]:
    """class 順序：registry labels（spatial 層，rowid 序）優先，未知者字典序附後。"""
    if project_id is not None:
        cur = conn.execute(
            "SELECT name FROM labels WHERE layer = 'spatial' AND project_id = ? "
            "ORDER BY rowid", (project_id,))
    else:
        cur = conn.execute(
            "SELECT DISTINCT name FROM labels WHERE layer = 'spatial' ORDER BY rowid")
    known = list(dict.fromkeys(r["name"] for r in cur))
    ordered = [n for n in known if n in labels_in_use]
    ordered += sorted(labels_in_use - set(ordered))
    return ordered


def to_yolo(run_dir, videos: Optional[list[str]] = None, db_path=None,
            project_id: Optional[str] = None, *,
            ann_ids: Optional[set[str]] = None,
            include_images: bool = True) -> dict:
    """spatial 標註 → YOLO detection（正規化 cx cy w h）＋ polygon 另出 YOLO-seg。

    輸出結構：
      run_dir/images/{vid}_f{frame:07d}.jpg     （ffmpeg 抽自 std_path，已存在不重抽）
      run_dir/labels/{...}.txt                  （detection；point 無面積故略過）
      run_dir/labels_seg/{...}.txt              （僅 polygon，YOLO-seg 格式）
      run_dir/data.yaml                         （class 清單）
      run_dir/images_meta.csv                   （每張圖回溯 sidecar）
    ann_ids 給定時只匯出該子集（篩選式下載，dataset_export 注入）；
    include_images=False 時不抽/不複製影像（label-only），其餘產物不變。
    回傳 manifest dict（檔案清單與統計）。
    """
    if include_images:
        _require("ffmpeg")
    run_dir = Path(run_dir)
    registry.init_db(db_path)
    with registry.connect(db_path) as conn:
        vids = _load_videos(conn, videos, project_id=project_id)
        rows = [r for r in rows_from_registry(conn, project_id=project_id)
                if r["video_id"] in vids
                and (ann_ids is None or r["ann_id"] in ann_ids)]
        if not rows:
            raise ConvertError("registry 內沒有符合條件的 spatial 標註。")
        classes = _yolo_class_order(conn, {r["label"] for r in rows},
                                    project_id=project_id)
    cls_id = {n: i for i, n in enumerate(classes)}

    images_dir = run_dir / "images"
    labels_dir = run_dir / "labels"
    seg_dir = run_dir / "labels_seg"
    if include_images:
        images_dir.mkdir(parents=True, exist_ok=True)
    labels_dir.mkdir(parents=True, exist_ok=True)

    # 每張唯一 (video, frame) 一張圖；label 行按圖聚合
    det_lines: dict[tuple[str, int], list[str]] = {}
    seg_lines: dict[tuple[str, int], list[str]] = {}
    # sidecar 追溯：每張圖聚合貢獻標註的 project / annotator（多值 "|" 串接）
    img_ids: dict[tuple[str, int], dict[str, set]] = {}
    n_det = n_seg = n_points_skipped = 0
    for r in rows:
        v = vids[r["video_id"]]
        w, h = float(v["width"]), float(v["height"])
        cid = cls_id[r["label"]]
        for kp in r["keypoints"]:
            key = (r["video_id"], int(kp["frame"]))
            pts = [float(p) for p in kp["points"]]
            det_lines.setdefault(key, [])
            m = img_ids.setdefault(key, {"projects": set(), "aids": set(),
                                         "names": set()})
            if r.get("project_id"):
                m["projects"].add(r["project_id"])
            if r.get("annotator_id"):
                m["aids"].add(r["annotator_id"])
            if r.get("annotator"):
                m["names"].add(r["annotator"])
            if r["shape"] == "point":
                n_points_skipped += 1        # 零面積，無法出 detection 框
            else:
                bx, by, bw, bh = bounding_box(r["shape"], pts)
                cx, cy = _clamp01((bx + bw / 2) / w), _clamp01((by + bh / 2) / h)
                nw, nh = _clamp01(bw / w), _clamp01(bh / h)
                det_lines[key].append(
                    f"{cid} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}")
                n_det += 1
            if r["shape"] == "polygon":
                coords = " ".join(
                    f"{_clamp01(pts[i] / w):.6f} {_clamp01(pts[i + 1] / h):.6f}"
                    for i in range(0, len(pts), 2))
                seg_lines.setdefault(key, []).append(f"{cid} {coords}")
                n_seg += 1

    # 抽圖 + 寫 label txt + sidecar
    meta_rows, n_new = [], 0
    for vid, frame in sorted(det_lines):
        v = vids[vid]
        name = _image_name(vid, frame)
        if include_images:
            n_new += _ensure_image(v, frame, images_dir / name)
        (labels_dir / f"{Path(name).stem}.txt").write_text(
            "\n".join(det_lines[(vid, frame)]) + ("\n" if det_lines[(vid, frame)] else ""),
            encoding="utf-8")
        if (vid, frame) in seg_lines:
            seg_dir.mkdir(parents=True, exist_ok=True)
            (seg_dir / f"{Path(name).stem}.txt").write_text(
                "\n".join(seg_lines[(vid, frame)]) + "\n", encoding="utf-8")
        fps = float(v["fps"])
        m = img_ids.get((vid, frame), {})
        meta_rows.append({"video_id": vid, "frame_index": frame,
                          "time_sec": round(frame / fps, 6), "fps": fps,
                          "image": f"images/{name}",
                          "project_id": "|".join(sorted(m.get("projects", ()))),
                          "annotator_id": "|".join(sorted(m.get("aids", ()))),
                          "annotator_name": "|".join(sorted(m.get("names", ())))})

    meta_path = run_dir / "images_meta.csv"
    _write_csv(meta_path, IMAGE_META_FIELDS, meta_rows)
    _check_time_invariant(_reread_csv(meta_path), "frame_index", "time_sec",
                          where="images_meta.csv")

    yaml_path = run_dir / "data.yaml"
    # 2026-11-02 起不寫 `path:`——Ultralytics check_det_dataset 在缺 path 時
    # 以 data.yaml 所在目錄為資料集根（`path: .` 反而會解成執行當下的 CWD），整包搬到哪都能直接訓練。
    yaml_path.write_text(yaml.safe_dump({
        "train": "images", "val": "images",
        "names": {i: n for i, n in enumerate(classes)},
        # 追溯註記（YOLO txt 本身帶不了 ID → 見 images_meta.csv）
        "project_id": project_id,
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")

    return {
        "format": "yolo", "run_dir": str(run_dir),
        "files": [str(yaml_path), str(meta_path)],
        "images_dir": str(images_dir) if include_images else None,
        "labels_dir": str(labels_dir),
        "seg_dir": str(seg_dir) if n_seg else None,
        "classes": classes,
        "n_images": len(det_lines), "n_new_frames": n_new,
        "n_det_boxes": n_det, "n_seg_polygons": n_seg,
        "n_points_skipped": n_points_skipped,
    }


# ---------------------------------------------------- frame-label pairs ----

def _interval_seconds(interval_name: str) -> float:
    """interval 名 → 秒數：先查 ingest.yaml sampling.intervals，否則解析數字。"""
    intervals = config.load_config("ingest.yaml")["sampling"]["intervals"]
    if interval_name in intervals:
        return float(intervals[interval_name])
    name = interval_name
    if name.startswith("sample_"):
        name = name[len("sample_"):]
    if name.endswith("s"):
        name = name[:-1]
    try:
        return float(name)
    except ValueError:
        raise ConvertError(
            f"未知 interval「{interval_name}」；可用：{sorted(intervals)}，"
            f"或直接給秒數（如 '5'）。") from None


def _temporal_intervals_by_layer(conn, project_id: Optional[str] = None,
                                 ) -> dict[str, list[dict]]:
    """registry annotations（temporal 各層）→ derive 需要的 interval 列。"""
    out: dict[str, list[dict]] = {layer: [] for layer in TEMPORAL_LAYERS}
    sql = "SELECT * FROM annotations WHERE layer != 'spatial'"
    params: tuple = ()
    if project_id is not None:
        sql += " AND project_id = ?"          # annotations.project_id 直查
        params = (project_id,)
    cur = conn.execute(sql + " ORDER BY video_id, layer, start_frame", params)
    for r in cur:
        if r["layer"] not in out:
            continue
        row = {"video_id": r["video_id"], "start_frame": r["start_frame"],
               "end_frame": r["end_frame"], "label": r["label"],
               "annotator_id": r["annotator_id"] or r["annotator"],
               "project_id": r["project_id"] or ""}
        if r["layer"] == "triplet":
            attrs = json.loads(r["attrs_json"] or "{}")
            row.update({k: attrs.get(k, "") for k in ("instrument", "verb", "target")})
        out[r["layer"]].append(row)
    return out


def frame_pairs(run_dir, interval_name: str, db_path=None,
                project_id: Optional[str] = None) -> dict:
    """temporal interval 標註 → 抽樣 frame 的 (影像, label) 配對資料集。

    複用 pipeline.derive 的推導邏輯（interval → frame label）；
    每個抽樣 frame 抽出 JPG 到 run_dir/frames/，並寫 image_label_pairs.csv
    （video_id, image_path, frame_index, time_sec, fps, label_type, label, annotator）。
    """
    _require("ffmpeg")
    run_dir = Path(run_dir)
    sec = _interval_seconds(interval_name)
    registry.init_db(db_path)
    with registry.connect(db_path) as conn:
        vids = _load_videos(conn, project_id=project_id)
        intervals_by_layer = _temporal_intervals_by_layer(conn, project_id=project_id)
        annotator_names = {r["annotator_id"]: r["name"]
                           for r in registry.list_annotators(conn)}
    if not any(intervals_by_layer.values()):
        raise ConvertError("registry 內沒有 temporal 標註，無法產生 frame pairs。")

    # 只保留有標註的影片，逐 fps 分組推導（避免 derive 的混 fps 限制）
    used_vids = {r["video_id"] for rows in intervals_by_layer.values() for r in rows}
    missing = used_vids - set(vids)
    if missing:
        raise ConvertError(f"標註指向 registry 沒有的影片：{sorted(missing)}")
    derived: list[dict] = []
    for fps in sorted({float(vids[v]["fps"]) for v in used_vids}):
        meta = {v: {"fps": float(vids[v]["fps"]), "n_frames": int(vids[v]["n_frames"])}
                for v in used_vids if float(vids[v]["fps"]) == fps}
        step = int(round(sec * fps))
        if step <= 0:
            raise ConvertError(f"interval {sec}s 於 fps={fps} 的 step 為 0。")
        derived.extend(derive.derive_frame_labels(intervals_by_layer, meta, step))

    frames_dir = run_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    n_new, rows = 0, []
    extracted: set[tuple[str, int]] = set()
    for d in derived:
        vid, frame = d["video_id"], int(d["frame_index"])
        name = _image_name(vid, frame)
        if (vid, frame) not in extracted:
            n_new += _ensure_image(vids[vid], frame, frames_dir / name)
            extracted.add((vid, frame))
        rows.append({
            "video_id": vid, "image_path": f"frames/{name}",
            "frame_index": frame, "time_sec": d["time_sec"],
            "fps": float(vids[vid]["fps"]),
            "label_type": d["label_type"], "label": d["label"],
            "annotator": annotator_names.get(d["annotator_id"],
                                             d["annotator_id"]),
            "project_id": d.get("project_id", ""),
            "annotator_id": d["annotator_id"],
        })

    csv_path = run_dir / "image_label_pairs.csv"
    _write_csv(csv_path, PAIR_FIELDS, rows)
    reread = _reread_csv(csv_path)
    _check_time_invariant(reread, "frame_index", "time_sec",
                          where="image_label_pairs.csv")
    for r in reread:   # 每列影像必須實際存在
        if not (run_dir / r["image_path"]).exists():
            raise ConvertError(f"影像不存在：{r['image_path']}")

    return {
        "format": "frame-pairs", "run_dir": str(run_dir),
        "interval": interval_name, "interval_sec": sec,
        "files": [str(csv_path)], "frames_dir": str(frames_dir),
        "n_rows": len(rows), "n_images": len(extracted), "n_new_frames": n_new,
    }


# ----------------------------------------------------- clip-label pairs ----

def _cut_clip(std_path: Path, fps: float, start: int, end: int,
              out_path: Path) -> None:
    """ffmpeg 精準切段（含 start..end frame，re-encode 保 frame 精度）。"""
    target = config.load_config("ingest.yaml")["target"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y",
           "-ss", f"{start / fps:.6f}",
           "-t", f"{(end - start + 1) / fps:.6f}",
           "-i", str(std_path),
           "-c:v", "libx264", "-crf", str(target["crf"]),
           "-preset", str(target["preset"]), "-pix_fmt", str(target["pix_fmt"]),
           "-an", str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out_path.exists():
        raise ConvertError(
            f"ffmpeg 切 clip 失敗（{out_path.name}）：\n{proc.stderr[-500:]}")


def clip_pairs(run_dir, db_path=None, cut: bool = False,
               project_id: Optional[str] = None) -> dict:
    """temporal 區段 → clip_label_pairs.csv（＋可選 ffmpeg 切出 clip）。

    欄位：video_id, start_frame, end_frame, start_time, end_time, fps,
          layer, label, annotator, clip_path。
    cut=True 時切出 run_dir/clips/{vid}_f{start:07d}-f{end:07d}.mp4
    （re-encode 保精度；已存在不重切）。
    """
    run_dir = Path(run_dir)
    registry.init_db(db_path)
    with registry.connect(db_path) as conn:
        vids = _load_videos(conn, project_id=project_id)
        sql = "SELECT * FROM annotations WHERE layer != 'spatial'"
        params: tuple = ()
        if project_id is not None:
            sql += " AND project_id = ?"      # annotations.project_id 直查
            params = (project_id,)
        anns = list(conn.execute(
            sql + " ORDER BY video_id, layer, start_frame", params))
    if not anns:
        raise ConvertError("registry 內沒有 temporal 標註，無法產生 clip pairs。")
    if cut:
        _require("ffmpeg")

    clips_dir = run_dir / "clips"
    rows, n_cut = [], 0
    for r in anns:
        vid = r["video_id"]
        v = vids.get(vid)
        if v is None:
            raise ConvertError(f"registry 查不到 video_id={vid}（ann_id={r['ann_id']}）")
        fps = float(v["fps"])
        start, end = int(r["start_frame"]), int(r["end_frame"])
        clip_rel = ""
        if cut:
            clip_name = f"{vid}_f{start:07d}-f{end:07d}.mp4"
            clip_path = clips_dir / clip_name
            if not clip_path.exists():
                std_path = config.to_abs(v["std_path"])   # schema v1.4：相對 data 根
                if not std_path or not std_path.exists():
                    raise ConvertError(f"標準化影片不存在：{std_path}")
                _cut_clip(std_path, fps, start, end, clip_path)
                n_cut += 1
            clip_rel = f"clips/{clip_name}"
        rows.append({
            "video_id": vid, "start_frame": start, "end_frame": end,
            "start_time": round(start / fps, 6), "end_time": round(end / fps, 6),
            "fps": fps, "layer": r["layer"], "label": r["label"],
            "annotator": r["annotator"], "ann_id": r["ann_id"],
            "project_id": r["project_id"] or "",
            "annotator_id": r["annotator_id"] or "",
            "clip_path": clip_rel,
        })

    csv_path = run_dir / "clip_label_pairs.csv"
    _write_csv(csv_path, CLIP_FIELDS, rows)
    reread = _reread_csv(csv_path)
    _check_time_invariant(reread, "start_frame", "start_time",
                          where="clip_label_pairs.csv (start)")
    _check_time_invariant(reread, "end_frame", "end_time",
                          where="clip_label_pairs.csv (end)")

    return {
        "format": "clip-pairs", "run_dir": str(run_dir),
        "files": [str(csv_path)],
        "clips_dir": str(clips_dir) if cut else None,
        "n_rows": len(rows), "n_clips_cut": n_cut,
    }
