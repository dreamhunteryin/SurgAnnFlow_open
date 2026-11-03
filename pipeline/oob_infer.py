#!/usr/bin/env python3
"""OOBNet out-of-body 推論（onnxruntime，主 venv 內執行；不需要 TensorFlow）。

兩種用法：
- 由 pipeline/preprocess.py 的 detect_oob() 直接 import 呼叫 `run(...)`（進度走 callback）。
- 獨立 CLI（除錯/手動重跑）：
      python -m pipeline.oob_infer --video in.mp4 --model oobnet.onnx --out oob.csv [--device auto]
  stdout 印 `PROGRESS <pct>` 行（節流：增量 ≥1% 或每 5 秒心跳）與統計行。
- `--self-test [--model x.onnx]`：印 onnxruntime 版本/可用 provider，給模型時試載＋一次前向。

設計要點：
- 模型：`pipeline/oob_export_onnx.py` 轉出的序列版 OOBNet——輸入 frames[1, T, 64, 64, 3] float32
  （RGB、bilinear 縮到 64×64、x/127.5-1），輸出 oob_prob[1, T, 1]。LSTM 狀態在圖內，
  **整段影片的取樣幀必須一次餵入**（分 chunk 會截斷 LSTM 記憶，結果與原 TF stateful 模型不同）。
  記憶體：每取樣幀 48 KB，1 fps 取樣 2 小時 ≈ 350 MB 輸入。
- 裝置（GPU/CPU 自動降級）：device=auto → onnxruntime 有 CUDAExecutionProvider 就用 GPU，否則 CPU；
  GPU 推論失敗（如顯存不足）自動改 CPU 重跑一次。device=cpu 強制 CPU；device=cuda 只用 GPU（失敗即報錯）。
  `CUDA_VISIBLE_DEVICES=""` 時 CUDA provider 初始化失敗 → 自動走 CPU。
- 低頻取樣：cv2 grab() 逐幀跳過、只 read() 取樣幀，不落地圖檔。
- 前處理與原 TF 路徑對齊：tf.image.resize 預設＝bilinear、half-pixel、無 antialias；
  cv2.resize INTER_LINEAR 對 float32 輸入為同一公式（先轉 float32 再縮，避免 uint8 定點捨入）。
- 輸出 CSV 欄位 FRAME_ID,OOB,TIME_SEC：FRAME_ID 為來源影片實際幀序號（與舊全幀版相容），
  TIME_SEC 為秒數。格式與 TF 版完全相同。

授權註記：
    OOBNet 模型架構與預訓練權重 (c) Research Group CAMMA, University of
    Strasbourg / IHU Strasbourg（https://github.com/CAMMA-public/out-of-body-detector）。
    權重授權 **CC BY-NC-SA 4.0 —— 僅限非商業研究使用**；本平台若商業化必須更換模型。
    oobnet.onnx 為同一權重的衍生檔。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from typing import Callable, Optional

import cv2
import numpy as np

INPUT_SHAPE = (64, 64)      # OOBNet 固定輸入尺寸 (H, W)
INPUT_NAME = "frames"
OUTPUT_NAME = "oob_prob"
DEVICES = ("auto", "cuda", "cpu")

# 進度節流（CLI stdout 的 PROGRESS 行與 callback 共用）：增量 ≥1% 或距上次 ≥5 秒
PROGRESS_MIN_DELTA_PCT = 1.0
PROGRESS_MIN_INTERVAL_SEC = 5.0
# 進度分配：解碼/取樣占 0–DECODE_SHARE（逐幀可估），最後整段推論占其餘（單次呼叫，無中間進度）
DECODE_SHARE = 90.0

ProgressCb = Callable[[float], None]   # stage 內百分比 0–100，保證單調不減
LogCb = Callable[[str], None]


class OOBInferError(RuntimeError):
    """可預期的推論錯誤（影片打不開、模型缺、無幀、裝置不可用）。"""


def preprocess_bgr(frame_bgr: np.ndarray) -> np.ndarray:
    """單幀 BGR uint8 → [64, 64, 3] float32（RGB、bilinear 縮放、x/127.5-1；對齊 TF 前處理）。"""
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB).astype(np.float32)
    small = cv2.resize(rgb, (INPUT_SHAPE[1], INPUT_SHAPE[0]), interpolation=cv2.INTER_LINEAR)
    return small / 127.5 - 1.0


def _providers_for(device: str) -> list[str]:
    import onnxruntime as ort
    if device not in DEVICES:
        raise OOBInferError(f"oob device 只能是 {DEVICES}：{device}")
    avail = ort.get_available_providers()
    if device == "auto" and os.environ.get("CUDA_VISIBLE_DEVICES", None) in ("", "-1"):
        return ["CPUExecutionProvider"]   # 明確隱藏 GPU → 直接 CPU（免去 CUDA 初始化失敗的雜訊）
    if device == "cpu":
        return ["CPUExecutionProvider"]
    if "CUDAExecutionProvider" in avail:
        return (["CUDAExecutionProvider"] if device == "cuda"
                else ["CUDAExecutionProvider", "CPUExecutionProvider"])
    if device == "cuda":
        raise OOBInferError(f"device=cuda 但 onnxruntime 無 CUDAExecutionProvider（可用：{avail}）")
    return ["CPUExecutionProvider"]


def load_session(model_path: str, device: str = "auto", threads: int = 0,
                 gpu_mem_limit_mb: int = 0):
    """建立 onnxruntime session。回傳 (session, 實際使用的主 provider 名)。

    threads：CPU intra-op 執行緒上限（0＝onnxruntime 預設全核）。
    gpu_mem_limit_mb：CUDA arena 上限（0＝不限）。OOB 與 SAM 同程序共用一張卡時，
      設上限可避免整段序列推論吃光顯存；超過即 OOM → run() 自動改 CPU。
    """
    import onnxruntime as ort
    if not os.path.isfile(model_path):
        raise OOBInferError(f"OOB ONNX 模型不存在：{model_path}")
    ort.set_default_logger_severity(3)   # 降級時 CUDA 載入失敗的 E/W 訊息不灌 log（只留 error 以上）
    so = ort.SessionOptions()
    so.log_severity_level = 3
    if threads > 0:
        so.intra_op_num_threads = threads
    providers = _providers_for(device)
    if providers[0] == "CUDAExecutionProvider" and hasattr(ort, "preload_dlls"):
        # 系統無 CUDA toolkit 時，CUDA provider 需要先載入 pip nvidia-* wheels 的
        # cuBLAS/cuDNN .so（onnxruntime ≥1.21 內建；找不到就交由 ORT 降級/報錯）
        try:
            ort.preload_dlls()
        except Exception:  # noqa: BLE001
            pass
    if providers[0] == "CUDAExecutionProvider":
        opts = {"arena_extend_strategy": "kSameAsRequested"}
        if gpu_mem_limit_mb > 0:
            opts["gpu_mem_limit"] = str(int(gpu_mem_limit_mb) * 1024 * 1024)
        providers = [("CUDAExecutionProvider", opts), *providers[1:]]
    try:
        sess = ort.InferenceSession(model_path, sess_options=so, providers=providers)
    except Exception as e:  # CUDA 初始化失敗（無卡、CUDA_VISIBLE_DEVICES=""、驅動不符）
        if device == "auto" and providers[0] != "CPUExecutionProvider":  # 有 CUDA 項 → 降 CPU
            sess = ort.InferenceSession(model_path, sess_options=so,
                                        providers=["CPUExecutionProvider"])
        else:
            raise OOBInferError(f"onnxruntime 載入模型失敗（{providers}）：{e}") from e
    return sess, sess.get_providers()[0]


def predict(session, frames: np.ndarray) -> np.ndarray:
    """[T, 64, 64, 3] float32 已前處理 → [T] 體外機率（整段一次推論）。"""
    x = np.ascontiguousarray(frames, dtype=np.float32)[None]  # [1, T, 64, 64, 3]
    out = session.run([OUTPUT_NAME], {INPUT_NAME: x})[0]
    return out.reshape(-1).astype(np.float32)


def sample_video(video_path: str, sample_fps: float,
                 progress_cb: Optional[ProgressCb] = None,
                 log_cb: Optional[LogCb] = None):
    """低頻取樣解碼 → (frame_ids, times_sec, frames[T,64,64,3] float32, src_fps, n_frames)。

    取樣規則與 TF 版相同：step = round(src_fps / sample_fps)，取 idx % step == 0 的幀。
    progress_cb 收到 0–DECODE_SHARE 的百分比（已節流、單調不減）。
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise OOBInferError(f"無法開啟影片：{video_path}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if src_fps <= 0:
        cap.release()
        raise OOBInferError(f"影片 fps 無效：{video_path}")
    step = max(1, int(round(src_fps / sample_fps))) if sample_fps > 0 else 1
    est = max(1, (n_frames + step - 1) // step) if n_frames > 0 else 0
    if log_cb:
        log_cb(f"start video={os.path.basename(video_path)} src_fps={src_fps:.3f} "
               f"n_frames={n_frames} step={step} est_samples={est}")

    ids: list[int] = []
    frames: list[np.ndarray] = []
    idx = 0
    t0 = time.time()
    last_pct, last_t = -1.0, t0
    while True:
        if idx % step == 0:
            ok, frame = cap.read()
            if not ok:
                break
            ids.append(idx)
            frames.append(preprocess_bgr(frame))
            if log_cb and len(frames) % 600 == 0:  # 1 fps 時每 10 分鐘影片回報一次
                log_cb(f"samples={len(frames)} "
                       f"samples_per_sec={len(frames) / max(time.time() - t0, 1e-6):.1f}")
        elif not cap.grab():
            break
        idx += 1
        if progress_cb and n_frames > 0:
            pct = min(DECODE_SHARE, idx / n_frames * DECODE_SHARE)
            now = time.time()
            if pct - last_pct >= PROGRESS_MIN_DELTA_PCT or now - last_t >= PROGRESS_MIN_INTERVAL_SEC:
                progress_cb(pct)
                last_pct, last_t = pct, now
    cap.release()
    if not frames:
        raise OOBInferError(f"影片解不出任何幀：{video_path}")
    fid = np.asarray(ids, dtype=np.int64)
    return fid, fid / src_fps, np.stack(frames).astype(np.float32), src_fps, n_frames


def write_csv(out_csv: str, frame_ids, times, probs) -> None:
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["FRAME_ID", "OOB", "TIME_SEC"])
        for fid, t, p in zip(frame_ids, times, probs):
            w.writerow([int(fid), f"{float(p):.6g}", f"{float(t):.3f}"])


def run(video_path: str, model_path: str, out_csv: str, sample_fps: float = 1.0, *,
        device: str = "auto", threads: int = 0, gpu_mem_limit_mb: int = 0,
        progress_cb: Optional[ProgressCb] = None,
        log_cb: Optional[LogCb] = None) -> dict:
    """影片 → 取樣 → 整段 ONNX 推論 → CSV。回傳統計 dict（samples / provider / elapsed_sec / …）。

    progress_cb(pct)：0–100、單調不減；log_cb(msg)：人讀的統計行。錯誤以 OOBInferError 拋出。
    device=auto 時 GPU 推論失敗（如顯存不足）會自動以 CPU 重跑一次。
    """
    session, provider = load_session(model_path, device, threads, gpu_mem_limit_mb)
    t0 = time.time()
    fid, times, frames, src_fps, n_frames = sample_video(video_path, sample_fps, progress_cb, log_cb)
    t_decode = time.time() - t0
    if progress_cb:
        progress_cb(DECODE_SHARE)
    t1 = time.time()
    try:
        probs = predict(session, frames)
    except Exception as e:
        if device != "auto" or provider == "CPUExecutionProvider":
            raise OOBInferError(f"OOB 推論失敗（{provider}）：{e}") from e
        if log_cb:
            log_cb(f"GPU 推論失敗（{type(e).__name__}），改用 CPU 重跑：{str(e)[:200]}")
        del session
        session, provider = load_session(model_path, "cpu", threads)
        probs = predict(session, frames)
    t_infer = time.time() - t1
    write_csv(out_csv, fid, times, probs)
    if progress_cb:
        progress_cb(100.0)
    stats = {"samples": int(len(fid)), "src_fps": src_fps, "n_frames": n_frames,
             "decode_sec": round(t_decode, 2), "infer_sec": round(t_infer, 2),
             "elapsed_sec": round(time.time() - t0, 2), "provider": provider}
    if log_cb:
        log_cb(f"done samples={stats['samples']} decode_sec={stats['decode_sec']} "
               f"infer_sec={stats['infer_sec']} elapsed_sec={stats['elapsed_sec']} "
               f"provider={provider} out={out_csv}")
    return stats


def self_test(model_path: Optional[str], device: str = "auto") -> int:
    import onnxruntime as ort
    print(f"onnxruntime {ort.__version__}  available_providers={ort.get_available_providers()}  "
          f"opencv {cv2.__version__}  numpy {np.__version__}")
    if model_path:
        try:
            s, provider = load_session(model_path, device)
        except OOBInferError as e:
            print(f"error={e}", file=sys.stderr)
            return 2
        meta = s.get_inputs()[0]
        print(f"model_ok={model_path} provider={provider} input={meta.name}{meta.shape} "
              f"output={s.get_outputs()[0].name}{s.get_outputs()[0].shape}")
        dummy = np.zeros((3, *INPUT_SHAPE, 3), dtype=np.float32) - 1.0  # 三幀全黑
        p = predict(s, dummy)
        print(f"forward_ok=1 T=3 probs={[round(float(v), 4) for v in p]}")
    print("self_test=ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="OOBNet 低頻取樣推論（onnxruntime）→ CSV")
    ap.add_argument("--video", help="輸入影片路徑")
    ap.add_argument("--model", help="oobnet.onnx（CC BY-NC-SA 4.0，非商業）")
    ap.add_argument("--out", help="輸出 CSV 路徑（FRAME_ID,OOB,TIME_SEC）")
    ap.add_argument("--sample-fps", type=float, default=1.0, help="取樣頻率（每秒抽幾幀；預設 1）")
    ap.add_argument("--device", choices=DEVICES, default="auto", help="auto（CUDA→CPU）| cuda | cpu")
    ap.add_argument("--threads", type=int, default=0, help="onnxruntime CPU intra-op 執行緒數（0＝預設）")
    ap.add_argument("--gpu-mem-limit-mb", type=int, default=0, help="CUDA 顯存上限 MB（0＝不限）")
    ap.add_argument("--self-test", action="store_true", help="環境自檢（版本/provider/模型試載）")
    args = ap.parse_args()
    if args.self_test:
        return self_test(args.model, args.device)
    missing = [k for k in ("video", "model", "out") if not getattr(args, k)]
    if missing:
        ap.error("缺少參數：" + ", ".join(f"--{k}" for k in missing))
    if not os.path.isfile(args.video):
        print(f"error=video_not_found path={args.video}", file=sys.stderr)
        return 2

    def progress(pct: float) -> None:
        print(f"PROGRESS {pct:.1f}", flush=True)

    def log(msg: str) -> None:
        print(msg, flush=True)

    try:
        run(args.video, args.model, args.out, args.sample_fps, device=args.device,
            threads=args.threads, gpu_mem_limit_mb=args.gpu_mem_limit_mb,
            progress_cb=progress, log_cb=log)
    except OOBInferError as e:
        print(f"error={e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
