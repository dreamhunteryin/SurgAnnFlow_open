# SurgAnnFlow 安裝手冊（給 AI agent 與人類）

> English summary: Step-by-step install guide for SurgAnnFlow on Ubuntu, written so that a CLI agent
> (e.g. Claude Code) can execute and verify each step. Each step lists the command, the expected output,
> a verification command and where to look on failure. Agents MUST stop and ask the human at the points
> marked "停下問人" (sudo, OOB model license, overwriting settings, deleting data).

本手冊把 SurgAnnFlow 從零裝到「瀏覽器打得開標註介面」。適用：**Ubuntu 24.04（已驗證）**；22.04 預期可用但未實測（其他 Linux 發行版指令可能不同）。

**給 AI agent 的總原則**

1. 一步一步做，每步做完先跑該步的「驗證」，通過才往下。失敗就看該步的「失敗時」與文末〈除錯對照表〉。
2. 每條指令都在 `APP_DIR`（專案目錄）執行，且先進入 venv（`. .venv/bin/activate`），除非該步另有說明。
3. 遇到〈必須停下問人〉清單裡的事，**一律停下來問使用者，不可自行決定**。
4. 本手冊中的 `$APP_DIR`、`$DATA_DIR` 等是下方變數區的值；執行前先 `export` 好，或直接代入。

---

## 0. 可客製變數區（先確認再開工）

agent 開工前，把下表逐項向使用者確認；使用者說「用預設」就採預設值。

| 變數 | 預設值 | 說明 |
|---|---|---|
| `APP_DIR` | `$HOME/SurgAnnFlow` | 程式碼放哪（git clone 的目的地）。標註資料庫 `registry/` 與備份 `backups/` 也固定放在這裡。 |
| `DATA_DIR` | `$APP_DIR/data` | 「data 根」：影片、抽出的影格、匯出的資料集、AI 模型都放這。**影片很佔空間**（一支 2 小時標準化影片約 7 GB），建議指到大容量磁碟。 |
| `PORT` | `8090` | 標註網頁的 port（瀏覽器網址 `http://127.0.0.1:<PORT>`）。同一台機器裝兩套時要不同。 |
| `ANNOTATOR_NAME` | `annotator1` | 標註頁頂欄「標註者」的預設名稱（之後可在介面名冊中增減）。 |
| `TZ` | 留空 | 時區，例如 `Asia/Taipei`、`Europe/Paris`。留空＝用作業系統時區（查：`timedatectl`）。 |
| `RECIPE` | 自動判斷 | 安裝配方：`gpu`（有 NVIDIA 顯卡且 driver 正常）或 `cpu`。判斷方式見步驟 3。 |
| `INSTALL_SYSTEMD` | 問使用者 | 要不要裝成「開機自動啟動、當掉自動重開」的常駐服務。需要 sudo（見步驟 9）。不裝也能用，只是要手動啟動。 |

```bash
# 例：全部採預設時
export APP_DIR="$HOME/SurgAnnFlow"
export DATA_DIR="$APP_DIR/data"
export PORT=8090
export ANNOTATOR_NAME=annotator1
export TZ_VALUE=""          # 用 TZ_VALUE 而非 TZ，避免改到目前終端機的時區
```

> **data 根放別顆磁碟時**：那個目錄必須是目前使用者可寫的。若需要 `sudo mkdir` / `sudo chown`，屬於
> 〈必須停下問人〉的 sudo 操作。

---

## 必須停下問人

agent 遇到以下任何一項，**停下來、說明原因、等使用者明確回覆**才可繼續：

| 情境 | 為什麼 | agent 該怎麼做 |
|---|---|---|
| 任何需要 `sudo` 的指令 | 會改動系統、需要管理員密碼 | 依〈sudo 的三種做法〉請使用者選一種。**不得索取、記錄或保存密碼。** |
| OOB 模型授權同意 | 該模型授權為 CC BY-NC-SA 4.0（僅限非商業）；同意是使用者本人的法律行為 | 把 `fetch-models --only oob` 印出的授權全文給使用者看，請**使用者本人在自己的終端機**執行 `surgann fetch-models --only oob` 並輸入 `accept`。**agent 不可代為同意、不可自行加 `--accept-oob-license`**（該旗標只在使用者已明確同意、且由使用者本人要求時才用）。 |
| 要覆蓋 `deploy/.env` 既有值（`surgann setup --force`） | 使用者可能手改過設定 | 先列出「舊值 → 新值」，取得同意才加 `--force`。 |
| 刪除任何檔案或目錄（影片、data 根、`registry/`、`backups/`、模型） | 標註資料只有一份，刪了救不回 | 先問；刪 `registry/` 前一定先 `surgann backup`。 |
| 結束/停掉不是本安裝啟動的程式（例如 port 被別的程式占用） | 可能是使用者的其他服務 | 回報是哪個程式（`ss -ltnp`），讓使用者決定換 port 或停掉它。 |

