"""上傳入庫 pipeline 的單 worker 工作佇列（pipeline_jobs 表）。

流程：

    enqueue_job() → [worker] merge → oob →（status=awaiting_qc，QC 關卡）
      → confirm_oob()（API 呼叫，使用者可修改切除段）→ [worker] cut_encode
      → verify → register → done

核心設計：
- **單 thread serial**（單 GPU，全程不並行）；Worker.start()/stop(graceful)/
  啟動時自動 resume（status=running 的中斷 job 收回為 queued，從原 stage 續跑）。
- **產物快取**：data/work/{job_id}/ 下 merged.mp4 / oob.csv 存在即跳過
  （preprocess 各函式內建 resume），kill 後重啟不重算。
- **進度**：各 stage 權重（preprocess.yaml jobs.stage_weights）＋ merge / oob /
  cut_encode 三個長時 stage 的 stage 內 progress callback →
  pipeline_jobs.progress（0–100 全程）持續落 DB（節流見 _stage_progress_cb）。
- **原始檔唯讀**：_incoming 內原始檔全程唯讀；唯一允許的變動是 register 成功後把整個
  病人資料夾搬到 data/raw_videos/archived/（使用者核定之固定政策，
  不刪除；可清理空間由 reclaimable_space() 回報，實際刪除永遠人工確認）。
- **PHI 收斂**：videos.source_path 不寫原始路徑；原始檔名/sha256/順序/手術日期
  只進 deid_map（API 永不回傳）。

pipeline/annotator/server.py 掛載方式：

    from pipeline import jobs
    worker = jobs.Worker()          # app 啟動時
    worker.start()
    # API handlers 直接呼叫模組函式：
    jobs.enqueue_job(...) / jobs.confirm_oob(...) / jobs.retry_job(...)
    jobs.delete_job(...) / jobs.get_job(...) / jobs.list_jobs(...)
    jobs.reclaimable_space()
    worker.stop(graceful=True)      # app 關閉時

所有函式的 db_path/目錄參數僅供測試注入，正式呼叫一律用預設（config）。
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

from . import config, ingest, preprocess, registry
from .preprocess import PreprocessError

logger = logging.getLogger("surgann.jobs")

# stage 執行順序（awaiting_qc 是 status 不是 stage：oob 完成後停在 stage=oob、
# status=awaiting_qc；confirm_oob() 核可後轉 stage=cut_encode、status=queued）
STAGES = ("merge", "oob", "cut_encode", "verify", "register")
ACTIVE_STATUSES = ("queued", "running", "awaiting_qc")
_DEFAULT_WEIGHTS = {"merge": 10, "oob": 30, "cut_encode": 45,
                    "verify": 5, "register": 10}
_MIN_KEEP_SEC = 0.04  # confirm 後補集的最小 in-body 段（1 frame @25fps）


class JobError(RuntimeError):
    """job 佇列操作的可預期錯誤（狀態不符、資料夾不存在等）。"""


# ------------------------------------------------------------------ helpers --

def _load_cfg() -> dict:
    return config.load_config("preprocess.yaml")


def _weights(cfg: dict) -> dict[str, float]:
    w = dict(_DEFAULT_WEIGHTS)
    w.update((cfg.get("jobs") or {}).get("stage_weights") or {})
    return w


def _parse_job(row) -> Optional[dict]:
    """DB 列 → JSON-friendly dict（params_json/artifacts_json 解開為 dict）。"""
    if row is None:
        return None
    d = dict(row)
    d["params"] = json.loads(d.pop("params_json") or "{}")
    d["artifacts"] = json.loads(d.pop("artifacts_json") or "{}")
    return d


def _complement(segments, total: float) -> list[list[float]]:
    """已排序無重疊的段清單 → [0,total] 補集（keep↔cut 互轉共用）。"""
    out: list[list[float]] = []
    cursor = 0.0
    for s, e in segments:
        if s - cursor > 1e-6:
            out.append([round(cursor, 3), round(float(s), 3)])
        cursor = max(cursor, float(e))
    if total - cursor > 1e-6:
        out.append([round(cursor, 3), round(total, 3)])
    return out


def _normalize_cut_segments(segments, total: float) -> list[list[float]]:
    """驗證/正規化使用者確認的切除段：排序、夾入 [0,total]、合併重疊。"""
    segs: list[list[float]] = []
    for item in segments or []:
        try:
            s, e = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            raise JobError(f"切除段格式錯誤（需 [start_sec, end_sec]）：{item!r}")
        s, e = max(0.0, s), min(total, e)
        if e - s <= 0:
            continue
        segs.append([round(s, 3), round(e, 3)])
    segs.sort()
    merged: list[list[float]] = []
    for s, e in segs:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def _dir_bytes(path: Path) -> int:
    if not path or not Path(path).exists():
        return 0
    return sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())


# --------------------------------------------------------------- module API --
# （annotator server 直接 import 呼叫；db_path 等參數僅測試注入用）

def enqueue_job(patient_folder: str, series_id: str,
                project_id: Optional[str] = None,
                params: Optional[dict] = None, *,
                db_path=None, incoming_dir: Optional[Path] = None) -> str:
    """把 _incoming 下的病人資料夾排入佇列（stage=merge, status=queued）。回傳 job_id。

    Args:
        patient_folder: data/raw_videos/_incoming/ 下的資料夾（相對路徑）。
        series_id: 影片完成後歸屬的 series（必須已存在）。
        project_id: 可選；入庫後順掛的標註專案。
        params: 可選 dict，UI 已確認的參數（決策 H）：
            - "file_order": [檔名, …] 使用者拖曳確認後的合併順序
              （未給則用 plan_merge 檔名排序預設；檔名集合必須與資料夾一致）
            - "merge_plan": plan_merge 回傳的完整 MergePlan dict（優先於 file_order）
            - "surgery_date": "YYYY-MM-DD"（僅寫入 deid_map，PHI）

    Raises:
        JobError: 資料夾不存在/越界、series 不存在、同資料夾已有進行中 job。
    """
    incoming = Path(incoming_dir) if incoming_dir else config.INCOMING_DIR
    folder = (incoming / patient_folder).resolve()
    try:
        rel = folder.relative_to(incoming.resolve())
    except ValueError:
        raise JobError(f"patient_folder 必須位於 {incoming} 之內：{patient_folder}")
    if not folder.is_dir():
        raise JobError(f"病人資料夾不存在：{folder}")

    params = dict(params or {})
    job_id = str(uuid.uuid4())
    with registry.connect(db_path) as conn:
        if registry.get_series(conn, series_id) is None:
            raise JobError(f"series 不存在：{series_id}")
        dup = conn.execute(
            "SELECT job_id FROM pipeline_jobs WHERE patient_folder = ? "
            f"AND status IN ({', '.join('?' for _ in ACTIVE_STATUSES)})",
            (str(rel), *ACTIVE_STATUSES)).fetchone()
        if dup:
            raise JobError(f"此資料夾已有進行中 job：{dup['job_id']}")
        registry.insert_pipeline_job(
            conn, job_id=job_id, patient_folder=str(rel), series_id=series_id,
            project_id=project_id, stage="merge", status="queued", progress=0.0,
            params_json=json.dumps(params, ensure_ascii=False),
            artifacts_json="{}")
    logger.info("enqueue job %s folder=%s series=%s", job_id, rel, series_id)
    return job_id


def confirm_oob(job_id: str, segments, *, db_path=None) -> dict:
    """QC 核可：使用者確認/修改「待切除段」後，讓 job 進入 cut_encode。

    Args:
        segments: 切除段清單 [[start_sec, end_sec], …]（秒，可為空 = 不切除）。
            以使用者送回的清單為準（可增刪/調整 pipeline 建議的段）。

    Returns:
        更新後的 job dict（同 get_job）。

    Raises:
        JobError: job 不存在或 status != awaiting_qc、切除段覆蓋整支影片。
    """
    with registry.connect(db_path) as conn:
        job = _parse_job(registry.get_pipeline_job(conn, job_id))
        if job is None:
            raise JobError(f"job 不存在：{job_id}")
        if job["status"] != "awaiting_qc":
            raise JobError(
                f"job 不在 QC 關卡（status={job['status']}），無法 confirm")
        art = job["artifacts"]
        total = float(art["merged_duration"])
        cut = _normalize_cut_segments(segments, total)
        # in-body keep = 切除段補集；過短段（<1 frame）視為浮點雜訊丟棄
        keep = [[s, e] for s, e in _complement(cut, total)
                if e - s >= _MIN_KEEP_SEC]
        if not keep:
            raise JobError("切除段覆蓋整支影片，沒有任何 in-body 段可保留")
        art["segments_oob_confirmed"] = cut
        art["segments_keep_confirmed"] = keep
        art["qc_confirmed_at"] = registry._now_iso()
        registry.update_pipeline_job(
            conn, job_id, stage="cut_encode", status="queued",
            artifacts_json=json.dumps(art, ensure_ascii=False))
        row = registry.get_pipeline_job(conn, job_id)
    logger.info("confirm_oob %s：cut=%d 段 keep=%d 段", job_id, len(cut), len(keep))
    return _parse_job(row)


def retry_job(job_id: str, *, db_path=None) -> dict:
    """失敗 job 重排入佇列，從失敗的 stage 續跑（工作目錄產物快取沿用）。

    Raises:
        JobError: job 不存在或 status != failed。
    """
    with registry.connect(db_path) as conn:
        job = registry.get_pipeline_job(conn, job_id)
        if job is None:
            raise JobError(f"job 不存在：{job_id}")
        if job["status"] != "failed":
            raise JobError(f"只有 failed job 可 retry（status={job['status']}）")
        registry.update_pipeline_job(conn, job_id, status="queued", error=None)
        row = registry.get_pipeline_job(conn, job_id)
    logger.info("retry job %s（stage=%s）", job_id, row["stage"])
    return _parse_job(row)


def delete_job(job_id: str, purge_workdir: bool = False, *,
               db_path=None, work_dir: Optional[Path] = None) -> dict:
    """刪除 job 列（清理失敗/已完成 job 用）；purge_workdir=True 連同 data/work/{job_id}/。

    ★ 本函式只執行，不問問題——實際檔案刪除的二次確認由 API 層（annotator server）負責。
    running 中的 job 不可刪（先 stop worker 或等 stage 收尾）。

    Returns:
        {"job_id", "deleted": bool, "workdir_purged": bool, "freed_bytes": int}
    """
    wroot = Path(work_dir) if work_dir else config.WORK_DIR
    with registry.connect(db_path) as conn:
        job = registry.get_pipeline_job(conn, job_id)
        if job is None:
            raise JobError(f"job 不存在：{job_id}")
        if job["status"] == "running":
            raise JobError("job 正在執行中，不可刪除（先停 worker 或等其收尾）")
        deleted = registry.delete_pipeline_job(conn, job_id)
    freed = 0
    purged = False
    workdir = wroot / job_id
    if purge_workdir and workdir.is_dir():
        freed = _dir_bytes(workdir)
        shutil.rmtree(workdir, ignore_errors=True)
        purged = True
    logger.info("delete job %s（purge=%s freed=%d）", job_id, purged, freed)
    return {"job_id": job_id, "deleted": deleted,
            "workdir_purged": purged, "freed_bytes": freed}


def get_job(job_id: str, *, db_path=None) -> Optional[dict]:
    """查單一 job（params/artifacts 已解為 dict）；不存在回 None。"""
    with registry.connect(db_path) as conn:
        return _parse_job(registry.get_pipeline_job(conn, job_id))


def list_jobs(status: Optional[str] = None, limit: Optional[int] = None, *,
              db_path=None) -> list[dict]:
    """列 jobs（新到舊）；status 可過濾（queued/running/awaiting_qc/done/failed）。"""
    with registry.connect(db_path) as conn:
        return [_parse_job(r) for r in
                registry.list_pipeline_jobs(conn, status=status, limit=limit)]


def reclaimable_space(*, db_path=None, work_dir: Optional[Path] = None,
                      archived_dir: Optional[Path] = None) -> dict:
    """回報可人工清理的空間（UI「可清理空間」；本函式只盤點，一律不刪）。

    Returns:
        {"archived_dir": str, "archived_bytes": int,
         "failed_jobs": [{"job_id", "patient_folder", "workdir_bytes"}, …],
         "failed_workdir_bytes": int, "total_bytes": int}
    """
    wroot = Path(work_dir) if work_dir else config.WORK_DIR
    aroot = Path(archived_dir) if archived_dir else config.ARCHIVED_DIR
    archived_bytes = _dir_bytes(aroot)
    failed = []
    with registry.connect(db_path) as conn:
        rows = registry.list_pipeline_jobs(conn, status="failed")
    for r in rows:
        b = _dir_bytes(wroot / r["job_id"])
        failed.append({"job_id": r["job_id"],
                       "patient_folder": r["patient_folder"],
                       "workdir_bytes": b})
    failed_bytes = sum(f["workdir_bytes"] for f in failed)
    return {"archived_dir": str(aroot), "archived_bytes": archived_bytes,
            "failed_jobs": failed, "failed_workdir_bytes": failed_bytes,
            "total_bytes": archived_bytes + failed_bytes}


# -------------------------------------------------------------------- Worker --

class Worker:
    """單 thread serial worker（單 GPU：全程不並行，一次只跑一個 job 的一個 stage）。

    - start()：確保 schema → 收回中斷 job（status=running → queued，stage 不動）
      → 背景 thread 開始輪詢佇列（FIFO）。
    - stop(graceful=True)：預設等當前 stage 收尾後停；graceful=False 立即返回
      （當前 stage 在 daemon thread 內繼續到程序結束，job 於下次 start() 收回續跑）。
    - resume：靠 stage 欄位 + data/work/{job_id}/ 產物快取（merged.mp4/oob.csv
      存在即跳過），從中斷/失敗的 stage 續起。

    建構參數僅供測試注入隔離環境；正式使用一律無參數（讀 config 與 preprocess.yaml）。
    """

    def __init__(self, *, db_path=None, cfg: Optional[dict] = None,
                 ingest_cfg: Optional[dict] = None,
                 work_dir: Optional[Path] = None,
                 incoming_dir: Optional[Path] = None,
                 archived_dir: Optional[Path] = None,
                 videos_dir: Optional[Path] = None,
                 poll_interval: Optional[float] = None):
        self.db_path = db_path
        self.cfg = cfg or _load_cfg()
        self.ingest_cfg = ingest_cfg or config.load_config("ingest.yaml")
        self.work_dir = Path(work_dir) if work_dir else config.WORK_DIR
        self.incoming_dir = (Path(incoming_dir) if incoming_dir
                             else config.INCOMING_DIR)
        self.archived_dir = (Path(archived_dir) if archived_dir
                             else config.ARCHIVED_DIR)
        self.videos_dir = Path(videos_dir) if videos_dir else config.VIDEOS_DIR
        jcfg = self.cfg.get("jobs") or {}
        self.poll_interval = (poll_interval if poll_interval is not None
                              else float(jcfg.get("poll_interval_sec", 2)))
        # stage 內進度落 DB 的節流（preprocess.yaml jobs.*）
        self._throttle_sec = float(jcfg.get("progress_db_min_interval_sec", 1.0))
        self._throttle_pct = float(jcfg.get("progress_db_min_delta_pct", 1.0))
        self._weights = _weights(self.cfg)
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------- lifecycle --

    def start(self) -> None:
        """啟動 worker thread；先收回上次中斷的 job（resume）。冪等（已跑則忽略）。"""
        if self._thread and self._thread.is_alive():
            return
        registry.init_db(self.db_path)
        with registry.connect(self.db_path) as conn:
            n = conn.execute(
                "UPDATE pipeline_jobs SET status = 'queued', updated_at = ? "
                "WHERE status = 'running'", (registry._now_iso(),)).rowcount
        if n:
            logger.info("resume：收回 %d 個中斷 job（status running → queued）", n)
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="surgann-job-worker", daemon=True)
        self._thread.start()

    def stop(self, graceful: bool = True, timeout: Optional[float] = None) -> None:
        """停止 worker。graceful=True（預設）阻塞至當前 stage 收尾；False 立即返回。"""
        self._stop_evt.set()
        if graceful and self._thread and self._thread.is_alive():
            self._thread.join(timeout)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------ loop --

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            job = self._claim_next()
            if job is None:
                self._stop_evt.wait(self.poll_interval)
                continue
            try:
                self._run_stages(job)
            except Exception:
                err = traceback.format_exc()
                logger.error("job %s 失敗：\n%s", job["job_id"], err)
                self._update(job["job_id"], status="failed", error=err)

    def _claim_next(self) -> Optional[dict]:
        with registry.connect(self.db_path) as conn:
            rows = registry.list_pipeline_jobs(conn, status="queued", limit=1,
                                               oldest_first=True)
        return _parse_job(rows[0]) if rows else None

    def _run_stages(self, job: dict) -> None:
        job_id = job["job_id"]
        stage = job["stage"]
        if stage not in STAGES:  # done（防禦：不應該發生）
            return
        (self.work_dir / job_id).mkdir(parents=True, exist_ok=True)
        for st in STAGES[STAGES.index(stage):]:
            if self._stop_evt.is_set():  # graceful stop：stage 邊界收手
                self._update(job_id, status="queued")
                logger.info("graceful stop：job %s 停在 stage=%s（已排回佇列）",
                            job_id, st)
                return
            self._update(job_id, stage=st, status="running",
                         progress=self._base(st), error=None)
            logger.info("job %s → stage %s", job_id, st)
            getattr(self, f"_stage_{st}")(job_id)
            if st == "oob":  # QC 關卡：停下等 confirm_oob()
                self._update(job_id, status="awaiting_qc",
                             progress=self._base("cut_encode"))
                logger.info("job %s awaiting_qc（等待人工核可切除段）", job_id)
                return
        self._update(job_id, stage="done", status="done", progress=100.0)
        logger.info("job %s done", job_id)

    # -------------------------------------------------------------- helpers --

    def _base(self, stage: str) -> float:
        """stage 起點的全程進度（前面各 stage 權重和）；stage='done' 回 100。"""
        if stage not in STAGES:
            return 100.0
        idx = STAGES.index(stage)
        return float(sum(self._weights[s] for s in STAGES[:idx]))

    def _update(self, job_id: str, **fields) -> None:
        with registry.connect(self.db_path) as conn:
            registry.update_pipeline_job(conn, job_id, **fields)

    def _get(self, job_id: str) -> dict:
        with registry.connect(self.db_path) as conn:
            job = _parse_job(registry.get_pipeline_job(conn, job_id))
        if job is None:
            raise JobError(f"job 消失：{job_id}")
        return job

    def _save_artifacts(self, job_id: str, artifacts: dict) -> None:
        self._update(job_id,
                     artifacts_json=json.dumps(artifacts, ensure_ascii=False))

    def _steplog(self, job_id: str):
        def cb(msg: str) -> None:
            logger.info("[%s] %s", job_id[:8], msg)
        return cb

    def _stage_progress_cb(self, job_id: str, stage: str):
        """節流 callback 工廠（merge / oob / cut_encode 三 stage 共用，單一實作）。

        回傳 cb(pct, t=0)：stage 內百分比 0–100 → 全程 progress
        （base + pct/100*weight）落 DB；距上次寫入 ≥_throttle_sec 或
        增量 ≥_throttle_pct 才寫，避免高頻更新灌爆 DB。
        """
        base = self._base(stage)
        weight = float(self._weights[stage])
        state = {"ts": 0.0, "pct": -1.0}  # (monotonic_time, 上次落 DB 的 pct)

        def cb(pct: float, _t: float = 0.0) -> None:
            now = time.monotonic()
            if (now - state["ts"] < self._throttle_sec
                    and pct - state["pct"] < self._throttle_pct):
                return
            state["ts"], state["pct"] = now, pct
            self._update(job_id, progress=base + pct / 100.0 * weight)
        return cb

    def _keep_segments(self, artifacts: dict) -> list[tuple[float, float]]:
        """cut_encode/verify 用的 in-body 段：QC 確認值優先，否則 pipeline 建議值。"""
        keep = artifacts.get("segments_keep_confirmed",
                             artifacts.get("segments_keep"))
        if not keep:
            raise PreprocessError("artifacts 缺 in-body 段（oob stage 未完成？）")
        return [(float(s), float(e)) for s, e in keep]

    # --------------------------------------------------------------- stages --

    def _stage_merge(self, job_id: str) -> None:
        job = self._get(job_id)
        art, params = job["artifacts"], job["params"]
        folder = self.incoming_dir / job["patient_folder"]
        workdir = self.work_dir / job_id

        if params.get("merge_plan"):  # UI 已確認的完整計畫（決策 H）
            plan = preprocess.MergePlan.from_dict(params["merge_plan"])
        else:
            plan = preprocess.plan_merge(folder, self.cfg)
            if params.get("file_order"):  # UI 拖曳後的檔名順序（決策 H）
                plan = self._reorder(plan, params["file_order"])
        merged = preprocess.merge(plan, workdir, self.cfg,
                                  on_step=self._steplog(job_id),
                                  progress_cb=self._stage_progress_cb(job_id, "merge"))
        art.update(merge_plan=plan.to_dict(), merged_path=str(merged),
                   single_file=plan.single_file)
        self._save_artifacts(job_id, art)

    @staticmethod
    def _reorder(plan: preprocess.MergePlan, order: list[str]) -> preprocess.MergePlan:
        by_name = {f["name"]: f for f in plan.files}
        if sorted(by_name) != sorted(order):
            raise PreprocessError(
                f"file_order 與資料夾內容不一致：{sorted(order)} vs {sorted(by_name)}")
        files = []
        for i, name in enumerate(order, 1):
            f = dict(by_name[name])
            f["order"] = i
            files.append(f)
        return preprocess.MergePlan(folder=plan.folder, files=files,
                                    single_file=plan.single_file,
                                    total_duration=plan.total_duration)

    def _stage_oob(self, job_id: str) -> None:
        # 起 OOB 前先驗環境（onnxruntime／ONNX 模型＋sha256）；缺件即以單一可照抄訊息
        # raise → worker 把 job 標 failed 並把補救步驟寫進 error（與 CLI 共用同一預檢）
        preprocess.ensure_oob_ready(self.cfg)
        job = self._get(job_id)
        art = job["artifacts"]
        workdir = self.work_dir / job_id
        merged = Path(art["merged_path"])
        csv_path = preprocess.detect_oob(merged, workdir, self.cfg,
                                         on_step=self._steplog(job_id),
                                         progress_cb=self._stage_progress_cb(job_id, "oob"))
        info = ingest.probe(merged)
        fps = ingest._parse_rate(info["r_frame_rate"])
        keep = preprocess.derive_segments(csv_path, self.cfg, fps=fps,
                                          duration=info["duration"])
        if not keep:
            raise PreprocessError(
                "OOB 偵測後沒有任何 in-body 段（整支都被判為體外），"
                "請人工檢查 oob.csv 與來源影片")
        segs_oob = _complement(keep, info["duration"])
        art.update(oob_csv_path=str(csv_path),
                   merged_duration=round(info["duration"], 3), merged_fps=fps,
                   segments_keep=[[s, e] for s, e in keep],
                   segments_oob=segs_oob,
                   # 每段機率統計（與 segments_oob 逐 index 對齊，QC modal 顯示用）
                   oob_segment_stats=preprocess.segment_prob_stats(
                       csv_path, segs_oob, fps=fps))
        self._save_artifacts(job_id, art)

    def _stage_cut_encode(self, job_id: str) -> None:
        job = self._get(job_id)
        art = job["artifacts"]
        workdir = self.work_dir / job_id
        merged = Path(art["merged_path"])
        keep = self._keep_segments(art)
        out = workdir / "final.mp4"

        fast = preprocess.probe_fast_path(merged, self.cfg, segments=keep)
        if fast:  # 決策 C-2：已 720p/25 CFR 且無切割 → remux（不重編碼）
            self._steplog(job_id)("fast path：remux（不重編碼）")
            preprocess.remux(merged, out, self.cfg)
            if not preprocess.verify_output(out, keep, self.cfg).ok:
                self._steplog(job_id)("fast path 產物未過驗證，退回完整重編碼")
                fast = False
        if not fast:
            preprocess.cut_encode(
                merged, keep, out, self.cfg,
                progress_cb=self._stage_progress_cb(job_id, "cut_encode"))
        art.update(final_path=str(out), fast_path=fast,
                   encoder_used=("remux" if fast else preprocess.resolve_encoder(self.cfg)))
        self._save_artifacts(job_id, art)

    def _stage_verify(self, job_id: str) -> None:
        job = self._get(job_id)
        art = job["artifacts"]
        keep = self._keep_segments(art)
        report = preprocess.verify_output(Path(art["final_path"]), keep, self.cfg)
        art["verify_report"] = {
            "ok": report.ok, "duration": report.duration,
            "expected_duration": report.expected_duration,
            "duration_diff_pct": report.duration_diff_pct,
            "fps": report.verify.fps, "n_frames": report.verify.n_frames,
            "width": report.verify.width, "height": report.verify.height,
            "problems": report.problems}
        self._save_artifacts(job_id, art)
        if not report.ok:
            raise PreprocessError(
                "最終驗證未通過：\n  - " + "\n  - ".join(report.problems))

    def _stage_register(self, job_id: str) -> None:
        """配號 → 入庫 → deid_map → 掛專案 → 原始資料夾搬 archived/ → 清理 workdir。

        每個子步驟冪等（video_id 先落 artifacts、搬移前檢查存在性），
        register 中途 kill 後 retry 不會重複配號或重複搬移。
        """
        job = self._get(job_id)
        art, params = job["artifacts"], job["params"]
        workdir = self.work_dir / job_id
        series_id = job["series_id"]

        # 1) 配號（先落 artifacts：crash 後 retry 沿用，不重複配號）
        video_id = art.get("video_id")
        if not video_id:
            with registry.connect(self.db_path) as conn:
                video_id = registry.next_video_id(conn, series_id)
            art["video_id"] = video_id
            self._save_artifacts(job_id, art)

        # 2) 原始檔清單 sha256（搬 archived 前計算；原始檔唯讀）
        if "original_files" not in art:
            orig = []
            for f in art["merge_plan"]["files"]:
                p = Path(f["path"])
                orig.append({"name": f["name"], "order": f["order"],
                             "size": f["size_bytes"],
                             "sha256": ingest.sha256(p) if p.is_file() else None,
                             "duration": f["duration_sec"]})
            art["original_files"] = orig
            self._save_artifacts(job_id, art)

        # 3) final.mp4 → data/videos/{video_id}/{video_id}.mp4（move 省磁碟）
        std_path = self.videos_dir / video_id / f"{video_id}.mp4"
        final = Path(art["final_path"])
        if final.is_file():
            std_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(final), str(std_path))
        elif not std_path.is_file():
            raise PreprocessError(
                f"final.mp4 與 std_path 皆不存在，無法入庫：{final}")

        # 4) oob.csv 移存影片目錄（workdir 之後清掉；csv 無 PHI，留作稽核）
        oob_dst = std_path.parent / "oob.csv"
        oob_src = workdir / "oob.csv"
        if oob_src.is_file():
            shutil.copy2(oob_src, oob_dst)

        # 5) 驗證＋入庫＋抽樣點（source_path=None：PHI 收斂，原始路徑只進 deid_map）
        ingest.register_video(std_path, video_id, self.ingest_cfg,
                              source_path=None, db_path=self.db_path,
                              on_step=self._steplog(job_id))

        # 6) deid_map（完整欄位）＋ touch_series ＋ 順掛專案
        keep = self._keep_segments(art)
        cut = art.get("segments_oob_confirmed", art.get("segments_oob") or [])
        pipeline_params = {
            "job_id": job_id,
            "merge": {"ratio_threshold": self.cfg["merge"]["ratio_threshold"]},
            "oob": {k: self.cfg["oob"].get(k) for k in
                    ("onnx_path", "onnx_bundled", "onnx_sha256", "device", "sample_fps", "threshold",
                     "min_segment_sec", "boundary_pad_sec")},
            "encode": {**self.cfg["encode"],
                       "encoder_used": art.get("encoder_used")},
            "fast_path": bool(art.get("fast_path")),
            "qc_confirmed_at": art.get("qc_confirmed_at"),
            "segments_keep": [[s, e] for s, e in keep]}
        with registry.connect(self.db_path) as conn:
            registry.upsert_deid_map(
                conn, video_id=video_id, series_id=series_id,
                original_folder=Path(job["patient_folder"]).name,
                original_files_json=json.dumps(art["original_files"],
                                               ensure_ascii=False),
                surgery_date=params.get("surgery_date"),
                merged_duration=art.get("merged_duration"),
                oob_segments_json=json.dumps(cut),
                oob_csv_path=str(oob_dst) if oob_dst.is_file() else None,
                pipeline_params_json=json.dumps(pipeline_params,
                                                ensure_ascii=False),
                job_id=job_id)
            registry.set_video_series(conn, video_id, series_id)  # videos.series=series_id
            registry.touch_series(conn, series_id)
            if job["project_id"]:
                registry.add_video_to_project(conn, job["project_id"], video_id)

        # 7) 原始資料夾搬 archived/（唯一允許的變動，成功後才搬、不刪除）
        src_folder = self.incoming_dir / job["patient_folder"]
        if src_folder.is_dir():
            self.archived_dir.mkdir(parents=True, exist_ok=True)
            dest = self.archived_dir / src_folder.name
            if dest.exists():
                dest = self.archived_dir / f"{src_folder.name}_{job_id[:8]}"
            shutil.move(str(src_folder), str(dest))
            art["archived_to"] = str(dest)

        # 8) 清理 workdir（產物已各就各位；失敗 job 的 workdir 才保留供除錯）
        shutil.rmtree(workdir, ignore_errors=True)
        art["std_path"] = str(std_path)
        self._save_artifacts(job_id, art)
