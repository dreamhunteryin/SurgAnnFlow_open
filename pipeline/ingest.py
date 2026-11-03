"""影片 ingest：標準化轉檔 → 驗證 → 入庫 → 抽樣點註冊。

核心不變式：
- 原始影片唯讀，永不修改。
- CFR 25fps 是 frame/timestamp 正確性的基石；驗證不過即擋下，不入庫。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config


class IngestError(RuntimeError):
    """ingest 過程中的可預期錯誤（驗證失敗、缺工具等）。"""


# ---------------------------------------------------------------- 工具偵測 ----

def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise IngestError(f"找不到必要工具 `{tool}`，請先安裝。")
    return path


# ---------------------------------------------------------------- ffprobe ----

def probe(path: Path) -> dict:
    """回傳影片 video stream + format 的關鍵資訊。"""
    _require("ffprobe")
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,codec_name:format=duration",
        "-of", "json", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise IngestError(f"ffprobe 讀取失敗：{path}\n{out.stderr.strip()}")
    data = json.loads(out.stdout)
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    return {
        "width": int(stream.get("width", 0)),
        "height": int(stream.get("height", 0)),
        "codec": stream.get("codec_name"),
        "r_frame_rate": stream.get("r_frame_rate"),
        "avg_frame_rate": stream.get("avg_frame_rate"),
        "nb_frames": _to_int(stream.get("nb_frames")),
        "duration": float(fmt.get("duration", 0.0)),
    }


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_rate(rate: str | None) -> float:
    """'25/1' -> 25.0；容錯處理。"""
    if not rate:
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        den = float(den)
        return float(num) / den if den else 0.0
    return float(rate)


def _count_frames(path: Path) -> int:
    """精確逐幀計數（container 未提供 nb_frames 時的後援；較慢）。"""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-count_frames", "-show_entries", "stream=nb_read_frames",
        "-of", "default=nokey=1:noprint_wrappers=1", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    return _to_int(out.stdout.strip()) or 0


# ------------------------------------------------------------------- sha256 --

def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------- transcode ---

def build_ffmpeg_cmd(src: Path, dst: Path, t: dict) -> list[str]:
    """依設定組出標準化 ffmpeg 指令。"""
    vf = f"scale=-2:{t['height']}:flags=lanczos,fps={t['fps']}"
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264", "-preset", str(t["preset"]),
        "-crf", str(t["crf"]), "-pix_fmt", str(t["pix_fmt"]),
        "-g", str(t["gop"]), "-keyint_min", str(t["gop"]), "-sc_threshold", "0",
    ]
    if t.get("faststart", True):
        cmd += ["-movflags", "+faststart"]
    if t.get("strip_audio", True):
        cmd += ["-an"]
    cmd += [str(dst)]
    return cmd


def transcode(src: Path, dst: Path, t: dict) -> None:
    """執行標準化轉檔；ffmpeg 進度直接串到終端機。"""
    _require("ffmpeg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_ffmpeg_cmd(src, dst, t)
    proc = subprocess.run(cmd)  # 繼承 stdout/stderr → 使用者看得到編碼進度
    if proc.returncode != 0:
        raise IngestError(f"ffmpeg 轉檔失敗（return code {proc.returncode}）。")


# ------------------------------------------------------------------ verify ---

@dataclass
class VerifyResult:
    ok: bool
    fps: float
    n_frames: int
    duration: float
    width: int
    height: int
    problems: list[str] = field(default_factory=list)


def verify(std_path: Path, v: dict) -> VerifyResult:
    """驗證標準化後影片是否符合 CFR/解析度/幀數。"""
    info = probe(std_path)
    fps = _parse_rate(info["r_frame_rate"])
    n_frames = info["nb_frames"] or _count_frames(std_path)
    duration = info["duration"]
    problems: list[str] = []

    if abs(fps - v["expect_fps"]) > v["fps_tolerance"]:
        problems.append(
            f"r_frame_rate={info['r_frame_rate']} (≈{fps}) ≠ 期望 CFR {v['expect_fps']}"
        )
    if info["height"] != v["expect_height"]:
        problems.append(f"height={info['height']} ≠ 期望 {v['expect_height']}")

    expected_frames = round(duration * v["expect_fps"])
    if abs(n_frames - expected_frames) > v["frame_count_tolerance"]:
        problems.append(
            f"frame 數 {n_frames} 與 round(duration*fps)={expected_frames} "
            f"差距 > ±{v['frame_count_tolerance']}"
        )

    return VerifyResult(
        ok=not problems, fps=fps, n_frames=n_frames, duration=duration,
        width=info["width"], height=info["height"], problems=problems,
    )


# ------------------------------------------------------- sampling 抽樣點註冊 ---

def build_sampling_rows(video_id: str, n_frames: int, fps: float, intervals: dict) -> list[dict]:
    """依設定的間隔秒數，產生 frames 表的抽樣點列（只記 metadata，不抽圖）。"""
    rows: list[dict] = []
    for purpose, sec in intervals.items():
        step = int(round(sec * fps))
        if step <= 0:
            continue
        for idx in range(0, n_frames, step):
            rows.append({
                "video_id": video_id,
                "frame_index": idx,
                "time_sec": round(idx / fps, 6),
                "path": None,   # 尚未抽圖；surgann sample 時才落地
                "purpose": purpose,
            })
    return rows


# ---------------------------------------------------------------- 主流程 ------

@dataclass
class IngestOutcome:
    video_id: str
    std_path: Path
    verify: VerifyResult
    n_sampling_rows: int
    sampling_counts: dict[str, int]


def register_video(std_path: Path, video_id: str, cfg: dict, *,
                   source_path: str | None = None, db_path=None,
                   on_step=None) -> IngestOutcome:
    """register-only 入口：僅驗證＋sha256＋入庫＋抽樣點（轉檔已由呼叫端完成）。

    供 preprocess pipeline（轉檔在 cut_encode 完成）與完整 ingest 共用。
    video_id 由呼叫端明確傳入（新制 videoID 配號屬 registry helper 地盤）。
    source_path 為單一檔案時計算來源 sha256；為資料夾或 None 時 sha256_source 留空
    （多檔來源的逐檔 sha256 由 deid_map 承載）。
    """
    from . import registry  # 延遲載入避免循環

    def step(msg: str):
        if on_step:
            on_step(msg)

    # 一律轉絕對再交給 registry（registry 寫入時以 config.to_rel 轉 schema v1.4 相對路徑；
    # 若此處留 cwd 相對值會被誤認成「已是相對 data 根」）
    std_path = Path(os.path.abspath(std_path))
    if not std_path.exists():
        raise IngestError(f"標準化檔不存在：{std_path}")
    if source_path:
        source_path = os.path.abspath(source_path)

    # 1) 驗證（不過即擋下，不入庫）
    step("ffprobe 驗證（CFR / 解析度 / 幀數）")
    vr = verify(std_path, cfg["verify"])
    if not vr.ok:
        raise IngestError("驗證未通過，已擋下不入庫：\n  - " + "\n  - ".join(vr.problems))

    # 2) 雜湊
    step("計算 sha256")
    sha_src = (sha256(Path(source_path))
               if source_path and Path(source_path).is_file() else None)
    sha_std = sha256(std_path)

    # 3) 入庫 videos + 抽樣點 frames
    step("寫入 registry（videos + frames 抽樣點）")
    sampling_rows = build_sampling_rows(
        video_id, vr.n_frames, vr.fps, cfg["sampling"]["intervals"]
    )
    now = datetime.now(timezone.utc).isoformat()
    with registry.connect(db_path) as conn:
        registry.upsert_video(
            conn,
            video_id=video_id,
            source_path=source_path,      # registry.upsert_video 自動 to_rel（schema v1.4）
            std_path=str(std_path),
            fps=vr.fps,
            n_frames=vr.n_frames,
            duration_sec=vr.duration,
            width=vr.width,
            height=vr.height,
            sha256_source=sha_src,
            sha256_std=sha_std,
            status="verified",
            ingested_at=now,
        )
        registry.replace_frames(conn, video_id, sampling_rows)
        counts = registry.count_frames(conn, video_id)

    return IngestOutcome(
        video_id=video_id, std_path=std_path, verify=vr,
        n_sampling_rows=len(sampling_rows), sampling_counts=counts,
    )


def ingest(src: Path, video_id: str, cfg: dict, db_path=None, force: bool = False,
           on_step=None) -> IngestOutcome:
    """單支影片完整 ingest：轉檔 → register_video。src 唯讀，永不修改。"""

    def step(msg: str):
        if on_step:
            on_step(msg)

    src = Path(src).resolve()
    if not src.exists():
        raise IngestError(f"來源影片不存在：{src}")

    std_path = config.std_video_path(video_id)
    if std_path.exists() and not force:
        raise IngestError(
            f"標準化檔已存在：{std_path}（用 --force 覆寫，或改用其他 --video-id）"
        )

    # 1) 標準化轉檔
    step(f"轉檔標準化 → {std_path}")
    transcode(src, std_path, cfg["target"])

    # 2) 驗證 + sha256 + 入庫 + 抽樣點（register-only 共用段）
    return register_video(std_path, video_id, cfg, source_path=str(src),
                          db_path=db_path, on_step=on_step)