### sudo 的三種做法

系統層級的動作（`apt install`、安裝 systemd 服務）需要 sudo。agent 依使用者選擇擇一：

1. **使用者自己在 agent 的終端裡執行**：在 Claude Code 中，使用者可在輸入框打 `!` 開頭的指令（例如
   `! sudo apt install -y ffmpeg`），該指令會由使用者自己在同一個終端執行、輸入密碼，結果 agent 看得到。
   agent 只需把要跑的完整指令列給使用者複製。**最推薦。**
2. **使用者授權 agent 執行 sudo**：使用者明確說「你可以跑 sudo」後，agent 才執行；需要密碼時由使用者
   自己在終端輸入。agent **不得**要求使用者把密碼打在對話中，也不得把密碼寫進任何檔案或指令。
3. **完全不用 sudo**：系統套件請管理員代裝；常駐服務改用〈步驟 9B 無 sudo 替代方案〉。

---

## 步驟 1　系統前置套件（需 sudo）

需要：`git`、Python 3.10 以上與 `venv` 模組、`ffmpeg`（影片轉檔）。

```bash
# 先檢查已經有哪些（不需 sudo）
git --version; python3 --version; python3 -c "import venv; print('venv OK')"; ffmpeg -version | head -1
```

缺任何一項時（**停下問人：sudo**）：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip ffmpeg
```

- **預期**：`python3 --version` 顯示 3.10 以上；印出 `venv OK`；`ffmpeg -version` 第一行出現版本號。
- **失敗時**：`python3-venv` 找不到 → Ubuntu 版本太舊或軟體源未更新，先 `sudo apt update`；
  Python 低於 3.10 → 改用較新的 Ubuntu（建議 24.04）。

## 步驟 2　取得程式碼

```bash
git clone https://github.com/dreamhunteryin/SurgAnnFlow_open.git "$APP_DIR"
cd "$APP_DIR"
```

- **預期**：`$APP_DIR` 下出現 `pipeline/`、`deploy/`、`pyproject.toml`、`INSTALL_agent.md`。
- **驗證**：`ls "$APP_DIR/pipeline/cli.py" "$APP_DIR/deploy/.env.example"`
- **失敗時**：`$APP_DIR` 已存在且非空 → 不要刪，**停下問人**（可能是舊安裝；升級請看第 12 節）。

## 步驟 3　建 venv 並依配方安裝

> **venv 是什麼**：專案專用的獨立 Python 環境，裝的套件不會污染系統。之後每開一個新終端機，都要先
> `cd "$APP_DIR" && . .venv/bin/activate` 才能用 `surgann` 指令。

**先決定配方**：

```bash
nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv,noheader
```

- 有輸出（顯卡名稱、driver 版本）→ `RECIPE=gpu`。
- 指令不存在或報錯 → `RECIPE=cpu`（無 GPU 也能完整使用，只是 AI 推論與轉檔較慢，見第 10 節）。
- GPU 配方是針對特定顯卡世代鎖版的組合；**顯卡不是 GTX 10 系列時，先讀 `deploy/requirements-gpu.txt` 檔頭的
  「其他顯卡世代」說明**，必要時依說明調整（調整前可先問使用者）。

```bash
cd "$APP_DIR"
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e . -r deploy/requirements-$RECIPE.txt     # RECIPE 代入 gpu 或 cpu
```

- **預期**：最後一行 `Successfully installed ... surgann-0.1.0 ...`。
- **驗證**：`surgann --help` 列出 `doctor`、`setup`、`fetch-models`、`annotate` 等子指令。
- **失敗時**：
  - `surgann: command not found` → 沒進 venv：`. "$APP_DIR/.venv/bin/activate"`。
  - 下載套件逾時 → 網路問題，重跑同一條 pip 指令即可（已下載的會沿用）。
  - 換配方（cpu↔gpu）：`pip uninstall -y onnxruntime onnxruntime-gpu` 後重跑上面的 `pip install`。兩者不可並存。

## 步驟 4　第一次健檢（預期會有 FAIL）

> **doctor 是什麼**：一鍵健檢，逐項列出 PASS（正常）/ WARN（可用但留意）/ FAIL（阻斷，必須修），
> 每項附「→ 修復指令」。有任何 FAIL 時結束碼為 1——**這是正常回報，不是程式當掉**。

```bash
surgann doctor
```

- **預期（全新安裝）**：Python、套件、ffmpeg、OOB 模型（`model_oob_oob_onnx`，隨 repo 附帶）為 PASS；`deploy/.env`、data 根、`registry.db` 為 FAIL；
  SAM 模型、OOB 授權同意（`oob_license`）、systemd 為 WARN。這些都會在後面步驟補齊。
- **若 Python / 套件 / ffmpeg / onnxruntime 是 FAIL**：先依該行的 `→` 提示修好，再往下。

## 步驟 5　初始化（寫設定、建 data 根、建資料庫）

```bash
surgann setup --yes --data-dir "$DATA_DIR" --port "$PORT" --annotator "$ANNOTATOR_NAME" --tz "$TZ_VALUE"
# 要裝 systemd 常駐服務時，再加 --systemd（只產生設定檔並印出 sudo 指令，不會自己執行 sudo）
```

`setup` 依序做四件事，輸出 `[1/4]`～`[4/4]`：

1. 寫 `deploy/.env`（不存在就以 `deploy/.env.example` 為底建立，權限 600）。**已存在時只補缺的鍵，不覆蓋既有值**；
   若你指定的值與既有值不同，會印出 `! ... 未套用；要覆蓋請加 --force`——**此時停下問人**。
2. 建 data 根與子目錄（`videos/`、`frames/`、`exports/`、`work/`、`raw_videos/_incoming/`、`models/sam/`、`cache/` 等）。
3. 建標註資料庫 `registry/registry.db`（已存在時會先自動備份到 `backups/`，再做無害的初始化）。
4. （加 `--systemd` 時）產生 `deploy/systemd/generated/surgannflow-annotator.service` 並印出安裝指令。

- **預期**：結束碼 0，最後印出「下一步」清單。
- **驗證**：`ls -l deploy/.env`（權限 `-rw-------`）；`ls "$DATA_DIR"`；`ls registry/registry.db`。
- **失敗時**：`無法建立 data 根` → 該路徑目前使用者不可寫；需要 `sudo mkdir -p ... && sudo chown $USER: ...`，**停下問人**。

> **關於 TZ**：`surgann` 每次啟動會套用 `deploy/.env` 的 `TZ`（影響時間戳與備份/匯出檔名）；
> 若環境中已經設了 `TZ` 環境變數（例如 systemd 服務），則以環境變數為準。留空＝系統時區。

## 步驟 6　準備 AI 模型（下載 SAM、同意 OOB 授權）

兩組模型取得方式不同：

- **SAM** 要連網下載到 `<data 根>/models/sam/`，下載後自動驗 sha256（檔案指紋，確認檔案完整沒被竄改）；
  中斷可續傳，重跑即可；已存在且正確會自動略過。
- **OOB** 模型（`oobnet.onnx`）**隨 repo 附帶**在 `pipeline/models/oob/`（出處與授權見同目錄 `LICENSE-OOBNet.md`），
  clone 下來就有、不需下載；這一步只驗檔並取得使用者對授權的同意。

> **離線環境**：OOB 模型 clone 後即可用。SAM 模型仍需連網下載；無法連網時，可在別台機器取得兩個 SAM 檔後
> 手動放進 `<data 根>/models/sam/`，再跑 `surgann doctor` 確認 `model_sam_*` 的 sha256 為 PASS。

**6a. SAM（點一下自動框出物件的智慧標註工具，約 360 MB）**

```bash
surgann fetch-models --only sam
```

- **預期**：encoder / decoder 兩檔各印「✓ 下載完成，sha256 正確」（或「已存在且 sha256 正確，略過」），結束碼 0。
- **缺 SAM 時**：標註照常可用，只是「✨ SAM 智慧工具」停用。

**6b. OOB（上傳入庫時自動偵測並切除「體外畫面」的去識別化模型，約 68 MB，隨 repo 附帶）——停下問人**

附帶不等於同意：使用前必須由使用者本人同意授權。agent 先跑一次（非互動，不會記錄同意），讓授權全文印出來：

```bash
surgann fetch-models --only oob
```

- **預期**：先驗附帶檔 sha256，再印出 OOB 模型授權聲明（CAMMA, University of Strasbourg；CC BY-NC-SA 4.0，僅限非商業研究），
  未同意時以結束碼 2 結束、不寫入任何設定。
- **agent 必須**：把授權聲明原文給使用者看並詢問是否同意。**使用者要同意時，請使用者本人在自己的終端機**執行
  同一指令，看完授權後輸入 `accept`。agent 不可代為輸入、不可自行加 `--accept-oob-license`。
- **同意後預期**：印出「✓ 已記錄同意 CC BY-NC-SA 4.0（僅限非商業研究）…」，`deploy/.env` 多一行
  `OOB_LICENSE_ACCEPTED=<同意當天日期>`；`surgann doctor` 的 `oob_license` 變 PASS。
- **使用者不同意時**：跳過即可。標註、匯出都能用；只有「📥 影片入庫」的自動去識別化（OOB 偵測）無法執行，
  且 `surgann doctor` 會持續顯示 1 項 WARN（`oob_license`）——在驗收時向使用者說明這是預期結果。
- **失敗時**：`sha256 不符` 或缺檔（結束碼 1）→ 附帶檔損毀或被改動，執行
  `git checkout -- pipeline/models/oob/oobnet.onnx`（或重新 `git clone` 本 repo）；
  `尚無 deploy/.env` → 先完成步驟 5 的 `surgann setup`。

## 步驟 7　再次健檢，期望沒有 FAIL

```bash
surgann doctor --json | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok =', d['ok'], d['counts']); [print(c['status'], c['id'], '→', c['fix']) for c in d['checks'] if c['status']!='PASS']"
```

- **預期**：`ok = True`，`FAIL` 為 0。可接受的 WARN：`gpu`（無 GPU 機器）、`nvenc`（無 GPU 將改用 libx264）、
  `systemd`（尚未安裝常駐服務）、`oob_license`（使用者不同意 OOB 授權時）。
- **失敗時**：依 `→` 提示或文末〈除錯對照表〉處理，修完重跑本步。

## 步驟 8　啟動並用瀏覽器開啟（前景模式）

```bash
surgann annotate
```

- **預期**：終端機印出服務網址，例如 `http://127.0.0.1:8090`；程式持續執行（這個終端機要保持開著，`Ctrl+C` 停止）。
- **驗證**（另開一個終端機）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:$PORT/api/config    # 預期：200
curl -s http://127.0.0.1:$PORT/ | head -c 200                               # 預期：HTML 開頭
```

- 請使用者在**同一台電腦**的瀏覽器開 `http://127.0.0.1:<PORT>`，看到專案首頁即成功。
- 預設只接受本機連線；平台目前**沒有登入機制**，不建議直接開放到區網或網際網路。
- **失敗時**：`address already in use` → port 被占用，見〈除錯對照表〉`port`。

