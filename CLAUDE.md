# SurgAnnFlow — 給 AI agent 的工作指引

> English: To install, read `INSTALL_agent.md` and follow it step by step; never modify raw videos, run
> `surgann backup` before touching the database, and ask the human before any sudo, license acceptance,
> settings overwrite or deletion.

本 repo 是手術影片標註平台 SurgAnnFlow（Python CLI `surgann` + 網頁標註介面）。與使用者互動以使用者的語言為準。

## 安裝

**請讀 `INSTALL_agent.md` 並逐步執行**——每步都有預期輸出與驗證指令。健檢一律用 `surgann doctor`（`--json` 給機器讀；有 FAIL 時結束碼 1）。

## 資料安全（必守）

1. `<data 根>/raw_videos/` **唯讀**：不得修改、覆寫、搬移或刪除任何原始影片。
2. `registry/registry.db` 是標註資料的**唯一**來源：動資料庫、大規模修改、任何破壞性操作前，先執行 `surgann backup`（或 `pre-change-backup` skill）。
3. 以下情況**先停下來問人**，取得明確同意才做：使用 `sudo`；同意 OOB 模型授權（須由使用者本人在終端機執行 `surgann fetch-models --only oob` 並輸入 `accept`；agent 不可代為同意、不可自行加 `--accept-oob-license`）；覆蓋 `deploy/.env` 既有值（`--force`）；刪除任何檔案或目錄；停掉非本平台的程式。
4. 密碼、token 只放 `deploy/.env`（不進 git）；不得向使用者索取 sudo 密碼，也不得寫入任何檔案。

## 可用 skills（`.claude/skills/`）

- `smoke-test`：改動 `pipeline/annotator/` 後或安裝驗收時，驗證核心頁面與 API 沒壞。
- `pre-change-backup`：動資料庫或做破壞性操作前的備份流程。
- `storage-audit`：盤點 data 根各區用量（唯讀，只列清單不刪檔）。

## 程式結構速覽

- `pipeline/`：所有邏輯（`cli.py` 為 `surgann` 指令薄入口；`annotator/` 為網頁介面；`configs/` 為參數與 taxonomy 種子）。
- `deploy/`：`.env.example`（設定範本）、`requirements-{cpu,gpu}.txt`（安裝配方）、`systemd/`（常駐服務範本）。
- 路徑、port 等環境設定一律放 `deploy/.env`，不要寫死在程式裡。
