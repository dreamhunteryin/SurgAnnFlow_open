"""上傳前處理 pipeline：合併 → OOB 偵測 → 切除+標準化（單一 pass）→ 驗證。

核心不變式：
- 原始影片唯讀（R9）：所有產物落在 workdir，永不改動來源資料夾。
- 單一重編碼：OOB 切除與 720p/25 CFR 標準化在同一個 ffmpeg pass 完成，
  最終編碼參數 = ingest 標準（驗證必過），另補 metadata 全剝（去識別化）。
- 每階段產物存在即跳過（resume）：merged.mp4 / oob.csv。
- 驗證重用 ingest.verify（R4）；duration 檢查對 in-body 段總長 ±cfg 容差。

OOB 模型：OOBNet（CAMMA, U. Strasbourg）—— 權重 CC BY-NC-SA 4.0，僅限非商業
研究使用。推論為 ONNX + onnxruntime，在主 venv 內直接呼叫
（見 detect_oob 與 pipeline/oob_infer.py；轉檔工具 pipeline/oob_export_onnx.py）。
"""
from __future__ import annotations

import csv
import functools
import hashlib
import re
import shutil
import statistics
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, NamedTuple, Optional

from . import config, ingest, oob_infer


class PreprocessError(RuntimeError):
    """前處理過程中的可預期錯誤（合併截斷、推論失敗、驗證不過等）。"""


ProgressCb = Callable[[float, float], None]   # (percent 0-100, out_time_sec)
StageProgressCb = Callable[[float], None]     # stage 內百分比 0-100（保證單調不減）
StepCb = Callable[[str], None]


def _step(on_step: Optional[StepCb], msg: str) -> None:
    if on_step:
        on_step(msg)