要常駐（關掉終端機、重開機後仍在跑）→ 先 `Ctrl+C` 停掉前景程式，再做步驟 9。

## 步驟 9　（選用）常駐服務

> **systemd 是什麼**：Linux 內建的服務管理員，負責開機把程式叫起來、當掉自動重開、集中保存日誌。

### 9A. 系統層 systemd（建議；需 sudo——停下問人）

```bash
. .venv/bin/activate           # 必須在本專案 venv 內執行：產生的服務會用這個 venv 的 surgann
surgann setup --yes --systemd
```

- **預期**：`[4/4] systemd unit 已渲染：deploy/systemd/generated/surgannflow-annotator.service`，
  接著印出下列 sudo 指令（實際路徑以印出的為準）。程式本身不會執行 sudo。
- 依〈sudo 的三種做法〉讓使用者執行：

```bash
sudo cp deploy/systemd/generated/surgannflow-annotator.service /etc/systemd/system/surgannflow-annotator.service
sudo systemctl daemon-reload
sudo systemctl enable --now surgannflow-annotator
```

- **驗證**（不需 sudo）：

```bash
systemctl status surgannflow-annotator --no-pager      # 預期：Active: active (running)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:$PORT/api/config   # 預期：200
surgann doctor                                          # systemd 一列變 PASS
```

