"""sample：依 registry frames 表抽出 JPEG，供 spatial 標註。

命名規範（全平台強制）：{video_id}_f{frame_index:07d}.jpg
——檔名本身攜帶回溯資訊，任何平台匯出後都能還原 video_id + frame_index。

抽幀策略：等間隔 purpose（sample_5s / sample_30s）以 ffmpeg select 濾鏡單趟跑完，
輸出序號 i 對應 frame_index = i * step（決定論），再改名為正式檔名。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config, registry
from .ingest import IngestError, _require


class SampleError(RuntimeError):
    """sample 過程中的可預期錯誤。"""


@dataclass
class SampleOutcome:
    video_id: str
    purpose: str
    out_dir: Path
    n_frames: int
    step: int


def _uniform_step(indices: list[int]) -> int:
    """驗證抽樣點為等間隔（含 0 起點），回傳間隔。"""
    if not indices or indices[0] != 0:
        raise SampleError("frames 表抽樣點須從 frame 0 開始（等間隔 purpose）。")
    if len(indices) == 1:
        return 1
    step = indices[1] - indices[0]
    for a, b in zip(indices, indices[1:]):
        if b - a != step:
            raise SampleError("frames 表抽樣點非等間隔，此 purpose 不支援批次抽幀。")
    return step


def sample(video_id: str, purpose: str, jpeg_qv: int = 2, force: bool = False,
           db_path=None, on_step=None) -> SampleOutcome:
    """抽出單支影片某 purpose 的所有 frame JPEG，回寫 frames.path。"""
    _require("ffmpeg")

    def step_msg(m):
        if on_step:
            on_step(m)

    with registry.connect(db_path) as conn:
        video = registry.get_video(video_id, conn)
        if video is None:
            raise SampleError(f"registry 內沒有 video_id={video_id}，請先 surgann ingest。")
        rows = list(conn.execute(
            "SELECT frame_index FROM frames WHERE video_id = ? AND purpose = ? "
            "ORDER BY frame_index", (video_id, purpose)))
        indices = [r["frame_index"] for r in rows]
    if not indices:
        raise SampleError(f"frames 表沒有 {video_id} / purpose={purpose} 的抽樣點。")

    std_path = config.to_abs(video["std_path"])   # schema v1.4：相對 data 根 → 絕對
    if not std_path or not std_path.exists():
        raise SampleError(f"標準化影片不存在：{std_path}")
    step = _uniform_step(indices)

    out_dir = config.FRAMES_DIR / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    expected = [config.frame_path(video_id, i) for i in indices]
    if all(p.exists() for p in expected) and not force:
        step_msg("所有抽幀已存在（--force 可重抽），僅回寫 registry path")
        _write_paths(video_id, purpose, indices, out_dir, db_path)
        return SampleOutcome(video_id, purpose, out_dir, len(indices), step)

    # 單趟抽幀：select 每 step 幀取一張；輸出序號 1-based → frame = (i-1)*step
    step_msg(f"ffmpeg 抽幀（每 {step} frames 一張，共 {len(indices)} 張）")
    tmp_pattern = out_dir / f".tmp_{purpose}_%07d.jpg"
    cmd = [
        "ffmpeg", "-y", "-i", str(std_path),
        "-vf", f"select='not(mod(n\\,{step}))'",
        "-fps_mode", "passthrough",
        "-q:v", str(jpeg_qv),
        str(tmp_pattern),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SampleError(f"ffmpeg 抽幀失敗：\n{proc.stderr[-800:]}")

    tmp_files = sorted(out_dir.glob(f".tmp_{purpose}_*.jpg"))
    if len(tmp_files) != len(indices):
        for t in tmp_files:
            t.unlink()
        raise SampleError(
            f"抽出張數 {len(tmp_files)} ≠ frames 表抽樣點 {len(indices)}，已中止（不入庫）。")

    step_msg("改名為正式檔名（{video_id}_f{frame:07d}.jpg）並回寫 registry")
    for i, tmp in enumerate(tmp_files):
        tmp.replace(config.frame_path(video_id, indices[i]))
    _write_paths(video_id, purpose, indices, out_dir, db_path)
    return SampleOutcome(video_id, purpose, out_dir, len(indices), step)


def _write_paths(video_id: str, purpose: str, indices: list[int],
                 out_dir: Path, db_path=None) -> None:
    with registry.connect(db_path) as conn:
        conn.executemany(
            "UPDATE frames SET path = ? WHERE video_id = ? AND frame_index = ? "
            "AND purpose = ?",
            [(config.to_rel(config.frame_path(video_id, i)), video_id, i, purpose)
             for i in indices],   # schema v1.4：frames.path 存相對 data 根
        )
