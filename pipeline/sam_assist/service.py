"""SAM 推論服務（階段一 A1：encoder + decoder 全後端，ONNX Runtime）。

支援兩個 model family（pipeline/configs/sam.yaml 的 model.active 一鍵切換）：
- sam1（MobileSAM 等）：encoder 單一 embedding [1,256,64,64]、前處理內嵌於 ONNX、
  decoder 吃 orig_im_size 並在圖內還原到原始解析度。
- sam2（SAM2 hiera 系列）：encoder 固定輸入 [1,3,1024,1024]（前處理在圖外：
  squash resize + ImageNet 正規化）、輸出 3 張量（image_embed + 兩層 high_res_feats）、
  decoder 輸出 3 張 256×256 multimask logits，需自行選擇並 resize 回原始解析度。
  約定對照 samexporter/sam2_onnx.py（模型即該工具轉出）。

流程：video_id+frame → registry 查標準化 mp4 → ffmpeg 抽幀（CFR 保證對齊）
     → encoder embedding（記憶體 LRU + 磁碟 LRU fp16 快取，磁碟路徑含 model tag）
     → decoder（點/框 prompt）→ mask 二值化 → 最大連通域 → approxPolyDP → polygon。

GPU：device=cuda 時預載 pip nvidia-* wheels 的 CUDA/cuDNN .so（無系統 toolkit），
    Pascal 1080 Ti 實測可用組合見 deploy/requirements-gpu.txt；失敗自動降級 CPU 並記於 status.detail。
禁用 PyTorch（Pascal 顯卡相容性：新版 PyTorch wheel 已不支援 sm_61）；全參數走 sam.yaml（R5）。
"""
from __future__ import annotations

import ctypes
import glob
import os
import subprocess
import sysconfig
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .. import config, registry
from ..ingest import _require


class SamNotReady(RuntimeError):
    """模型檔缺失或 ONNX session 載入失敗（detail 說明原因）。"""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


# ---------------------------------------------------------------- config


def _load_cfg() -> dict:
    return config.load_config("sam.yaml")


def _abs(path_str: str) -> Path:
    """yaml 內路徑：相對值一律「相對 data 根」，絕對值原樣（config.data_path）。"""
    return config.data_path(path_str)


def _cuda_provider_available(ort) -> bool:
    """onnxruntime 是否帶 CUDAExecutionProvider（CPU 版 ORT 沒有）。"""
    try:
        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:  # noqa: BLE001 — 偵測失敗一律視為不可用
        return False


# ---------------------------------------------------------------- CUDA 預載

# pip nvidia-* wheels 的 .so 載入順序（相依在前）；系統無 CUDA toolkit 時必須預載
_CUDA_LIB_GLOBS = [
    "nvidia/cuda_runtime/lib/libcudart.so*",
    "nvidia/cublas/lib/libcublasLt.so*",
    "nvidia/cublas/lib/libcublas.so*",
    "nvidia/cuda_nvrtc/lib/libnvrtc.so*",
    "nvidia/cudnn/lib/libcudnn.so*",
    "nvidia/cufft/lib/libcufft.so*",
    "nvidia/curand/lib/libcurand.so*",
]


def _preload_cuda_libs() -> None:
    site_packages = sysconfig.get_path("purelib")
    for pattern in _CUDA_LIB_GLOBS:
        for so in sorted(glob.glob(os.path.join(site_packages, pattern))):
            try:
                ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass  # 缺檔時交由 ORT 自行報錯/降級


# ---------------------------------------------------------------- service