- 服務的特性：資料所在磁碟（若是獨立掛載的磁碟）**沒掛好就不啟動**，避免在空目錄上開起來寫錯位置；
  手動 `stop` 不會被自動拉回；只有當掉才自動重開。
- **日常操作**：

```bash
systemctl status surgannflow-annotator --no-pager
sudo systemctl restart surgannflow-annotator      # 改了 deploy/.env 的 port / 標註者名稱後
sudo systemctl stop surgannflow-annotator
journalctl -u surgannflow-annotator -n 100 --no-pager   # 看最近 100 行日誌（服務起不來先看這個）
journalctl -u surgannflow-annotator -f                  # 即時跟看（Ctrl+C 離開，不會停服務）
```

- 改了 `TZ`、換了 data 根位置、搬了專案目錄或換了 venv → 重跑 `surgann setup --yes --systemd`，
  再照印出的指令重新 `cp` + `daemon-reload` + `restart`。

**systemd 常見失敗對照**

| 症狀（`systemctl status` / `journalctl` 看到） | 可能原因 | 處置 |
|---|---|---|
| `Address already in use` / 反覆重啟 | 前景的 `surgann annotate` 還開著，或別的程式占用 port | 停掉前景程式；或改 `deploy/.env` 的 `ANNOTATOR_PORT` 後 `restart` |
| 停在 `inactive (dead)`，日誌提到 mount / `Dependency failed` | 資料磁碟未掛載 | 確認磁碟已掛載（`findmnt <掛載點>`），掛好後 `sudo systemctl start surgannflow-annotator` |
| `status=203/EXEC` 或 `No such file or directory`（指向 `.venv/bin/surgann`） | venv 路徑錯（渲染時不在本專案 venv、或 venv 被刪/搬） | 進入正確 venv → `surgann setup --yes --systemd` → 重新 `cp` + `daemon-reload` + `restart` |
| `Permission denied` / `status=217/USER` | 服務使用者對專案或 data 根無權限，或使用者名稱不存在 | 確認以「平常使用的帳號」執行 `setup`；`ls -ld "$APP_DIR" "$DATA_DIR"` 檢查擁有者 |
| `doctor` 顯示「unit 指向的不是本專案目錄」或「與最新渲染結果不同」 | 裝的是舊版或另一份安裝的服務檔 | 依 doctor 的 `→` 指令重新安裝 |

