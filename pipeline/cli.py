"""surgann：標註平台的薄入口 CLI（R10）。核心邏輯都在 pipeline/ 各模組。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import config, ingest as ingest_mod, registry

app = typer.Typer(
    add_completion=False,
    help="Surgical Annotation Platform CLI（surgann）",
    no_args_is_help=True,
)
console = Console()


@app.command("init-db")
def init_db():
    """建立 / 初始化 registry.db（SQLite metadata registry）。冪等。"""
    path = registry.init_db()
    console.print(f"[green]✓[/] registry 已就緒：[bold]{path}[/]")


def _require_series(series_id: str) -> None:
    """series_id 必須已存在於 series 表（v1.3 起系列先建後用，維持 ID 前綴一致性）。"""
    with registry.connect() as conn:
        if registry.get_series(conn, series_id) is not None:
            return
        avail = ", ".join(f"{s['series_id']}({s['name']})"
                          for s in registry.list_series_detail(conn)) or "（無）"
    console.print(f"[red]✗[/] 系列 '{series_id}' 不存在，現有：{avail}（請先在入庫頁 UI 或 API 建立）")
    raise typer.Exit(code=1)


@app.command()
def ingest(
    source: Path = typer.Argument(..., exists=True, dir_okay=False,
                                  help="來源影片（data/raw_videos/ 內，唯讀）"),
    video_id: str = typer.Option(..., "--video-id", "-i",
                                 help="去識別化編號，如 ABCD_0001（{seriesID}_{四碼續號}）"),
    config_file: str = typer.Option("ingest.yaml", "--config", "-c",
                                    help="pipeline/configs/ 下的設定檔"),
    force: bool = typer.Option(False, "--force", help="覆寫已存在的標準化檔並重新入庫"),
    project: Optional[str] = typer.Option(
        None, "--project", "-P",
        help="入庫後加入此專案播放清單（省略＝進影片庫待分配，不綁專案）"),
    series: Optional[str] = typer.Option(
        None, "--series", "-s", help="系列 series_id（四碼，如 ABCD；先於 UI/API 建立）"),
):
    """影片 ingest：標準化轉檔 → 驗證 → 入庫 → 抽樣點註冊。"""
    cfg = config.load_config(config_file)
    console.print(f"[bold]Ingest[/] {source}  →  video_id=[cyan]{video_id}[/]")
    registry.init_db()  # 確保 schema 存在
    if project is not None:
        with registry.connect() as conn:
            if registry.get_project(conn, project) is None:
                console.print(f"[red]✗[/] 專案編碼 '{project}' 不存在")
                raise typer.Exit(code=1)
    if series is not None:
        _require_series(series)

    def on_step(msg: str):
        console.print(f"  [dim]•[/] {msg}")

    try:
        out = ingest_mod.ingest(source, video_id, cfg, force=force, on_step=on_step)
    except ingest_mod.IngestError as e:
        console.print(f"[red]✗ ingest 失敗：[/]{e}")
        raise typer.Exit(code=1)

    with registry.connect() as conn:
        if project is not None:
            registry.add_video_to_project(conn, project, video_id)
        if series is not None:
            registry.set_video_series(conn, video_id, series)

    v = out.verify
    console.print(
        f"[green]✓ 入庫成功[/]  fps={v.fps}  frames={v.n_frames}  "
        f"{v.width}x{v.height}  duration={v.duration:.2f}s"
    )
    counts = ", ".join(f"{k}={n}" for k, n in sorted(out.sampling_counts.items()))
    console.print(f"  抽樣點：{counts}（共 {out.n_sampling_rows} 筆）")
    console.print(f"  標準化檔：[bold]{out.std_path}[/]")
    if project is not None:
        console.print(f"  已加入專案：[cyan]{project}[/]")
    if series is not None:
        console.print(f"  系列：[cyan]{series}[/]")


@app.command()
def preprocess(
    folder: Optional[Path] = typer.Argument(None, exists=True, file_okay=False,
                                  help="病人資料夾（內含待合併的原始影片，唯讀）；"
                                       "搭 --check 時可省略"),
    workdir: Optional[Path] = typer.Option(None, "--workdir",
                                           help="暫存工作目錄（預設 data/work/<資料夾名>）"),
    out: Optional[Path] = typer.Option(None, "--out",
                                       help="最終輸出檔（預設 workdir/final.mp4；"
                                            "入庫時自動搬到 data/videos/<video_id>/）"),
    no_register: bool = typer.Option(False, "--no-register",
                                     help="只產出最終檔，不入庫 registry"),
    video_id: Optional[str] = typer.Option(None, "--video-id", "-i",
                                           help="入庫用影片編號（--no-register 時免填）"),
    series: Optional[str] = typer.Option(None, "--series", "-s",
                                         help="系列 series_id（四碼，如 ABCD；先於 UI/API 建立）"),
    config_file: str = typer.Option("preprocess.yaml", "--config", "-c",
                                    help="pipeline/configs/ 下的設定檔"),
    check: bool = typer.Option(False, "--check",
                               help="只跑 OOB 環境預檢（onnxruntime／授權同意／ONNX 模型／sha256），"
                                    "不執行前處理；可省略 folder"),
):
    """前處理：合併 → OOB 去識別 → 720p/25 CFR 標準化（單一 pass）→ 驗證 →（可選）入庫。"""
    from . import preprocess as pp

    cfg = config.load_config(config_file)

    # OOB 環境預檢（--check：只檢查不執行；否則開跑前先驗，缺件即以單一補救訊息停手）
    problems = pp.preflight_oob(cfg)
    if check:
        if problems:
            console.print("[red]✗ OOB 前處理環境預檢未通過：[/]")
            for p in problems:
                console.print(f"  [yellow]•[/] {p}")
            raise typer.Exit(code=1)
        console.print("[green]✓ OOB 前處理環境預檢通過[/]"
                      f"（onnxruntime、ONNX 模型、授權同意 {config.oob_license_accepted_on()} 皆就緒；"
                      f"編碼器 {cfg['encode']['encoder']}→{pp.resolve_encoder(cfg)}）")
        console.print(f"  {pp.describe_oob_model(cfg)}", markup=False)
        warn = pp.oob_model_warning(pp.resolve_oob_model(cfg))
        if warn:
            console.print(f"  [yellow]⚠[/] {warn}")
        raise typer.Exit(code=0)
    if problems:
        console.print("[red]✗ OOB 前處理環境預檢未通過，請先補齊再重跑：[/]")
        for p in problems:
            console.print(f"  [yellow]•[/] {p}")
        raise typer.Exit(code=1)

    if folder is None:
        console.print("[red]✗[/] 缺少病人資料夾參數（除非只跑 --check）")
        raise typer.Exit(code=1)
    if not no_register and not video_id:
        console.print("[red]✗[/] 入庫需要 --video-id（或改用 --no-register 只產檔）")
        raise typer.Exit(code=1)
    if series is not None and not no_register:
        registry.init_db()
        _require_series(series)

    workdir = workdir or (config.WORK_DIR / folder.name)
    console.print(f"[bold]Preprocess[/] {folder}  →  workdir=[cyan]{workdir}[/]")

    def on_step(msg: str):
        console.print(f"  [dim]•[/] {msg}")

    _last = {"pct": -10}

    def on_progress(pct: float, t: float):
        if pct - _last["pct"] >= 5 or pct >= 100:  # 每 5% 印一次
            _last["pct"] = pct
            console.print(f"    [dim]encode {pct:5.1f}%  out_time={t:8.1f}s[/]")

    try:
        plan = pp.plan_merge(folder, cfg)
        console.print(f"  合併順序（{len(plan.files)} 檔"
                      f"{'，單檔免合併' if plan.single_file else ''}）：")
        for f in plan.files:
            console.print(f"    {f['order']:2d}. {f['name']}  "
                          f"[dim]{f['duration_sec']:.1f}s  "
                          f"{f['size_bytes'] / 1e6:.0f} MB[/]")
        outcome = pp.run_preprocess(folder, workdir, out=out, cfg=cfg, plan=plan,
                                    on_step=on_step, progress_cb=on_progress)
    except pp.PreprocessError as e:
        console.print(f"[red]✗ preprocess 失敗：[/]{e}")
        raise typer.Exit(code=1)

    r = outcome.report
    console.print(
        f"[green]✓ 前處理完成[/]{'（fast path remux）' if outcome.fast_path else ''}  "
        f"duration={r.duration:.2f}s（in-body 預期 {r.expected_duration:.2f}s，"
        f"差 {r.duration_diff_pct:.3f}%）\n"
        f"  in-body 段：{len(outcome.segments)}  最終檔：[bold]{outcome.final}[/]")

    if no_register:
        console.print("  [dim]--no-register：未入庫。[/]")
        return

    # 入庫：搬進標準位置 → register-only（僅驗證＋sha256＋入庫＋抽樣點）
    import shutil as _shutil
    ingest_cfg = config.load_config("ingest.yaml")
    std_path = config.std_video_path(video_id)
    std_path.parent.mkdir(parents=True, exist_ok=True)
    _shutil.move(str(outcome.final), std_path)
    console.print(f"  已搬入標準位置：{std_path}")
    registry.init_db()
    try:
        # source_path 不寫病人資料夾原名（PHI 收斂）；
        # 原始來源對照由 deid_map 承載。
        out_reg = ingest_mod.register_video(std_path, video_id, ingest_cfg,
                                            source_path=None, on_step=on_step)
    except ingest_mod.IngestError as e:
        console.print(f"[red]✗ 入庫失敗：[/]{e}")
        raise typer.Exit(code=1)
    if series is not None:
        with registry.connect() as conn:
            registry.set_video_series(conn, video_id, series)
    v = out_reg.verify
    counts = ", ".join(f"{k}={n}" for k, n in sorted(out_reg.sampling_counts.items()))
    console.print(
        f"[green]✓ 入庫成功[/]  fps={v.fps}  frames={v.n_frames}  "
        f"{v.width}x{v.height}  duration={v.duration:.2f}s\n"
        f"  抽樣點：{counts}（共 {out_reg.n_sampling_rows} 筆）"
        + (f"\n  系列：[cyan]{series}[/]" if series else ""))


@app.command()
def status():
    """列出 registry 內已入庫的影片。"""
    registry.init_db()
    with registry.connect() as conn:
        videos = registry.list_videos(conn)
        if not videos:
            console.print("[yellow]registry 內尚無影片。[/] 先執行：surgann ingest ...")
            return
        table = Table(title="Registry — videos")
        for col in ("video_id", "fps", "n_frames", "resolution", "duration", "status"):
            table.add_column(col)
        for r in videos:
            table.add_row(
                r["video_id"], f'{r["fps"]}', f'{r["n_frames"]}',
                f'{r["width"]}x{r["height"]}', f'{r["duration_sec"]:.1f}s', r["status"],
            )
        console.print(table)


# ------------------------------------------------ SurgAnnFlow Annotator ----

@app.command()
def annotate(
    host: str = typer.Option("127.0.0.1", help="綁定位址（預設只限本機連入；改 0.0.0.0 會對區網開放，請先確認網路安全）"),
    port: Optional[int] = typer.Option(None, help="預設取 deploy/.env 的 ANNOTATOR_PORT（無則 8090）"),
):
    """啟動 SurgAnnFlow 標註介面（影片庫、temporal/spatial 標註、匯出）。"""
    import uvicorn
    from .annotator.server import create_app

    port = port or int(config.load_env().get("ANNOTATOR_PORT", "8090"))
    console.print(f"[green]SurgAnn Annotator[/] → 瀏覽器開啟 [bold]http://{host}:{port}[/]（Ctrl+C 停止）")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


# --------------------------------------------------------------- 匯出 ----

def _check_project(project: Optional[str]) -> None:
    """--project 給定時先確認專案編碼存在，避免誤導的「沒有標註」錯誤。"""
    if project is None:
        return
    registry.init_db()
    with registry.connect() as conn:
        if registry.get_project(conn, project) is None:
            console.print(f"[red]✗[/] 專案編碼 '{project}' 不存在")
            raise typer.Exit(code=1)


@app.command("export-temporal")
def export_temporal_cmd(
    project: Optional[str] = typer.Option(None, "--project", "-P",
                                          help="限定專案（專案編碼；預設全 registry）"),
):
    """匯出 annotator 的 temporal 標註：JSON 快照 + phase/action/event/triplet CSV。"""
    from . import export_temporal as ex
    from .export_common import ExportError

    _check_project(project)
    try:
        out = ex.run_export(project_id=project)
    except ExportError as e:
        console.print(f"[red]✗ export-temporal 失敗：[/]{e}")
        raise typer.Exit(code=1)
    counts = ", ".join(f"{k}={n}" for k, n in out.row_counts.items())
    console.print(
        f"[green]✓ 匯出完成[/] run_id=[cyan]{out.run_id}[/]\n"
        f"  影片={out.n_tasks}  標註={out.n_annotations}  區段列：{counts}\n"
        f"  輸出目錄：[bold]{out.run_dir}[/]"
    )


@app.command()
def derive(
    run_id: Optional[str] = typer.Option(None, "--run-id", "-r",
                                         help="export run（預設取最新一次）"),
):
    """由 interval 標註推導每 5s/30s frame-level label。"""
    from . import derive as derive_mod

    intervals = config.load_config("ingest.yaml")["sampling"]["intervals"]
    with registry.connect() as conn:
        run = (conn.execute("SELECT * FROM export_runs WHERE run_id = ?",
                            (run_id,)).fetchone()
               if run_id else registry.latest_export_run(conn))
    if run is None:
        console.print("[red]✗[/] 找不到 export run，請先執行 [bold]surgann export-ls[/]")
        raise typer.Exit(code=1)

    out = derive_mod.run_derive(config.to_abs(run["run_dir"]), intervals)  # v1.4 相對路徑
    console.print(f"[green]✓ derive 完成[/] run_id=[cyan]{run['run_id']}[/]")
    for name, path in out.outputs.items():
        grid = ", ".join(f"{v}:{n}點" for v, n in out.grid_counts[name].items()) or "無標註"
        console.print(f"  {name}: {out.row_counts[name]} 列（{grid}）→ {path.name}")


# ------------------------------------------------------------- M2: 抽幀 -----

@app.command("sample")
def sample_cmd(
    video_id: str = typer.Argument(..., help="已 ingest 的影片編號，如 ABCD_0001"),
    purpose: str = typer.Option("sample_30s", "--purpose", "-p",
                                help="frames 表的抽樣 purpose（sample_5s / sample_30s）"),
    force: bool = typer.Option(False, "--force", help="重抽已存在的 JPEG"),
):
    """依 frames 表抽出 JPEG（{video_id}_f{frame:07d}.jpg），供 spatial 標註與資料集匯出。"""
    from . import sample as sample_mod

    try:
        out = sample_mod.sample(video_id, purpose, force=force,
                                on_step=lambda m: console.print(f"  [dim]•[/] {m}"))
    except sample_mod.SampleError as e:
        console.print(f"[red]✗ sample 失敗：[/]{e}")
        raise typer.Exit(code=1)
    console.print(
        f"[green]✓ 抽幀完成[/] {out.video_id} / {out.purpose}：{out.n_frames} 張"
        f"（每 {out.step} frames）→ [bold]{out.out_dir}[/]"
    )


@app.command("export-spatial")
def export_spatial_cmd(
    project: Optional[str] = typer.Option(None, "--project", "-P",
                                          help="限定專案（專案編碼；預設全 registry）"),
):
    """匯出 annotator 的 spatial 標註：spatial.json + keypoint 補抽幀 + COCO。"""
    from . import export_spatial as ex

    _check_project(project)
    try:
        out = ex.run_export(project_id=project)
    except ex.SpatialExportError as e:
        console.print(f"[red]✗ export-spatial 失敗：[/]{e}")
        raise typer.Exit(code=1)
    console.print(
        f"[green]✓ 匯出完成[/] run_id=[cyan]{out.run_id}[/]\n"
        f"  影片={out.n_videos}  標註={out.n_annotations}  "
        f"影像={out.n_images}（補抽 {out.n_new_frames} 張）  "
        f"COCO 標註列={out.n_coco_annotations}\n"
        f"  輸出目錄：[bold]{out.run_dir}[/]"
    )


# ------------------------------------------- M3: 資料集匯出 / 格式轉換 -------

@app.command("export-dataset")
def export_dataset_cmd(
    fmt: str = typer.Option(..., "--format", "-f",
                            help="mosaic | coco | yolo | frame-pairs | clip"),
    interval: str = typer.Option("sample_5s", "--interval",
                                 help="frame-pairs 的抽樣間隔（sample_5s / sample_30s）"),
    videos: Optional[list[str]] = typer.Option(None, "--videos",
                                               help="限定影片（預設全 registry）"),
    cut: bool = typer.Option(False, "--cut", help="clip：實際用 ffmpeg 切出影片檔"),
    zip_out: bool = typer.Option(False, "--zip",
                                 help="mosaic/coco/yolo：另打包成 {run_dir}.zip 供下載"),
    project: Optional[str] = typer.Option(None, "--project", "-P",
                                          help="限定專案（專案編碼；預設全 registry）"),
):
    """把內部標註轉成訓練用資料集。

    spatial + 對應 images（皆自帶影像，可 --zip 打包）：mosaic / coco / yolo。
    temporal：frame-pairs（影格分類配對）/ clip（區段配對）。
    """
    from . import convert, dataset_export, export_common
    _check_project(project)

    # spatial + images 三格式：統一入口（run_dataset_export，自帶影像、可打包）
    if fmt in dataset_export.FORMATS:
        try:
            out = dataset_export.run_dataset_export(
                fmt, project_id=project, videos=videos, make_zip=zip_out)
        except Exception as e:  # noqa: BLE001 — 匯出錯誤統一回報
            console.print(f"[red]✗ export-dataset 失敗：[/]{e}")
            raise typer.Exit(code=1)
        console.print(
            f"[green]✓ 資料集匯出完成[/] 格式=[cyan]{fmt}[/] "
            f"run_id=[cyan]{out.run_id}[/]\n"
            f"  影片={out.n_videos}  影像={out.n_images}  標註={out.n_annotations}\n"
            f"  輸出：[bold]{out.output_path}[/]")
        for k, v in (out.manifest or {}).items():
            console.print(f"  {k}: {v}")
        return

    # temporal 配對：frame-pairs / clip（v1.2 落點按專案分層；無 -P → _global）
    run_dir = export_common.exports_dir(project) / export_common.next_run_id(project)
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        if fmt == "frame-pairs":
            m = convert.frame_pairs(run_dir, interval, project_id=project)
        elif fmt == "clip":
            m = convert.clip_pairs(run_dir, cut=cut, project_id=project)
        else:
            console.print(f"[red]✗[/] 未知格式 '{fmt}'"
                          f"（mosaic / coco / yolo / frame-pairs / clip）")
            raise typer.Exit(code=1)
    except Exception as e:  # noqa: BLE001 — 轉換錯誤統一回報
        console.print(f"[red]✗ export-dataset 失敗：[/]{e}")
        raise typer.Exit(code=1)
    with registry.connect() as conn:
        import json as _json
        registry.add_export_run(conn, run_id=run_dir.name, run_dir=str(run_dir),
                                config_json=_json.dumps({"format": fmt, "interval": interval,
                                                         "videos": videos, "cut": cut,
                                                         "project_id": project}),
                                created_at=_now_iso())
    console.print(f"[green]✓ 資料集匯出完成[/] 格式=[cyan]{fmt}[/] → [bold]{run_dir}[/]")
    for k, v in (m or {}).items():
        console.print(f"  {k}: {v}")


@app.command("import-mosaic")
def import_mosaic_cmd(
    json_path: Path = typer.Argument(..., exists=True, dir_okay=False,
                                     help="MOSaiC 匯出的 annotations JSON"),
    video_id: str = typer.Option(..., "--video", help="匯入到哪支已入庫影片"),
    annotator: str = typer.Option("mosaic-import", "--annotator"),
    temporal_layer: str = typer.Option("event", "--temporal-layer",
                                       help="MOSaiC temporal label 對映不到現有層時的預設層"),
    project: Optional[str] = typer.Option(None, "--project", "-P",
                                          help="標註寫入哪個專案（來源 JSON 無 "
                                               "project_id 時採用；預設 default）"),
):
    """匯入 MOSaiC 標註 JSON → registry（座標 %→像素、numberFrames→end_frame）。"""
    from . import mosaic_io
    _check_project(project)
    try:
        out = mosaic_io.import_mosaic(json_path, video_id, annotator=annotator,
                                      default_temporal_layer=temporal_layer,
                                      project_id=project)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]✗ import-mosaic 失敗：[/]{e}")
        raise typer.Exit(code=1)
    console.print(f"[green]✓ MOSaiC 匯入完成[/] → {video_id}：{out}")


@app.command("export-mosaic")
def export_mosaic_cmd(
    video_id: str = typer.Option(..., "--video"),
    out: Optional[Path] = typer.Option(None, "-o", "--out", help="輸出 JSON 路徑"),
    project: Optional[str] = typer.Option(None, "--project", "-P",
                                          help="只輸出該專案的標註（預設全部）"),
):
    """把某影片的內部標註反向轉成 MOSaiC JSON 格式。"""
    import json as _json
    from . import export_common, mosaic_io
    _check_project(project)
    try:
        data = mosaic_io.export_mosaic(video_id, project_id=project)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]✗ export-mosaic 失敗：[/]{e}")
        raise typer.Exit(code=1)
    dest = out or (export_common.exports_dir(project) / f"{video_id}_mosaic.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    console.print(f"[green]✓ 已輸出 MOSaiC 格式[/] → [bold]{dest}[/]")


@app.command("backup")
def backup_cmd(
    label: Optional[str] = typer.Option(None, "--label", help="備份名稱後綴"),
    rsync_dest: Optional[str] = typer.Option(None, "--rsync", help="rsync 目的地（第二顆碟/外接碟）"),
):
    """安全備份（動 DB 或大改動前先跑）：registry.db + 可攜標註快照 → backups/，可 rsync。"""
    from . import backup as backup_mod
    try:
        out = backup_mod.run_backup(label=label,
                                    rsync_dest=rsync_dest,
                                    on_step=lambda m: console.print(f"  [dim]•[/] {m}"))
    except backup_mod.BackupError as e:
        console.print(f"[red]✗ backup 失敗：[/]{e}")
        raise typer.Exit(code=1)
    counts = ", ".join(f"{k}={n}" for k, n in out.table_counts.items())
    console.print(
        f"[green]✓ 備份完成[/] → [bold]{out.archive}[/]\n"
        f"  表：{counts}"
        + (f"  rsync→{out.rsynced}" if out.rsynced else "")
    )


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# --- 後續里程碑的指令佔位（結構先立，實作於各自 milestone）---
_STUBS = {
    "agreement": "標註者一致性分析（規劃中）",
}


def _make_stub(name: str, desc: str):
    @app.command(name=name, help=f"[尚未實作] {desc}")
    def _stub():
        console.print(f"[yellow]`surgann {name}` 尚未實作 —[/] {desc}")
        raise typer.Exit(code=1)
    return _stub


for _name, _desc in _STUBS.items():
    _make_stub(_name, _desc)


# --------------------------------------- 可攜安裝（邏輯在 pipeline/setup.py）----

@app.command("doctor")
def doctor_cmd(
    as_json: bool = typer.Option(False, "--json", help="輸出 JSON（給 AI agent / 腳本解析）"),
    no_hash: bool = typer.Option(False, "--no-hash", help="不重算模型 sha256（較快）"),
):
    """一鍵健檢：每項 PASS/WARN/FAIL＋修復提示；有 FAIL（阻斷項）時結束碼 1。"""
    import json as _json
    from . import setup as setup_mod
    checks = setup_mod.run_doctor(hash_models=not no_hash)
    rep = setup_mod.doctor_report(checks)
    if as_json:
        print(_json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(setup_mod.format_doctor_text(checks))
    raise typer.Exit(code=rep["exit_code"])


@app.command("setup")
def setup_cmd(
    data_dir: Optional[str] = typer.Option(None, "--data-dir", help="data 根（寫入 .env 的 SURGANN_DATA_DIR）"),
    port: Optional[int] = typer.Option(None, "--port", help="annotator port（ANNOTATOR_PORT）"),
    annotator: Optional[str] = typer.Option(None, "--annotator", help="預設標註者名稱（ANNOTATOR_NAME）"),
    tz: Optional[str] = typer.Option(None, "--tz", help="時區，如 Asia/Taipei；留空＝系統預設"),
    yes: bool = typer.Option(False, "--yes", "-y", help="不互動：未指定的值用既有值或預設"),
    force: bool = typer.Option(False, "--force", help="覆蓋 .env 既有值（預設只補缺的鍵）"),
    systemd: bool = typer.Option(False, "--systemd", help="渲染 systemd unit 並印出 sudo 安裝指令（不代跑）"),
):
    """初始化：寫 deploy/.env（保留既有值）→ 建 data 根 → init-db →（選用）渲染 systemd。"""
    import sys as _sys
    from . import setup as setup_mod
    ask = None
    if not yes and _sys.stdin.isatty():
        def ask(prompt: str, default: str) -> str:
            return typer.prompt(prompt, default=default, show_default=True)
    raise typer.Exit(code=setup_mod.run_setup(
        data_dir=data_dir, port=port, annotator=annotator, tz=tz,
        yes=yes, force=force, systemd=systemd, ask=ask))


@app.command("fetch-models")
def fetch_models_cmd(
    only: Optional[str] = typer.Option(None, "--only", help="只處理 sam 或 oob（預設兩者）"),
    accept_oob_license: bool = typer.Option(False, "--accept-oob-license",
                                            help="非互動記錄 OOB 模型 CC BY-NC-SA 4.0 授權同意"
                                                 "（僅限使用者本人已閱讀並同意後；AI agent 不可代加）"),
    sam_tag: Optional[str] = typer.Option(None, "--sam-tag",
                                          help="SAM zoo tag（預設 sam.yaml model.active；all=全部）"),
):
    """SAM：下載到 <data 根>/models/ 並驗 sha256（續傳＋重試）。OOB：隨 repo 附帶，驗 sha256 後請使用者同意授權。"""
    import sys as _sys
    from . import setup as setup_mod
    ask = (lambda prompt: input(prompt)) if _sys.stdin.isatty() else None
    raise typer.Exit(code=setup_mod.run_fetch_models(
        only=only, accept_oob_license=accept_oob_license, sam_tag=sam_tag, ask=ask))


def _apply_tz() -> None:
    """deploy/.env 的 TZ 套用到本進程（時間戳、備份/匯出檔名）；已有 TZ 環境變數（如 systemd）則尊重之，留空＝系統時區。"""
    import os
    import time
    tz = config.load_env().get("TZ", "").strip()
    if tz and not os.environ.get("TZ"):
        os.environ["TZ"] = tz
        time.tzset()


def main():
    """console_scripts 進入點。"""
    _apply_tz()
    app()


if __name__ == "__main__":
    main()
