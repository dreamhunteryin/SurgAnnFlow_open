"""可攜安裝三件套：`surgann doctor` / `surgann setup` / `surgann fetch-models`。

cli.py 只做薄入口註冊（R10），所有邏輯在這裡。設計原則：

- **程式本身從不執行 sudo**：需要 root 的步驟（安裝 systemd unit）只「印出」指令，交給人或 agent 執行。
- **輸出要讓 AI agent 好解析**：doctor 有 `--json`，每項固定欄位 id/status/detail/fix；
  任一 FAIL（阻斷項）→ 結束碼 1。
- **不改既有設定**：setup 寫 `deploy/.env` 時保留所有既有鍵、值與註解，只補缺的（除非 `--force`）。
- **設定值執行期讀取**（R5）：data 根取 `config.DATA_DIR`；模型路徑、下載網址、sha256 取 yaml。
- **OOB 模型隨 repo 附帶**（`pipeline/models/oob/`）：fetch-models 對 OOB 只驗檔＋記錄授權同意，
  同意必須是使用者本人（寫入 `deploy/.env` 的 `OOB_LICENSE_ACCEPTED`）。

白話小辭典（R0）：
- sha256：檔案的「指紋」。下載完重算一次，和公布的值一樣就代表檔案完整沒被竄改。
- 續傳：下載中斷時保留已下載的部分（`.part` 檔），下次從斷點接著抓，不用重來。
- systemd unit：Linux 開機時負責把服務叫起來、掛掉自動重開的設定檔；放進 /etc 需要 sudo。
"""
from __future__ import annotations

import getpass
import hashlib
import importlib.metadata as md
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import config

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _say(msg: str) -> None:
    """即時輸出（flush）：被 agent 以管線/檔案接收時進度也不會卡在緩衝區。"""
    print(msg, flush=True)

SERVICE_NAME = "surgannflow-annotator"
UNIT_TEMPLATE = config.DEPLOY_DIR / "systemd" / f"{SERVICE_NAME}.service.in"
UNIT_GENERATED_DIR = config.DEPLOY_DIR / "systemd" / "generated"   # .gitignore
UNIT_INSTALL_PATH = Path("/etc/systemd/system") / f"{SERVICE_NAME}.service"
ENV_EXAMPLE = config.DEPLOY_DIR / ".env.example"

DEFAULT_PORT = 8090
DEFAULT_ANNOTATOR = "annotator1"
MIN_PYTHON = (3, 10)
FREE_WARN_GB = 50          # data 根剩餘空間低於此值 → WARN（一支 2h 標準化影片約 7 GB）
FREE_FAIL_GB = 2           # 低於此值 → FAIL（入庫/抽幀會寫爆）

# setup 會確保 .env 有這些鍵
ENV_KEYS = ("ANNOTATOR_PORT", "ANNOTATOR_NAME", "SURGANN_DATA_DIR", "TZ")
# 已淘汰鍵：單一來源在 config.DEPRECATED_ENV_KEYS；舊版 config 無此常數時用這份
OBSOLETE_ENV_KEYS: dict[str, str] = getattr(config, "DEPRECATED_ENV_KEYS", None) or {
    "OOB_PYTHON": "OOB 已改 ONNX、與主 venv 共用，此鍵不再使用，可自 deploy/.env 移除",
}

# data 根底下要建的子目錄：取自 config 常數（相對 DATA_DIR），避免在此重抄一份（R4）
_DATA_DIR_ATTRS = ("INCOMING_DIR", "ARCHIVED_DIR", "WORK_DIR", "VIDEOS_DIR",
                   "FRAMES_DIR", "EXPORTS_DIR")

# ---------------------------------------------------------------------------
# SAM 模型下載來源：單一來源在 pipeline/configs/sam.yaml 的 model.zoo.<tag>.sources
# （encoder/decoder 各 {url, sha256, size}）。無 sources 的 tag（mobile_sam，
# 上游只有 zip）不支援自動下載。

OOB_LICENSE_TEXT = """\
================ OOB 模型授權聲明（請完整閱讀）================
模型：OOBNet（out-of-body 體外畫面偵測，MobileNetV2 + LSTM）
出處：CAMMA research group, University of Strasbourg
      https://github.com/CAMMA-public/out-of-body-detector
授權：Creative Commons BY-NC-SA 4.0（姓名標示—非商業性—相同方式分享）
      https://creativecommons.org/licenses/by-nc-sa/4.0/
重點：只能用於【非商業】研究；使用時須標示出處；改作須以相同授權釋出。
      平台若要商業化，必須換成其他授權的模型。
本平台提供的是由原始權重轉出的 ONNX 檔，授權條款與原始權重相同。
==============================================================="""


# =========================================================== 共用小工具 ===

@dataclass
class Check:
    id: str
    name: str
    status: str
    detail: str
    fix: str = ""


@dataclass
class ModelFile:
    group: str                 # "sam" / "oob"
    label: str                 # 人看的名稱
    path: Path
    url: Optional[str] = None
    sha256: Optional[str] = None
    size: Optional[int] = None
    note: str = ""             # 無法自動下載等原因


def _data_dir() -> Path:
    """平台實際使用的 data 根（由 config 解析；舊版 config 無 DATA_DIR 時即 <repo>/data）。"""
    return Path(getattr(config, "DATA_DIR", config.PROJECT_ROOT / "data"))


def expected_data_dir(env: Optional[dict] = None) -> Path:
    """依規則「應該」用的 data 根：環境變數 > deploy/.env > <repo>/data。

    只給 doctor 比對 config 是否已依規則解析、以及 setup 決定預設值用；
    平台本體一律讀 config.DATA_DIR（單一實作在 config）。
    """
    env = config.load_env() if env is None else env
    raw = os.environ.get("SURGANN_DATA_DIR") or env.get("SURGANN_DATA_DIR") or ""
    if not raw.strip():
        return config.PROJECT_ROOT / "data"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (config.PROJECT_ROOT / p)


def resolve_data_path(p: str | Path, data_dir: Optional[Path] = None) -> Path:
    """yaml 內的模型/快取路徑 → 絕對路徑。

    相容兩種寫法：
    - 舊寫法 `data/models/sam`（相對 PROJECT_ROOT）→ 視為 `<data 根>/models/sam`
      （data 根預設即 <repo>/data，兩者等價；data 根搬走後仍指對位置）
    - 新寫法 `models/sam`（相對 data 根）
    - 絕對路徑原樣
    """
    p = Path(p).expanduser()
    if p.is_absolute():
        return p
    base = data_dir or _data_dir()
    parts = p.parts
    if parts and parts[0] == "data":
        return base.joinpath(*parts[1:])
    return base / p


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def _run(cmd: list[str], timeout: float = 15) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _mount_point(path: Path) -> Path:
    p = path.resolve()
    while not p.exists():
        p = p.parent
    while not os.path.ismount(p):
        p = p.parent
    return p


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# =========================================================== 模型清單 ===

def _load_yaml(name: str) -> dict:
    try:
        return config.load_config(name) or {}
    except FileNotFoundError:
        return {}