### 9B. 無 sudo 替代方案

| 方式 | 指令要點 | 開機自啟 | 當掉自動重開 | 限制 |
|---|---|---|---|---|
| **使用者層 systemd** + linger | 見下方 | 是（需 linger） | 是 | `enable-linger` 通常也需管理員做一次；不會等待資料磁碟掛載 |
| **tmux / nohup 背景執行** | `tmux new -d -s surgann 'cd "$APP_DIR" && .venv/bin/surgann annotate'` 或 `nohup .venv/bin/surgann annotate > annotate.log 2>&1 &` | 否 | 否 | 重開機或程式當掉就停了，要手動再開 |
| **crontab `@reboot`** | `crontab -e` 加一行：`@reboot cd <APP_DIR> && .venv/bin/surgann annotate >> <APP_DIR>/annotate.log 2>&1` | 是 | 否 | 不會等磁碟掛載、當掉不重開、日誌要自己管 |

**使用者層 systemd 作法**（先以 `surgann setup --yes --systemd` 產生系統層檔，再轉成使用者層）：

```bash
mkdir -p ~/.config/systemd/user
sed -e '/^User=/d' -e '/^Group=/d' -e '/^RequiresMountsFor=/d' -e '/^ProtectSystem=/d' \
    -e 's/^WantedBy=multi-user.target/WantedBy=default.target/' \
    deploy/systemd/generated/surgannflow-annotator.service \
    > ~/.config/systemd/user/surgannflow-annotator.service
systemctl --user daemon-reload
systemctl --user enable --now surgannflow-annotator
systemctl --user status surgannflow-annotator --no-pager          # 預期：active (running)
journalctl --user -u surgannflow-annotator -n 100 --no-pager      # 看日誌
```

