"""專案路徑與設定載入（單一來源，避免各處 hard-code 路徑）。

data 根可搬：
  DATA_DIR 解析順序（先找到先用）：
    1. 環境變數 SURGANN_DATA_DIR
    2. deploy/.env 的 SURGANN_DATA_DIR
    3. 預設 <repo>/data
  相對值以 repo 根（PROJECT_ROOT）為基準；支援 ~ 展開。
  raw_videos / videos / frames / exports / work / models / cache 皆衍生自 DATA_DIR；
  REGISTRY_DIR、BACKUPS_DIR 固定在 repo 內（不跟著搬）。
  程式內一律透過 config.DATA_DIR 取得，不得自行拼 PROJECT_ROOT / "data"。

registry schema v1.4 路徑語意：
  6 個路徑欄位存「相對 data 根的 POSIX 路徑」；data 根外保留絕對路徑。
  寫入前 to_rel()、讀出後 to_abs()——所有讀寫 registry 路徑欄位的模組都必須經過這兩個函式。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath
from typing import Optional, Union

import yaml

# pipeline/ 的上層即專案根目錄
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# pipeline/ 套件目錄（隨套件附帶的資源，如 models/oob/oobnet.onnx，以此為基準）
PIPELINE_DIR = Path(__file__).resolve().parent

CONFIGS_DIR = PIPELINE_DIR / "configs"

DEPLOY_DIR = PROJECT_ROOT / "deploy"
ENV_FILE = DEPLOY_DIR / ".env"

# 已淘汰、讀到就忽略的 .env 鍵（留在檔案裡無害；首次讀到時於 stderr 提示一次）
#   OOB_PYTHON —— OOB 推論已改 onnxruntime 在主 venv 內執行，不再有外部 TF venv
DEPRECATED_ENV_KEYS = {
    "OOB_PYTHON": "OOB 推論已改 ONNX（主 venv 內 onnxruntime），此鍵已不使用，可自 deploy/.env 移除",
}
_deprecated_warned: set[str] = set()


def load_env(path: Path | None = None) -> dict[str, str]:
    """讀 deploy/.env（KEY=VALUE，支援 # 註解）。祕密只存這裡，不進版控。

    已淘汰的鍵（DEPRECATED_ENV_KEYS）不回傳，並於 stderr 提示一次。
    """
    path = path or ENV_FILE
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if key in DEPRECATED_ENV_KEYS:
            if key not in _deprecated_warned:
                _deprecated_warned.add(key)
                print(f"[surgann] 提示：{path} 的 {key} 已忽略——{DEPRECATED_ENV_KEYS[key]}",
                      file=sys.stderr)
            continue
        env[key] = val.strip().strip('"').strip("'")
    return env


# ----------------------------------------------------------- data 根解析 -----

DATA_DIR_ENV_KEY = "SURGANN_DATA_DIR"


def _resolve_data_dir() -> tuple[Path, str]:
    """回傳 (DATA_DIR 絕對路徑, 來源說明)。來源：env / .env / default。"""
    val = (os.environ.get(DATA_DIR_ENV_KEY) or "").strip()
    source = "env"
    if not val:
        val = (load_env().get(DATA_DIR_ENV_KEY) or "").strip()
        source = "deploy/.env"
    if not val:
        return PROJECT_ROOT / "data", "default"
    p = Path(val).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    # abspath：正規化 .. 但不解 symlink（data 根本身可以是 symlink）
    return Path(os.path.abspath(p)), source


DATA_DIR, DATA_DIR_SOURCE = _resolve_data_dir()

RAW_VIDEOS_DIR = DATA_DIR / "raw_videos"
INCOMING_DIR = RAW_VIDEOS_DIR / "_incoming"   # 上傳暫置區（病人資料夾；pipeline 全程唯讀）
ARCHIVED_DIR = RAW_VIDEOS_DIR / "archived"    # 入庫成功後原始資料夾搬入（不刪除）
WORK_DIR = DATA_DIR / "work"          # preprocess pipeline 暫存區（每 job 一個子目錄）
VIDEOS_DIR = DATA_DIR / "videos"
FRAMES_DIR = DATA_DIR / "frames"
EXPORTS_DIR = DATA_DIR / "exports"
MODELS_DIR = DATA_DIR / "models"      # 模型檔（models/sam/、models/oob/；不進 git）
CACHE_DIR = DATA_DIR / "cache"        # SAM embedding 等可重算快取
# 新匯出按專案分層：data/exports/{project_id}/{run_id}/（由匯出模組建立）。
# 播放清單模式上線前的舊 run 全部歸檔於此（只搬不刪）。
LEGACY_EXPORTS_DIR = EXPORTS_DIR / "legacy"

# 固定在 repo 內，不跟 data 根搬
REGISTRY_DIR = PROJECT_ROOT / "registry"
REGISTRY_DB = REGISTRY_DIR / "registry.db"
BACKUPS_DIR = PROJECT_ROOT / "backups"


# ------------------------------------------- registry 路徑互轉（schema v1.4）---

PathLike = Union[str, os.PathLike]


def _rel_under(p: Path, root: Path) -> Optional[str]:
    """p 在 root 之內 → 相對 POSIX 字串；否則 None。先比字面（不解 symlink），再比 resolve 後。"""
    try:
        return Path(os.path.abspath(p)).relative_to(os.path.abspath(root)).as_posix()
    except ValueError:
        pass
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def to_rel(path: Optional[PathLike], base: Optional[Path] = None) -> Optional[str]:
    """寫入 registry 前呼叫：絕對路徑 → 相對 data 根的 POSIX 字串。

    - None / 空字串 → 原樣回傳（欄位允許空）。
    - 在 data 根內 → 相對 POSIX 字串（如 `videos/ABCD_0001/ABCD_0001.mp4`；data 根本身 → `.`）。
    - 在 data 根外 → 原樣絕對字串（POSIX 分隔）。
    - 已是相對 → 視為已是 schema v1.4 儲存值，正規化為 POSIX 後回傳（冪等：to_rel(to_rel(x)) == to_rel(x)）。
    base：測試注入用；預設 DATA_DIR。
    """
    if path is None or str(path) == "":
        return path  # type: ignore[return-value]
    p = Path(path)
    if not p.is_absolute():
        return PurePosixPath(*p.parts).as_posix()
    rel = _rel_under(p, base or DATA_DIR)
    return rel if rel is not None else p.as_posix()


def to_abs(stored: Optional[PathLike], base: Optional[Path] = None) -> Optional[Path]:
    """讀出 registry 後呼叫：儲存值 → 絕對 Path。

    - 相對 → 接上 data 根（DATA_DIR / stored）。
    - 已是絕對（schema v1.3 舊值或 data 根外路徑）→ 原樣 Path（不改寫、不檢查存在）。
    - None / 空字串 → 原樣回傳（不報錯）。
    base：測試注入用；預設 DATA_DIR。
    """
    if stored is None or str(stored) == "":
        return stored  # type: ignore[return-value]
    p = Path(stored)
    return p if p.is_absolute() else (base or DATA_DIR) / p


def data_path(value: PathLike) -> Path:
    """設定檔（yaml）中的路徑 → 絕對 Path：相對值一律「相對 data 根」，絕對值原樣。

    例：preprocess.yaml `oob.onnx_path: models/oob/oobnet.onnx` → DATA_DIR/models/oob/oobnet.onnx。
    """
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else DATA_DIR / p


def package_path(value: PathLike) -> Path:
    """隨套件附帶資源的路徑 → 絕對 Path：相對值一律「相對 pipeline/ 套件目錄」，絕對值原樣。

    例：preprocess.yaml `oob.onnx_bundled: models/oob/oobnet.onnx` → pipeline/models/oob/oobnet.onnx。
    """
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else PIPELINE_DIR / p


# ------------------------------------------------------ OOB 授權同意紀錄 -----
# OOB 模型（OOBNet，CC BY-NC-SA 4.0）隨 repo 附帶，但「附帶」不等於使用者已同意授權。
# 同意紀錄：deploy/.env 的 OOB_LICENSE_ACCEPTED=<ISO 日期>，由 `surgann fetch-models --only oob`
# 在使用者同意後寫入（寫入端在 pipeline/setup.py）。這裡只負責讀取端。
# 解析順序同 SURGANN_DATA_DIR：環境變數 > deploy/.env。值必須是 ISO 日期（YYYY-MM-DD）。

OOB_LICENSE_ENV_KEY = "OOB_LICENSE_ACCEPTED"


def oob_license_accepted_value() -> tuple[str, str]:
    """回傳 (原始值, 來源)：來源為 "env" / "deploy/.env" / ""（未設定）。"""
    val = (os.environ.get(OOB_LICENSE_ENV_KEY) or "").strip()
    if val:
        return val, "env"
    val = (load_env().get(OOB_LICENSE_ENV_KEY) or "").strip()
    return (val, "deploy/.env") if val else ("", "")


def oob_license_accepted_on() -> Optional[str]:
    """已同意 → 同意日期（ISO 字串）；未設定或值不是合法 ISO 日期 → None。"""
    import datetime as _dt
    val, _ = oob_license_accepted_value()
    if not val:
        return None
    try:
        return _dt.date.fromisoformat(val[:10]).isoformat() if len(val) >= 10 else None
    except ValueError:
        return None


def oob_license_accepted() -> bool:
    """使用者是否已同意 OOB 模型授權（OOB_LICENSE_ACCEPTED 為合法 ISO 日期）。doctor / preprocess 共用。"""
    return oob_license_accepted_on() is not None


def std_video_path(video_id: str) -> Path:
    """標準化影片的標準落點：<data 根>/videos/{video_id}/{video_id}.mp4。"""
    return VIDEOS_DIR / video_id / f"{video_id}.mp4"


def frame_path(video_id: str, frame_index: int) -> Path:
    """抽幀 JPEG 的標準落點：<data 根>/frames/{video_id}/{video_id}_f{frame:07d}.jpg。"""
    return FRAMES_DIR / video_id / f"{video_id}_f{int(frame_index):07d}.jpg"


def load_config(name: str) -> dict:
    """讀取 pipeline/configs/ 下的 YAML 設定檔。"""
    path = CONFIGS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"設定檔不存在：{path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