def sam_models(tag: Optional[str] = None) -> list[ModelFile]:
    """SAM 模型檔清單（預設 sam.yaml model.active；tag='all' = zoo 內全部）。"""
    cfg = _load_yaml("sam.yaml")
    m = cfg.get("model", {})
    zoo = m.get("zoo", {})
    model_dir = resolve_data_path(m.get("dir", "models/sam"))
    tags = list(zoo) if tag == "all" else [tag or m.get("active")]
    out: list[ModelFile] = []
    for t in tags:
        spec = zoo.get(t)
        if not spec:
            out.append(ModelFile("sam", f"{t}（不在 sam.yaml model.zoo）", model_dir / f"<{t}>",
                                 note=f"model.zoo 無 {t!r}，可選：{', '.join(zoo)}"))
            continue
        sources = spec.get("sources") or {}
        for part in ("encoder", "decoder"):
            fname = spec.get(part)
            src = sources.get(part) or {}
            out.append(ModelFile(
                "sam", f"SAM {t} {part}", model_dir / fname,
                url=src.get("url"), sha256=src.get("sha256"), size=src.get("size"),
                note="" if src.get("url") else
                     f"{t} 無結構化下載來源（上游僅提供 zip），請依 sam.yaml 註解手動下載"))
    return out


# OOB 模型隨 repo 附帶（git 追蹤 pipeline/models/oob/oobnet.onnx），clone 即有、不再下載。
# 解析規則的單一實作在 preprocess.resolve_oob_model()；授權同意的讀取在 config.oob_license_accepted_on()，
# 寫入（使用者同意後）在本檔 record_oob_license()。
OOB_LICENSE_KEY = getattr(config, "OOB_LICENSE_ENV_KEY", "OOB_LICENSE_ACCEPTED")
OOB_LICENSE_FILE = "pipeline/models/oob/LICENSE-OOBNet.md"
OOB_RESTORE_FIX = ("附帶的 OOB 模型缺檔或被改動：git checkout -- pipeline/models/oob/oobnet.onnx"
                   "（或重新 git clone 本 repo）")
OOB_ACCEPT_FIX = "由使用者本人執行 surgann fetch-models --only oob，閱讀授權後輸入 accept 同意"


@dataclass
class OobModel:
    path: Path
    source: str                 # "bundled"（repo 附帶，預設）| "override"（preprocess.yaml oob.onnx_path 指定的自訂模型）
    sha_ok: Optional[bool]      # 與 onnx_sha256 比對；None＝未比對
    expected_sha: Optional[str] = None
    error: str = ""             # 解析失敗（缺檔/設定錯）時的訊息（含補救方式）；非空＝不可用


def _oob_cfg() -> dict:
    return _load_yaml("preprocess.yaml").get("oob", {}) or {}


def resolve_oob() -> OobModel:
    """OOB 模型的實際位置與來源：委派 preprocess.resolve_oob_model（單一實作，R4）。

    該函式找不到模型時會 raise（附帶檔缺失，或 onnx_path 指定的自訂模型不存在——不退回附帶）；
    這裡把訊息放進 error，讓 doctor / fetch-models 以 FAIL／結束碼 1 呈現。
    """
    want = str(_oob_cfg().get("onnx_sha256") or "").strip().lower() or None
    try:
        from . import preprocess as pp
        path, source, sha_ok = pp.resolve_oob_model()
        return OobModel(Path(path), str(source), sha_ok, want)
    except Exception as e:
        o = _oob_cfg()
        override = str(o.get("onnx_path") or "").strip()
        raw = override or o.get("onnx_bundled") or "models/oob/oobnet.onnx"
        if override:
            path, source = resolve_data_path(raw), "override"
        else:
            pkg = getattr(config, "package_path", None)
            path = pkg(raw) if callable(pkg) else Path(__file__).resolve().parent / raw
            source = "bundled"
        return OobModel(path, source, None, want, error=str(e) or type(e).__name__)


def oob_license_accepted() -> Optional[str]:
    """已同意 OOB 授權 → 同意日期（ISO）；未同意 → None。讀取端單一實作在 config。"""
    fn = getattr(config, "oob_license_accepted_on", None)
    if callable(fn):
        return fn()
    raw = config.load_env().get(OOB_LICENSE_KEY, "").strip() if config.ENV_FILE.exists() else ""
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


# ============================================================== doctor ===

def _chk_python() -> Check:
    v = sys.version_info
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    detail = f"Python {v.major}.{v.minor}.{v.micro}（{sys.executable}）"
    if v < MIN_PYTHON:
        return Check("python", "Python 版本", FAIL, detail,
                     f"需要 Python ≥ {MIN_PYTHON[0]}.{MIN_PYTHON[1]}；"
                     "Ubuntu：sudo apt install python3 python3-venv")
    if not in_venv:
        return Check("python", "Python 版本", WARN, detail + "，不在 venv 內",
                     "建議在專案目錄建 venv：python3 -m venv .venv && . .venv/bin/activate")
    return Check("python", "Python 版本", PASS, detail + "，venv")


def _chk_packages() -> list[Check]:
    out = []
    try:
        ver = md.version("surgann")
        out.append(Check("pkg_surgann", "surgann 套件安裝", PASS, f"surgann {ver}"))
    except md.PackageNotFoundError:
        out.append(Check("pkg_surgann", "surgann 套件安裝", WARN,
                         "目前環境未以 pip 安裝 surgann（surgann 指令可能不存在）",
                         "在專案目錄執行：pip install -e . -r deploy/requirements-cpu.txt（或 -gpu）"))
    mods = [("typer", "typer"), ("rich", "rich"), ("yaml", "PyYAML"), ("fastapi", "fastapi"),
            ("uvicorn", "uvicorn"), ("numpy", "numpy"), ("cv2", "opencv-python-headless")]
    missing, found = [], []
    for mod, dist in mods:
        if importlib.util.find_spec(mod) is None:
            missing.append(dist)
        else:
            try:
                found.append(f"{dist} {md.version(dist)}")
            except md.PackageNotFoundError:
                found.append(dist)
    if missing:
        out.append(Check("pkg_deps", "Python 相依套件", FAIL, "缺少：" + ", ".join(missing),
                         "pip install -e . -r deploy/requirements-cpu.txt（有 NVIDIA GPU 改 -gpu）"))
    else:
        out.append(Check("pkg_deps", "Python 相依套件", PASS, "; ".join(found)))
    return out


def _chk_ffmpeg() -> list[Check]:
    out = []
    for tool in ("ffmpeg", "ffprobe"):
        p = shutil.which(tool)
        if not p:
            out.append(Check(tool, tool, FAIL, f"找不到 {tool}", "sudo apt install ffmpeg"))
            continue
        r = _run([tool, "-version"])
        first = (r.stdout.splitlines() or ["?"])[0] if r else "?"
        out.append(Check(tool, tool, PASS, f"{first}（{p}）"))
    return out