- **linger 是什麼**：讓「使用者層服務」在該使用者沒登入時也能跑、開機就啟動。沒開 linger 時，服務只在使用者登入期間運作。
  開啟：`loginctl enable-linger $USER`（多數系統需要管理員權限一次——**停下問人**）。
  驗證：`loginctl show-user $USER -p Linger` 應為 `Linger=yes`。
- 上面的 `sed` 拿掉了「等資料磁碟掛載」的保護：若 data 根在外接/獨立磁碟上，開機時可能比磁碟早啟動。

## 步驟 10　有 GPU / 無 GPU 的差異（預期行為）

| 元件 | 有 NVIDIA GPU | 無 GPU（CPU 配方） |
|---|---|---|
| ✨ SAM 智慧工具 | GPU 推論，點一下約 0.5 秒 | 自動改用 CPU，每張影像第一次點擊需數秒，之後同張影像較快 |
| OOB 體外畫面偵測 | GPU（顯存不足時自動改 CPU 重跑） | CPU；推論本身不慢，瓶頸在影片解碼 |
| 影片轉檔（入庫標準化） | NVENC 硬體編碼 | 自動改 libx264：**明顯較慢**，長影片可能要數十分鐘以上，屬正常現象 |

全部為**自動降級**，不需要改設定。`doctor` 在無 GPU 機器上的 `gpu`、`nvenc` 兩列 WARN 屬正常。
GPU 機器上若 `onnxruntime` 一列 WARN 寫「CUDA 實測」失敗 → 套件版本與顯卡 driver 不合，見 `deploy/requirements-gpu.txt`
檔頭的世代對照；在修好之前 SAM / OOB 會自動走 CPU。

## 步驟 11　驗收

全部通過才算安裝完成：

1. `surgann doctor` 結果 `FAIL 0`（使用者拒絕 OOB 授權時仍應 `FAIL 0`，另有 1 項 `oob_license` WARN，需向使用者說明）。
2. **smoke-test**：若 agent 支援 skills，執行本 repo 的 `smoke-test` skill（`.claude/skills/smoke-test/`）。
   全新站還沒有影片，需要影片的項目（影片清單、標註讀取、影片串流）可先入庫一支測試影片再驗：
   `surgann ingest <任一 mp4 檔> --video-id TEST_0001`（原始檔不會被修改）；或先略過這幾項並在回報中註明。
3. 使用者在瀏覽器開 `http://127.0.0.1:<PORT>` 看到首頁；右上「📖 使用手冊」可開啟操作教學。

向使用者回報時附上：`APP_DIR`、`DATA_DIR`、網址、`doctor` 的 PASS/WARN/FAIL 計數、是否裝了常駐服務、使用者是否已同意 OOB 授權。

---

## 12. 日常維運簡述

**備份**（標註資料只有一份：`registry/registry.db`）

```bash
surgann backup                          # registry.db + 可攜 JSON 標註快照 → backups/（每次自成時間戳檔，不覆蓋舊備份）
surgann backup --label pre-upgrade      # 加好認的名稱
surgann backup --rsync /path/to/other/disk   # 另外同步一份到第二顆碟/外接碟
```

建議每日收工備份一次；**升級、搬移資料、任何刪除操作之前一定先備份**。

每次備份產生 `backups/<時間戳>[_<label>]/` 目錄與同名 `.tar.gz` 壓縮檔（時間戳為 UTC），內含：
`registry.db`（資料庫完整副本）、`registry_snapshot.json`（可攜 JSON 快照，不依賴 SQLite 版本）、`manifest.json`（備份時間、schema 版本、當時的 data 根、各表筆數）。

**還原（破壞性操作：agent 必須先停下問人）**

還原會用舊備份蓋掉目前的標註資料庫，備份之後新增的標註會消失。agent 必須先向使用者說明要還原哪一份備份（時間、label、`manifest.json` 的表筆數），取得明確同意才動手。