class SamService:
    """單例使用（走 get_service()）；predict 為同步函式，可安全丟 threadpool。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        m = cfg["model"]
        r = cfg.get("runtime", {})
        self.model_tag: str = m["active"]
        zoo = m.get("zoo", {})
        if self.model_tag not in zoo:
            raise SamNotReady(
                f"model.active={self.model_tag!r} 不在 model.zoo（{list(zoo)}）")
        spec = zoo[self.model_tag]
        self.family: str = spec.get("family", "sam1")
        if self.family not in ("sam1", "sam2"):
            raise SamNotReady(f"未知 model family：{self.family!r}")
        self.model_name: str = spec.get("name", self.model_tag)
        self.long_side: int = int(r.get("encoder_long_side", 1024))
        self.multimask_strategy: str = r.get("multimask_strategy", "best_iou")
        self._detail: Optional[str] = None

        model_dir = _abs(m["dir"])
        enc_path = model_dir / spec["encoder"]
        dec_path = model_dir / spec["decoder"]
        for p in (enc_path, dec_path):
            if not p.exists():
                raise SamNotReady(
                    f"模型檔不存在：{p}（下載來源見 pipeline/configs/sam.yaml 註解）")

        try:
            import onnxruntime as ort
        except ImportError as e:
            raise SamNotReady(f"onnxruntime 未安裝：{e}") from e
        self._ort = ort

        requested = str(r.get("device", "auto")).lower()
        if requested not in ("auto", "cuda", "cpu"):
            raise SamNotReady(f"runtime.device 只接受 auto / cuda / cpu：{requested!r}")
        if requested == "auto":
            # auto：ORT 帶 CUDA provider 才嘗試 cuda（失敗照下方流程降級 cpu）
            device = "cuda" if _cuda_provider_available(ort) else "cpu"
        else:
            device = requested
        try:
            self._build_sessions(enc_path, dec_path, device)
        except SamNotReady as e:
            if device != "cuda":
                raise
            # CUDA session 建不起來（缺 CUDA 函式庫等）→ 降級 CPU
            self._build_sessions(enc_path, dec_path, "cpu")
            self._detail = f"CUDA session 建立失敗已降級 CPU：{e.detail}"
        if r.get("warmup", True):
            try:
                self._warmup()
            except Exception as e:
                if self.device == "cuda":
                    # GPU 執行期失敗（如 kernel 不支援該架構）→ 降級 CPU 重建
                    self._build_sessions(enc_path, dec_path, "cpu")
                    self._detail = f"CUDA 執行失敗已降級 CPU：{e}"
                    try:
                        self._warmup()
                    except Exception as e2:
                        raise SamNotReady(f"warmup 失敗（CPU）：{e2}") from e2
                else:
                    raise SamNotReady(f"warmup 失敗：{e}") from e

        c = cfg["cache"]
        self._mem_max = int(c.get("mem_frames", 8))
        self._mem: OrderedDict[tuple[str, int], dict] = OrderedDict()
        self._disk_dir = _abs(c.get("disk_dir", "cache/sam_emb"))
        self._disk_limit = float(c.get("disk_limit_gb", 20)) * (1024 ** 3)
        self._lock = threading.Lock()

        _require("ffmpeg")

    def _build_sessions(self, enc_path: Path, dec_path: Path, device: str) -> None:
        ort = self._ort
        if device == "cuda":
            _preload_cuda_libs()
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        else:
            providers = ["CPUExecutionProvider"]
        opts = ort.SessionOptions()
        opts.log_severity_level = 3  # 隱藏 graph 整理類 warning
        try:
            self._encoder = ort.InferenceSession(str(enc_path), opts, providers=providers)
            self._decoder = ort.InferenceSession(str(dec_path), opts, providers=providers)
        except Exception as e:  # ORT 載入失敗種類繁多，一律轉 SamNotReady
            raise SamNotReady(f"ONNX session 載入失敗（device={device}）：{e}") from e
        self._enc_input = self._encoder.get_inputs()[0].name
        self._enc_outputs = [o.name for o in self._encoder.get_outputs()]
        # 實際落點（要求 cuda 但 ORT 靜默 fallback 時如實回報）
        actual = self._encoder.get_providers()
        self.device = "cuda" if actual and actual[0] == "CUDAExecutionProvider" else "cpu"
        if device == "cuda" and self.device != "cuda":
            self._detail = f"CUDAExecutionProvider 不可用，實際 providers={actual}"

    def _warmup(self) -> None:
        """跑一次假圖 encoder+decoder：暴露 GPU kernel 問題並吃掉首推 autotune 延遲。"""
        dummy = np.zeros((64, 64, 3), dtype=np.uint8)
        emb = self._encode(dummy)
        self._run_decoder(emb, [[32.0, 32.0]], [1.0], None, 64, 64)

    # ------------------------------------------------------------ public

    def status(self) -> dict:
        return {"ready": True, "model": self.model_name,
                "device": self.device, "detail": self._detail,
                "epsilon_px": float(self.cfg["polygon"].get("epsilon_px", 2.0))}

    def predict(self, video_id: str, frame: int,
                points: Optional[list] = None, labels: Optional[list] = None,
                box: Optional[list] = None,
                epsilon_px: Optional[float] = None) -> dict:
        """點/框 prompt → polygon（原始影片像素座標）。
        epsilon_px：前端精細度滑桿覆寫簡化強度（None＝config 預設＋自動頂點數調整）。

        raises: SamNotReady / FileNotFoundError（video_id 不存在或幀抽不出）
                / ValueError（參數不合法）
        """
        points, labels, box = self._validate(frame, points, labels, box)
        if epsilon_px is not None:
            if not isinstance(epsilon_px, (int, float)) or not (0.1 <= epsilon_px <= 20):
                raise ValueError(f"epsilon_px 須在 0.1–20 px：{epsilon_px!r}")
            epsilon_px = float(epsilon_px)

        video = self._resolve_video(video_id)
        w, h = int(video["width"]), int(video["height"])
        n_frames = int(video["n_frames"])
        if frame >= n_frames:
            raise FileNotFoundError(
                f"frame {frame} 超出範圍（{video_id} 共 {n_frames} 幀）")
        self._check_bounds(points, box, w, h)

        emb = self._get_embedding(video_id, frame, video)
        mask, score = self._run_decoder(emb, points, labels, box, w, h)
        polygon = self._mask_to_polygon(mask, epsilon_px)
        return {"polygon": polygon, "score": score}

    # ------------------------------------------------------------ 驗證

    @staticmethod
    def _validate(frame, points, labels, box):
        if not isinstance(frame, int) or frame < 0:
            raise ValueError(f"frame 須為非負整數：{frame!r}")
        if points in ([], None):
            points = None
        if points is None and box is None:
            raise ValueError("points 與 box 至少需提供一種")
        if points is not None:
            if labels is None or len(labels) != len(points):
                raise ValueError("labels 須與 points 等長")
            for p in points:
                if len(p) != 2:
                    raise ValueError(f"point 須為 [x,y]：{p!r}")
            if any(l not in (0, 1) for l in labels):
                raise ValueError("labels 僅允許 0（負點）或 1（正點）")
        if box is not None:
            if len(box) != 4:
                raise ValueError(f"box 須為 [x1,y1,x2,y2]：{box!r}")
            x1, y1, x2, y2 = box
            box = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
            if box[0] == box[2] or box[1] == box[3]:
                raise ValueError("box 面積為零")
        return points, labels, box

    @staticmethod
    def _check_bounds(points, box, w, h):
        coords = list(points or [])
        if box is not None:
            coords += [[box[0], box[1]], [box[2], box[3]]]
        for x, y in coords:
            if not (0 <= x <= w and 0 <= y <= h):
                raise ValueError(f"座標 ({x},{y}) 超出畫面 {w}x{h}")

    # ------------------------------------------------------------ 影片與抽幀

    @staticmethod
    def _resolve_video(video_id: str):
        with registry.connect() as conn:
            video = registry.get_video(video_id, conn)
        if video is None:
            raise FileNotFoundError(f"registry 內沒有 video_id={video_id}")
        std_path = config.to_abs(video["std_path"])   # v1.4：相對 data 根 → 絕對
        if not std_path or not std_path.exists():
            raise FileNotFoundError(f"標準化影片不存在：{std_path}")
        return video

    @staticmethod
    def _extract_frame(video, frame: int) -> np.ndarray:
        """ffmpeg -ss {frame/fps} 抽單幀 → RGB ndarray (h,w,3)。CFR+1s GOP 保證對齊。"""
        w, h = int(video["width"]), int(video["height"])
        t = frame / float(video["fps"])
        cmd = [
            "ffmpeg", "-v", "error",
            "-ss", f"{t:.6f}", "-i", str(config.to_abs(video["std_path"])),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ]
        proc = subprocess.run(cmd, capture_output=True)
        expected = w * h * 3
        if proc.returncode != 0 or len(proc.stdout) != expected:
            raise FileNotFoundError(
                f"幀抽取失敗（{video['video_id']} frame {frame}）："
                f"{proc.stderr.decode(errors='replace')[-300:]}")
        return np.frombuffer(proc.stdout, dtype=np.uint8).reshape(h, w, 3)

    # ------------------------------------------------------------ embedding 快取
    # 磁碟路徑含 model tag（{model_tag}/{video_id}/f{frame:07d}.npz），
    # 換模型不會吃到舊 embedding；npz 內含該 family 全部張量（fp16 落地）。

    def _get_embedding(self, video_id: str, frame: int, video) -> dict:
        key = (video_id, frame)
        with self._lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                return self._mem[key]

        disk_path = (self._disk_dir / self.model_tag / video_id
                     / f"f{frame:07d}.npz")
        if disk_path.exists():
            try:
                with np.load(disk_path) as z:
                    emb = {k: z[k].astype(np.float32) for k in z.files}  # fp16→fp32
                os.utime(disk_path)  # 更新 mtime 供磁碟 LRU
                self._mem_put(key, emb)
                return emb
            except Exception:
                disk_path.unlink(missing_ok=True)  # 壞檔重算

        emb = self._encode(self._extract_frame(video, frame))
        self._mem_put(key, emb)
        self._disk_put(disk_path, emb)
        return emb

    def _encode(self, rgb: np.ndarray) -> dict:
        h, w = rgb.shape[:2]
        if self.family == "sam1":
            # 長邊縮放（保長寬比），正規化/padding 內嵌於 ONNX
            scale = self.long_side / max(h, w)
            nh, nw = int(h * scale + 0.5), int(w * scale + 0.5)
            resized = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
            out = self._encoder.run(None, {self._enc_input: resized.astype(np.float32)})
            return {"image_embed": out[0]}
        # sam2：squash resize 到正方形 + ImageNet 正規化 + NCHW（samexporter 約定）
        side = self.long_side
        resized = cv2.resize(rgb, (side, side), interpolation=cv2.INTER_LINEAR)
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        x = (resized.astype(np.float32) / 255.0 - mean) / std
        x = x.transpose(2, 0, 1)[np.newaxis]
        out = self._encoder.run(None, {self._enc_input: x})
        return dict(zip(self._enc_outputs, out))

    def _mem_put(self, key, emb: dict):
        with self._lock:
            self._mem[key] = emb
            self._mem.move_to_end(key)
            while len(self._mem) > self._mem_max:
                self._mem.popitem(last=False)

    def _disk_put(self, path: Path, emb: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as f:
            np.savez(f, **{k: v.astype(np.float16) for k, v in emb.items()})
        tmp.replace(path)
        self._trim_disk()

    def _trim_disk(self):
        """磁碟總量超過上限 → 依 mtime 刪最舊（跨所有 model tag）。"""
        files = [p for p in self._disk_dir.rglob("*.np[yz]") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        if total <= self._disk_limit:
            return
        for p in sorted(files, key=lambda p: p.stat().st_mtime):
            try:
                size = p.stat().st_size
                p.unlink()
                total -= size
            except OSError:
                continue
            if total <= self._disk_limit:
                break

    # ------------------------------------------------------------ decoder

    def _run_decoder(self, emb: dict, points, labels, box,
                     w: int, h: int) -> tuple[np.ndarray, float]:
        if self.family == "sam1":
            return self._decode_sam1(emb, points, labels, box, w, h)
        return self._decode_sam2(emb, points, labels, box, w, h)

    def _decode_sam1(self, emb, points, labels, box, w, h):
        """SAM1 ONNX 約定：座標縮放到 1024 長邊空間、box 用 labels 2/3、
        無 box 時補 padding 點 (0,0)/-1；mask 於圖內還原到 orig_im_size。"""
        scale = self.long_side / max(h, w)
        coords, labs = [], []
        if points is not None:
            coords += [[x * scale, y * scale] for x, y in points]
            labs += [float(l) for l in labels]
        if box is not None:
            x1, y1, x2, y2 = box
            coords += [[x1 * scale, y1 * scale], [x2 * scale, y2 * scale]]
            labs += [2.0, 3.0]
        else:
            coords.append([0.0, 0.0])
            labs.append(-1.0)

        outs = self._decoder.run(None, {
            "image_embeddings": emb["image_embed"],
            "point_coords": np.array([coords], dtype=np.float32),
            "point_labels": np.array([labs], dtype=np.float32),
            "mask_input": np.zeros((1, 1, 256, 256), dtype=np.float32),
            "has_mask_input": np.zeros(1, dtype=np.float32),
            "orig_im_size": np.array([h, w], dtype=np.float32),
        })
        masks = outs[0].reshape(-1, outs[0].shape[-2], outs[0].shape[-1])
        scores = outs[1].reshape(-1)
        best = self._select_mask(masks, scores)
        return masks[best], float(scores[best] if len(scores) else 0.0)

    def _decode_sam2(self, emb, points, labels, box, w, h):
        """SAM2 ONNX 約定（samexporter）：座標各軸獨立縮放到 1024×1024 squash 空間、
        box 用 labels 2/3、免 padding 點；輸出 3 張 256×256 multimask logits，
        依 multimask_strategy 選擇後 resize 回原始解析度。"""
        side = self.long_side
        coords, labs = [], []
        if points is not None:
            coords += [[x * side / w, y * side / h] for x, y in points]
            labs += [float(l) for l in labels]
        if box is not None:
            x1, y1, x2, y2 = box
            coords += [[x1 * side / w, y1 * side / h],
                       [x2 * side / w, y2 * side / h]]
            labs += [2.0, 3.0]

        outs = self._decoder.run(None, {
            "image_embed": emb["image_embed"],
            "high_res_feats_0": emb["high_res_feats_0"],
            "high_res_feats_1": emb["high_res_feats_1"],
            "point_coords": np.array([coords], dtype=np.float32),
            "point_labels": np.array([labs], dtype=np.float32),
            "mask_input": np.zeros((1, 1, side // 4, side // 4), dtype=np.float32),
            "has_mask_input": np.zeros(1, dtype=np.float32),
        })
        masks = outs[0][0]            # [3, 256, 256] logits
        scores = outs[1].reshape(-1)  # [3]
        best = self._select_mask(masks, scores)
        full = cv2.resize(masks[best], (w, h), interpolation=cv2.INTER_LINEAR)
        return full, float(scores[best] if len(scores) else 0.0)

    def _select_mask(self, masks: np.ndarray, scores: np.ndarray) -> int:
        """multimask 選擇策略（runtime.multimask_strategy）：
        best_iou＝decoder IoU 預測最高（預設）；largest＝二值化後面積最大。"""
        if len(masks) <= 1:
            return 0
        if self.multimask_strategy == "largest":
            thr = float(self.cfg["polygon"].get("mask_threshold", 0.0))
            areas = [(m > thr).sum() for m in masks]
            return int(np.argmax(areas))
        return int(np.argmax(scores)) if len(scores) == len(masks) else 0

    # ------------------------------------------------------------ mask → polygon

    def _mask_to_polygon(self, mask: np.ndarray,
                         epsilon_override: Optional[float] = None) -> list:
        """threshold → 最大連通域 → 外輪廓 → approxPolyDP（孔洞忽略）。
        epsilon_override：使用者滑桿值——直接採用、停用自動頂點數調整（僅保 500 點硬上限）。"""
        p = self.cfg["polygon"]
        thr = float(p.get("mask_threshold", 0.0))
        eps0 = float(p.get("epsilon_px", 2.0))
        vmin = int(p.get("min_vertices", 15))
        vmax = int(p.get("max_vertices", 60))

        binary = (mask > thr).astype(np.uint8)
        if binary.sum() == 0:
            return []

        n, cc_labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
        if n <= 1:
            return []
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        component = (cc_labels == largest).astype(np.uint8)

        contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return []
        contour = max(contours, key=cv2.contourArea)

        if epsilon_override is not None:
            hard = int(p.get("max_vertices_hard", 150))
            eps = epsilon_override
            approx = cv2.approxPolyDP(contour, eps, True)
            while len(approx) > hard:  # 超過硬上限 → 加大 epsilon 直到符合
                eps *= 1.3
                approx = cv2.approxPolyDP(contour, eps, True)
            if len(approx) < 3:
                return []
            h, w = mask.shape[-2], mask.shape[-1]
            return [[float(min(max(x, 0), w - 1)), float(min(max(y, 0), h - 1))]
                    for x, y in approx.reshape(-1, 2)]

        # 以 config epsilon 起跳，超出目標頂點區間時倍率調整（保底 3 點）
        eps = eps0
        approx = cv2.approxPolyDP(contour, eps, True)
        for _ in range(8):
            if len(approx) > vmax:
                eps *= 1.5
            elif len(approx) < vmin and eps > 0.5:
                eps /= 1.5
            else:
                break
            approx = cv2.approxPolyDP(contour, eps, True)
        if len(approx) < 3:
            approx = cv2.approxPolyDP(contour, eps0, True)
        if len(approx) < 3:
            return []

        h, w = mask.shape[-2], mask.shape[-1]
        return [[float(min(max(x, 0), w - 1)), float(min(max(y, 0), h - 1))]
                for x, y in approx.reshape(-1, 2)]


# ---------------------------------------------------------------- singleton

_service: Optional[SamService] = None
_service_lock = threading.Lock()


def get_service() -> SamService:
    """lazy singleton；模型缺失/載入失敗 raise SamNotReady（不快取失敗，可重試）。"""
    global _service
    if _service is not None:
        return _service
    with _service_lock:
        if _service is None:
            _service = SamService(_load_cfg())
        return _service