def _run_ffmpeg_progress(cmd: list[str], expected: float,
                         progress_cb: Optional[ProgressCb],
                         err_prefix: str) -> None:
    """執行 ffmpeg 並以 -progress pipe:1 解析 out_time_ms 回報進度
    （cut_encode / merge / fallback 共用的解析邏輯，R4 單一實作）。

    約定：cmd 的最後一個元素必須是輸出路徑，本函式在其前統一插入
    -progress pipe:1；失敗以 err_prefix 開頭 raise PreprocessError。
    expected <= 0 時 pct 固定回報 0（無法估總長）。
    """
    full = [*cmd[:-1], "-progress", "pipe:1", cmd[-1]]
    proc = subprocess.Popen(full, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        if progress_cb and line.startswith("out_time_ms="):
            try:
                t = int(line.split("=", 1)[1]) / 1e6  # out_time_ms 實為微秒
            except ValueError:
                continue
            pct = min(100.0, t / expected * 100.0) if expected > 0 else 0.0
            progress_cb(pct, t)
    proc.wait()
    if proc.returncode != 0:
        err = (proc.stderr.read() or "").strip()[-800:] if proc.stderr else ""
        raise PreprocessError(f"{err_prefix}：{err}")


# ------------------------------------------------------------ 檔名排序 key ----

def video_sort_key(filepath: str | Path):
    """手術錄影檔名排序 key（邏輯吸收自 GPU_merge_video stage1，涵蓋自家資料常見命名）：

      Ch#_###_CH###_V → V###_### → V### → VID###[_LETTERS] → Video_### → natural sort
    """
    stem = Path(filepath).stem
    s = stem.upper()

    m = re.fullmatch(r"CH(\d+)_(\d+)_CH(\d+)_V", s)   # channel-specific clip（先於 Ch#_###_V）
    if m:
        return ("ch", int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"CH(\d+)_(\d+)_V", s)           # base clip, no channel
    if m:
        return ("ch", int(m.group(1)), int(m.group(2)), -1)
    m = re.fullmatch(r"V(\d+)_(\d+)", s)              # segment clip（先於 V###）
    if m:
        return ("v", int(m.group(1)), int(m.group(2)))
    m = re.fullmatch(r"V(\d+)", s)                    # base clip
    if m:
        return ("v", int(m.group(1)), -1)
    m = re.fullmatch(r"VID(\d+)(?:_([A-Z]+))?", s)    # action cam：無尾碼 → A→Z → AA→…
    if m:
        suffix = m.group(2) or ""
        rank = -1 if not suffix else sum(
            (ord(c) - ord("A") + 1) * (26 ** (len(suffix) - 1 - i))
            for i, c in enumerate(suffix))
        return ("vid", int(m.group(1)), rank)
    m = re.fullmatch(r"VIDEO_(\d+)", s)               # Video_###
    if m:
        return ("video", int(m.group(1)))

    # fallback: natural sort（re.split 產生固定交錯型別，跨檔名可比較）
    parts = [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", stem)]
    return ("z_fallback", parts)


# ---------------------------------------------------------------- MergePlan ---

@dataclass
class MergePlan:
    """合併計畫（可序列化；之後 UI 顯示排序清單供人工調序）。"""
    folder: str
    files: list[dict] = field(default_factory=list)
    # 每項：{order, name, path, size_bytes, duration_sec}
    single_file: bool = False
    total_duration: float = 0.0

    def to_dict(self) -> dict:
        return {"folder": self.folder, "files": self.files,
                "single_file": self.single_file, "total_duration": self.total_duration}

    @classmethod
    def from_dict(cls, d: dict) -> "MergePlan":
        return cls(folder=d["folder"], files=list(d["files"]),
                   single_file=bool(d["single_file"]),
                   total_duration=float(d["total_duration"]))


def _probe_duration(path: Path) -> float:
    try:
        return ingest.probe(path)["duration"]
    except ingest.IngestError:
        return 0.0


def plan_merge(folder: Path, cfg: dict) -> MergePlan:
    """掃描病人資料夾（非遞迴）→ 依 video_sort_key 排序 → 回傳可序列化的合併計畫。

    單檔資料夾 single_file=True（決策 C-1：免合併，直接進後續階段）。
    """
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise PreprocessError(f"病人資料夾不存在：{folder}")
    exts = {e.lower() for e in cfg["merge"]["extensions"]}

    paths = sorted(
        (p for p in folder.iterdir()
         if p.is_file() and not p.name.startswith("._")
         and p.suffix.lower() in exts),
        key=video_sort_key)
    if not paths:
        raise PreprocessError(f"資料夾內找不到影片檔（{sorted(exts)}）：{folder}")

    files = []
    total = 0.0
    for i, p in enumerate(paths, 1):
        dur = _probe_duration(p)
        total += dur
        files.append({"order": i, "name": p.name, "path": str(p),
                      "size_bytes": p.stat().st_size, "duration_sec": round(dur, 3)})
    return MergePlan(folder=str(folder), files=files,
                     single_file=(len(files) == 1), total_duration=round(total, 3))


# -------------------------------------------------------------------- merge ---

@functools.lru_cache(maxsize=1)
def nvenc_available() -> bool:
    """h264_nvenc 是否真的能編碼（以 0.1 秒測試片實跑一次；結果在本程序內快取）。

    encode.encoder=auto 與 merge.fallback_encoder=auto 共用（R4）；doctor 亦呼叫此函式。
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi",
             "-i", "testsrc=duration=0.1:size=320x240:rate=10",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def _write_concat_list(paths: list[str], list_path: Path) -> None:
    with open(list_path, "w", encoding="utf-8") as f:
        for p in paths:
            escaped = str(p).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")


# merge 進度對映：stream copy 嘗試階段最多佔 stage 前 15%（copy 很快、re-encode
# fallback 才是大宗；若 copy 對映 0-100，截斷輸出常把 out_time 推近尾端，
# fallback 只剩壓縮到看不見的區間 → 進度條又近乎凍結）。成功即直接收尾 100。
_COPY_PHASE_FRAC = 0.15


def _concat_copy(list_path: Path, out: Path, expected: float = 0.0,
                 progress_cb: Optional[ProgressCb] = None) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostats",
           "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
           "-f", "concat", "-safe", "0", "-i", str(list_path),
           "-avoid_negative_ts", "make_zero",
           "-c", "copy", "-movflags", "+faststart", str(out)]
    _run_ffmpeg_progress(cmd, expected, progress_cb, "ffmpeg concat 失敗")


def merge(plan: MergePlan, workdir: Path, cfg: dict,
          on_step: Optional[StepCb] = None,
          progress_cb: Optional[StageProgressCb] = None) -> Path:
    """concat demuxer + stream copy 合併；輸出 duration ratio < 閾值 →
    自動逐段 re-encode fallback 再 concat（損壞 NAL 導致 silent truncation 的救援）。

    單檔（plan.single_file）直接回傳來源路徑，不落地複本（R9 唯讀 OK）。
    產物 workdir/merged.mp4 已存在即跳過（resume）。

    progress_cb：merge stage 內百分比 0–100，保證單調不減。stream copy 依
    ffmpeg out_time 對映 0–_COPY_PHASE_FRAC（成功即收尾 100）；fallback 觸發時
    以當下已回報值 p0（≤15）為起點，剩餘區間 [p0,100] 由逐段 re-encode
    （佔 9 成，按各段 duration 加權）與末段 concat（佔 1 成）瓜分，永不倒退。
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "merged.mp4"

    reported = 0.0  # 已回報的最高百分比（單調不減保證）

    def _report(pct: float) -> None:
        nonlocal reported
        reported = max(reported, min(100.0, pct))
        if progress_cb:
            progress_cb(reported)

    if plan.single_file:
        _step(on_step, "單檔資料夾：免合併（決策 C-1）")
        _report(100.0)
        return Path(plan.files[0]["path"])
    if out.exists():
        _step(on_step, f"merged.mp4 已存在，跳過合併（resume）：{out}")
        _report(100.0)
        return out

    input_total = sum(f["duration_sec"] for f in plan.files)
    if input_total <= 0:
        raise PreprocessError("輸入影片總長為 0（來源檔皆無法 probe），中止合併")
    threshold = float(cfg["merge"]["ratio_threshold"])

    src_paths = [f["path"] for f in plan.files]
    list_path = workdir / "concat_list.txt"
    _write_concat_list(src_paths, list_path)

    _step(on_step, f"stream copy 合併 {len(src_paths)} 檔 → {out.name}")
    tmp = workdir / "merged.part.mp4"
    _concat_copy(list_path, tmp, expected=input_total,
                 progress_cb=lambda pct, _t: _report(pct * _COPY_PHASE_FRAC))
    ratio = _probe_duration(tmp) / input_total

    if ratio >= threshold:
        tmp.rename(out)
        _report(100.0)
        _step(on_step, f"合併完成 ratio={ratio:.4f}（閾值 {threshold}）")
        return out

    # ---- fallback：逐段 re-encode 修復（stream copy 遇損壞幀會靜默截斷）----
    _step(on_step, f"ratio={ratio:.4f} < {threshold}：疑似 silent truncation，"
                   f"啟動逐段 re-encode fallback")
    tmp.unlink(missing_ok=True)
    enc_choice = cfg["merge"].get("fallback_encoder", "auto")
    use_nvenc = (enc_choice == "nvenc") or (enc_choice == "auto" and nvenc_available())
    if use_nvenc:
        codec = ["-c:v", "h264_nvenc", "-preset", "p4",
                 "-cq", str(cfg["merge"].get("fallback_cq", 19)), "-b:v", "0"]
    else:
        codec = ["-c:v", "libx264", "-preset", "fast",
                 "-crf", str(cfg["merge"].get("fallback_crf", 18))]

    # 進度對映：剩餘 [p0,100]，re-encode 佔 9 成（按段長加權）、末段 concat 佔 1 成
    p0 = reported
    enc_span = (100.0 - p0) * 0.9

    seg_dir = workdir / "reencoded_parts"
    seg_dir.mkdir(exist_ok=True)
    for stale in seg_dir.glob("part_*.tmp.mp4"):  # 清掉中斷殘留的半成品（resume 安全）
        _step(on_step, f"清除中斷殘留半成品：{stale.name}")
        stale.unlink()

    fixed_paths: list[str] = []
    done_dur = 0.0
    for f in plan.files:
        seg_out = seg_dir / f"part_{f['order']:03d}.mp4"
        seg_dur = float(f["duration_sec"])
        seg_w = seg_dur / input_total * enc_span  # 此段佔全 stage 的進度寬
        if not seg_out.exists():  # 逐段 resume（.mp4 只在成功 rename 後出現 → 可信）
            _step(on_step, f"re-encode [{f['order']}/{len(plan.files)}] {f['name']} "
                           f"({'NVENC' if use_nvenc else 'libx264'})")
            seg_tmp = seg_dir / f"part_{f['order']:03d}.tmp.mp4"
            cmd = ["ffmpeg", "-y", "-v", "error", "-nostats",
                   "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
                   "-i", f["path"], "-avoid_negative_ts", "make_zero",
                   *codec, "-an", str(seg_tmp)]
            base_pct = p0 + done_dur / input_total * enc_span
            _run_ffmpeg_progress(
                cmd, seg_dur,
                lambda pct, _t, b=base_pct, w=seg_w: _report(b + pct / 100.0 * w),
                f"re-encode 失敗（{f['name']}）")
            seg_tmp.rename(seg_out)  # 原子性：成功才轉正，殘檔不會被 resume 誤認
        fixed_paths.append(str(seg_out))
        done_dur += seg_dur
        _report(p0 + done_dur / input_total * enc_span)

    _write_concat_list(fixed_paths, list_path)
    _concat_copy(list_path, tmp, expected=input_total,
                 progress_cb=lambda pct, _t: _report(
                     p0 + enc_span + pct / 100.0 * (100.0 - p0 - enc_span)))
    ratio = _probe_duration(tmp) / input_total
    if ratio < threshold:
        raise PreprocessError(
            f"re-encode fallback 後 ratio 仍 {ratio:.4f} < {threshold}，"
            f"來源檔可能嚴重損壞，請人工檢查：{plan.folder}")
    tmp.rename(out)
    _report(100.0)
    _step(on_step, f"fallback 合併完成 ratio={ratio:.4f}")
    return out


# --------------------------------------------------------------- detect_oob ---

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@functools.lru_cache(maxsize=8)
def _sha256_cached(path: str, size: int, mtime_ns: int) -> str:
    """同一檔（路徑＋大小＋mtime 不變）在本程序內只算一次 sha256（68 MB 約 0.2 秒）。"""
    return _sha256(Path(path))


def _file_sha256(path: Path) -> str:
    st = path.stat()
    return _sha256_cached(str(path), st.st_size, st.st_mtime_ns)


class OobModel(NamedTuple):
    """resolve_oob_model 的結果（可直接 tuple 拆包：path, source, sha_ok = ...）。

    source：'override'（data 根的 onnx_path，使用者自訂／替換）| 'bundled'（隨 repo 附帶）
    sha_ok：True/False＝與 onnx_sha256 比對結果；None＝未比對（verify_sha=False 或未設 onnx_sha256）
    """
    path: Path
    source: Literal["override", "bundled"]
    sha_ok: Optional[bool]


def resolve_oob_model(cfg: Optional[dict] = None, *, verify_sha: bool = True) -> OobModel:
    """解析實際要用的 OOB ONNX 模型（preprocess / jobs / doctor / fetch-models 共用單一函式）。

    規則（覆寫預設關閉，避免舊副本遮住新版附帶模型）：
      - oob.onnx_path 有設定 → 檔案存在 → source='override'；不存在 → raise（不退回附帶模型）
      - oob.onnx_path 未設定 → oob.onnx_bundled（相對 pipeline/ 套件目錄）→ source='bundled'；不存在 → raise
    sha 政策（由呼叫端執行，這裡只回報 sha_ok）：bundled 不符＝錯誤；override 不符＝僅警告。
    cfg 省略時自行讀 preprocess.yaml。只讀檔、不跑推論、不檢查授權同意（見 config.oob_license_accepted）。
    """
    if cfg is None:
        cfg = config.load_config("preprocess.yaml")
    ocfg = cfg.get("oob") or {}
    override_raw = str(ocfg.get("onnx_path") or "").strip()
    bundled_raw = str(ocfg.get("onnx_bundled") or "").strip()
    if not override_raw and not bundled_raw:
        raise PreprocessError(
            "preprocess.yaml 的 oob 區未設定 onnx_bundled / onnx_path（OOB ONNX 模型路徑）"
            "\n      → 對照 repo 內 pipeline/configs/preprocess.yaml 範本補上")
    if override_raw:
        # 明確設定自訂模型：找不到就報錯，不靜默退回附帶模型
        p = config.data_path(override_raw)
        if not p.is_file():
            raise PreprocessError(
                f"preprocess.yaml oob.onnx_path 指定的自訂 OOB 模型不存在：{p}"
                "\n      → 確認路徑（相對 data 根或絕對路徑）；若要改回附帶模型，把 oob.onnx_path 留空")
        found = (p, "override")
    else:
        p = config.package_path(bundled_raw)
        if not p.is_file():
            raise PreprocessError(
                f"找不到附帶的 OOB ONNX 模型：{p}"
                "\n      → 附帶模型應隨 repo 一起取得：在 repo 根執行 `git status pipeline/models/oob/` "
                "確認未被刪除，必要時 `git checkout -- pipeline/models/oob/oobnet.onnx` 還原")
        found = (p, "bundled")
    path, source = found
    want = str(ocfg.get("onnx_sha256") or "").strip().lower()
    sha_ok: Optional[bool] = None
    if verify_sha and want:
        sha_ok = _file_sha256(path) == want
    return OobModel(path, source, sha_ok)  # type: ignore[arg-type]


# 補救指令的單一指路（避免多處 hard-code 字串）
_FETCH_CMD = "surgann fetch-models --only oob"
_LICENSE = "授權 CC BY-NC-SA 4.0，僅限非商業研究"


def _license_problem() -> Optional[str]:
    """OOB 授權同意閘門：未同意 → 可照抄的補救訊息；已同意 → None。"""
    if config.oob_license_accepted():
        return None
    val, src = config.oob_license_accepted_value()
    why = (f"{config.OOB_LICENSE_ENV_KEY}={val!r}（來自 {src}）不是合法 ISO 日期（YYYY-MM-DD）"
           if val else f"尚未同意 OOB 模型授權（{config.OOB_LICENSE_ENV_KEY} 未設定）")
    return (f"{why}。OOB 模型雖已隨 repo 附帶，但使用前必須先閱讀並同意授權（{_LICENSE}）。"
            f"\n      → {_FETCH_CMD}"
            f"\n        （顯示授權全文，同意後寫入 deploy/.env 的 "
            f"{config.OOB_LICENSE_ENV_KEY}=<今天日期>；完成後重跑本指令）")


def oob_model_warning(model: OobModel) -> Optional[str]:
    if model.source == "override" and model.sha_ok is False:
        return (f"正在使用自訂 OOB 模型（data 根的 oob.onnx_path）：{model.path}，"
                f"其 sha256 與附帶模型不同——若非刻意替換模型，請刪除該檔改用附帶模型")
    return None


def describe_oob_model(cfg: dict) -> str:
    """一行人看的模型說明（CLI --check / 推論 log 用）；解析失敗時回傳錯誤字串。"""
    try:
        m = resolve_oob_model(cfg)
    except PreprocessError as e:
        return str(e)
    label = {"override": "自訂（data 根 onnx_path）", "bundled": "附帶（隨 repo）"}[m.source]
    sha = {True: "sha256 相符", False: "sha256 不符", None: "未驗 sha256"}[m.sha_ok]
    return f"OOB 模型：{label} {m.path}（{sha}）"


def preflight_oob(cfg: dict) -> list[str]:
    """OOB stage 開跑前一次驗完相依，回傳「可照抄補救」的問題清單（空 = 通過）。

    逐項檢查並各附怎麼補：
      1) 主 venv 可 import onnxruntime
      2) 已同意 OOB 模型授權（config.oob_license_accepted；deploy/.env OOB_LICENSE_ACCEPTED）
      3) 模型可解析（resolve_oob_model：onnx_path 有設定才用自訂，否則附帶 onnx_bundled）
      4) 附帶模型 sha256 必須等於 onnx_sha256（自訂覆寫檔不符只警告，不列為問題）
    只做檢查、不執行推論；供 CLI --check、job worker 起 OOB 前、doctor 共用（R4）。
    """
    problems: list[str] = []
    import importlib.util
    if importlib.util.find_spec("onnxruntime") is None:
        problems.append(
            "主 venv 沒有 onnxruntime。"
            "\n      → 依安裝配方重裝套件（GPU 機：onnxruntime-gpu；無 GPU：onnxruntime）")
    lic = _license_problem()
    if lic:
        problems.append(lic)
    ocfg = cfg["oob"]
    if ocfg.get("weights_path") and not (ocfg.get("onnx_path") or ocfg.get("onnx_bundled")):
        problems.append(
            "preprocess.yaml 仍是舊鍵 oob.weights_path（TensorFlow .h5，新版已不再使用）。"
            "\n      → 改用 oob.onnx_bundled / oob.onnx_path（見 repo 內 preprocess.yaml 範本）")
        return problems
    try:
        model = resolve_oob_model(cfg)
    except PreprocessError as e:
        problems.append(str(e))
        return problems
    if model.source == "bundled" and model.sha_ok is False:
        problems.append(
            f"附帶 OOB ONNX 模型 sha256 不符（檔案損毀或被改動）：{model.path}"
            f"\n      實際 {_file_sha256(model.path)}"
            f"\n      預期 {str(ocfg.get('onnx_sha256')).strip().lower()}"
            f"\n      → 在 repo 根執行 `git checkout -- pipeline/models/oob/oobnet.onnx` 還原")
    return problems


def ensure_oob_ready(cfg: dict) -> None:
    """preflight_oob 的 raise 版：任何一項不過即以單一、可照抄訊息 raise（R10 薄入口用）。"""
    problems = preflight_oob(cfg)
    if problems:
        raise PreprocessError(
            "OOB 前處理環境預檢未通過，請先補齊以下項目再重跑：\n  - "
            + "\n  - ".join(problems))


def detect_oob(video: Path, workdir: Path, cfg: dict,
               on_step: Optional[StepCb] = None,
               progress_cb: Optional[StageProgressCb] = None) -> Path:
    """在主 venv 內直接呼叫 oob_infer.run()（onnxruntime；GPU 可用則用，否則 CPU）
    做低頻取樣推論 → workdir/oob.csv。

    CSV 快取存在即跳過（resume）。輸出格式 FRAME_ID,OOB,TIME_SEC
    （FRAME_ID = 來源實際幀序號，與既有全幀版 CSV 向下相容）。
    progress_cb：oob stage 內百分比 0–100（單調不減）。
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    csv_path = workdir / "oob.csv"
    if csv_path.exists():
        _step(on_step, f"oob.csv 已存在，跳過推論（resume）：{csv_path}")
        if progress_cb:
            progress_cb(100.0)
        return csv_path

    ocfg = cfg["oob"]
    # 授權閘門與附帶檔 sha 在推論入口再驗一次：任何呼叫端都不可能繞過預檢而靜默跑 OOB
    lic = _license_problem()
    if lic:
        raise PreprocessError(lic)
    m = resolve_oob_model(cfg)
    if m.source == "bundled" and m.sha_ok is False:
        raise PreprocessError(
            f"附帶 OOB ONNX 模型 sha256 不符：{m.path}"
            f"\n  → 在 repo 根執行 `git checkout -- pipeline/models/oob/oobnet.onnx` 還原")
    _step(on_step, describe_oob_model(cfg))
    warn = oob_model_warning(m)
    if warn:
        _step(on_step, f"⚠ {warn}")
    model = m.path
    tmp = workdir / "oob.csv.part"
    tmp.unlink(missing_ok=True)
    device = str(ocfg.get("device") or "auto")
    _step(on_step, f"OOB 推論（取樣 {ocfg['sample_fps']} fps，onnxruntime device={device}）"
                   f"→ {csv_path.name}")

    best = 0.0  # 單調不減保證

    def _progress(pct: float) -> None:
        nonlocal best
        best = max(best, min(100.0, float(pct)))
        if progress_cb:
            progress_cb(best)

    try:
        oob_infer.run(str(video), str(model), str(tmp), float(ocfg["sample_fps"]),
                      device=device, threads=int(ocfg.get("threads") or 0),
                      gpu_mem_limit_mb=int(ocfg.get("gpu_mem_limit_mb") or 0),
                      progress_cb=_progress,
                      log_cb=lambda line: _step(on_step, f"  [oob] {line}"))
    except oob_infer.OOBInferError as e:
        tmp.unlink(missing_ok=True)
        raise PreprocessError(f"OOB 推論失敗：{e}") from e
    if not tmp.exists():
        raise PreprocessError("OOB 推論未產出 CSV")
    tmp.rename(csv_path)  # 原子性快取：成功才轉正，避免半成品被 resume 誤用
    if progress_cb:
        progress_cb(100.0)
    return csv_path


# ---------------------------------------------------------- derive_segments ---

def _read_oob_csv(oob_csv: Path,
                  fps: Optional[float] = None) -> tuple[list[float], list[float]]:
    """讀 OOB 機率 CSV → (times, probs)，derive_segments / segment_prob_stats 共用。

    相容兩種格式：新版含 TIME_SEC 直接用；舊版全幀 CSV 需傳 fps 換算。
    """
    times: list[float] = []
    probs: list[float] = []
    with open(oob_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        has_time = reader.fieldnames and "TIME_SEC" in reader.fieldnames
        if not has_time and not fps:
            raise PreprocessError(
                f"CSV 無 TIME_SEC 欄位（舊全幀格式），需提供 fps 參數：{oob_csv}")
        for row in reader:
            times.append(float(row["TIME_SEC"]) if has_time
                         else int(row["FRAME_ID"]) / fps)
            probs.append(float(row["OOB"]))
    if not times:
        raise PreprocessError(f"OOB CSV 無資料列：{oob_csv}")
    return times, probs


def derive_segments(oob_csv: Path, cfg: dict, fps: Optional[float] = None,
                    duration: Optional[float] = None) -> list[tuple[float, float]]:
    """解析 OOB 機率 CSV → 保留的 in-body 段 [(start_sec, end_sec), …]。

    threshold / min_segment_sec / boundary_pad_sec 皆取自 cfg（R5）。
    切除段（OOB）先往外各擴 boundary_pad_sec（隱私優先），再取補集為 in-body，
    短於 min_segment_sec 的 in-body 段視為雜訊丟棄。

    相容兩種 CSV：新版含 TIME_SEC 直接用；舊版全幀 CSV 需傳 fps 換算。
    duration 給定時，末段 in-body 上界 clamp 到影片總長。
    """
    ocfg = cfg["oob"]
    threshold = float(ocfg["threshold"])
    min_seg = float(ocfg["min_segment_sec"])
    pad = float(ocfg["boundary_pad_sec"])

    times, probs = _read_oob_csv(oob_csv, fps=fps)

    # 取樣間隔（一個樣本代表往後 dt 秒）
    dt = (times[-1] - times[0]) / (len(times) - 1) if len(times) > 1 else 1.0
    total = duration if duration is not None else times[-1] + dt

    # 1) OOB 樣本連續段 → 原始 OOB 區間
    oob_raw: list[list[float]] = []
    for t, p in zip(times, probs):
        if p >= threshold:
            if oob_raw and t - oob_raw[-1][1] <= dt * 0.5:  # 相鄰取樣點 → 延伸
                # （中間隔著 in-body 樣本時不合併；短 in-body 段交由
                #   min_segment_sec 與 boundary_pad 處理，語意不因取樣頻率而變）
                oob_raw[-1][1] = t + dt
            else:
                oob_raw.append([t, t + dt])

    # 2) 外擴 padding + 合併重疊
    oob_seg: list[list[float]] = []
    for s, e in oob_raw:
        s, e = max(0.0, s - pad), min(total, e + pad)
        if oob_seg and s <= oob_seg[-1][1]:
            oob_seg[-1][1] = max(oob_seg[-1][1], e)
        else:
            oob_seg.append([s, e])

    # 3) 補集 = in-body；濾除過短雜訊段
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in oob_seg + [[total, total]]:
        if s - cursor >= min_seg:
            keep.append((round(cursor, 3), round(s, 3)))
        cursor = max(cursor, e)
    return keep


# ------------------------------------------------------- segment_prob_stats ---

def segment_prob_stats(oob_csv: Path, segments,
                       fps: Optional[float] = None) -> list[dict]:
    """對每個建議切除段計算 OOB 機率統計，供 QC modal 顯示信心值。

    回傳 list 與 segments 逐 index 對齊，每項：
      {"start", "end", "prob_median", "prob_max", "n_samples"}
    統計取樣＝段（外擴後）時間區間 [start, end] 內 oob.csv 的所有樣本；
    段內無樣本時（極端短段）median/max 取距離區間最近的一筆，n_samples=0。
    """
    times, probs = _read_oob_csv(oob_csv, fps=fps)
    out: list[dict] = []
    for seg in segments:
        s, e = float(seg[0]), float(seg[1])
        vals = [p for t, p in zip(times, probs) if s <= t <= e]
        if vals:
            med, mx, n = statistics.median(vals), max(vals), len(vals)
        else:  # 段內無樣本 → 取距離 [s, e] 最近的一筆（區間外距離取兩端較近者）
            i = min(range(len(times)),
                    key=lambda k: min(abs(times[k] - s), abs(times[k] - e)))
            med = mx = probs[i]
            n = 0
        out.append({"start": s, "end": e,
                    "prob_median": round(float(med), 3),
                    "prob_max": round(float(mx), 3),
                    "n_samples": n})
    return out


# ---------------------------------------------------------- probe_fast_path ---

def probe_fast_path(video: Path, cfg: dict,
                    segments: Optional[list[tuple[float, float]]] = None) -> bool:
    """已 1280x720 / 25fps 嚴格 CFR h264 且無切割需求 → True（可走 remux fast path）。

    CFR / 解析度 / 幀數判定重用 ingest.verify（R4）——fast path 產物必須能過
    ingest 驗證，這裡先用同一把尺量輸入。
    """
    ecfg = cfg["encode"]
    try:
        info = ingest.probe(video)
    except ingest.IngestError:
        return False
    if info.get("codec") != "h264" or info.get("height") != int(ecfg["height"]):
        return False

    ingest_verify_cfg = config.load_config("ingest.yaml")["verify"]
    if not ingest.verify(video, ingest_verify_cfg).ok:
        return False

    if segments is not None:
        dur = info["duration"]
        covered = sum(e - s for s, e in segments)
        if abs(covered - dur) > 0.5 or len(segments) != 1:  # 有任何切割需求即否
            return False
    return True


def remux(video: Path, out: Path, cfg: dict) -> Path:
    """fast path：stream copy remux + metadata 全剝 + faststart + 去音軌（不重編碼）。"""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video),
           "-c", "copy", "-an",
           "-map_metadata", "-1", "-map_chapters", "-1", "-disposition", "0",
           "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise PreprocessError(f"remux 失敗：{r.stderr.strip()[-800:]}")
    return out


# ---------------------------------------------------------------- cut_encode --

ENCODERS = ("auto", "nvenc", "libx264")


def resolve_encoder(cfg: dict) -> str:
    """encode.encoder → 實際使用的編碼器（'nvenc' | 'libx264'）。

    auto：nvenc_available() 為真用 NVENC，否則 libx264（無 NVIDIA 卡也能入庫）。
    nvenc / libx264：照設定（nvenc 無卡時由 ffmpeg 報錯，不靜默降級）。
    """
    enc = str(cfg["encode"].get("encoder") or "auto").strip().lower()
    if enc not in ENCODERS:
        raise PreprocessError(f"preprocess.yaml encode.encoder 只能是 {ENCODERS}：{enc}")
    if enc == "auto":
        return "nvenc" if nvenc_available() else "libx264"
    return enc


def _encoder_args(cfg: dict) -> list[str]:
    ecfg = cfg["encode"]
    common = ["-pix_fmt", str(ecfg["pix_fmt"]),
              "-g", str(ecfg["gop"]), "-keyint_min", str(ecfg["gop"]),
              "-sc_threshold", "0"]
    if resolve_encoder(cfg) == "nvenc":
        n = ecfg["nvenc"]
        args = ["-c:v", "h264_nvenc", "-preset", str(n["preset"]),
                "-rc", "vbr", "-cq", str(n["cq"]), "-b:v", "0",
                "-no-scenecut", "1", *common]
        if n.get("maxrate"):  # 碼率上限：保護串流頻寬與磁碟（實測定案）
            args += ["-maxrate", str(n["maxrate"]),
                     "-bufsize", str(n.get("bufsize", "16M"))]
        if n.get("multipass"):
            args += ["-multipass", str(n["multipass"])]
        return args
    x = ecfg["x264"]
    args = ["-c:v", "libx264", "-preset", str(x["preset"]),
            "-crf", str(x["crf"]), *common]
    if int(x.get("threads", 0)) > 0:  # 上限 CPU 執行緒，保留資源給標註服務
        args += ["-threads", str(x["threads"])]
    return args


def cut_encode(video: Path, segments: Optional[list[tuple[float, float]]],
               out: Path, cfg: dict,
               progress_cb: Optional[ProgressCb] = None) -> Path:
    """★單一 ffmpeg pass：filter_complex trim/concat 切除 OOB 段 →
    scale=-2:720:lanczos,fps=25 → 1s GOP → -an → metadata 全剝 → faststart。

    segments=None 或空 → 不切割，整支標準化。
    -progress pipe:1 解析 out_time 提供進度 callback（供之後 job worker 接進度）。
    """
    ecfg = cfg["encode"]
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    vf_std = f"scale=-2:{ecfg['height']}:flags=lanczos,fps={ecfg['fps']}"
    if segments:
        parts, labels = [], []
        for i, (s, e) in enumerate(segments):
            parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},"
                         f"setpts=PTS-STARTPTS[s{i}]")
            labels.append(f"[s{i}]")
        parts.append(f"{''.join(labels)}concat=n={len(segments)}:v=1:a=0[cat]")
        parts.append(f"[cat]{vf_std}[vout]")
        filter_complex = ";".join(parts)
        expected = sum(e - s for s, e in segments)
        vf_args = ["-filter_complex", filter_complex, "-map", "[vout]"]
    else:
        expected = _probe_duration(video)
        vf_args = ["-vf", vf_std]

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-nostats",
           "-i", str(video), *vf_args,
           *_encoder_args(cfg), "-an",
           "-map_metadata", "-1", "-map_chapters", "-1", "-disposition", "0",
           "-movflags", "+faststart", str(out)]
    _run_ffmpeg_progress(cmd, expected, progress_cb, "cut_encode ffmpeg 失敗")
    if progress_cb:
        progress_cb(100.0, expected)
    return out


# -------------------------------------------------------------- verify_output -

@dataclass
class PreprocessReport:
    ok: bool
    duration: float
    expected_duration: float
    duration_diff_pct: float
    verify: ingest.VerifyResult
    problems: list[str] = field(default_factory=list)


def verify_output(final: Path, segments: Optional[list[tuple[float, float]]],
                  cfg: dict) -> PreprocessReport:
    """輸出 duration ≈ in-body 段總長 ±cfg 容差 ＋ ingest 現行 CFR/幀數檢查（重用，R4）。"""
    ingest_verify_cfg = config.load_config("ingest.yaml")["verify"]
    vr = ingest.verify(final, ingest_verify_cfg)
    problems = list(vr.problems)

    expected = sum(e - s for s, e in segments) if segments else vr.duration
    tol_pct = float(cfg["verify"]["duration_tolerance_pct"])
    diff_pct = (abs(vr.duration - expected) / expected * 100.0) if expected > 0 else 0.0
    if diff_pct > tol_pct:
        problems.append(
            f"duration {vr.duration:.2f}s 與 in-body 總長 {expected:.2f}s "
            f"差 {diff_pct:.2f}% > ±{tol_pct}%")
    return PreprocessReport(ok=not problems, duration=vr.duration,
                            expected_duration=expected, duration_diff_pct=diff_pct,
                            verify=vr, problems=problems)


# ------------------------------------------------------------------ 主流程 ----

@dataclass
class PreprocessOutcome:
    plan: MergePlan
    merged: Path
    oob_csv: Optional[Path]
    segments: list[tuple[float, float]]
    final: Path
    fast_path: bool
    report: PreprocessReport


def run_preprocess(folder: Path, workdir: Path, out: Optional[Path] = None,
                   cfg: Optional[dict] = None, plan: Optional[MergePlan] = None,
                   on_step: Optional[StepCb] = None,
                   progress_cb: Optional[ProgressCb] = None) -> PreprocessOutcome:
    """病人資料夾 → 合併 → OOB → 切除+標準化（或 fast path remux）→ 驗證。

    plan 可由呼叫端（UI 人工調序後）傳入；未給則以 plan_merge 自動排序。
    最終檔預設 workdir/final.mp4；驗證不過即 raise，不產出可入庫產物。
    """
    cfg = cfg or config.load_config("preprocess.yaml")
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out = Path(out) if out else workdir / "final.mp4"

    plan = plan or plan_merge(folder, cfg)
    merged = merge(plan, workdir, cfg, on_step=on_step)

    oob_csv = detect_oob(merged, workdir, cfg, on_step=on_step)
    info = ingest.probe(merged)
    fps = ingest._parse_rate(info["r_frame_rate"])
    segments = derive_segments(oob_csv, cfg, fps=fps, duration=info["duration"])
    if not segments:
        raise PreprocessError(
            "OOB 偵測後沒有任何 in-body 段（整支都被判為體外），"
            "請人工檢查 oob.csv 與來源影片")
    removed = info["duration"] - sum(e - s for s, e in segments)
    _step(on_step, f"in-body {len(segments)} 段，切除 {removed:.1f}s "
                   f"({removed / info['duration'] * 100 if info['duration'] else 0:.1f}%)")

    fast = probe_fast_path(merged, cfg, segments=segments)
    if fast:
        _step(on_step, "fast path：已 720p/25 CFR 且無切割段 → remux（不重編碼）")
        remux(merged, out, cfg)
        report = verify_output(out, segments, cfg)
        if not report.ok:  # 例如 remux 後 CFR 判定失敗 → 退回重編碼
            _step(on_step, "fast path 產物未過驗證，退回完整重編碼：\n  - "
                           + "\n  - ".join(report.problems))
            fast = False
    if not fast:
        _step(on_step, f"cut_encode（encoder={cfg['encode']['encoder']}→{resolve_encoder(cfg)}，"
                       f"單一 pass）→ {out}")
        cut_encode(merged, segments, out, cfg, progress_cb=progress_cb)
        report = verify_output(out, segments, cfg)
        if not report.ok:
            raise PreprocessError("最終驗證未通過：\n  - " + "\n  - ".join(report.problems))

    return PreprocessOutcome(plan=plan, merged=merged, oob_csv=oob_csv,
                             segments=segments, final=out, fast_path=fast,
                             report=report)