```bash
cd "$APP_DIR" && . .venv/bin/activate
ls backups/                                         # 1) 找要還原的備份，例如 backups/20261102_120735_pre-upgrade.tar.gz
cat backups/<時間戳>_<label>/manifest.json          #    確認時間與表筆數（若只剩 .tar.gz，先做第 2 步再看）

mkdir -p /tmp/surgann-restore                       # 2) 解開到暫存目錄（不要直接解進 registry/）
tar -xzf backups/<時間戳>_<label>.tar.gz -C /tmp/surgann-restore

sudo systemctl stop surgannflow-annotator           # 3) 先停服務（有裝系統層服務時；sudo：停下問人）
                                                    #    前景模式則在該終端機按 Ctrl+C；使用者層服務用 systemctl --user stop

cp registry/registry.db "registry/registry.db.before-restore-$(date +%Y%m%d%H%M%S)"   # 4) 現有資料庫另存，不刪除
cp /tmp/surgann-restore/<時間戳>_<label>/registry.db registry/registry.db             # 5) 蓋回

sudo systemctl start surgannflow-annotator          # 6) 啟動（或前景 surgann annotate）
surgann doctor                                      # 7) 驗證：registry 一列為 PASS；surgann status 列出的影片數應合理
```

- **預期**：`doctor` 無 FAIL；網頁上的專案與標註回到備份當時的狀態。
- **還原後有問題**：停服務，把第 4 步另存的 `registry.db.before-restore-*` 改回 `registry/registry.db`，即回到還原前。
- **`registry_snapshot.json` 的用途**：跨 SQLite 版本或換機器時的可攜備份（純 JSON，每張表一份資料列）；一般還原用 `registry.db` 即可。
- 備份只含資料庫，不含影片與影格（在 data 根）；換機器時需另外整包複製 data 根，並確認 `SURGANN_DATA_DIR` 指向它（資料庫內存的是相對 data 根的路徑）。

**更新 / 升級**

```bash
cd "$APP_DIR" && . .venv/bin/activate
surgann backup --label pre-upgrade
git pull
pip install -e . -r deploy/requirements-$RECIPE.txt
surgann setup --yes                     # 補新版本需要的設定鍵＋init-db（冪等）；不覆蓋既有值
surgann doctor
sudo systemctl restart surgannflow-annotator   # 有裝系統層服務時（sudo：停下問人）；前景模式則重新 surgann annotate
```

**搬移 data 根到別顆磁碟**（停下問人：涉及搬移資料）：先停服務 → 整個目錄搬過去 → 改 `deploy/.env` 的
`SURGANN_DATA_DIR`（或 `surgann setup --yes --data-dir <新位置> --force`）→ 有裝 systemd 則重跑 `surgann setup --yes --systemd` 並重裝
→ `surgann doctor`。資料庫裡存的是相對 data 根的路徑，不必改資料庫。

**解除安裝**（全部步驟都要先問人）

```bash
sudo systemctl disable --now surgannflow-annotator           # 有裝系統層服務時
sudo rm /etc/systemd/system/surgannflow-annotator.service && sudo systemctl daemon-reload
# 使用者層服務：systemctl --user disable --now surgannflow-annotator && rm ~/.config/systemd/user/surgannflow-annotator.service
```

刪除 `APP_DIR`（含標註資料庫與備份）與 `DATA_DIR`（含影片）**無法復原**：先 `surgann backup`、把
`registry/`、`backups/` 複製到安全處，並取得使用者明確同意後才可刪。

---

## 除錯對照表（`surgann doctor` 的 id → 處置）

`doctor --json` 每項有固定欄位 `id` / `status` / `detail` / `fix`；`fix` 就是建議指令。

