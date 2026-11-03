"""SurgAnn Annotator 後端（temporal/spatial 標註 + 專案管理）：標註 CRUD API +
專案（Roboflow 式 project）/ 入庫 / 匯出 API + 影片與前端靜態服務。

原則：
- 標註直接讀寫 registry `annotations` 表（單一事實來源），無平台格式轉換層。
- 只存 frame index；timestamp 一律由 frame / fps 推導。
- taxonomy 以 registry `projects`/`labels` 表為準（每專案獨立）；
  pipeline/configs/labels_*.yaml 僅作 default 專案的種子與 triplet 三選單後援。
- 身分讀 deploy/.env（帳密不寫進程式碼）；原始影片唯讀。
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import config, jobs, preprocess, registry

PALETTE = [
    "#4dc9b0", "#e6a23c", "#5b9bd5", "#c678dd", "#98c379", "#e06c75",
    "#56b6c2", "#d19a66", "#7f9cf5", "#f471b5", "#34d399", "#f87171",
]

STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPORAL_LAYERS = ("phase", "action", "event", "triplet")
SPATIAL_LAYER = "spatial"
LAYERS = (*TEMPORAL_LAYERS, SPATIAL_LAYER)
PROJECT_TYPES = ("temporal", "spatial", "mixed")
DATASET_EXPORT_KINDS = ("mosaic", "coco", "yolo")   # 統一資料集匯出（標註＋對應 images，直接產 zip）
EXPORT_KINDS = ("temporal", "spatial", *DATASET_EXPORT_KINDS)
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# spatial shape → 座標數規則（偶數座標 = [x1,y1,x2,y2,…]，影片原生像素）
SHAPE_RULES = {
    "bbox": lambda n: n == 4,
    "point": lambda n: n == 2,
    "ellipse": lambda n: n == 4,          # [cx, cy, rx, ry]
    "polygon": lambda n: n >= 6 and n % 2 == 0,
    "polyline": lambda n: n >= 4 and n % 2 == 0,
}

# 背景 ingest 工作狀態（in-memory；server 重啟即清空，raw-scan 會重新反映 DB 事實）
INGEST_JOBS: dict[str, dict] = {}

# 瀏覽器 chunk 上傳暫存區（dot 開頭 → incoming-scan 不會誤認為病人資料夾）
UPLOAD_STAGING_DIRNAME = ".uploads"
UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


# ------------------------------------------------------------ pydantic ------

class AnnotationIn(BaseModel):
    layer: str
    label: str
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    attrs: Optional[dict] = None
    ann_id: Optional[str] = None     # 前端 undo 重建用：有給且不衝突則沿用原 id
    project_id: Optional[str] = None  # 必填（標註按專案隔離）；缺 → 422
    annotator: Optional[str] = None   # 顯示名；server 以 get_or_create_annotator 換 ID


class AnnotationPatch(BaseModel):
    label: Optional[str] = None
    start_frame: Optional[int] = Field(default=None, ge=0)
    end_frame: Optional[int] = Field(default=None, ge=0)
    attrs: Optional[dict] = None
    project_id: Optional[str] = None  # 專案歸屬檢查（?project= 或此欄擇一必填）


class LabelIn(BaseModel):
    layer: str                       # phase / action / event / spatial
    name: str
    color: Optional[str] = None
    project_id: Optional[str] = None  # 省略時：由 video_id 推、再退回 default
    video_id: Optional[str] = None


class GenEventsIn(BaseModel):
    label: str                       # event layer 的 label
    interval_sec: float = Field(gt=0)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    project_id: Optional[str] = None  # 省略時退回影片所屬專案（相容舊前端）
    annotator: Optional[str] = None   # 顯示名；省略時用 .env 預設


class LabelPatch(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None


class ProjectIn(BaseModel):
    name: str
    project_type: str = "mixed"      # temporal / spatial / mixed
    annotation_group: str = ""
    code: str = ""                   # 專案編碼（固定 project_id）；留空自動由名稱產生
    copy_taxonomy_from: str = ""     # 選填：來源 project_id——複製其全部類別與
    #   triplet 三選單到新專案（只複製 taxonomy，絕不複製標註）


class LabelsImportIn(BaseModel):
    from_project: str                # 來源 project_id（匯入其類別到目標專案，合併語意）


class IngestIn(BaseModel):
    filename: str                    # data/raw_videos/ 內的檔名（不含路徑）
    project_id: str = registry.DEFAULT_PROJECT_ID
    series_id: Optional[str] = None  # 掃碟入庫選 series 後由 next_video_id 配號
    video_id: Optional[str] = None   # 相容舊呼叫；未給時必須給 series_id 自動配號


class AnnotatorIn(BaseModel):
    name: str


class ProjectVideoIn(BaseModel):
    video_id: str


class ImportSeriesIn(BaseModel):
    series: str                      # series_id（videos.series 存的是 series_id）


class VideoPatch(BaseModel):
    series: Optional[str] = None     # series_id；空字串＝解除系列


class SeriesIn(BaseModel):
    name: str
    institution: Optional[str] = None
    surgery_type: Optional[str] = None
    note: Optional[str] = None


class SeriesPatch(BaseModel):
    name: Optional[str] = None
    institution: Optional[str] = None
    surgery_type: Optional[str] = None
    note: Optional[str] = None


class PipelineJobIn(BaseModel):
    """入庫 pipeline job（→ pipeline.jobs.enqueue_job）。"""
    patient_folder: str              # data/raw_videos/_incoming/ 下的相對路徑
    series_id: str
    project_id: Optional[str] = None  # 可選：入庫完成後順掛標註專案
    params: Optional[dict] = None     # {"file_order": [...], "surgery_date": "..."}（使用者確認的合併順序）


class ConfirmOobIn(BaseModel):
    segments: List[List[float]] = []  # 待切除段 [[start_sec, end_sec], …]；可空＝不切除


class UploadFileSpec(BaseModel):
    name: str
    size: int = Field(ge=0)


class UploadCompleteIn(BaseModel):
    upload_id: str
    folder: str
    files: List[UploadFileSpec]


class ExportIn(BaseModel):
    kind: str                        # temporal / spatial / mosaic / coco / yolo
    project_id: Optional[str] = None  # 給定時匯出限定該專案
    video_id: Optional[str] = None


class SamPredictIn(BaseModel):
    """SAM 輔助標註 prompt session（階段一 A1：後端推論，前端只收 polygon）。
    座標一律影片原生像素；points/labels 對齊（1=正點、0=負點）。"""
    video_id: str
    frame: int = Field(ge=0)
    points: List[List[float]] = Field(default_factory=list)   # [[x,y], …]
    labels: List[int] = Field(default_factory=list)           # 與 points 對齊
    box: Optional[List[float]] = None                         # [x1,y1,x2,y2]
    epsilon_px: Optional[float] = None   # polygon 簡化強度 px（0.1–20；None=自動頂點數）


EXPORT_SCOPES = ("all", "spatial", "temporal")


class FilteredExportIn(BaseModel):
    """互動式資料集下載（POST /api/projects/{id}/export）。
    對應 pipeline.dataset_export.export_filtered_dataset 的跨區契約參數。"""
    fmt: str                                   # mosaic / coco / yolo
    include_images: bool = True                # False = label only
    scope: str = "all"                         # all / spatial / temporal
    temporal_gate: Optional[Union[str, List[str]]] = None  # temporal label（單一或複選清單＝聯集；
    #   空清單視同 None）；非 None 時 scope 必須為 spatial。向下相容單字串。
    include_labels: Optional[list[str]] = None  # None = 全部 label


# ------------------------------------------------------------- helpers ------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _labels_of(taxonomy: dict, layer: str) -> list[str]:
    if layer == "triplet":
        return [taxonomy["triplet"]["segment_label"]]
    if layer == SPATIAL_LAYER:
        return [l["name"] for l in taxonomy.get("spatial", {}).get("labels", [])]
    return list(taxonomy.get(layer, []))


def _validate_spatial_attrs(video, attrs: dict, start: int, end: int) -> None:
    """spatial 標註的 shape/keypoints 驗證。"""
    shape = (attrs or {}).get("shape")
    kps = (attrs or {}).get("keypoints")
    if shape not in SHAPE_RULES:
        raise HTTPException(422, f"spatial 標註缺少或未知 shape：{shape}")
    if not isinstance(kps, list) or not kps:
        raise HTTPException(422, "spatial 標註至少需要 1 個 keypoint")
    w, h = video["width"], video["height"]
    seen_frames = set()
    for kp in kps:
        f, pts = kp.get("frame"), kp.get("points")
        if not isinstance(f, int) or not (start <= f <= end):
            raise HTTPException(422, f"keypoint frame {f} 不在標註區間 [{start}, {end}]")
        if f in seen_frames:
            raise HTTPException(422, f"keypoint frame {f} 重複")
        seen_frames.add(f)
        if not isinstance(pts, list) or not SHAPE_RULES[shape](len(pts)):
            raise HTTPException(422, f"shape={shape} 的座標數不合法：{pts}")
        if shape == "ellipse":
            cx, cy, rx, ry = pts
            ok = 0 <= cx <= w and 0 <= cy <= h and rx > 0 and ry > 0
        else:
            xs, ys = pts[0::2], pts[1::2]
            ok = all(-1 <= x <= w + 1 for x in xs) and all(-1 <= y <= h + 1 for y in ys)
        if not ok:
            raise HTTPException(422, f"keypoint 座標超出影片範圍（{w}x{h}）：{pts}")


def _row_to_dict(r) -> dict:
    d = dict(r)
    d["attrs"] = json.loads(d.pop("attrs_json") or "{}")
    return d


def _validate(taxonomy: dict, video, layer: str, label: str,
              start: int, end: int, attrs: dict | None = None) -> None:
    if layer not in LAYERS:
        raise HTTPException(422, f"未知 layer：{layer}")
    if label not in _labels_of(taxonomy, layer):
        raise HTTPException(422, f"layer '{layer}' 沒有 label '{label}'（taxonomy 為準）")
    if end < start:
        raise HTTPException(422, f"end_frame ({end}) < start_frame ({start})")
    if end >= video["n_frames"]:
        raise HTTPException(422, f"end_frame ({end}) 超出影片範圍（n_frames={video['n_frames']}）")
    if layer == SPATIAL_LAYER:
        _validate_spatial_attrs(video, attrs or {}, start, end)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


def _safe_component(name: str, what: str = "名稱") -> str:
    """單一路徑元件的防穿越檢查：含路徑分隔符 / dot 開頭 / 空字串一律 422。"""
    raw = str(name).strip()
    n = Path(raw.replace("\\", "/")).name
    if not n or n != raw or n.startswith(".") or n in (".", ".."):
        raise HTTPException(422, f"不合法的{what}：{name!r}")
    return n


def _jobs_call(fn, *args, **kwargs):
    """薄轉發 pipeline.jobs：JobError → 404（不存在）/ 409（狀態不符等）。"""
    try:
        return fn(*args, **kwargs)
    except jobs.JobError as e:
        raise HTTPException(404 if "不存在" in str(e) else 409, str(e))


# ----------------------------------------------------------------- app ------

def create_app() -> FastAPI:
    env = config.load_env()
    annotator_name = env.get("ANNOTATOR_NAME", "annotator1")
    # triplet 三選單後援（專案 extra_json 無 triplet 設定時沿用 yaml）
    triplet_fallback = config.load_config("labels_temporal.yaml").get("triplet", {})

    registry.init_db()

    # 入庫 pipeline 單 worker（pipeline/jobs.py；app 生命週期綁定）。
    # graceful stop 只等到 stage 邊界；逾時（如 cut_encode 長轉檔中）讓 daemon
    # thread 隨程序結束，下次啟動由 Worker.start() 的 resume 收回續跑。
    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        worker = jobs.Worker()
        worker.start()
        _app.state.job_worker = worker
        try:
            yield
        finally:
            worker.stop(graceful=True, timeout=5.0)

    app = FastAPI(title="SurgAnn Annotator", docs_url="/api/docs",
                  openapi_url="/api/openapi.json", lifespan=_lifespan)

    # 前端 UI 檔（html/js/css）一律 no-cache：瀏覽器每次以 ETag 驗證，
    # 檔案沒變回 304、變了立即拿到新版——避免改版後瀏覽器吃舊快取。
    # 影片串流（/media/*.mp4）不受影響，仍可正常快取。
    @app.middleware("http")
    async def _no_cache_ui(request, call_next):
        resp = await call_next(request)
        p = request.url.path
        if p == "/" or p.endswith((".html", ".js", ".css")):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    # -------------------------------------------- taxonomy（依專案組裝）-----

    def _get_project_or_404(conn, project_id: str):
        proj = registry.get_project(conn, project_id)
        if proj is None:
            raise HTTPException(404, f"project '{project_id}' 不存在")
        return proj

    def _taxonomy_for(conn, project_id: str) -> dict:
        """labels 表 → 與舊版 /api/config 相同結構的 taxonomy dict。"""
        proj = _get_project_or_404(conn, project_id)
        tax: dict = {"phase": [], "action": [], "event": [], "colors": {},
                     "spatial": {"labels": []}}
        segment_label = None
        for r in registry.list_labels(conn, project_id):
            if r["layer"] == SPATIAL_LAYER:
                tax["spatial"]["labels"].append(
                    {"name": r["name"], "color": r["color"] or ""})
            elif r["layer"] == "triplet":
                segment_label = r["name"]
            else:
                tax.setdefault(r["layer"], []).append(r["name"])
                if r["color"]:
                    tax["colors"][f"{r['layer']}/{r['name']}"] = r["color"]
        extra = json.loads(proj["extra_json"] or "{}")
        trip = extra.get("triplet") or {}
        tax["triplet"] = {
            "segment_label": segment_label
                             or triplet_fallback.get("segment_label", "Triplet"),
            "instrument": trip.get("instrument")
                          or triplet_fallback.get("instrument", []),
            "verb": trip.get("verb") or triplet_fallback.get("verb", []),
            "target": trip.get("target") or triplet_fallback.get("target", []),
        }
        return tax

    def _project_of_video(conn, video_id: str) -> str:
        """影片的「代表」專案：播放清單第一個（影片與專案為多對多），退回 default。
        僅供未帶 project 參數的舊呼叫相容；新前端一律明確帶 project。"""
        video = registry.get_video(video_id, conn)
        if video is None:
            raise HTTPException(404, f"video {video_id} 不存在")
        pids = registry.video_projects(conn, video_id)
        return pids[0] if pids else registry.DEFAULT_PROJECT_ID

    def _taxonomy_of_video(conn, video) -> dict:
        return _taxonomy_for(conn, _project_of_video(conn, video["video_id"]))

    # ------------------------------------------------------------ config ----

    @app.get("/api/config")
    def get_config(project: Optional[str] = None, video: Optional[str] = None):
        """依專案回傳 taxonomy；?video= 由影片反查專案；無參數 = default 專案。
        oob_license_accepted：是否已同意 OOB 模型授權（入庫頁據此提示；每次即時讀取，
        使用者在終端機同意後重新整理頁面即生效）。"""
        with registry.connect() as conn:
            pid = project
            if video:
                pid = _project_of_video(conn, video)
            pid = pid or registry.DEFAULT_PROJECT_ID
            taxonomy = _taxonomy_for(conn, pid)
        return {"taxonomy": taxonomy, "layers": list(LAYERS),
                "annotator": annotator_name, "project": pid,
                "oob_license_accepted": config.oob_license_accepted()}

    @app.post("/api/labels")
    def add_label(body: LabelIn):
        """手動新增 label（寫入指定專案的 labels 表）。回傳更新後 taxonomy。"""
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "label 名稱不可空白")
        if body.layer not in ("phase", "action", "event", "spatial"):
            raise HTTPException(422, f"不支援對 layer '{body.layer}' 新增 label")
        with registry.connect() as conn:
            pid = body.project_id
            if not pid and body.video_id:
                pid = _project_of_video(conn, body.video_id)
            pid = pid or registry.DEFAULT_PROJECT_ID
            taxonomy = _taxonomy_for(conn, pid)
            if name in _labels_of(taxonomy, body.layer):
                raise HTTPException(409, f"{body.layer} 已有 label '{name}'")
            color = body.color
            if body.layer == "spatial" and not color:
                color = PALETTE[len(taxonomy["spatial"]["labels"]) % len(PALETTE)]
            registry.add_label(conn, pid, body.layer, name, color=color)
            taxonomy = _taxonomy_for(conn, pid)
        return {"ok": True, "project": pid, "taxonomy": taxonomy}

    @app.get("/api/projects/{project_id}/labels")
    def project_labels(project_id: str):
        """專案 taxonomy 管理清單：每個 label 附使用筆數（刪除前提示用）。"""
        with registry.connect() as conn:
            _get_project_or_404(conn, project_id)
            out = []
            for r in registry.list_labels(conn, project_id):
                out.append({
                    "label_id": r["label_id"], "layer": r["layer"],
                    "name": r["name"], "color": r["color"] or "",
                    "in_use": registry.label_usage(
                        conn, project_id, r["layer"], r["name"]),
                })
            return out

    @app.patch("/api/labels/{label_id}")
    def patch_label(label_id: str, body: LabelPatch):
        """改名/改色。改名會同步更新此專案內既有標註的 label。"""
        with registry.connect() as conn:
            row = registry.get_label(conn, label_id)
            if row is None:
                raise HTTPException(404, "label 不存在")
            new_name = row["name"]
            if body.name is not None:
                new_name = body.name.strip()
                if not new_name:
                    raise HTTPException(422, "label 名稱不可空白")
                if new_name != row["name"]:
                    dup = conn.execute(
                        "SELECT 1 FROM labels WHERE project_id = ? AND layer = ? "
                        "AND name = ? AND label_id != ?",
                        (row["project_id"], row["layer"], new_name, label_id),
                    ).fetchone()
                    if dup:
                        raise HTTPException(
                            409, f"{row['layer']} 已有 label '{new_name}'")
            n_renamed = 0
            if new_name != row["name"]:
                n_renamed = registry.label_usage(
                    conn, row["project_id"], row["layer"], row["name"])
            registry.update_label(conn, label_id, name=new_name,
                                  color=body.color, updated_at=_now())
            return {"ok": True, "label_id": label_id, "name": new_name,
                    "renamed_annotations": n_renamed}

    @app.delete("/api/labels/{label_id}")
    def remove_label(label_id: str, force: bool = False):
        """刪 label。已被標註使用時：force=false → 409（前端跳確認）；
        force=true → 連同該專案內使用它的標註一併刪除。"""
        with registry.connect() as conn:
            row = registry.get_label(conn, label_id)
            if row is None:
                raise HTTPException(404, "label 不存在")
            n = registry.label_usage(
                conn, row["project_id"], row["layer"], row["name"])
            if n and not force:
                raise HTTPException(
                    409,
                    f"類別「{row['name']}」已被 {n} 筆標註使用；"
                    f"確認刪除將一併刪除這些標註")
            registry.delete_label(conn, label_id, delete_annotations=True)
            return {"ok": True, "deleted_annotations": n}

    # ---------------------------------------------------------- projects ----

    def _project_dict(conn, row) -> dict:
        d = dict(row)
        d.pop("extra_json", None)
        d["n_videos"] = conn.execute(   # 專案影片數 = 播放清單列數
            "SELECT COUNT(*) AS n FROM project_videos WHERE project_id = ?",
            (row["project_id"],)).fetchone()["n"]
        return d

    @app.get("/api/projects")
    def list_projects():
        with registry.connect() as conn:
            return [_project_dict(conn, r) for r in registry.list_projects(conn)]

    def _copy_labels(conn, src_pid: str, dst_pid: str) -> tuple[int, int]:
        """把來源專案 labels 表全部層複製到目標專案（合併語意：同 layer+name
        已存在則跳過）。只動 labels，絕不碰 annotations。回傳 (匯入, 跳過)。"""
        existing = {(r["layer"], r["name"])
                    for r in registry.list_labels(conn, dst_pid)}
        imported = skipped = 0
        for r in registry.list_labels(conn, src_pid):
            if (r["layer"], r["name"]) in existing:
                skipped += 1
                continue
            registry.add_label(conn, dst_pid, r["layer"], r["name"],
                               color=r["color"], extra_json=r["extra_json"])
            imported += 1
        return imported, skipped

    def _copy_triplet_menus(conn, src_row, dst_pid: str) -> bool:
        """來源專案 extra_json 有 triplet 三選單、目標沒有 → 帶入目標。
        回傳是否有寫入。"""
        src_extra = json.loads(src_row["extra_json"] or "{}")
        if not src_extra.get("triplet"):
            return False
        dst_row = registry.get_project(conn, dst_pid)
        dst_extra = json.loads(dst_row["extra_json"] or "{}")
        if dst_extra.get("triplet"):
            return False
        dst_extra["triplet"] = src_extra["triplet"]
        conn.execute("UPDATE projects SET extra_json = ? WHERE project_id = ?",
                     (json.dumps(dst_extra, ensure_ascii=False), dst_pid))
        return True

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectIn):
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "專案名稱不可空白")
        if body.project_type not in PROJECT_TYPES:
            raise HTTPException(422, f"project_type 必須是 {PROJECT_TYPES}")
        code = body.code.strip()
        if code and not ID_RE.fullmatch(code):
            raise HTTPException(422, "專案編碼只能用英數、-、_（如 chole-phase）")
        copy_from = body.copy_taxonomy_from.strip()
        with registry.connect() as conn:
            src = None
            if copy_from:   # 複製既有專案的 taxonomy（不複製標註）
                src = _get_project_or_404(conn, copy_from)
            if code:
                if registry.get_project(conn, code) is not None:
                    raise HTTPException(409, f"專案編碼 '{code}' 已存在")
                pid = code
            else:
                base = _slugify(name)
                pid, i = base, 2
                while registry.get_project(conn, pid) is not None:
                    pid, i = f"{base}-{i}", i + 1
            registry.create_project(conn, project_id=pid, name=name,
                                    project_type=body.project_type,
                                    annotation_group=body.annotation_group.strip())
            if src is not None:
                _copy_labels(conn, copy_from, pid)
                _copy_triplet_menus(conn, src, pid)
            return _project_dict(conn, registry.get_project(conn, pid))

    @app.post("/api/projects/{project_id}/labels/import")
    def import_labels(project_id: str, body: LabelsImportIn):
        """從其他專案匯入類別到本專案（合併語意：同 layer+name 已存在則跳過；
        目標無 triplet 三選單而來源有時一併帶入）。絕不碰 annotations。"""
        src_pid = body.from_project.strip()
        if not src_pid:
            raise HTTPException(422, "from_project 必填")
        if src_pid == project_id:
            raise HTTPException(422, "來源專案不可與目標專案相同")
        with registry.connect() as conn:
            _get_project_or_404(conn, project_id)
            src = _get_project_or_404(conn, src_pid)
            imported, skipped = _copy_labels(conn, src_pid, project_id)
            triplet_copied = _copy_triplet_menus(conn, src, project_id)
        return {"ok": True, "from_project": src_pid, "imported": imported,
                "skipped": skipped, "triplet_copied": triplet_copied}

    def _video_dict(conn, r, project_id: Optional[str] = None) -> dict:
        """影片卡資料。project_id 給定時 n_annotations 只算該專案的（隔離視角），
        並附 in_project 供前端判斷；一律附 series 與 projects（播放清單反查）。"""
        d = dict(r)
        d["url"] = f"/media/{r['video_id']}/{r['video_id']}.mp4"
        d["thumb_url"] = f"/api/videos/{r['video_id']}/thumb"
        d["group_name"] = d.get("group_name") or "default"
        d["series"] = d.get("series") or ""
        d["projects"] = registry.video_projects(conn, r["video_id"])
        d.pop("project_id", None)   # 已停用欄位，不再對外
        d.pop("source_path", None)  # PHI 收斂：原始路徑不對外
        if d.get("std_path"):       # registry 存相對 data 根；對外維持絕對路徑（API 回傳格式不變）
            d["std_path"] = str(config.to_abs(d["std_path"]))
        if project_id is not None:
            d["n_annotations"] = registry.count_annotations(
                conn, r["video_id"], project_id)
        else:
            d["n_annotations"] = conn.execute(
                "SELECT COUNT(*) AS n FROM annotations WHERE video_id = ?",
                (r["video_id"],)).fetchone()["n"]
        return d

    @app.get("/api/projects/{project_id}/videos")
    def project_videos(project_id: str):
        with registry.connect() as conn:
            _get_project_or_404(conn, project_id)
            return [_video_dict(conn, r, project_id=project_id)
                    for r in registry.list_videos(conn, project_id=project_id)]

    @app.post("/api/projects/{project_id}/videos", status_code=201)
    def add_project_video(project_id: str, body: ProjectVideoIn):
        """把已入庫影片加入專案播放清單（冪等，不複製檔案）。"""
        with registry.connect() as conn:
            _get_project_or_404(conn, project_id)
            if registry.get_video(body.video_id, conn) is None:
                raise HTTPException(404, f"video {body.video_id} 不存在")
            registry.add_video_to_project(conn, project_id, body.video_id)
        return {"ok": True, "project_id": project_id, "video_id": body.video_id}

    @app.post("/api/projects/{project_id}/videos/import-series")
    def import_series(project_id: str, body: ImportSeriesIn):
        """把整個系列的已入庫影片全部加入專案（冪等）。回傳新加入支數。"""
        series = body.series.strip()
        if not series:
            raise HTTPException(422, "series 不可空白")
        with registry.connect() as conn:
            _get_project_or_404(conn, project_id)
            vids = registry.list_videos(conn, series=series)
            if not vids:
                raise HTTPException(404, f"系列「{series}」沒有已入庫影片")
            already = {r["video_id"]
                       for r in registry.list_videos(conn, project_id=project_id)}
            added = 0
            for v in vids:
                if v["video_id"] not in already:
                    registry.add_video_to_project(conn, project_id, v["video_id"])
                    added += 1
        return {"ok": True, "series": series, "n_total": len(vids), "n_added": added}

    @app.delete("/api/projects/{project_id}/videos/{video_id}")
    def remove_project_video(project_id: str, video_id: str, confirm: bool = False):
        """把影片移出專案播放清單。該專案在此片有標註時：
        未帶 confirm → 409 附 n_annotations（前端跳警告）；
        confirm=1 → 連同該專案的標註一併刪除（其他專案不受影響；原始影片檔不會被刪）。"""
        with registry.connect() as conn:
            _get_project_or_404(conn, project_id)
            if registry.get_video(video_id, conn) is None:
                raise HTTPException(404, f"video {video_id} 不存在")
            n = registry.count_annotations(conn, video_id, project_id)
            if n and not confirm:
                raise HTTPException(409, {
                    "n_annotations": n,
                    "message": f"此專案在 {video_id} 有 {n} 筆標註，"
                               f"移出將一併刪除（需帶 confirm=1）"})
            deleted = registry.delete_annotations(conn, video_id, project_id) if n else 0
            removed = registry.remove_video_from_project(conn, project_id, video_id)
            if not removed and not deleted:
                raise HTTPException(404, f"{video_id} 不在專案 {project_id} 的清單中")
        return {"ok": True, "removed": removed, "deleted_annotations": deleted}

    # ------------------------------------------------------------ videos ----

    @app.get("/api/videos")
    def list_videos(series: Optional[str] = None):
        """影片庫清單；?series= 給定時只回該系列的（接 registry 的 series 過濾）。"""
        with registry.connect() as conn:
            return [_video_dict(conn, r)
                    for r in registry.list_videos(conn, series=series)]

    @app.patch("/api/videos/{video_id}")
    def patch_video(video_id: str, body: VideoPatch):
        """改影片庫層級屬性（目前僅 series；值必須是既有 series_id）。"""
        with registry.connect() as conn:
            if registry.get_video(video_id, conn) is None:
                raise HTTPException(404, f"video {video_id} 不存在")
            if body.series is not None:
                sid = body.series.strip()
                if sid and registry.get_series(conn, sid) is None:
                    raise HTTPException(
                        422, f"series '{sid}' 不存在（請先在「影片入庫」頁建立系列）")
                registry.set_video_series(conn, video_id, sid or None)
                if sid:
                    registry.touch_series(conn, sid)
            return _video_dict(conn, registry.get_video(video_id, conn))

    # ------------------------------------------------- series（資產層）------

    def _series_dict(conn, series_id: str) -> dict:
        row = registry.get_series(conn, series_id)
        if row is None:
            raise HTTPException(404, f"series '{series_id}' 不存在")
        d = dict(row)
        d["n_videos"] = conn.execute(
            "SELECT COUNT(*) AS n FROM videos WHERE series = ?",
            (series_id,)).fetchone()["n"]
        return d

    @app.get("/api/series")
    def get_series():
        """系列清單（series 表全列＋衍生影片數）：
        [{series_id, name, institution, surgery_type, note,
          created_at, updated_at, n_videos}, …]"""
        with registry.connect() as conn:
            return registry.list_series_detail(conn)

    @app.post("/api/series", status_code=201)
    def create_series(body: SeriesIn):
        """建立系列（seriesID＝隨機四碼大寫英文，自動產碼查重；名稱唯一）。"""
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "系列名稱不可空白")
        with registry.connect() as conn:
            try:
                sid = registry.create_series(
                    conn, name, institution=body.institution,
                    surgery_type=body.surgery_type, note=body.note)
            except sqlite3.IntegrityError:
                raise HTTPException(409, f"系列名稱「{name}」已存在")
            return _series_dict(conn, sid)

    @app.patch("/api/series/{series_id}")
    def patch_series(series_id: str, body: SeriesPatch):
        """更新系列 metadata（name/institution/surgery_type/note；ID 不可改）。"""
        if body.name is not None and not body.name.strip():
            raise HTTPException(422, "系列名稱不可空白")
        with registry.connect() as conn:
            if registry.get_series(conn, series_id) is None:
                raise HTTPException(404, f"series '{series_id}' 不存在")
            try:
                registry.update_series(
                    conn, series_id, name=body.name,
                    institution=body.institution,
                    surgery_type=body.surgery_type, note=body.note)
            except sqlite3.IntegrityError:
                raise HTTPException(409, f"系列名稱「{body.name}」已存在")
            return _series_dict(conn, series_id)

    # ------------------------------------------------- annotators（名冊）----

    @app.get("/api/annotators")
    def get_annotators():
        with registry.connect() as conn:
            return [{"annotator_id": r["annotator_id"], "name": r["name"]}
                    for r in registry.list_annotators(conn)]

    @app.post("/api/annotators")
    def create_annotator(body: AnnotatorIn):
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "標註者名稱不可空白")
        with registry.connect() as conn:
            aid = registry.get_or_create_annotator(conn, name)
        return {"annotator_id": aid, "name": name}

    @app.get("/api/videos/{video_id}/thumb")
    def thumb(video_id: str):
        with registry.connect() as conn:
            video = registry.get_video(video_id, conn)
        if video is None:
            raise HTTPException(404, f"video {video_id} 不存在")
        out = config.FRAMES_DIR / video_id / "_thumb.jpg"
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            mid = max(0, video["n_frames"] // 2)
            subprocess.run([
                "ffmpeg", "-y", "-ss", str(mid / video["fps"]),
                "-i", str(config.to_abs(video["std_path"])), "-frames:v", "1",
                "-vf", "scale=360:-2", "-q:v", "5", str(out),
            ], capture_output=True)
            if not out.exists():
                raise HTTPException(500, "縮圖產生失敗")
        return FileResponse(out, media_type="image/jpeg")

    # ------------------------------------------------- raw 掃描與入庫 -------

    @app.get("/api/raw-scan")
    def raw_scan(project: Optional[str] = None):
        """掃描 data/raw_videos/（唯讀），回報每個檔案是否已入庫。
        ?project= 給定時每列另附 in_project（該影片是否已在此專案清單）。
        不建議 caseNNN 式命名——video_id 於入庫時依所選 series 由
        registry.next_video_id 自動配號（{seriesID}_{NNNN}）。"""
        with registry.connect() as conn:
            if project is not None:
                _get_project_or_404(conn, project)
            by_source = {}
            for r in registry.list_videos(conn):
                if r["source_path"]:
                    by_source[config.to_abs(r["source_path"]).name] = r["video_id"]
            in_proj = ({r["video_id"] for r in
                        registry.list_videos(conn, project_id=project)}
                       if project is not None else set())
        out = []
        for p in sorted(config.RAW_VIDEOS_DIR.iterdir()):
            if not p.is_file() or p.name.startswith("."):
                continue
            row = {
                "filename": p.name,
                "size": p.stat().st_size,
                "ingested": p.name in by_source,
                "video_id": by_source.get(p.name),
                "job": INGEST_JOBS.get(p.name),
            }
            if project is not None:
                row["in_project"] = by_source.get(p.name) in in_proj
            out.append(row)
        return out

    def _run_ingest(filename: str, video_id: str, project_id: str,
                    series_id: Optional[str] = None) -> None:
        """背景執行：標準化轉檔（唯讀來源）→ 入庫 → 掛 series → 加入專案清單。"""
        from .. import ingest as ingest_mod
        job = INGEST_JOBS[filename]
        try:
            cfg = config.load_config("ingest.yaml")
            ingest_mod.ingest(config.RAW_VIDEOS_DIR / filename, video_id, cfg)
            with registry.connect() as conn:
                registry.add_video_to_project(conn, project_id, video_id)
                if series_id:
                    registry.set_video_series(conn, video_id, series_id)
                    registry.touch_series(conn, series_id)
            job.update(status="done", finished_at=_now())
        except Exception as e:                       # noqa: BLE001 — 回報給前端
            job.update(status="error", error=str(e), finished_at=_now())

    @app.post("/api/ingest", status_code=202)
    def start_ingest(body: IngestIn, tasks: BackgroundTasks):
        filename = body.filename.strip()
        if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
            raise HTTPException(422, f"不合法的檔名：{filename}")
        src = config.RAW_VIDEOS_DIR / filename
        if not src.is_file():
            raise HTTPException(404, f"raw_videos 內找不到 {filename}")
        job = INGEST_JOBS.get(filename)
        if job and job.get("status") == "running":
            raise HTTPException(409, f"{filename} 正在入庫中")
        series_id = (body.series_id or "").strip() or None
        with registry.connect() as conn:
            _get_project_or_404(conn, body.project_id)
            if series_id and registry.get_series(conn, series_id) is None:
                raise HTTPException(
                    422, f"series '{series_id}' 不存在（請先在「影片入庫」頁建立系列）")
            if body.video_id:                        # 相容舊呼叫：手動指定 ID
                video_id = body.video_id.strip()
                if not ID_RE.fullmatch(video_id):
                    raise HTTPException(422, "video_id 只能用英數、-、_")
                if registry.get_video(video_id, conn) is not None:
                    raise HTTPException(409, f"video_id {video_id} 已存在")
            else:                                    # 依 series 自動配號
                if not series_id:
                    raise HTTPException(422, "請先選擇系列（series_id）——"
                                             "video_id 依系列自動配號")
                video_id = registry.next_video_id(conn, series_id)
                pending = {j.get("video_id") for j in INGEST_JOBS.values()
                           if j.get("status") == "running"}
                while video_id in pending:           # 併發入庫中尚未落 DB 的號
                    stem, num = video_id.rsplit("_", 1)
                    video_id = f"{stem}_{int(num) + 1:04d}"
        INGEST_JOBS[filename] = {"status": "running", "video_id": video_id,
                                 "project_id": body.project_id, "started_at": _now()}
        tasks.add_task(_run_ingest, filename, video_id, body.project_id, series_id)
        return {"ok": True, "status": "running", "filename": filename,
                "video_id": video_id}

    # --------------------------------- 上傳入庫 pipeline -------------------
    # 流程：_incoming/ 病人資料夾（掃碟或瀏覽器上傳落地）→ incoming-scan 排序預覽
    # → 人工確認順序 enqueue → worker（merge→oob→[QC]→cut_encode→verify→register）。
    # ★ deid_map 內容絕不經任何 API 回傳（PHI）。

    @app.get("/api/incoming-scan")
    def incoming_scan():
        """掃 data/raw_videos/_incoming/ 的病人資料夾（唯讀）。

        每夾回傳 plan_merge 排序預覽（檔名/順序/大小/duration、單檔免合併旗標）、
        mtime 第二意見警告、進行中 job。支援兩層結構：
        _incoming/{資料夾} 與 _incoming/{batch}/{資料夾}（瀏覽器上傳落地）。
        """
        inc = config.INCOMING_DIR
        pcfg = config.load_config("preprocess.yaml")

        def _has_video(d: Path) -> bool:
            exts = {e.lower() for e in pcfg["merge"]["extensions"]}
            return any(p.is_file() and p.suffix.lower() in exts
                       and not p.name.startswith("._") for p in d.iterdir())

        folders: list[tuple[str, Path]] = []
        if inc.is_dir():
            for d in sorted(inc.iterdir()):
                if not d.is_dir() or d.name.startswith("."):
                    continue                      # .uploads 暫存區等一律跳過
                if _has_video(d):
                    folders.append((d.name, d))
                else:                             # batch 目錄：往下一層找病人資料夾
                    for sub in sorted(d.iterdir()):
                        if sub.is_dir() and not sub.name.startswith(".") \
                                and _has_video(sub):
                            folders.append((f"{d.name}/{sub.name}", sub))

        active: dict[str, dict] = {}
        with registry.connect() as conn:
            for st in jobs.ACTIVE_STATUSES:
                for r in registry.list_pipeline_jobs(conn, status=st):
                    active[r["patient_folder"]] = {
                        "job_id": r["job_id"], "stage": r["stage"],
                        "status": r["status"], "progress": r["progress"]}

        out = []
        for rel, path in folders:
            entry: dict = {"patient_folder": rel, "job": active.get(rel)}
            try:
                plan = preprocess.plan_merge(path, pcfg)
                files = [{"order": f["order"], "name": f["name"],
                          "size_bytes": f["size_bytes"],
                          "duration_sec": f["duration_sec"]}
                         for f in plan.files]      # 不回傳絕對路徑
                by_mtime = [f["name"] for f in sorted(
                    plan.files, key=lambda f: Path(f["path"]).stat().st_mtime)]
                entry.update(
                    files=files, single_file=plan.single_file,
                    total_duration=plan.total_duration,
                    total_bytes=sum(f["size_bytes"] for f in plan.files),
                    mtime_mismatch=(by_mtime != [f["name"] for f in plan.files]))
            except preprocess.PreprocessError as e:
                entry["error"] = str(e)
            out.append(entry)
        return {"incoming_dir": "data/raw_videos/_incoming/", "folders": out}

    # ---- 瀏覽器資料夾上傳（50MB chunk 循序 POST → 拼裝落地）----

    def _staging_dir(upload_id: str, folder: str) -> Path:
        if not UPLOAD_ID_RE.fullmatch(upload_id or ""):
            raise HTTPException(422, f"不合法的 upload_id：{upload_id!r}")
        return (config.INCOMING_DIR / UPLOAD_STAGING_DIRNAME / upload_id /
                _safe_component(folder, "資料夾名"))

    def _received_chunks(staging: Path, filename: str) -> list[int]:
        pat = re.compile(re.escape(filename) + r"\.part(\d{5})$")
        out = []
        if staging.is_dir():
            for p in staging.iterdir():
                m = pat.fullmatch(p.name)
                if m:
                    out.append(int(m.group(1)))
        return sorted(out)

    @app.get("/api/upload/status")
    def upload_status(upload_id: str, folder: str, filename: str):
        """斷點續傳查詢：回報某檔已收到的 chunk 序號清單。"""
        staging = _staging_dir(upload_id, folder)
        fname = _safe_component(filename, "檔名")
        return {"received_chunks": _received_chunks(staging, fname)}

    @app.post("/api/upload/chunk")
    async def upload_chunk(request: Request, upload_id: str, folder: str,
                           filename: str, index: int, total: int):
        """收一個 chunk（raw bytes body，前端 file.slice() 循序 POST）。"""
        if index < 0 or total <= 0 or index >= total:
            raise HTTPException(422, f"chunk 序號不合法：{index}/{total}")
        staging = _staging_dir(upload_id, folder)
        fname = _safe_component(filename, "檔名")
        data = await request.body()
        if not data:
            raise HTTPException(422, "chunk 內容為空")
        staging.mkdir(parents=True, exist_ok=True)
        part = staging / f"{fname}.part{index:05d}"
        tmp = part.with_suffix(part.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.rename(part)                          # 原子性：寫完才算收到
        return {"ok": True, "received": len(_received_chunks(staging, fname))}

    @app.post("/api/upload/complete")
    def upload_complete(body: UploadCompleteIn):
        """全部檔案 chunk 收齊後拼裝＋大小校驗，落地 _incoming/{batch}/{資料夾}。

        回傳 patient_folder（相對路徑），可直接餵給 incoming-scan / enqueue。
        缺 chunk 或大小不符 → 422（暫存保留，前端可續傳後重試）。
        """
        staging = _staging_dir(body.upload_id, body.folder)
        if not staging.is_dir():
            raise HTTPException(404, "找不到此上傳的暫存資料（chunk 尚未收到？）")
        if not body.files:
            raise HTTPException(422, "files 清單不可為空")

        specs = []                                # 先全部驗證，再一次拼裝
        for f in body.files:
            fname = _safe_component(f.name, "檔名")
            parts = [staging / f"{fname}.part{i:05d}"
                     for i in _received_chunks(staging, fname)]
            got = {int(p.name[-5:]) for p in parts}
            missing = [i for i in range(max(got) + 1 if got else 0)
                       if i not in got]
            total_bytes = sum(p.stat().st_size for p in parts)
            if not parts or missing or total_bytes != f.size:
                raise HTTPException(422, {
                    "filename": fname, "expected_bytes": f.size,
                    "received_bytes": total_bytes,
                    "received_chunks": sorted(got),
                    "message": f"{fname} chunk 不完整或大小不符，請續傳後重試"})
            specs.append((fname, parts))

        for fname, parts in specs:                # 串接拼裝（邊拼邊刪 chunk 省磁碟）
            dest = staging / fname
            with open(dest, "wb") as out:
                for p in parts:
                    with open(p, "rb") as src:
                        shutil.copyfileobj(src, out, 1 << 20)
                    p.unlink()
        for stray in staging.glob("*.part[0-9]*"):  # 未列在 files 的殘留 chunk 不落地
            stray.unlink()

        batch = f"upload_{datetime.now().strftime('%Y%m%d')}_{body.upload_id[:8]}"
        dest_dir = config.INCOMING_DIR / batch / staging.name
        if dest_dir.exists():
            raise HTTPException(409, f"落地目錄已存在：{batch}/{staging.name}")
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(dest_dir))
        shutil.rmtree(staging.parent, ignore_errors=True)   # 清掉 upload_id 暫存
        return {"ok": True, "patient_folder": f"{batch}/{dest_dir.name}",
                "n_files": len(specs)}

    # ---- pipeline jobs（薄轉發 pipeline/jobs.py）----

    def _job_out(job: Optional[dict]) -> Optional[dict]:
        """job dict 出站前遮蔽 PHI：surgery_date 只進 deid_map，不回前端。"""
        if job and isinstance(job.get("params"), dict) \
                and job["params"].get("surgery_date"):
            job = {**job, "params": {**job["params"], "surgery_date": "***"}}
        return job

    @app.post("/api/pipeline/jobs", status_code=201)
    def create_pipeline_job(body: PipelineJobIn):
        """病人資料夾排入處理佇列（params.file_order＝UI 確認過的合併順序）。"""
        if body.project_id:
            with registry.connect() as conn:
                _get_project_or_404(conn, body.project_id)
        job_id = _jobs_call(jobs.enqueue_job, body.patient_folder.strip(),
                            body.series_id.strip(), project_id=body.project_id,
                            params=body.params)
        return {"job_id": job_id, "job": _job_out(jobs.get_job(job_id))}

    @app.get("/api/pipeline/jobs")
    def list_pipeline_jobs(status: Optional[str] = None,
                           limit: Optional[int] = None):
        """列 jobs（新到舊）；?status= 過濾 queued/running/awaiting_qc/done/failed。"""
        return [_job_out(j) for j in jobs.list_jobs(status=status, limit=limit)]

    @app.get("/api/pipeline/jobs/{job_id}")
    def get_pipeline_job(job_id: str):
        job = jobs.get_job(job_id)
        if job is None:
            raise HTTPException(404, f"job 不存在：{job_id}")
        return _job_out(job)

    @app.post("/api/pipeline/jobs/{job_id}/confirm-oob")
    def confirm_pipeline_oob(job_id: str, body: ConfirmOobIn):
        """QC 核可：以使用者確認的切除段清單放行 cut_encode（可空＝不切）。"""
        return _job_out(_jobs_call(jobs.confirm_oob, job_id, body.segments))

    @app.post("/api/pipeline/jobs/{job_id}/retry")
    def retry_pipeline_job(job_id: str):
        return _job_out(_jobs_call(jobs.retry_job, job_id))

    @app.delete("/api/pipeline/jobs/{job_id}")
    def delete_pipeline_job(job_id: str, purge: bool = False):
        """刪 job 紀錄；purge=true 連同 data/work/{job_id}/（前端二次確認後才帶）。"""
        return _jobs_call(jobs.delete_job, job_id, purge_workdir=purge)

    @app.get("/api/pipeline/reclaimable")
    def pipeline_reclaimable():
        """可人工清理的空間盤點（只盤點不刪除）。"""
        return jobs.reclaimable_space()

    @app.get("/api/pipeline/jobs/{job_id}/frame")
    def pipeline_job_frame(job_id: str, t: float = 0.0):
        """QC 縮圖：從 workdir 的 merged.mp4 即時抽指定秒數的一張 jpeg。"""
        job = jobs.get_job(job_id)
        if job is None:
            raise HTTPException(404, f"job 不存在：{job_id}")
        merged = (job.get("artifacts") or {}).get("merged_path")
        if not merged or not Path(merged).is_file():
            raise HTTPException(409, "此 job 沒有可抽圖的 merged 影片"
                                     "（尚未合併或工作目錄已清理）")
        t = max(0.0, float(t))
        out = config.WORK_DIR / job_id / "qc" / f"t{int(round(t * 1000))}.jpg"
        if not out.is_file():
            out.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", merged,
                 "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", str(out)],
                capture_output=True)
            if not out.is_file():
                raise HTTPException(500, "抽圖失敗")
        return FileResponse(out, media_type="image/jpeg")

    # ------------------------------------------------------------ export ----

    @app.post("/api/export")
    def run_export(body: ExportIn):
        """觸發既有匯出管線。project_id 給定時只匯出該專案的影片。
        mosaic/coco/yolo 走統一資料集匯出（pipeline.dataset_export），
        自帶對應 images 且直接產 zip（下載端點回傳現成 zip）。"""
        if body.kind not in EXPORT_KINDS:
            raise HTTPException(422, f"kind 必須是 {EXPORT_KINDS}")
        pid = body.project_id
        if pid:
            with registry.connect() as conn:
                _get_project_or_404(conn, pid)
        extra: dict = {}
        try:
            if body.kind == "temporal":
                from ..export_temporal import run_export as run
                out = run(project_id=pid)
            elif body.kind == "spatial":
                from ..export_spatial import run_export as run
                out = run(project_id=pid)
            else:                                    # mosaic / coco / yolo
                from ..dataset_export import DatasetExportError, run_dataset_export
                try:
                    out = run_dataset_export(body.kind, project_id=pid,
                                             make_zip=True)
                except DatasetExportError as e:      # 範圍內無標註等使用者層錯誤
                    raise HTTPException(422, str(e))
                extra = {"n_videos": out.n_videos, "n_images": out.n_images,
                         "n_annotations": out.n_annotations}
        except HTTPException:
            raise
        except Exception as e:                       # noqa: BLE001 — 匯出失敗回報
            raise HTTPException(502, f"{body.kind} 匯出失敗：{e}")
        return {"ok": True, "kind": body.kind, "project_id": pid,
                "run_id": out.run_id, "run_dir": str(config.to_abs(out.run_dir)),
                "download_url": f"/api/export/{out.run_id}/download", **extra}

    @app.get("/api/export/{run_id}/download")
    def download_export(run_id: str):
        """下載 export run 的 zip。路徑一律由 registry export_runs.run_dir 定位
        （data/exports/{project_id}/{run_id}/，zip 同層）；dataset 匯出已在匯出時
        產出 {run_id}.zip → 直接回傳；其餘現場把 run_dir 打包後回傳。"""
        if not ID_RE.fullmatch(run_id):
            raise HTTPException(422, f"不合法的 run_id：{run_id}")
        with registry.connect() as conn:
            row = conn.execute(
                "SELECT run_dir FROM export_runs WHERE run_id = ?",
                (run_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"export run {run_id} 不存在")
        run_dir = config.to_abs(row["run_dir"])   # registry 存相對 data 根 → 轉絕對
        if not run_dir or not run_dir.is_dir():
            # 容錯：舊 run 已搬進 legacy/（部分 DB 列可能未同步更新）
            run_dir = config.LEGACY_EXPORTS_DIR / run_id
            if not run_dir.is_dir():
                raise HTTPException(404, f"export run {run_id} 的目錄不存在")
        ready = run_dir.parent / f"{run_id}.zip"   # dataset_export make_zip 產物
        if ready.is_file():
            return FileResponse(ready, media_type="application/zip",
                                filename=f"{run_id}.zip")
        # 現場打包：zip 落在 run 目錄同層（{project_id}/{run_id}.zip）
        shutil.make_archive(str(run_dir.parent / run_id), "zip",
                            root_dir=str(run_dir.parent), base_dir=run_id)
        return FileResponse(ready, media_type="application/zip",
                            filename=f"{run_id}.zip")

    @app.post("/api/projects/{project_id}/export")
    def filtered_export(project_id: str, body: FilteredExportIn):
        """互動式資料集下載：內容（label / label+images）、範圍（all / spatial /
        temporal / temporal_gate 篩選 spatial）、label 勾選、格式（mosaic/coco/yolo）。
        呼叫 pipeline.dataset_export.export_filtered_dataset（跨區契約，由
        export-convert 實作）並直接串流 zip 回瀏覽器。"""
        if body.fmt not in DATASET_EXPORT_KINDS:
            raise HTTPException(422, f"fmt 必須是 {DATASET_EXPORT_KINDS}")
        if body.scope not in EXPORT_SCOPES:
            raise HTTPException(422, f"scope 必須是 {EXPORT_SCOPES}")
        # 單字串或清單皆可（清單＝聯集）；空清單/空字串視同未指定
        gate = body.temporal_gate
        if isinstance(gate, list):
            gate = [g for g in gate if g] or None
        elif isinstance(gate, str) and not gate.strip():
            gate = None
        if gate is not None and body.scope != "spatial":
            raise HTTPException(
                422, "temporal_gate 只能搭配 scope='spatial'"
                     "（以 temporal 標註的時間範圍篩選 spatial 標註）")
        if body.include_labels is not None and not body.include_labels:
            raise HTTPException(422, "include_labels 不可為空清單（全部匯出請省略或傳 null）")
        with registry.connect() as conn:
            _get_project_or_404(conn, project_id)
        try:
            from ..dataset_export import DatasetExportError, export_filtered_dataset
        except ImportError:
            raise HTTPException(
                501, "匯出功能尚未就緒：pipeline.dataset_export.export_filtered_dataset "
                     "未提供（請通知管理者更新匯出模組）")
        try:
            zip_path = export_filtered_dataset(
                config.REGISTRY_DB, project_id, config.EXPORTS_DIR,
                fmt=body.fmt, include_images=body.include_images,
                scope=body.scope, temporal_gate=gate,
                include_labels=body.include_labels)
        except (DatasetExportError, ValueError) as e:
            # 使用者層錯誤（label 查無 / 範圍內無標註 / 參數不合法）→ 4xx
            raise HTTPException(422, str(e))
        except HTTPException:
            raise
        except Exception as e:                   # noqa: BLE001 — 匯出失敗回報
            raise HTTPException(502, f"{body.fmt} 匯出失敗：{e}")
        zip_path = Path(zip_path)
        if not zip_path.is_file():
            raise HTTPException(502, f"匯出未產生 zip：{zip_path}")
        return FileResponse(zip_path, media_type="application/zip",
                            filename=zip_path.name)

    # ---------------------------------------- SAM 輔助標註（階段一 A1）------
    # 薄包裝 pipeline.sam_assist（跨區契約，由 sam_assist 模組實作）。
    # lazy import：sam_assist 缺依賴或未就緒時 server 照常啟動，端點回 409 降級。
    # 同步 def 端點 → FastAPI 自動丟 threadpool 跑，CPU 推論不卡 event loop。

    @app.post("/api/sam/predict")
    def sam_predict(body: SamPredictIn):
        """prompt（正/負點＋box）→ SAM 推論 → 已簡化 polygon（影片原生像素）。"""
        try:
            from ..sam_assist import SamNotReady, get_service
        except Exception:                        # noqa: BLE001 — 缺依賴即降級
            raise HTTPException(409, "sam_not_ready")
        try:
            out = get_service().predict(body.video_id, body.frame,
                                        body.points, body.labels, body.box,
                                        epsilon_px=body.epsilon_px)
        except SamNotReady:
            raise HTTPException(409, "sam_not_ready")
        except FileNotFoundError as e:           # 影片/幀不存在
            raise HTTPException(404, str(e))
        except ValueError as e:                  # prompt 參數不合法
            raise HTTPException(422, str(e))
        except Exception as e:                   # noqa: BLE001 — 推論失敗回報
            raise HTTPException(502, f"SAM 推論失敗：{e}")
        return {**out, "frame": body.frame}

    @app.get("/api/sam/status")
    def sam_status():
        """SAM 服務狀態；sam_assist 不可 import 也回 200（ready:false）。"""
        try:
            from ..sam_assist import get_service
            return get_service().status()
        except Exception as e:                   # noqa: BLE001 — 降級不報錯
            return {"ready": False, "model": None, "device": None,
                    "detail": f"sam_assist 未就緒：{e}"}

    # ------------------------------------------------------- annotations ----

    def _resolve_annotator(conn, name: Optional[str]) -> tuple[str, str]:
        """顯示名 → (annotator_id, name)。未給時退回 .env 預設名（相容舊前端）。"""
        clean = (name or "").strip() or annotator_name
        return registry.get_or_create_annotator(conn, clean), clean

    @app.post("/api/videos/{video_id}/generate-events")
    def generate_events(video_id: str, body: GenEventsIn):
        """從 start_frame（含）起每 interval_sec 建一個 1-frame event 標註。"""
        with registry.connect() as conn:
            video = registry.get_video(video_id, conn)
            if video is None:
                raise HTTPException(404, f"video {video_id} 不存在")
            pid = body.project_id or _project_of_video(conn, video_id)
            _get_project_or_404(conn, pid)
            taxonomy = _taxonomy_for(conn, pid)
            if body.label not in _labels_of(taxonomy, "event"):
                raise HTTPException(422, f"event 沒有 label '{body.label}'")
            aid, aname = _resolve_annotator(conn, body.annotator)
            end = min(body.end_frame, video["n_frames"] - 1)
            step = max(1, round(body.interval_sec * video["fps"]))
            now = _now()
            created = []
            f = body.start_frame
            while f <= end:
                ann = dict(
                    ann_id=str(uuid.uuid4()), video_id=video_id, layer="event",
                    label=body.label, start_frame=f, end_frame=f,
                    attrs_json="{}", annotator=aname,
                    project_id=pid, annotator_id=aid,
                    created_at=now, updated_at=now,
                )
                registry.add_annotation(
                    conn, video_id=video_id, project_id=pid, annotator_id=aid,
                    layer="event", label=body.label, start_frame=f, end_frame=f,
                    attrs_json="{}", annotator=aname, ann_id=ann["ann_id"],
                    created_at=now, updated_at=now)
                created.append(_row_to_dict(ann))
                f += step
        return {"created": created, "count": len(created)}

    @app.get("/api/videos/{video_id}/annotations")
    def list_anns(video_id: str, project: str):
        """列影片標註。?project= 必填（標註按專案隔離；缺 → 422），
        只回該專案的標註——影片庫瀏覽模式一律不載標註。"""
        with registry.connect() as conn:
            if registry.get_video(video_id, conn) is None:
                raise HTTPException(404, f"video {video_id} 不存在")
            _get_project_or_404(conn, project)
            return [_row_to_dict(r)
                    for r in registry.list_annotations(conn, video_id,
                                                       project_id=project)]

    @app.post("/api/videos/{video_id}/annotations", status_code=201)
    def create_ann(video_id: str, body: AnnotationIn):
        if not body.project_id:
            raise HTTPException(422, "project_id 必填（標註按專案隔離）")
        with registry.connect() as conn:
            video = registry.get_video(video_id, conn)
            if video is None:
                raise HTTPException(404, f"video {video_id} 不存在")
            _get_project_or_404(conn, body.project_id)
            taxonomy = _taxonomy_for(conn, body.project_id)
            _validate(taxonomy, video, body.layer, body.label,
                      body.start_frame, body.end_frame, body.attrs)
            aid, aname = _resolve_annotator(conn, body.annotator)
            now = _now()
            new_id = str(uuid.uuid4())
            if body.ann_id and registry.get_annotation(conn, body.ann_id) is None:
                new_id = body.ann_id   # undo 重建：沿用原 ann_id（不衝突才接受）
            ann = dict(
                ann_id=new_id, video_id=video_id, layer=body.layer,
                label=body.label, start_frame=body.start_frame,
                end_frame=body.end_frame,
                attrs_json=json.dumps(body.attrs or {}, ensure_ascii=False),
                annotator=aname, project_id=body.project_id, annotator_id=aid,
                created_at=now, updated_at=now,
            )
            registry.add_annotation(
                conn, video_id=video_id, project_id=body.project_id,
                annotator_id=aid, layer=body.layer, label=body.label,
                start_frame=body.start_frame, end_frame=body.end_frame,
                attrs_json=ann["attrs_json"], annotator=aname, ann_id=new_id,
                created_at=now, updated_at=now)
            return _row_to_dict(ann)

    @app.patch("/api/annotations/{ann_id}")
    def patch_ann(ann_id: str, body: AnnotationPatch,
                  project: Optional[str] = None):
        """改標註。呼叫端必須帶專案（?project= 或 body.project_id；缺 → 422）；
        與該筆標註自身的專案不符 → 404（不洩漏其他專案標註的存在性）。"""
        pid = project or body.project_id
        if not pid:
            raise HTTPException(422, "project 必填（標註按專案隔離）")
        with registry.connect() as conn:
            _get_project_or_404(conn, pid)
            row = registry.get_annotation(conn, ann_id, project_id=pid)
            if row is None:
                raise HTTPException(404, "annotation 不存在")
            video = registry.get_video(row["video_id"], conn)
            taxonomy = _taxonomy_for(conn, pid)   # taxonomy 以標註所屬專案為準
            new_label = body.label if body.label is not None else row["label"]
            new_start = body.start_frame if body.start_frame is not None else row["start_frame"]
            new_end = body.end_frame if body.end_frame is not None else row["end_frame"]
            new_attrs = body.attrs if body.attrs is not None else json.loads(row["attrs_json"] or "{}")
            _validate(taxonomy, video, row["layer"], new_label, new_start, new_end, new_attrs)
            fields = dict(label=new_label, start_frame=new_start,
                          end_frame=new_end, updated_at=_now())
            if body.attrs is not None:
                fields["attrs_json"] = json.dumps(body.attrs, ensure_ascii=False)
            registry.update_annotation(conn, ann_id, **fields)
            return _row_to_dict(registry.get_annotation(conn, ann_id))

    @app.delete("/api/annotations/{ann_id}", status_code=204)
    def delete_ann(ann_id: str, project: str):
        """刪標註。?project= 必填（缺 → 422）；與該筆標註自身的專案不符
        → 404（不洩漏其他專案標註的存在性）。"""
        with registry.connect() as conn:
            _get_project_or_404(conn, project)
            if not registry.delete_annotation(conn, ann_id, project_id=project):
                raise HTTPException(404, "annotation 不存在")

    # 影片串流（StaticFiles 原生支援 byte-range）；來源目錄唯讀，不受影響
    app.mount("/media", StaticFiles(directory=config.VIDEOS_DIR), name="media")
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app
