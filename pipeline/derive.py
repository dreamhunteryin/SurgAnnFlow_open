"""derive（M1）：interval 標註 → 每 5s/30s（任意間隔）frame-level label。

抽樣點 label 不由人標，由 interval 推導；間隔可事後任意調整——
換個 intervals 設定重跑即可，不需重標。
輸出 frame_labels_{interval}.csv：video_id, frame_index, time_sec, label_type, label, annotator_id。
覆蓋判定：start_frame <= frame_index <= end_frame；無覆蓋輸出空 label（保留完整抽樣網格）。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from . import registry

FRAME_LABEL_FIELDS = ["video_id", "frame_index", "time_sec", "label_type", "label",
                      "annotator_id", "project_id"]
LAYERS = ("phase", "action", "event", "triplet")


def load_interval_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _interval_label(row: dict) -> str:
    """triplet 列以 instrument,verb,target 組合為 label；其餘取 label 欄。"""
    if "instrument" in row:
        return f'{row.get("instrument", "")},{row.get("verb", "")},{row.get("target", "")}'
    return row["label"]


def derive_frame_labels(intervals_by_layer: dict[str, list[dict]],
                        video_meta: dict[str, dict],
                        step_frames: int) -> list[dict]:
    """單一抽樣間隔的推導。video_meta: video_id -> {fps, n_frames}。"""
    out: list[dict] = []
    # (video, layer, annotator, project) 分組——每位標註者獨立一份推導結果
    # v1.2 加 project 隔離（interval 列無 project_id 時歸 ""）
    groups: dict[tuple[str, str, str, str], list[dict]] = {}
    for layer, rows in intervals_by_layer.items():
        for r in rows:
            key = (r["video_id"], layer, r["annotator_id"],
                   r.get("project_id", "") or "")
            groups.setdefault(key, []).append(r)

    for (vid, layer, annotator, project), rows in sorted(groups.items()):
        meta = video_meta.get(vid)
        if meta is None:
            continue
        fps, n_frames = meta["fps"], meta["n_frames"]
        spans = [(int(r["start_frame"]), int(r["end_frame"]), _interval_label(r))
                 for r in rows]
        for f in range(0, n_frames, step_frames):
            labels = [lab for sf, ef, lab in spans if sf <= f <= ef]
            for lab in (labels or [""]):   # 無覆蓋 → 空 label，保留抽樣網格
                out.append({
                    "video_id": vid, "frame_index": f,
                    "time_sec": round(f / fps, 6),
                    "label_type": layer, "label": lab, "annotator_id": annotator,
                    "project_id": project,
                })
    return out


@dataclass
class DeriveOutcome:
    run_dir: Path
    outputs: dict[str, Path] = field(default_factory=dict)   # interval 名 -> csv
    row_counts: dict[str, int] = field(default_factory=dict)
    grid_counts: dict[str, dict[str, int]] = field(default_factory=dict)  # interval -> video -> 抽樣點數


def run_derive(run_dir: Path, intervals: dict[str, float], db_path=None) -> DeriveOutcome:
    """對一個 export run 目錄執行推導，輸出 frame_labels_{interval}.csv 至同目錄。"""
    run_dir = Path(run_dir)
    intervals_by_layer = {
        layer: load_interval_csv(run_dir / f"{layer}.csv") for layer in LAYERS
    }
    with registry.connect(db_path) as conn:
        video_meta = {
            r["video_id"]: {"fps": float(r["fps"]), "n_frames": int(r["n_frames"])}
            for r in registry.list_videos(conn)
        }

    outcome = DeriveOutcome(run_dir=run_dir)
    for name, sec in intervals.items():
        # step 依各影片 fps 計算才嚴謹；目前全庫統一 25fps，取第一支的 fps 即可，
        # 但仍逐支驗證，混 fps 時直接報錯。
        fps_set = {m["fps"] for m in video_meta.values()}
        if len(fps_set) > 1:
            raise RuntimeError(f"registry 內 fps 不一致（{fps_set}），derive 需逐支拆分實作。")
        fps = fps_set.pop() if fps_set else 25.0
        step = int(round(sec * fps))

        rows = derive_frame_labels(intervals_by_layer, video_meta, step)
        path = run_dir / f"frame_labels_{name}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FRAME_LABEL_FIELDS)
            w.writeheader()
            w.writerows(rows)

        grid = {}
        for vid, meta in video_meta.items():
            pts = {r["frame_index"] for r in rows if r["video_id"] == vid}
            if pts:
                grid[vid] = len(pts)
        outcome.outputs[name] = path
        outcome.row_counts[name] = len(rows)
        outcome.grid_counts[name] = grid
    return outcome