| id | 狀態 | 意思 | 處置 |
|---|---|---|---|
| `python` | FAIL / WARN | Python 低於 3.10 / 不在 venv 內 | 裝新版 Python（sudo）；或 `. .venv/bin/activate` |
| `pkg_surgann` | WARN | 目前環境沒裝 surgann | 步驟 3 的 `pip install -e . -r deploy/requirements-$RECIPE.txt` |
| `pkg_deps` | FAIL | 缺 Python 套件 | 同上 |
| `ffmpeg` / `ffprobe` | FAIL | 找不到 ffmpeg | `sudo apt install ffmpeg`（停下問人） |
| `gpu` | WARN | 沒偵測到 NVIDIA GPU 或 driver 未載入 | 無 GPU 屬正常；有 GPU 者裝 driver（`sudo ubuntu-drivers install` 後重開機，停下問人） |
| `nvenc` | WARN / FAIL | 無硬體編碼器 / 設定強制 NVENC 但不可用 | WARN 正常（改 libx264）；FAIL → `pipeline/configs/preprocess.yaml` 的 `encode.encoder` 改回 `auto` |
| `onnxruntime` | FAIL | 沒裝 onnxruntime，或 CPU 版與 GPU 版同時存在 | `pip uninstall -y onnxruntime onnxruntime-gpu` 後依配方重裝其一 |
| `onnxruntime` | WARN | GPU 機器只有 CPU 版 / CUDA 實測失敗 | 改裝 GPU 配方；或依 `deploy/requirements-gpu.txt` 世代對照調整版本 |
| `model_sam_*` | WARN / FAIL | SAM 模型缺檔 / sha256 不符 | `surgann fetch-models --only sam`（不符的舊檔會改名保留） |
| `model_oob_oob_onnx` | FAIL | 附帶的 OOB 模型缺檔或 sha256 不符（檔案損毀或被改動）；或 `preprocess.yaml` 的 `oob.onnx_path` 指定的自訂模型不存在 | `git checkout -- pipeline/models/oob/oobnet.onnx`（或重新 `git clone` 本 repo）；自訂模型不存在 → 修正路徑或把 `onnx_path` 清空改回附帶模型 |
| `oob_license` | WARN | 使用者尚未同意 OOB 模型授權：標註可用，影片上傳入庫的去識別化不可用 | 由**使用者本人**執行 `surgann fetch-models --only oob` 並輸入 `accept`（停下問人，agent 不可代為同意） |
| `env_file` | FAIL | 沒有 `deploy/.env` | `surgann setup --yes` |
| `env_file` | WARN | 缺鍵 / 其他使用者可讀 | `surgann setup --yes`（只補缺鍵）；`chmod 600 deploy/.env` |
| `env_port` | FAIL | `ANNOTATOR_PORT` 不是合法 port | 改成 1024–65535 間的整數 |
| `env_obsolete_*` | WARN | `.env` 有已淘汰的舊鍵 | 可自 `deploy/.env` 刪除該行（不影響運作） |
| `data_dir_config` | WARN | `SURGANN_DATA_DIR` 環境變數與 `.env` 不一致 | 確認終端機沒有殘留的 `SURGANN_DATA_DIR` 環境變數（`env | grep SURGANN`） |
| `data_dir` | FAIL | data 根不存在 / 不可寫 / 空間幾乎用完 | `surgann setup --yes`；權限或搬磁碟需 sudo（停下問人） |
| `data_dir` | WARN | 缺子目錄 / 剩餘空間低於 50 GB | `surgann setup --yes`；留意空間 |
| `registry` | FAIL | 沒有 `registry.db` / 打不開 | `surgann setup --yes`；打不開可能是毀損，從 `backups/` 還原前**停下問人** |
| `registry_schema` | WARN | 舊版資料庫沒有版本紀錄 | `surgann init-db` |
| `port` | FAIL | port 被其他程式或另一份安裝占用 | `ss -ltnp | grep :<PORT>` 查是誰（停下問人）；或 `surgann setup --yes --port <新 port> --force`（覆蓋設定，先問人） |
| `systemd` | WARN | 常駐服務未安裝 / 未啟動 / 與本安裝不符 | 選用；依步驟 9 安裝，或依該行 `→` 指令修正 |

其他常見狀況：

| 狀況 | 處置 |
|---|---|
| 瀏覽器打不開，但 `curl .../api/config` 是 200 | 瀏覽器與服務不在同一台電腦；預設只接受本機連線 |
| `surgann` 指令找不到 | `cd "$APP_DIR" && . .venv/bin/activate` |
| 影片卡片沒有縮圖 / 影片播不出來 | 跑 `surgann doctor` 看 `data_dir`；確認 `SURGANN_DATA_DIR` 指向實際放影片的位置 |
| `fetch-models` 中斷 | 重跑同一指令，會從斷點續傳 |
