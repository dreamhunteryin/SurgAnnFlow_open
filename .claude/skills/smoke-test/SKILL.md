---
name: smoke-test
description: annotator 核心功能煙霧測試 SOP。改動 pipeline/annotator/（前端或後端）後必跑：啟動服務、逐一驗證核心 API 與頁面沒壞，守住「現有功能完整保留」底線。
---

# smoke-test — annotator 核心功能驗證

改動 `pipeline/annotator/` 後必跑。目的：**證明既有功能沒被弄壞**，不是完整測試。

## 步驟

```bash
# 0. CLI 本身沒壞
source .venv/bin/activate && surgann --help >/dev/null && echo CLI_OK

# 1. 取得實際 port（deploy/.env 的 ANNOTATOR_PORT，沒設＝8090）與網址
PORT=$(grep -E '^ANNOTATOR_PORT=' deploy/.env 2>/dev/null | cut -d= -f2); PORT=${PORT:-8090}
BASE=http://127.0.0.1:$PORT

# 2. 背景啟動 annotator（若已在跑則跳過此步；自己啟動的要記下 PID，測完只停它）
surgann annotate & SMOKE_PID=$!
sleep 3

# 3. 動態取測試對象：專案用 default；影片取 /api/videos 清單第一支（沒有就留空）
PROJ=default
VID=$(curl -sf $BASE/api/videos | python3 -c 'import json,sys; v=json.load(sys.stdin); print(v[0]["video_id"] if v else "")')
echo "PORT=$PORT VID=${VID:-<無影片>}"
```

然後用 curl 逐項打 API：

| # | 驗證 | 指令 | 預期 |
|---|---|---|---|
| 1 | landing 頁 | `curl -sf $BASE/ \| head -c 200` | HTML |
| 2 | 專案清單 API | `curl -sf $BASE/api/projects` | JSON、至少 1 專案（`default`） |
| 3 | 影片清單 API | `curl -sf $BASE/api/videos` | JSON；video_id 格式為 `{seriesID}_{NNNN}`（如 `ABCD_0001`）。尚未入庫時為空陣列 `[]` |
| 4 | 專案 taxonomy API | `curl -sf $BASE/api/projects/$PROJ/labels` | JSON、labels 非空 |
| 5 | 標註讀取 API | `curl -sf "$BASE/api/videos/$VID/annotations?project=$PROJ"` | JSON、筆數 ≥ 改動前（`?project=` 必填，缺 → 422） |
| 6 | 影片串流 byte-range | `curl -sf -r 0-1023 -o /dev/null -w "%{http_code}" $BASE/media/$VID/$VID.mp4` | `206` |
| 7 | 靜態檔含版本戳 | `curl -sf $BASE/app.html \| grep -o 'app.js?v=[^"]*'` | 有 `?v=`，且改過 JS/CSS 時版本戳已更新 |
| 8 | 系列 API | `curl -sf $BASE/api/series` | JSON、每列含 `series_id`（四碼）與 `name`、`n_videos` |
| 9 | 影片入庫頁 | `curl -sf $BASE/upload.html \| head -c 200` | HTML |
| 10 | PHI 收斂 | `curl -sf $BASE/api/videos \| grep -c source_path` | `0`（videos 回應不得含 source_path） |

`VID` 為空（尚未入庫任何影片）時，#5、#6 記為 **SKIP（無影片）**，不算失敗；
要完整驗證請先入庫一支測試影片（`surgann ingest <影片檔> --video-id <ID>`，或從介面「影片入庫」頁），再重跑本表。

（路由以 `pipeline/annotator/server.py` 為準——現況：標註 CRUD 在 `/api/videos/{video_id}/annotations`（GET/POST）與 `/api/annotations/{ann_id}`（PATCH/DELETE）；若日後改動，同步更新本表。）

## 標註 CRUD 完整性（改到標註讀寫時必做）

1. `POST /api/videos/{video_id}/annotations` 建一筆測試標註（label 用現有 taxonomy；body 必帶 `project_id`）→ 記下 ann_id
2. GET（帶 `?project=`）確認讀得到、欄位完整（video_id/layer/label/start_frame/end_frame）
3. `PATCH /api/annotations/{ann_id}?project=<project_id>` 改 end_frame → GET 確認生效（project 必帶，且須與標註歸屬專案一致，不符 → 404）
4. `DELETE /api/annotations/{ann_id}?project=<project_id>` → GET 確認消失
5. **測試資料必清理**，不得留在 registry。

## 判定

- 任何一項失敗 → 改動**不得 commit**，把失敗輸出回報給下指令的人（使用者或協調的 agent）。
- 全過 → 回報「smoke-test 全過（N 項）」並附關鍵輸出。
- 測完把自己啟動的服務停掉（`kill $SMOKE_PID`）；**不得動使用者原本就在跑的服務**。
