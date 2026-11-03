---
name: storage-audit
description: data 根儲存盤點 SOP。定期或使用者喊「磁碟快滿了」時跑：各區用量、孤兒 frames、不在 export_runs 的散落匯出目錄、legacy 歸檔大小。只列清單回報，一律不動手刪。
---

# storage-audit — data 根儲存盤點（唯讀）

## 何時跑

- 使用者要求盤點磁碟 / 匯出目錄雜亂 / 磁碟空間吃緊時
- 大量匯出或批次 ingest 之後的例行檢查

## 鐵則（先讀再動）

- **本 skill 全程唯讀**：只產出清單與數字回報，**任何刪除/搬移都必須先列清單向使用者確認**。
- data 根不一定在專案內的 `data/`：位置由 `deploy/.env` 的 `SURGANN_DATA_DIR` 決定（留空＝`<專案>/data`），以下一律用 `$DATA` 代表。
- `$DATA/raw_videos/` 唯讀，連 touch 都不准。
- 匯出目錄結構：`$DATA/exports/{project_id}/{run_id}/`；舊資料歸檔在 `$DATA/exports/legacy/`（只增不減）。

## 步驟（照抄執行，於專案根目錄）

```bash
# 0) 取得實際 data 根
DATA=$(.venv/bin/python -c 'from pipeline import config; print(config.DATA_DIR)'); echo "data 根：$DATA"

# 1) 各區用量總覽
du -sh "$DATA"/raw_videos "$DATA"/videos "$DATA"/frames "$DATA"/exports "$DATA"/exports/legacy "$DATA"/models 2>/dev/null

# 2) exports 逐項大小（找異常肥大的 run）
du -sh "$DATA"/exports/* "$DATA"/exports/legacy/* 2>/dev/null | sort -rh | head -20

# 3) 孤兒 frames：frames/ 底下的 video 目錄已不在 registry
.venv/bin/python - <<'EOF'
from pathlib import Path
from pipeline import registry, config
with registry.connect() as conn:
    known = {r["video_id"] for r in registry.list_videos(conn)}
orphans = [d for d in sorted(config.FRAMES_DIR.iterdir())
           if d.is_dir() and d.name not in known] if config.FRAMES_DIR.exists() else []
print("孤兒 frames 目錄：", [d.name for d in orphans] or "無")
EOF

# 4) 散落 exports：磁碟上有、export_runs 表沒有（或反之 DB 有、磁碟沒有）
.venv/bin/python - <<'EOF'
from pathlib import Path
from pipeline import registry, config
with registry.connect() as conn:
    rows = {r["run_id"]: r["run_dir"] for r in conn.execute("SELECT run_id, run_dir FROM export_runs")}
# run_dir 存的是相對 data 根的路徑（registry schema v1.4），一律經 config.to_abs 還原
registered = {config.to_abs(p).resolve() for p in rows.values() if p}
on_disk = set()
if config.EXPORTS_DIR.exists():
    for top in config.EXPORTS_DIR.iterdir():
        if top.name == "legacy":
            continue
        on_disk.add(top.resolve())
        # 結構 exports/{project_id}/{run_id}/：project 目錄本身不會註冊，走進一層
        if top.is_dir():
            on_disk.update(c.resolve() for c in top.iterdir())
stray = sorted(str(p) for p in on_disk
               if p not in registered
               and not any(r == p or r.is_relative_to(p) for r in registered))
missing = sorted(f"{rid} → {rd}" for rid, rd in rows.items() if not rd or not config.to_abs(rd).exists())
print("磁碟有但 DB 沒有（候選清理，需使用者確認）：");  [print(" ", s) for s in stray] or print("  無")
print("DB 有但磁碟沒有（run_dir 失聯）：");  [print(" ", s) for s in missing] or print("  無")
EOF

# 5) legacy 歸檔大小與筆數
du -sh "$DATA"/exports/legacy 2>/dev/null; ls "$DATA"/exports/legacy 2>/dev/null | wc -l
```

## 回報格式

一張小表：各區大小 ／ 孤兒 frames 清單 ／ 散落 exports 清單 ／ run_dir 失聯清單 ／ legacy 總量。
若有可回收空間，**只提出建議清單**（路徑 + 大小），等使用者點頭後才執行刪除。