def _nvenc_available() -> Optional[bool]:
    """沿用 preprocess 的 NVENC 偵測（R4）；函式不存在時回 None（無法判定）。"""
    try:
        from . import preprocess as pp
    except Exception:
        return None
    for name in ("nvenc_available", "check_nvenc", "_check_nvenc"):
        fn = getattr(pp, name, None)
        if callable(fn):
            return bool(fn())
    return None


def _chk_gpu() -> tuple[Check, bool]:
    if not shutil.which("nvidia-smi"):
        return Check("gpu", "NVIDIA GPU / driver", WARN, "未偵測到 nvidia-smi（無 NVIDIA GPU 或未裝 driver）",
                     "無 GPU 亦可運作（SAM/OOB 走 CPU、轉檔走 libx264，較慢）；"
                     "有 GPU 者：sudo ubuntu-drivers install 後重開機"), False
    r = _run(["nvidia-smi", "--query-gpu=name,driver_version,compute_cap",
              "--format=csv,noheader"])
    if not r or r.returncode != 0 or not r.stdout.strip():
        msg = (r.stderr or r.stdout).strip()[:200] if r else "執行失敗"
        return Check("gpu", "NVIDIA GPU / driver", WARN, f"nvidia-smi 失敗：{msg}",
                     "driver 可能未載入：重開機，或 sudo ubuntu-drivers install"), False
    gpus = "; ".join(line.strip() for line in r.stdout.strip().splitlines())
    return Check("gpu", "NVIDIA GPU / driver", PASS, f"name, driver, compute_cap = {gpus}"), True


def _chk_encoder(have_ffmpeg: bool) -> Check:
    enc = str((_load_yaml("preprocess.yaml").get("encode") or {}).get("encoder", "?"))
    if not have_ffmpeg:
        return Check("nvenc", "轉檔編碼器（NVENC）", WARN, "ffmpeg 不存在，略過", "先裝 ffmpeg")
    nv = _nvenc_available()
    if nv is None:
        return Check("nvenc", "轉檔編碼器（NVENC）", WARN,
                     f"無法判定 NVENC（preprocess 偵測函式不存在）；encode.encoder={enc}")
    if nv:
        return Check("nvenc", "轉檔編碼器（NVENC）", PASS, f"h264_nvenc 可用；encode.encoder={enc}")
    if enc == "nvenc":
        return Check("nvenc", "轉檔編碼器（NVENC）", FAIL,
                     "h264_nvenc 不可用，但 preprocess.yaml encode.encoder 寫死 nvenc → 入庫轉檔會失敗",
                     "把 pipeline/configs/preprocess.yaml 的 encode.encoder 改為 auto（或 libx264）")
    return Check("nvenc", "轉檔編碼器（NVENC）", WARN,
                 f"h264_nvenc 不可用，將改用 libx264（CPU，較慢）；encode.encoder={enc}")


def _chk_onnxruntime(have_gpu: bool) -> Check:
    dists = []
    for d in ("onnxruntime", "onnxruntime-gpu"):
        try:
            dists.append(f"{d} {md.version(d)}")
        except md.PackageNotFoundError:
            pass
    if importlib.util.find_spec("onnxruntime") is None:
        return Check("onnxruntime", "onnxruntime providers", FAIL, "未安裝 onnxruntime",
                     "CPU 機：pip install -r deploy/requirements-cpu.txt；GPU 機：-r deploy/requirements-gpu.txt")
    if len(dists) > 1:
        return Check("onnxruntime", "onnxruntime providers", FAIL,
                     f"同時裝了 {' 與 '.join(dists)}（兩者互斥，會互相覆蓋）",
                     "pip uninstall -y onnxruntime onnxruntime-gpu 後依配方重裝其中一個")
    import onnxruntime as ort
    prov = ort.get_available_providers()
    detail = f"{', '.join(dists)}；providers={prov}"
    device = str((_load_yaml("sam.yaml").get("runtime") or {}).get("device", "?"))
    detail += f"；sam.yaml runtime.device={device}"
    if have_gpu and "CUDAExecutionProvider" not in prov:
        return Check("onnxruntime", "onnxruntime providers", WARN, detail + "；有 GPU 但只有 CPU provider",
                     "改裝 GPU 配方：pip uninstall -y onnxruntime && pip install -r deploy/requirements-gpu.txt")
    if "CUDAExecutionProvider" in prov:
        live = _cuda_live_probe(ort)
        if live:
            return Check("onnxruntime", "onnxruntime providers", WARN, detail + f"；CUDA 實測：{live}",
                         "CUDA 套件版本與 driver 不合，見 deploy/requirements-gpu.txt 的世代對照；"
                         "SAM/OOB 會自動降級 CPU")
        detail += "；CUDA session 實測 OK"
    elif device == "cuda":
        detail += "（無 CUDA，SAM 會自動降級 CPU）"
    return Check("onnxruntime", "onnxruntime providers", PASS, detail)


