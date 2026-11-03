---
name: pre-change-backup
description: 動工前安全備份 SOP（registry.db 是標註的唯一來源）。凡動 registry DB、大規模重構、任何破壞性操作前必跑：標註快照 + git 歷史打包，確保隨時可救回。
---

# pre-change-backup — 動工前安全備份

## 何時必跑

- 任何會寫入/遷移 `registry/registry.db` schema 的改動前
- 大規模重構、批次改多檔前
- 任何破壞性操作（刪檔、刪目錄、改 `deploy/.env`）前
- 使用者說「幫我備份」時

## 步驟（照抄執行）

```bash
# 1. 標註資料備份：registry.db + 可攜 JSON 快照 → backups/
source .venv/bin/activate && surgann backup --label <這次動工的簡短代號>

# 2. git 歷史離線打包（程式碼保險，GitHub 之外的第二層）
git bundle create backups/repo_$(date +%Y%m%d)_<代號>.bundle --all
```

預期結果：`backups/` 出現 `<時間戳>_<代號>.tar.gz` 與 `repo_<日期>_<代號>.bundle`，且指令輸出表列筆數（videos/annotations/…）非零即正常。

## 還原方式（給未來救援用）

- 標註資料：解開 tar.gz，`registry.db` 蓋回 `registry/`（蓋回前先把現況另存）。
  `registry_snapshot.json` 為不依賴 SQLite 版本的可攜備份，搬機器時用它。
- 程式碼：`git clone repo_XXX.bundle 還原目錄` 即得完整歷史。

## 鐵則

- 備份失敗（指令報錯、tar.gz 沒出現）→ **停止原定改動**，把錯誤輸出回報給下指令的人（使用者或協調的 agent）。
- 禁止用備份檔覆蓋現有備份；`backups/` 只增不減（刪除或覆蓋備份屬破壞性操作，必須先經使用者確認）。
- `registry/registry.db` 是標註的**唯一**來源（沒有第二份資料庫可回頭），
  備份失敗＝毫無退路。
