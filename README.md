# SurgAnnFlow

> English summary: SurgAnnFlow is a self-hosted platform for annotating surgical videos (temporal phases/actions
> and spatial boxes/polygons with SAM assistance) and exporting training datasets (COCO / YOLO / MOSaiC /
> frame–label pairs / clips). It runs as a single process on your own Ubuntu machine. To install, let your AI
> agent read `INSTALL_agent.md`, or follow it by hand. Non-commercial use only (PolyForm Noncommercial 1.0.0).

SurgAnnFlow 是給手術影片研究用的**標註平台＋資料集匯出工具**，裝在你自己的電腦上（單機自架），
影片與標註資料都留在你的機器裡，不經過任何雲端服務。

## 功能

- **時序標註（temporal）**：在可縮放到單一影格的時間軸上標 phase / action / event / triplet 區段，可一邊播放一邊標。
- **空間標註（spatial）**：框、多邊形、點、橢圓、折線；**✨ SAM 智慧工具**——點一下物件，由 AI 自動生成貼合輪廓。
- **專案制**：每個專案有自己的類別清單（taxonomy）；影片入庫一次即可被多個專案引用。
- **上傳入庫與去識別化**：病人資料夾內的多段錄影自動合併、偵測並切除體外（out-of-body）畫面（需人工核可）、
  去音軌與 metadata，標準化為 720p / 25 fps 固定幀率，以匿名編號入庫。
- **資料集匯出**：COCO、YOLO（偵測＋分割）、MOSaiC JSON（可雙向轉換）、影格–label 配對、影片片段–label 配對、
  temporal CSV；每筆輸出皆可回溯影片與影格編號。
- **一鍵健檢與初始化**：`surgann doctor`（逐項檢查並給修復指令）、`surgann setup`、`surgann fetch-models`。
- **安全備份**：`surgann backup` 打包標註資料庫與可攜 JSON 快照。
- 介面支援繁體中文 / English 切換。

## 系統需求

| 項目 | 需求 |
|---|---|
| 作業系統 | Ubuntu 24.04（已驗證）；22.04 預期可用但未實測；其他 Linux 未測 |
| Python | 3.10 以上 |
| 系統套件 | `git`、`python3-venv`、`ffmpeg` |
| GPU | **選配**。有 NVIDIA 顯卡時 SAM、體外畫面偵測、影片轉檔會用 GPU 加速；沒有也能完整使用（自動改用 CPU，較慢） |
| 磁碟 | 依影片量而定；一支 2 小時的標準化影片約 7 GB。建議資料目錄放在大容量磁碟 |
| 瀏覽器 | 近期版 Chrome / Firefox / Edge |

## 快速開始

**建議：請你的 AI agent（例如 Claude Code）讀 [`INSTALL_agent.md`](INSTALL_agent.md) 並照著安裝。**
這份手冊寫成 agent 可以逐步執行與驗證的形式，並標明哪些步驟必須停下來問你（sudo、模型授權同意、刪除資料）。

也可以手動照 `INSTALL_agent.md` 做。大致流程：

```bash
git clone https://github.com/dreamhunteryin/SurgAnnFlow_open.git ~/SurgAnnFlow && cd ~/SurgAnnFlow
python3 -m venv .venv && . .venv/bin/activate
pip install -e . -r deploy/requirements-cpu.txt     # 有 NVIDIA GPU 改用 requirements-gpu.txt
surgann setup --yes                                # 建設定檔、資料目錄、資料庫
surgann fetch-models --only sam                    # 下載 SAM 模型
surgann fetch-models --only oob                    # OOB 模型已隨 repo 附帶；閱讀授權，同意請輸入 accept
surgann doctor                                     # 健檢，FAIL 為 0 即可
surgann annotate                                   # 瀏覽器開 http://127.0.0.1:8090
```

## 使用教學

標註介面內建完整的**使用手冊**（含快捷鍵表）：啟動後在網頁右上角點「📖 使用手冊」，
或直接開 `http://127.0.0.1:<port>/guide.html`（英文版 `guide_en.html`）。

常用指令一覽：`surgann --help`；每個子指令加 `--help` 看參數。

## 資料安全

- 原始影片放在 `<data 根>/raw_videos/`，平台任何流程都**不會修改、覆寫或刪除**原始檔。
- 標註資料只存在 `registry/registry.db` 一處：**定期執行 `surgann backup`**，升級或任何刪除操作前一定先備份。
- 平台目前**沒有登入機制**，預設只接受本機瀏覽器連線；請勿直接開放到網際網路。

## 授權

- **SurgAnnFlow 程式碼**：[PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/)
  ——可自由用於非商業目的（研究、教學、個人使用等）；**商業使用需另行取得授權**。全文見 `LICENSE`。
- **OOBNet 體外畫面偵測模型**（隨 repo 附帶於 `pipeline/models/oob/oobnet.onnx`，出處與授權見同目錄 `LICENSE-OOBNet.md`）：由
  [CAMMA research group, University of Strasbourg](https://github.com/CAMMA-public/out-of-body-detector)
  發布之預訓練權重轉換而來，授權為 [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)
  （姓名標示—非商業性—相同方式分享）。首次使用前須執行 `surgann fetch-models --only oob` 閱讀授權並輸入 `accept` 同意；未同意時標註功能照常，只有影片入庫的去識別化不可用。
- **SAM 2 模型**（`surgann fetch-models --only sam`）：Meta 發布的 Segment Anything Model 2，授權
  [Apache-2.0](https://github.com/facebookresearch/sam2/blob/main/LICENSE)；本平台下載其公開的 ONNX 轉換檔（[vietanhdev/segment-anything-2-onnx-models](https://huggingface.co/vietanhdev/segment-anything-2-onnx-models)，Apache-2.0）。

使用本平台產出的研究成果時，請一併標示上述模型出處。