def _cuda_live_probe(ort) -> str:
    """拿 SAM decoder（約 20 MB）真的建一次 CUDA session；回空字串＝成功，否則為問題描述。

    只驗得出「CUDA/cuDNN 函式庫載得起來」，驗不出推論時才出現的 kernel 錯誤。
    """
    dec = next((m for m in sam_models() if m.label.endswith("decoder") and m.path.exists()), None)
    if dec is None:
        return ""
    try:
        from .sam_assist.service import _preload_cuda_libs  # 與 SAM 服務同一套預載（R4）
        _preload_cuda_libs()
    except Exception:
        pass
    try:
        ort.set_default_logger_severity(3)   # 壓掉 graph 整理類 warning，避免污染 doctor 輸出
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        s = ort.InferenceSession(str(dec.path), opts,
                                 providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        actual = s.get_providers()
        return "" if actual and actual[0] == "CUDAExecutionProvider" else f"實際落到 {actual}"
    except Exception as e:
        return f"建立 session 失敗：{str(e)[:200]}"


def _chk_model(mf: ModelFile, missing_status: str, missing_fix: str, hash_models: bool) -> Check:
    cid = f"model_{mf.group}_{re.sub(r'[^a-z0-9]+', '_', mf.label.lower()).strip('_')}"
    if not mf.path.is_file():
        detail = f"缺檔：{mf.path}" + (f"（{mf.note}）" if mf.note else "")
        return Check(cid, mf.label, missing_status, detail, missing_fix)
    size = mf.path.stat().st_size
    if not mf.sha256:
        return Check(cid, mf.label, WARN, f"{mf.path}（{_human(size)}）存在，但設定檔無 sha256 可比對",
                     "於對應 yaml 補 sha256")
    if not hash_models:
        return Check(cid, mf.label, PASS, f"{mf.path}（{_human(size)}；--no-hash 略過 sha256）")
    got = sha256_file(mf.path)
    if got != mf.sha256:
        return Check(cid, mf.label, FAIL, f"{mf.path} sha256 不符（{got[:12]}… ≠ {mf.sha256[:12]}…）",
                     f"檔案毀損或版本不對：surgann fetch-models --only {mf.group}（舊檔會改名保留）")
    return Check(cid, mf.label, PASS, f"{mf.path}（{_human(size)}），sha256 正確")


def _chk_oob_model() -> Check:
    """OOB 模型（預設＝repo 附帶；oob.onnx_path 有填＝自訂模型）。附帶檔一律驗 sha256（--no-hash 也驗）。"""
    cid, name = "model_oob_oob_onnx", "OOB ONNX"
    m = resolve_oob()
    if m.error or not m.path.is_file():
        fix = ("確認 preprocess.yaml oob.onnx_path 的路徑；要改回附帶模型就把 onnx_path 留空"
               if m.source == "override" else OOB_RESTORE_FIX)
        return Check(cid, name, FAIL, f"來源 {m.source}：{m.error or f'缺檔 {m.path}'}", fix)
    size = _human(m.path.stat().st_size)
    if m.source == "override":
        where = f"{m.path}（{size}；來源 override＝preprocess.yaml oob.onnx_path 指定的自訂模型）"
        if m.sha_ok is False:
            return Check(cid, name, WARN, where + "，sha256 與附帶模型不同",
                         "若是刻意換用自訂模型可忽略；否則把 preprocess.yaml oob.onnx_path 留空，改回 repo 附帶模型")
        return Check(cid, name, PASS, where + ("，sha256 與附帶模型相同" if m.sha_ok else "（未比對 sha256）"))
    if m.sha_ok is None:
        return Check(cid, name, WARN, f"{m.path}（{size}；來源 bundled）存在，但 preprocess.yaml 無 onnx_sha256 可比對",
                     "於 preprocess.yaml oob 區補 onnx_sha256")
    if not m.sha_ok:
        return Check(cid, name, FAIL, f"{m.path}（來源 bundled）sha256 不符（檔案損毀或被改動）", OOB_RESTORE_FIX)
    return Check(cid, name, PASS, f"{m.path}（{size}；來源 bundled＝repo 附帶），sha256 正確")


def _chk_oob_license() -> Check:
    when = oob_license_accepted()
    if when:
        return Check("oob_license", "OOB 模型授權同意", PASS,
                     f"已同意 CC BY-NC-SA 4.0（{OOB_LICENSE_KEY}={when}，僅限非商業研究）")
    return Check("oob_license", "OOB 模型授權同意", WARN,
                 "尚未同意 OOB 模型授權（CC BY-NC-SA 4.0）：標註功能可用，但影片上傳入庫的去識別化（OOB 偵測）不可用",
                 OOB_ACCEPT_FIX + "（AI agent 不可代為同意，必須停下來問人）")


def _chk_env() -> tuple[list[Check], dict]:
    out = []
    if not config.ENV_FILE.exists():
        out.append(Check("env_file", "deploy/.env", FAIL, f"不存在：{config.ENV_FILE}",
                         "surgann setup --yes（會從 .env.example 建立）"))
        return out, {}
    env = config.load_env()
    missing = [k for k in ENV_KEYS if k not in env]
    mode = config.ENV_FILE.stat().st_mode & 0o777
    detail = f"{config.ENV_FILE}（權限 {oct(mode)}）"
    if missing:
        out.append(Check("env_file", "deploy/.env", WARN, detail + f"；缺鍵：{', '.join(missing)}（將用預設值）",
                         "surgann setup --yes（只補缺的鍵，不動既有值）"))
    elif mode & 0o077:
        out.append(Check("env_file", "deploy/.env", WARN, detail + "；其他使用者可讀（之後會放 token/帳密）",
                         f"chmod 600 {config.ENV_FILE}"))
    else:
        out.append(Check("env_file", "deploy/.env", PASS, detail + "；必要鍵齊全"))
    port = env.get("ANNOTATOR_PORT", str(DEFAULT_PORT))
    if not (port.isdigit() and 1 <= int(port) <= 65535):
        out.append(Check("env_port", "ANNOTATOR_PORT 格式", FAIL, f"ANNOTATOR_PORT={port!r} 不是合法 port",
                         "改成 1024–65535 間的整數，例如 8090"))
    raw_keys = {m.group(1) for m in map(_ENV_LINE.match,
                config.ENV_FILE.read_text(encoding="utf-8").splitlines()) if m}
    for k, why in OBSOLETE_ENV_KEYS.items():
        if k in raw_keys:
            out.append(Check(f"env_obsolete_{k.lower()}", f".env 舊鍵 {k}", WARN, why))
    return out, env


def _chk_data_dir(env: dict) -> list[Check]:
    out = []
    actual = _data_dir()
    expected = expected_data_dir(env).resolve()
    if actual.resolve() != expected:
        out.append(Check("data_dir_config", "data 根解析", WARN,
                         f"依 SURGANN_DATA_DIR 規則應為 {expected}，但平台目前實際使用 {actual}",
                         "pipeline/config.py 尚未支援 SURGANN_DATA_DIR（程式版本過舊），或環境變數與 .env 不一致"))
    if not actual.exists():
        out.append(Check("data_dir", "data 根可寫", FAIL, f"不存在：{actual}",
                         "surgann setup --yes（會建立 data 根與子目錄）"))
        return out
    writable = os.access(actual, os.W_OK | os.X_OK)
    missing = [str(getattr(config, a).relative_to(config.DATA_DIR)) for a in _DATA_DIR_ATTRS
               if hasattr(config, a) and not getattr(config, a).exists()]
    du = shutil.disk_usage(actual)
    free_gb = du.free / 1e9
    src = getattr(config, "DATA_DIR_SOURCE", "")
    detail = (f"{actual}（來源 {src or '?'}；掛載點 {_mount_point(actual)}；剩 {free_gb:.0f} GB / 共 {du.total / 1e9:.0f} GB）")
    if not writable:
        out.append(Check("data_dir", "data 根可寫", FAIL, detail + "；目前使用者無寫入權限",
                         f"sudo chown -R {getpass.getuser()}: {actual}"))
    elif free_gb < FREE_FAIL_GB:
        out.append(Check("data_dir", "data 根可寫", FAIL, detail + "；空間幾乎用完",
                         "清空間或把 SURGANN_DATA_DIR 指到較大的磁碟（見 storage-audit）"))
    elif missing:
        out.append(Check("data_dir", "data 根可寫", WARN, detail + f"；缺子目錄：{', '.join(missing)}",
                         "surgann setup --yes"))
    elif free_gb < FREE_WARN_GB:
        out.append(Check("data_dir", "data 根可寫", WARN, detail + f"；剩餘低於 {FREE_WARN_GB} GB",
                         "留意空間（一支 2 小時標準化影片約 7 GB）"))
    else:
        out.append(Check("data_dir", "data 根可寫", PASS, detail))
    return out


def _chk_registry() -> list[Check]:
    db = config.REGISTRY_DB
    if not db.exists():
        return [Check("registry", "registry.db", FAIL, f"不存在：{db}", "surgann setup --yes（或 surgann init-db）")]
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            n_videos = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] if "videos" in tables else 0
            meta = []
            if "schema_meta" in tables:
                cur = conn.execute("SELECT * FROM schema_meta LIMIT 5")
                cols = [c[0] for c in cur.description]
                meta = [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()
    except sqlite3.Error as e:
        return [Check("registry", "registry.db", FAIL, f"{db} 無法開啟：{e}",
                      "確認沒有毀損；必要時從 backups/ 還原（先問人，R9）")]
    base = f"{db}（{_human(db.stat().st_size)}，{len(tables)} 表，videos={n_videos}）"
    out = [Check("registry", "registry.db", PASS, base)]
    if meta:
        out.append(Check("registry_schema", "registry schema 版本", PASS,
                         "schema_meta：" + json.dumps(meta, ensure_ascii=False)))
    else:
        out.append(Check("registry_schema", "registry schema 版本", WARN,
                         "無 schema_meta 表（v1.3 以前版本只記在註解）",
                         "更新程式後執行 surgann init-db 會補上"))
    return out


def _probe_surgann(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/openapi.json", timeout=3) as r:
            return "SurgAnn" in json.loads(r.read().decode("utf-8")).get("info", {}).get("title", "")
    except Exception:
        return False


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def _port_owner_dir(port: int) -> Optional[Path]:
    """監聽該 port 的行程之工作目錄（只看得到自己帳號的行程；看不到回 None）。"""
    r = _run(["ss", "-ltnpH", f"sport = :{port}"])
    m = re.search(r"pid=(\d+)", r.stdout) if r and r.returncode == 0 else None
    if not m:
        return None
    try:
        return Path(os.readlink(f"/proc/{m.group(1)}/cwd")).resolve()
    except OSError:
        return None


def _chk_port(env: dict) -> Check:
    raw = env.get("ANNOTATOR_PORT", str(DEFAULT_PORT))
    if not raw.isdigit():
        return Check("port", "annotator port", WARN, f"ANNOTATOR_PORT={raw!r} 無效，略過")
    port = int(raw)
    if not port_in_use(port):
        return Check("port", "annotator port", PASS, f"{port} 空閒（annotator 尚未啟動）")
    if _probe_surgann(port):
        owner = _port_owner_dir(port)
        if owner is None:
            return Check("port", "annotator port", WARN,
                         f"{port} 有 SurgAnn Annotator 在跑，但無法確認是不是本安裝（可能是 systemd/他人帳號）",
                         f"ss -ltnp | grep :{port} 確認；同機多站請在 deploy/.env 改用不同 ANNOTATOR_PORT")
        if owner == config.PROJECT_ROOT.resolve():
            return Check("port", "annotator port", PASS, f"{port} 已由本安裝的 SurgAnn Annotator 使用中（服務在跑）")
        return Check("port", "annotator port", FAIL,
                     f"{port} 被【另一份】SurgAnn 安裝占用（{owner}）",
                     "在本安裝的 deploy/.env 改 ANNOTATOR_PORT（或 surgann setup --port <新 port> --force）")
    return Check("port", "annotator port", FAIL, f"{port} 被其他程式占用",
                 f"查是誰：ss -ltnp | grep :{port}；或在 deploy/.env 改 ANNOTATOR_PORT")


def _systemctl_show() -> dict:
    r = _run(["systemctl", "show", f"{SERVICE_NAME}.service", "--no-pager",
              "-p", "LoadState", "-p", "UnitFileState", "-p", "ActiveState", "-p", "FragmentPath"])
    if not r or r.returncode != 0:
        return {}
    return dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)


def _chk_systemd() -> Check:
    if not shutil.which("systemctl"):
        return Check("systemd", "systemd 常駐服務", WARN, "本機無 systemctl", "改用 tmux/nohup，見 INSTALL_agent.md")
    st = _systemctl_show()
    if not st or st.get("LoadState") == "not-found":
        return Check("systemd", "systemd 常駐服務", WARN, f"{SERVICE_NAME} 未安裝（選用：開機自啟/崩潰重啟）",
                     "surgann setup --systemd，再照印出的 sudo 指令安裝")
    frag = Path(st.get("FragmentPath") or "")
    detail = (f"LoadState={st.get('LoadState')} UnitFileState={st.get('UnitFileState')} "
              f"ActiveState={st.get('ActiveState')}（{frag}）")
    try:
        text = frag.read_text(encoding="utf-8") if frag.is_file() else ""
    except OSError:
        text = ""
    if text and str(config.PROJECT_ROOT) not in text:
        return Check("systemd", "systemd 常駐服務", WARN, detail + "；unit 指向的不是本專案目錄",
                     "surgann setup --systemd 重新渲染後依指示重裝")
    gen = UNIT_GENERATED_DIR / f"{SERVICE_NAME}.service"
    if text and gen.exists() and gen.read_text(encoding="utf-8") != text:
        return Check("systemd", "systemd 常駐服務", WARN, detail + "；已安裝的 unit 與最新渲染結果不同",
                     f"sudo cp {gen} {UNIT_INSTALL_PATH} && sudo systemctl daemon-reload && "
                     f"sudo systemctl restart {SERVICE_NAME}")
    if st.get("ActiveState") != "active":
        return Check("systemd", "systemd 常駐服務", WARN, detail,
                     f"journalctl -u {SERVICE_NAME} -n 50 --no-pager 看原因；sudo systemctl start {SERVICE_NAME}")
    return Check("systemd", "systemd 常駐服務", PASS, detail)


def run_doctor(hash_models: bool = True) -> list[Check]:
    """跑全部檢查（只讀；唯一副作用是 CUDA 實測會短暫占用 GPU）。"""
    checks: list[Check] = []

    def guard(fn: Callable, *a):
        try:
            r = fn(*a)
        except Exception as e:  # 檢查本身出錯不應讓 doctor 崩潰
            name = getattr(fn, "__name__", "check").lstrip("_").replace("chk_", "")
            r = Check(name, name, WARN, f"檢查時發生例外：{type(e).__name__}: {e}")
        return r

    def as_list(r):
        return r if isinstance(r, list) else [r]

    checks.append(guard(_chk_python))
    checks.extend(as_list(guard(_chk_packages)))
    checks.extend(as_list(guard(_chk_ffmpeg)))
    have_ffmpeg = bool(shutil.which("ffmpeg"))
    gpu = guard(_chk_gpu)
    if isinstance(gpu, tuple):
        checks.append(gpu[0])
        have_gpu = gpu[1]
    else:
        checks.append(gpu)
        have_gpu = False
    checks.append(guard(_chk_encoder, have_ffmpeg))
    checks.append(guard(_chk_onnxruntime, have_gpu))
    for mf in sam_models():
        checks.append(guard(_chk_model, mf, WARN,
                            "surgann fetch-models --only sam（缺 SAM 時標註照常，只是 AI 輔助停用）",
                            hash_models))
    checks.append(guard(_chk_oob_model))
    checks.append(guard(_chk_oob_license))
    envr = guard(_chk_env)
    if isinstance(envr, tuple):
        env_checks, env = envr
    else:
        env_checks, env = [envr], {}
    checks.extend(env_checks)
    checks.extend(as_list(guard(_chk_data_dir, env)))
    checks.extend(as_list(guard(_chk_registry)))
    checks.append(guard(_chk_port, env))
    checks.append(guard(_chk_systemd))
    return checks


def doctor_report(checks: list[Check]) -> dict:
    counts = {s: sum(c.status == s for c in checks) for s in (PASS, WARN, FAIL)}
    return {"tool": "surgann doctor", "format": 1, "ok": counts[FAIL] == 0,
            "exit_code": 1 if counts[FAIL] else 0, "counts": counts,
            "project_root": str(config.PROJECT_ROOT), "data_dir": str(_data_dir()),
            "checks": [asdict(c) for c in checks]}


def format_doctor_text(checks: list[Check]) -> str:
    lines = [f"SurgAnnFlow doctor — {config.PROJECT_ROOT}", ""]
    for c in checks:
        lines.append(f"[{c.status}] {c.name}：{c.detail}")
        if c.fix and c.status != PASS:
            lines.append(f"       → {c.fix}")
    rep = doctor_report(checks)
    n = rep["counts"]
    lines += ["", f"結果：PASS {n[PASS]} / WARN {n[WARN]} / FAIL {n[FAIL]} — "
              + ("沒有阻斷項" if rep["ok"] else "有阻斷項（FAIL），請依 → 提示修復後重跑 surgann doctor")]
    return "\n".join(lines)


# =============================================================== setup ===

@dataclass
class EnvWriteResult:
    path: Path
    created: bool
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)   # 指定了新值但因未 --force 而保留舊值


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _env_value(raw: str) -> str:
    return raw.strip().strip('"').strip("'")


