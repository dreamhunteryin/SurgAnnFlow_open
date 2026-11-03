"""匯出共用基礎設施（R4）：欄位定義、run 目錄/編號、一致性檢查、結果物件。

所有匯出路徑（temporal / spatial / dataset / mosaic）共用本模組，彼此不互相 import。

歷史：這些定義原本住在 `export_ls.py`（Label Studio 是第一個匯出實作，後續匯出
沿用其骨架）。2026-07-23 移除 LS/CVAT 堆疊時抽出為中性模組——核心匯出不該
經由某個特定標註平台的檔案載入（R4）。

不變式：任何輸出列必含 video_id + frame + timestamp，且 timestamp == frame / fps；
匯出結束跑一致性檢查，不通過即 fail。timestamp 一律由 pipeline 以 frame/fps 計算，
不使用播放器回報的秒數。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config


class ExportError(RuntimeError):
    """匯出過程中的可預期錯誤（一致性檢查失敗、來源缺漏等）。"""


TEMPORAL_FIELDS = ["video_id", "start_frame", "end_frame", "start_time", "end_time",
                   "label", "annotator_id", "annotation_id", "version"]
TRIPLET_FIELDS = TEMPORAL_FIELDS + ["instrument", "verb", "target"]
LAYERS = ("phase", "action", "event")          # 一般 timeline 層
TRIPLET_LAYER = "triplet"


GLOBAL_EXPORT_SCOPE = "_global"   # 無專案概念的匯出（未帶 -P）


def exports_dir(project_id: str | None = None) -> Path:
    """匯出根目錄（v1.2 按專案分層）：data/exports/{project_id}/。

    無專案概念（project_id=None）統一落 data/exports/_global/。
    播放清單模式上線前的舊 run 已歸檔 data/exports/legacy/（config.LEGACY_EXPORTS_DIR）。
    """
    d = config.EXPORTS_DIR / (project_id or GLOBAL_EXPORT_SCOPE)
    d.mkdir(parents=True, exist_ok=True)
    return d


def next_run_id(project_id: str | None = None,
                now: datetime | None = None) -> str:
    """run_id = 日期 + 專案 scope + 當日最大序號 + 1，如 2026-07-09_default_run01。

    v1.2：連號在 **該專案子目錄**（exports_dir(project_id)）內掃——各專案
    自己 run01 起跳；run_id 內嵌 scope（專案編碼；無專案 = global）確保
    export_runs.run_id（全域 PK）跨專案不撞號，且與 legacy 的
    {date}_runNN 舊格式不衝突。取「既有最大序號 +1」而非「項目數 +1」：
    子目錄內的 {run_id}.zip（dataset_export make_zip 產物，與 run 目錄同層）
    序號相同不灌水，且序號有空洞時也不會落到既有 run 上（撞號即失敗）。
    """
    date = (now or datetime.now()).strftime("%Y-%m-%d")
    scan_dir = exports_dir(project_id)
    scope = project_id or "global"
    pat = re.compile(rf"{re.escape(date)}_{re.escape(scope)}_run(\d+)")
    n = 0
    for p in scan_dir.glob(f"{date}_{scope}_run*"):
        m = pat.fullmatch(p.name[:-4] if p.name.endswith(".zip") else p.name)
        if m:
            n = max(n, int(m.group(1)))
    return f"{date}_{scope}_run{n + 1:02d}"


def check_invariants(rows: dict[str, list[dict]], fps_by_video: dict[str, float]) -> None:
    """一致性檢查：timestamp == frame / fps、frame 非負、start <= end。"""
    for layer, rs in rows.items():
        for r in rs:
            fps = fps_by_video[r["video_id"]]
            if r["start_frame"] < 0 or r["end_frame"] < r["start_frame"]:
                raise ExportError(f"一致性檢查失敗（{layer}）：frame 範圍異常 {r}")
            for fk, tk in (("start_frame", "start_time"), ("end_frame", "end_time")):
                if abs(r[tk] - r[fk] / fps) > 1e-6:
                    raise ExportError(f"一致性檢查失敗（{layer}）：{tk} != {fk}/fps {r}")


@dataclass
class ExportOutcome:
    run_id: str
    run_dir: Path
    n_tasks: int
    n_annotations: int
    row_counts: dict[str, int] = field(default_factory=dict)
    csv_paths: dict[str, Path] = field(default_factory=dict)