def write_env(desired: dict[str, str], explicit: set[str], force: bool = False,
              path: Optional[Path] = None, writer: str = "surgann setup") -> EnvWriteResult:
    """把 desired 寫進 .env：保留既有鍵/值/註解；新檔以 .env.example 為底。

    - 檔案不存在：複製 .env.example，範本內的鍵直接換成 desired 值
    - 既有檔：已存在的鍵不動（--force 才覆蓋）；缺的鍵附加到檔尾
    """
    path = path or config.ENV_FILE
    created = not path.exists()
    if created:
        base = ENV_EXAMPLE.read_text(encoding="utf-8") if ENV_EXAMPLE.exists() else ""
    else:
        base = path.read_text(encoding="utf-8")
    lines = base.splitlines()
    res = EnvWriteResult(path=path, created=created)
    seen: set[str] = set()
    for i, line in enumerate(lines):
        m = _ENV_LINE.match(line)
        if not m or m.group(1) not in desired or m.group(1) in seen:
            continue
        key, cur = m.group(1), _env_value(m.group(2))
        seen.add(key)
        new = desired[key]
        if created or (force and cur != new):
            if cur != new:
                lines[i] = f"{key}={new}"
                (res.added if created else res.updated).append(key)
            else:
                (res.added if created else res.kept).append(key)
        else:
            res.kept.append(key)
            if key in explicit and cur != new:
                res.conflicts.append(f"{key}：保留既有 {cur!r}（指定的 {new!r} 未套用；要覆蓋請加 --force）")
    missing = [k for k in desired if k not in seen]
    if missing:
        stamp = datetime.now().strftime("%Y-%m-%d")
        lines += ["", f"# --- {writer} 補上的鍵（{stamp}）---"]
        for k in missing:
            lines.append(f"{k}={desired[k]}")
            res.added.append(k)
    text = "\n".join(lines).rstrip("\n") + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, (path.stat().st_mode & 0o777) if not created else 0o600)
    os.replace(tmp, path)
    return res


def data_subdirs(data_dir: Path) -> list[Path]:
    rels = [getattr(config, a).relative_to(config.DATA_DIR) for a in _DATA_DIR_ATTRS if hasattr(config, a)]
    out = [data_dir / r for r in rels]
    sam_cfg = _load_yaml("sam.yaml")
    out.append(resolve_data_path((sam_cfg.get("model") or {}).get("dir", "models/sam"), data_dir))
    out.append(resolve_data_path((sam_cfg.get("cache") or {}).get("disk_dir", "cache/sam_emb"), data_dir))
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _cli_subprocess(args: list[str], env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    """以子行程跑 surgann 子指令：讓新寫入的 SURGANN_DATA_DIR 在全新的 config 載入下生效。"""
    env = dict(os.environ, **env_extra)
    return subprocess.run([sys.executable, "-m", "pipeline.cli", *args],
                          cwd=str(config.PROJECT_ROOT), env=env, capture_output=True, text=True)


def render_unit(data_dir: Path, tz: str, user: Optional[str] = None,
                venv: Optional[Path] = None) -> tuple[Path, str, list[str]]:
    """從 .service.in 渲染 unit 到 deploy/systemd/generated/；回傳（路徑、內容、注意事項）。"""
    import grp
    notes: list[str] = []
    user = user or getpass.getuser()
    group = grp.getgrgid(os.getgid()).gr_name
    venv = Path(venv or sys.prefix)
    exe = venv / "bin" / "surgann"
    if not exe.exists():
        notes.append(f"找不到 {exe}：請先在該 venv 執行 pip install -e .（否則服務起不來）")
    mounts = []
    for p in (config.PROJECT_ROOT, data_dir):
        mp = _mount_point(p)
        if str(mp) != "/" and str(mp) not in mounts:
            mounts.append(str(mp))
    values = {"@USER@": user, "@GROUP@": group, "@APP_DIR@": str(config.PROJECT_ROOT),
              "@VENV@": str(venv), "@DATA_DIR@": str(data_dir), "@MOUNTS@": " ".join(mounts),
              "@TZ@": tz}
    tpl = UNIT_TEMPLATE.read_text(encoding="utf-8")
    out_lines = []
    for line in tpl.splitlines():
        is_comment = line.lstrip().startswith("#")
        if not is_comment and "@MOUNTS@" in line and not mounts:
            line = "# RequiresMountsFor=（專案與 data 根都在根分割區，無獨立掛載點 → 省略）"
        elif not is_comment and "@TZ@" in line and not tz:
            line = "# Environment=TZ=（deploy/.env 的 TZ 留空 → 用系統時區）"
        for k, v in values.items():
            line = line.replace(k, v)
        out_lines.append(line)
    text = "\n".join(out_lines) + "\n"
    left = sorted(set(re.findall(r"@[A-Z_]+@", text)))
    if left:
        raise RuntimeError(f"unit 範本有未知佔位符：{left}")
    UNIT_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    out = UNIT_GENERATED_DIR / f"{SERVICE_NAME}.service"
    out.write_text(text, encoding="utf-8")
    return out, text, notes


def systemd_install_commands(unit_path: Path, port: int) -> list[str]:
    return [
        f"sudo cp {unit_path} {UNIT_INSTALL_PATH}",
        "sudo systemctl daemon-reload",
        f"sudo systemctl enable --now {SERVICE_NAME}",
        f"systemctl status {SERVICE_NAME} --no-pager          # 預期：Active: active (running)",
        f"curl -s -o /dev/null -w '%{{http_code}}\\n' http://127.0.0.1:{port}/api/config   # 預期：200",
    ]


def run_setup(*, data_dir: Optional[str], port: Optional[int], annotator: Optional[str],
              tz: Optional[str], yes: bool, force: bool, systemd: bool,
              say: Callable[[str], None] = _say,
              ask: Optional[Callable[[str, str], str]] = None) -> int:
    """初始化本機站台。回傳結束碼（0=成功）。ask=None 表示不互動。"""
    env_before = config.load_env()
    interactive = ask is not None and not yes

    def choose(key: str, flag_val, default: str, prompt: str) -> tuple[str, bool]:
        if flag_val is not None:
            return str(flag_val), True
        if key in env_before and not force:
            return env_before[key], False
        if interactive:
            return ask(prompt, env_before.get(key, default)), True
        return env_before.get(key, default), False

    dd_default = str(expected_data_dir(env_before))
    dd_flag = data_dir if data_dir is not None else os.environ.get("SURGANN_DATA_DIR") or None
    dd_raw, dd_explicit = choose("SURGANN_DATA_DIR", dd_flag, dd_default,
                                 "data 根目錄（影片/抽幀/匯出/模型放這；建議放大容量磁碟）")
    port_raw, port_explicit = choose("ANNOTATOR_PORT", port, str(DEFAULT_PORT), "annotator 網頁 port")
    name_raw, name_explicit = choose("ANNOTATOR_NAME", annotator, DEFAULT_ANNOTATOR, "預設標註者名稱")
    tz_raw, tz_explicit = choose("TZ", tz, "", "時區（留空＝用系統時區，例如 Asia/Taipei）")

    if not (port_raw.isdigit() and 1 <= int(port_raw) <= 65535):
        say(f"✗ port 不合法：{port_raw!r}")
        return 2
    # data 根：寫進 .env 的一律是絕對路徑；空字串（沿用舊 .env 的留空）＝預設 <repo>/data
    if dd_raw.strip():
        dd = Path(dd_raw).expanduser()
        dd = (dd if dd.is_absolute() else config.PROJECT_ROOT / dd).resolve()
        dd_store = str(dd)
    else:
        dd = (config.PROJECT_ROOT / "data").resolve()
        dd_store = ""
    desired = {"ANNOTATOR_PORT": port_raw, "ANNOTATOR_NAME": name_raw,
               "SURGANN_DATA_DIR": dd_store, "TZ": tz_raw}
    explicit = {k for k, e in zip(desired, (port_explicit, name_explicit, dd_explicit, tz_explicit)) if e}

    # 1) deploy/.env
    res = write_env(desired, explicit, force=force)
    say(f"[1/4] deploy/.env：{'新建（以 .env.example 為底）' if res.created else '更新'} {res.path}")
    say(f"      新增 {res.added or '-'}；覆蓋 {res.updated or '-'}；保留 {res.kept or '-'}")
    for c in res.conflicts:
        say(f"      ! {c}")
    env_after = config.load_env()
    dd_eff = expected_data_dir(env_after).resolve()
    port_eff = int(env_after.get("ANNOTATOR_PORT", DEFAULT_PORT))

    # 2) data 根與子目錄
    created_dirs = []
    try:
        dd_eff.mkdir(parents=True, exist_ok=True)
        for p in data_subdirs(dd_eff):
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
                created_dirs.append(str(p.relative_to(dd_eff)) if p.is_relative_to(dd_eff) else str(p))
    except OSError as e:
        say(f"✗ [2/4] 無法建立 data 根 {dd_eff}：{e}")
        say(f"      → 若在別顆磁碟：sudo mkdir -p {dd_eff} && sudo chown $USER: {dd_eff}（需要人執行）")
        return 1
    say(f"[2/4] data 根：{dd_eff}（新建子目錄 {created_dirs or '無，皆已存在'}）")

    # 3) registry：已有資料就先備份（R13），再冪等 init-db
    sub_env = {"SURGANN_DATA_DIR": str(dd_eff)}
    if config.REGISTRY_DB.exists() and config.REGISTRY_DB.stat().st_size > 0:
        r = _cli_subprocess(["backup", "--label", "pre-setup"], sub_env)
        if r.returncode != 0:
            say(f"✗ [3/4] init-db 前備份失敗，停手（R13）：{(r.stdout + r.stderr).strip()[-400:]}")
            return 1
        say("[3/4] 既有 registry 已先備份 → backups/（label=pre-setup）")
    r = _cli_subprocess(["init-db"], sub_env)
    if r.returncode != 0:
        say(f"✗ [3/4] init-db 失敗：{(r.stdout + r.stderr).strip()[-400:]}")
        return 1
    say(f"[3/4] registry 就緒：{config.REGISTRY_DB}")

    # 4) systemd（選用）：只渲染＋印指令，不執行 sudo
    if systemd:
        try:
            unit, _, notes = render_unit(dd_eff, env_after.get("TZ", ""))
        except (OSError, RuntimeError) as e:
            say(f"✗ [4/4] systemd 範本渲染失敗：{e}")
            return 1
        say(f"[4/4] systemd unit 已渲染：{unit}")
        for n in notes:
            say(f"      ! {n}")
        if port_in_use(port_eff):
            say(f"      ! port {port_eff} 目前已有程式在用；若是手動開的 surgann annotate，先 Ctrl+C 停掉再裝服務")
        say("      以下需要 sudo，本程式不會代跑——請人（或持有 sudo 的 agent）依序執行：")
        for c in systemd_install_commands(unit, port_eff):
            say(f"        {c}")
    else:
        say("[4/4] systemd：略過（要開機自啟請加 --systemd）")

    say("")
    say("下一步：")
    say("  surgann fetch-models --only sam   # 下載 SAM 模型（約 360 MB）")
    say("  surgann fetch-models --only oob   # 由使用者本人閱讀 OOB 模型授權（CC BY-NC-SA 4.0）並輸入 accept；"
        "AI agent 不可代為同意")
    say("  surgann doctor                    # 健檢，FAIL 為 0 即可")
    say(f"  surgann annotate                  # 瀏覽器開 http://127.0.0.1:{port_eff}")
    return 0


# ======================================================== fetch-models ===

class DownloadError(RuntimeError):
    pass


def download(url: str, dest: Path, sha256: Optional[str], size: Optional[int],
             say: Callable[[str], None] = _say, retries: int = 5, timeout: float = 60) -> None:
    """下載到 dest：`.part` 續傳（HTTP Range）＋重試（指數退避）＋ sha256 驗證後才改名就位。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, retries + 1):
        have = part.stat().st_size if part.exists() else 0
        if size and have > size:
            part.unlink()
            have = 0
        if size and have == size:
            break
        headers = {"User-Agent": "surgann-fetch-models"}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if have and r.status != 206:      # 伺服器不支援續傳 → 從頭
                    have = 0
                length = r.headers.get("Content-Length")
                total = size or (have + int(length) if length else None)
                if have:
                    say(f"      續傳：已有 {_human(have)}")
                next_mark = 0
                with open(part, "ab" if have else "wb") as f:
                    got = have
                    while True:
                        buf = r.read(1 << 20)
                        if not buf:
                            break
                        f.write(buf)
                        got += len(buf)
                        if total:
                            pct = got * 100 // total
                            if pct >= next_mark:
                                say(f"      {pct:3d}%  {_human(got)} / {_human(total)}")
                                next_mark = pct + 10
            if size and part.stat().st_size < size:
                raise DownloadError(f"連線提前結束（{part.stat().st_size}/{size} bytes）")
            break
        except urllib.error.HTTPError as e:
            if e.code == 416 and have:            # 已下載完整（Range 超出）
                break
            if e.code in (401, 403, 404):
                raise DownloadError(f"HTTP {e.code}：{url}") from e
            err = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError, DownloadError, TimeoutError) as e:
            err = str(e)
        if attempt == retries:
            raise DownloadError(f"重試 {retries} 次仍失敗（{err}）；已下載部分保留在 {part}，重跑即續傳")
        wait = min(2 ** attempt, 30)
        say(f"      第 {attempt} 次失敗（{err}），{wait}s 後重試…")
        time.sleep(wait)
    if sha256:
        got = sha256_file(part)
        if got != sha256:
            bad = part.with_name(dest.name + ".sha-mismatch")
            os.replace(part, bad)
            raise DownloadError(f"sha256 不符（{got[:12]}… ≠ {sha256[:12]}…），檔案改名為 {bad}；請重跑")
    os.replace(part, dest)


def _fetch_one(mf: ModelFile, say: Callable[[str], None]) -> bool:
    say(f"- {mf.label} → {mf.path}")
    if mf.path.exists():
        if mf.sha256 and sha256_file(mf.path) == mf.sha256:
            say("      已存在且 sha256 正確，略過")
            return True
        if not mf.sha256:
            say("      已存在（設定檔無 sha256 可驗），略過")
            return True
        if not mf.url:
            say(f"      ✗ 已存在但 sha256 不符，且無下載網址可重抓：{mf.note}")
            return False
        old = mf.path.with_name(mf.path.name + f".old-{datetime.now():%Y%m%d%H%M%S}")
        os.replace(mf.path, old)
        say(f"      既有檔 sha256 不符 → 改名保留為 {old.name}，重新下載")
    if not mf.url:
        say(f"      ✗ 無法自動下載：{mf.note}")
        return False
    try:
        download(mf.url, mf.path, mf.sha256, mf.size, say=say)
    except DownloadError as e:
        say(f"      ✗ {e}")
        return False
    say("      ✓ 下載完成" + ("，sha256 正確" if mf.sha256 else "（設定檔無 sha256，未驗證）"))
    return True


def record_oob_license(day: Optional[str] = None, path: Optional[Path] = None) -> EnvWriteResult:
    """在 .env 寫入/更新 OOB_LICENSE_ACCEPTED=<ISO 日期>（保留其他鍵與註解；沿用 write_env）。

    只能在使用者本人同意授權之後呼叫（accept_oob 內）。
    """
    day = day or datetime.now().date().isoformat()
    datetime.strptime(day, "%Y-%m-%d")      # 格式防呆
    return write_env({OOB_LICENSE_KEY: day}, {OOB_LICENSE_KEY}, force=True, path=path,
                     writer="surgann fetch-models")


def accept_oob(accept_flag: bool, say: Callable[[str], None] = _say,
               ask: Optional[Callable[[str], str]] = None) -> int:
    """OOB：驗附帶檔 → 顯示授權 → 取得同意 → 記錄同意日期。回傳 0／1（檔案問題）／2（未同意）。"""
    m = resolve_oob()
    say(f"- OOB ONNX → {m.path}（來源 {m.source}）")
    if m.error or not m.path.is_file():
        say(f"      ✗ {m.error or '缺檔'}")
        if m.source == "bundled" and not m.error:
            say(f"      → {OOB_RESTORE_FIX}")
        return 1
    if m.source == "bundled":
        if m.sha_ok is False:
            say(f"      ✗ sha256 不符（預期 {m.expected_sha}）。{OOB_RESTORE_FIX}")
            return 1
        say("      sha256 正確" if m.sha_ok else "      ! preprocess.yaml 無 onnx_sha256，未驗證")
    else:
        say("      使用 preprocess.yaml oob.onnx_path 指定的自訂模型（不是 repo 附帶檔）"
            + ("；sha256 與附帶模型不同" if m.sha_ok is False else ""))
    when = oob_license_accepted()
    if when:
        say(f"      已同意授權（{OOB_LICENSE_KEY}={when}），略過；授權全文見 {OOB_LICENSE_FILE}")
        return 0
    if not config.ENV_FILE.exists():
        say(f"      ✗ 尚無 {config.ENV_FILE}：先執行 surgann setup，再重跑本指令")
        return 1
    say(OOB_LICENSE_TEXT)
    say(f"（授權聲明亦見 repo 內 {OOB_LICENSE_FILE}）")
    agreed = accept_flag
    if not agreed and ask is not None:
        agreed = ask("同意以上授權條款請輸入 accept（其他任何輸入＝不同意）：").strip().lower() == "accept"
    if not agreed:
        say("✗ 未同意 OOB 授權，未記錄。影片上傳入庫的去識別化（OOB 偵測）在同意前不可用；標註功能不受影響。\n"
            "  AI agent：這一步必須停下來，把上面的授權聲明給使用者看並問是否同意——不可代為同意、"
            "不可自行加 --accept-oob-license。")
        return 2
    res = record_oob_license()
    say(f"  ✓ 已記錄同意 CC BY-NC-SA 4.0（僅限非商業研究）：{res.path} 的 {OOB_LICENSE_KEY}="
        f"{datetime.now().date().isoformat()}")
    return 0


def run_fetch_models(*, only: Optional[str], accept_oob_license: bool, sam_tag: Optional[str],
                     say: Callable[[str], None] = _say,
                     ask: Optional[Callable[[str], str]] = None) -> int:
    """SAM：下載並驗 sha256；OOB：隨 repo 附帶，只驗檔＋取得授權同意。
    回傳結束碼：0=全部就緒；1=有檔失敗；2=OOB 未同意授權。"""
    if only not in (None, "sam", "oob"):
        say(f"✗ --only 只接受 sam 或 oob（收到 {only!r}）")
        return 2
    ok, code = True, 0
    if only in (None, "sam"):
        say(f"== SAM 模型（sam.yaml；tag={sam_tag or 'model.active'}）==")
        for mf in sam_models(sam_tag):
            ok &= _fetch_one(mf, say)
    if only in (None, "oob"):
        say("== OOB 模型（隨 repo 附帶，不需下載；需同意授權）==")
        c = accept_oob(accept_oob_license, say=say, ask=ask)
        if c == 2:
            code = 2
        elif c:
            ok = False
    if code:
        return code
    return 0 if ok else 1
