# 影片下載器專案交接文件

本文件用來交接「影片下載器」專案的目前狀態、設計規則、版本規則、已知問題與後續維護方向。新的開發者或下一次接手修改時，請先讀完本文件，再修改主程式。

## 1. 專案目前狀態

目前主線版本以 `downloader.py` 為基準（版本 v1.1.2）。

這是一個使用 Tkinter 製作的桌面 GUI 影片下載器，核心下載能力由 `yt-dlp` 提供，轉檔與媒體檢測由 `ffmpeg` / `ffprobe` 提供。

目前支援：

- 單一影片下載。
- 播放清單下載。
- MP4 影片模式，下載高解析度來源後轉成高相容性的 H.264 MP4。
- 音訊模式：MP3、WAV、ALAC。
- 影片縮圖預覽。
- 下載與轉檔進度顯示。
- 取消下載。
- 下載完成後自動產生詳細紀錄。
- ffmpeg、ffprobe、yt-dlp、磁碟空間等啟動環境摘要。
- H.264 硬體編碼器偵測：NVENC、QSV、AMF，不可用時退回 libx264。
- 程式異常中斷後，清理舊的轉檔暫存檔。

版本號管理：自 v1.1.2 起，版本字串集中在 `APP_VERSION` 常數，視窗標題 `root.title(...)` 會自動套用，避免標題與實際版本不一致。

## 2. 主要檔案

目前專案最少需要以下檔案：

```text
downloader.py      主程式
README.md                 專案交接與規則文件
ffmpeg.exe                建議放在程式旁邊，或安裝到系統 PATH
ffprobe.exe               建議放在程式旁邊，或安裝到系統 PATH
app.ico                   可選，程式圖示
```

Python 套件需求：

```bash
pip install yt-dlp requests pillow
```

如果要檢查或更新 yt-dlp：

```bash
pip install -U yt-dlp
```

若是已打包成 exe，更新系統 Python 裡的 yt-dlp 不一定會更新 exe 內部版本。打包版通常需要重新打包。

## 3. 執行方式

開發模式：

```bash
python downloader.py
```

語法檢查：

```bash
python -m py_compile downloader.py
```

建議每次修改後都至少執行一次 `py_compile`。

## 4. 打包建議

可使用 PyInstaller 打包。範例：

```bash
pyinstaller --onefile --windowed --icon=app.ico downloader.py
```

如果要把 ffmpeg 與 ffprobe 一起放進 exe 附近，建議保持以下規則：

- 開發測試時，`ffmpeg.exe` 與 `ffprobe.exe` 可放在主程式同資料夾。
- 打包後，若使用 onefile，可透過 `resource_path()` 尋找 `_MEIPASS` 暫存位置中的資源。
- 若不打包 ffmpeg / ffprobe，使用者電腦需要自行安裝並加入 PATH。

## 5. 版本歷史摘要

### v1.0

初始可用版，已具備 GUI、yt-dlp 下載、ffmpeg 轉檔、縮圖、取消、進度與紀錄功能。

v1.0 主要問題：

- 啟動時背景執行緒可能比 UI 元件更早執行，造成競態風險。
- 背景執行緒會直接讀取 Tkinter `StringVar`。
- 下載結果依賴「下載前掃資料夾、下載後比對差異」，在 Downloads 大資料夾或其他程式同時寫檔時可能誤判。
- MP3 / WAV postprocessor 後的最終檔案不一定能精準對應項目。
- H.264 轉檔時音訊固定轉 AAC 192k，可能造成不必要二次壓縮。
- 播放清單檔名未補零。
- ALAC 縮圖暫存檔在失敗、取消、跳過時可能殘留。
- 部分註解與實際行為不一致。
- 存在一些只寫不讀的死狀態。

### v1.1

針對穩定性與下載結果追蹤做大幅修正。

主要改動：

- 先建立 UI，再啟動背景偵測執行緒。
- 背景下載執行緒不再直接讀 Tkinter 變數。
- 新增 yt-dlp hook-based 檔案追蹤，減少對資料夾掃描差異的依賴。
- H.264 轉檔時，音訊能 copy 就 copy，不能 copy 才轉 AAC。
- 改善取消與關閉流程。
- UI 文案改成「下載最高解析度來源，轉成 H.264」這類比較準確的說法。

### v1.1.1

吸收 v1.0 檢查報告中的細節修正。

主要改動：

- 播放清單檔名改成 `%(playlist_index)03d - %(title)s.%(ext)s`。
- `format_sort` 加入 `vcodec:h264` 與 `acodec:aac` 偏好。
- 移除 `current_ydl` 與 `completed_download_keys` 死碼。
- ALAC 縮圖清理改成 `try/finally`，覆蓋成功、失敗、取消、跳過。
- WAV 註解修正，不再誤導使用者以為會自動保留來源 bit-depth。
- 硬體編碼器品質參數註解修正，不再宣稱 NVENC / QSV / AMF 的數值等同 libx264 CRF。
- `finalize_transcoded_file()` 優先使用 `os.replace()`，失敗才用 `shutil.move()`。
- 修正 yt-dlp download log 過濾字串。
- `safe_folder_name()` 補上 `CON.txt` 類型的 Windows 保留名稱檢查。
- 啟動時清理舊的 `.__h264__.` / `.__alac__.` 暫存檔。
- JS runtime 警告語氣放緩。
- 分析中關閉視窗會先詢問。

### v1.1.2

針對 v1.1.1 檢查報告的小修版。

主要改動：

- H.264 轉 MP4 時只取第一條音訊軌 `0:a:0?`，避免多音軌來源中部分音訊格式不能 copy 進 MP4 而導致整檔失敗。
- `postprocessor_hook` 只處理 `finished` 狀態，不處理幾乎無效的 `started`。
- 移除 `current_selected_audio` 死狀態。
- 保留 `download_thread`，並實際用於關閉時等待 worker 結束。
- 關閉下載中視窗時，最多等待 5 秒，每 200ms 檢查一次 worker 是否結束。
- `safe_folder_name()` 補上 `.CON` 這種前導點保留名稱情況。
- 每次開始下載前，針對實際目標資料夾再清理一次舊暫存檔。
- `__files_to_move` 加上 try/except，保留追蹤能力但不讓內部欄位異常影響整體流程。

## 6. 版本命名規則

建議之後維持以下版本規則：

- `v1.1.3`：小 bug 修正、註解修正、UI 文字修正、錯誤提示改善。
- `v1.2`：新增使用者可見的新功能，例如 cookies 支援、下載模式選擇、設定檔保存。
- `v2.0`：大幅重構架構，例如拆分多檔案、改成設定檔系統、加入任務佇列、多下載併行。

每次發新版時，回覆或更新文件請包含：

```text
版本：
基準檔案：
修改重點：
修正原因：
覆蓋檔案：
是否需要重新打包 exe：
是否需要更新依賴套件：
是否需要使用者重新設定：
已做測試：
已知限制：
```

## 7. 程式設計規則

### 7.1 Tkinter 執行緒規則

Tkinter UI 元件與 `StringVar`、`BooleanVar` 等 Tkinter 變數，原則上只能在主執行緒讀寫。

背景執行緒如果要更新 UI，必須透過：

```python
self.safe_after(...)
```

不要在背景下載執行緒直接呼叫：

```python
self.status_label.config(...)
self.progress_bar.config(...)
self.audio_format_var.get()
self.format_var.get()
```

如果需要 UI 選項，必須在 `start_download()` 這類主執行緒函式先讀出普通字串，再傳入背景執行緒。

### 7.2 背景執行緒啟動規則

會更新 UI 的背景執行緒，必須在 `create_widgets()` 之後啟動。

正確順序：

```python
self.root.protocol("WM_DELETE_WINDOW", self.on_close)
self.create_widgets()
threading.Thread(...).start()
```

不要在 UI 元件尚未建立前啟動硬體偵測、JS runtime 檢查或環境摘要執行緒。

### 7.3 取消下載規則

取消機制由以下幾層組成：

- `cancel_event`
- `progress_hook`
- `match_filter`
- `YTDLPLogger.debug`
- `current_process.terminate()`，主要用於 ffmpeg 子程序

不要假設 yt-dlp 有一個可被外部呼叫的取消 API。若程式中出現類似 `current_ydl` 但沒有實際讀取或取消用途的狀態，應刪除，避免誤導維護者。

### 7.4 關閉視窗規則

如果正在下載或轉檔：

1. 先詢問使用者是否確定關閉。
2. 設定 `is_closing = True`。
3. 設定 `cancel_event`。
4. 終止目前 ffmpeg 子程序。
5. 最多等待 5 秒讓背景 worker 善後。
6. 到期或 worker 結束後再 destroy root。

如果正在分析內容，也應先詢問使用者，不要直接關閉。

### 7.5 死碼規則

不要保留只寫入、不讀取的狀態欄位。

已刪除或不應再新增的死狀態範例：

```python
self.current_ydl
self.completed_download_keys
self.current_selected_audio
```

例外：`download_thread` 目前不是死碼，因為關閉流程會檢查它是否仍在執行。

## 8. 檔案命名與資料夾規則

### 8.1 Windows 安全名稱

所有播放清單資料夾名稱必須透過 `safe_folder_name()` 處理。

必須處理：

- Windows 不允許的字元：`\ / : * ? " < > |`
- 結尾的點與空白。
- Windows 保留名稱：`CON`、`PRN`、`AUX`、`NUL`、`COM1` 到 `COM9`、`LPT1` 到 `LPT9`。
- `CON.txt` 這種主檔名是保留字的情況。
- `.CON` 這種前導點情況。

### 8.2 播放清單檔名

播放清單輸出檔名必須補零，避免檔案總管排序錯亂。

目前規則：

```python
"%(playlist_index)03d - %(title)s.%(ext)s"
```

效果：

```text
001 - title.mp4
002 - title.mp4
010 - title.mp4
100 - title.mp4
```

不要改回：

```python
"%(playlist_index)s - %(title)s.%(ext)s"
```

否則名稱排序會變成 `1, 10, 100, 2`。

## 9. 下載結果追蹤規則

v1.1 之後的核心設計是：不要再只靠「資料夾前後差異」判斷下載結果。

目前使用：

- `progress_hook`
- `postprocessor_hooks`
- `extract_hook_file_paths()`
- `remember_output_file()`
- `get_tracked_media_files()`
- 必要時才 fallback 到近期檔案掃描

這比掃整個 Downloads 更可靠，也比較不會把其他程式剛好產生的檔案誤判成這次下載結果。

### 9.1 postprocessor hook 規則

`postprocessor_hook()` 只處理：

```python
status == "finished"
```

不要處理 `started`，因為檔案通常還沒建立，`remember_output_file()` 會拒絕不存在的檔案。

### 9.2 `__files_to_move` 規則

`__files_to_move` 是 yt-dlp 內部欄位，不是穩定公開 API。

目前規則：

- 可以讀取它來增加檔案追蹤準確度。
- 必須包 try/except。
- 讀不到時不能讓整體下載流程失敗。
- 必須 fallback 到其他欄位或近期檔案掃描。

不要把 `__files_to_move` 當成唯一可靠來源。

## 10. 影片 MP4 / H.264 規則

影片模式目前目標是：

```text
下載高解析度來源，再轉成相容性高的 H.264 MP4。
```

下載格式規則：

```python
"format": "bestvideo+bestaudio/best"
"merge_output_format": "mp4"
"format_sort": ["res", "fps", "vcodec:h264", "acodec:aac"]
```

這代表：

- 優先解析度。
- 同解析度時優先較高 fps。
- 同條件下偏好 H.264 / AAC，減少不必要重編碼。
- 如果沒有 H.264，仍可下載其他格式再轉 H.264。

### 10.1 H.264 轉檔規則

轉檔時只取第一條影片與第一條音訊：

```python
"-map", "0:v:0?", "-map", "0:a:0?"
```

不要改回：

```python
"-map", "0:a?"
```

原因：多音軌來源可能同時包含 AAC、Opus、其他格式。如果 copy 所有音軌進 MP4，可能因其中一條音軌不相容而整檔失敗。

### 10.2 音訊 copy 規則

如果第一條音訊是常見可放進 MP4 的格式，可直接 copy，避免二次壓縮。

目前規則：

```python
if audio_codec in {"aac", "mp3", "alac"}:
    audio_args = ["-c:a", "copy"]
else:
    audio_args = ["-c:a", "aac", "-b:a", "192k"]
```

若未來遇到特定來源 MP4 muxer 仍不接受，應考慮加入 fallback：第一次 copy 失敗後，再自動以 AAC 重編一次。

### 10.3 硬體編碼器規則

目前偵測順序：

1. `h264_nvenc`
2. `h264_qsv`
3. `h264_amf`
4. `libx264`

注意：NVENC、QSV、AMF 的品質數值不等於 libx264 CRF。不要在註解中寫「各編碼器等價」。正確說法是：這些設定是為了在一般觀看品質、檔案大小與速度之間取得平衡。

## 11. 音訊模式規則

### 11.1 MP3

MP3 使用 yt-dlp `FFmpegExtractAudio`：

```python
preferredcodec = "mp3"
preferredquality = "320"
```

MP3 模式可使用 `EmbedThumbnail` 嵌入縮圖。

### 11.2 WAV

WAV 使用 yt-dlp `FFmpegExtractAudio`：

```python
preferredcodec = "wav"
```

重要規則：不要在註解或 UI 中暗示 WAV 會提升音質，也不要說它會自動保留來源 bit-depth。

對 YouTube 這類來源來說，原始音訊通常是 Opus 或 AAC，有損來源轉 WAV 不會變成真正更高音質，只是變成未壓縮容器，檔案會變大。

### 11.3 ALAC

ALAC 模式不是用 yt-dlp 的 `FFmpegExtractAudio` postprocessor，而是：

1. yt-dlp 下載 `bestaudio/best`。
2. 同時下載縮圖。
3. 程式自己呼叫 ffmpeg 轉成 ALAC `.m4a`。
4. 若有縮圖，轉成 JPEG attached picture 嵌入 m4a。
5. 成功、失敗、取消、跳過都要清理縮圖暫存檔。

ALAC 註解要保留這個觀念：如果來源是 YouTube 的 Opus/AAC，ALAC 不會提升音質，只是用無損格式保存「已經有損的來源」。

## 12. 暫存檔規則

轉檔暫存檔命名包含：

```text
.__h264__.
.__alac__.
```

啟動時與每次開始下載前，會清理 30 分鐘以前的這類暫存檔。

不要清理太新的暫存檔，避免誤刪目前正在處理的檔案。

不要清理不符合命名規則的使用者檔案。

## 13. 錯誤處理與使用者提示規則

### 13.1 BiliBili HTTP 412

錯誤範例：

```text
ERROR: [BiliBili] ... Unable to download JSON metadata: HTTP Error 412: Precondition Failed
```

意思：yt-dlp 在分析 BiliBili 影片資料時，被 BiliBili API 拒絕，還沒進入下載或轉檔階段。

這通常不是 ffmpeg 問題，也不是 H.264 / ALAC 轉檔邏輯問題。

可能原因：

- yt-dlp 的 BiliBili extractor 暫時跟不上網站變動。
- BiliBili 要求登入 cookie。
- 網址是短網址或帶太多分享參數。
- 地區、登入狀態、反爬條件或 API 驗證限制。

建議使用者先嘗試：

1. 更新 yt-dlp。
2. 改用乾淨的 BiliBili BV 網址。
3. 確認同一台電腦、同一網路、同一登入狀態下，瀏覽器可以播放。
4. 若仍失敗，未來版本應加入 cookies 支援。

### 13.2 未來建議加入 cookies 支援

建議規劃為 v1.2 功能，而不是 patch 小修。

可做：

- UI 新增「使用 Chrome cookies」選項。
- UI 新增「選擇 cookies.txt」選項。
- 分析與下載都套用 cookies。
- 遇到 BiliBili 412 時給出明確提示。

yt-dlp 可能用法：

```python
"cookiesfrombrowser": ("chrome",)
```

或：

```python
"cookiefile": "cookies.txt"
```

注意：cookies 可能含有登入資訊，不應寫入公開 log，不應自動上傳，不應存在專案版本庫。

## 14. UI 與使用者體驗規則

- 不要讓使用者看到過度技術化的錯誤訊息後完全不知道怎麼辦。
- 可以保留 console log 給開發者，但 messagebox 應盡量用白話說明。
- `HTTP 412`、`ffmpeg not found`、`ffprobe not found`、`Node.js not found` 等情境應提供下一步建議。
- JS runtime 警告不要寫得過度嚇人。正確說法是：一般下載通常仍可運作，但少部分 YouTube 新機制或高階格式可能受影響。
- 取消下載時要提示「可能需要等待目前網路請求或轉檔步驟結束」，避免使用者以為按下取消就會瞬間停止。

## 15. 測試清單

每次發新版前，至少做以下測試：

### 15.1 基本檢查

```bash
python -m py_compile downloader.py
```

檢查：

- 程式能啟動。
- 視窗標題版本正確。
- UI 不會一啟動就噴 `AttributeError`。
- console 能印出環境摘要。

### 15.2 單支影片

測試項目：

- 分析影片成功。
- 顯示縮圖。
- 顯示標題、頻道、長度、解析度、觀看次數、上傳日期。
- 影片 MP4 下載成功。
- H.264 轉檔成功或已是 H.264 時跳過。
- 詳細紀錄會產生。

### 15.3 播放清單

測試項目：

- 分析播放清單成功。
- 下載資料夾使用安全名稱。
- 檔案名稱有補零。
- 多項目進度顯示正常。
- 部分項目失敗時，不應讓整個程式崩潰。
- 詳細紀錄能列出成功、失敗、待確認項目。

### 15.4 音訊格式

測試：

- MP3 320kbps。
- WAV。
- ALAC。
- ALAC 有縮圖時能嵌入縮圖。
- ALAC 失敗或取消時縮圖暫存檔會被清理。

### 15.5 取消與關閉

測試：

- 下載中取消。
- 轉檔中取消。
- 下載中按視窗 X 關閉。
- 分析中按視窗 X 關閉。
- 關閉時不應留下大量 `.part`、`.__h264__.`、`.__alac__.` 檔案。

### 15.6 特殊網站

至少記錄結果：

- YouTube 單支影片。
- YouTube 播放清單。
- BiliBili 單支影片。
- 需要登入或有地區限制的影片。

如果遇到 BiliBili 412，目前先視為網站或 yt-dlp extractor 限制，不要誤判成 ffmpeg 或轉檔錯誤。

## 16. 已知限制與待辦事項

### 16.1 已知限制

- BiliBili 可能出現 HTTP 412，表示 yt-dlp 分析 metadata 時被拒絕。
- 尚未提供 cookies UI。
- `__files_to_move` 是 yt-dlp 內部欄位，未來 yt-dlp 更新後可能變動。
- 目前無內建 yt-dlp 自動更新功能。
- 目前無任務佇列與多任務管理。
- 目前主程式仍是單一大型 `.py` 檔，未拆分模組。

### 16.2 建議 v1.1.3

適合做小修：

- 改善 BiliBili 412 的 messagebox 提示。
- 在錯誤訊息中加入「建議更新 yt-dlp、改用乾淨 BV 網址、或使用 cookies」提示。
- 檢查所有 log 是否有過度技術化或誤導文案。

### 16.3 建議 v1.2

適合新增功能：

- cookies 支援。
- 儲存使用者設定，例如預設下載資料夾、預設格式。
- 下載模式選項：最高畫質、最高相容性、最快速度。
- yt-dlp 版本更新提示或更新引導。
- 更完整的網站錯誤提示對照表。

### 16.4 建議 v2.0

適合大改：

- 拆分成多檔案模組。
- 將 GUI、下載服務、轉檔服務、檔案追蹤、設定管理分離。
- 加入任務佇列。
- 加入多任務下載管理。
- 加入可測試的 service layer，減少 GUI 和邏輯混在一起。

## 17. 交接給下一位開發者的注意事項

接手時請先確認：

1. 目前基準檔是不是 `downloader.py`。
2. 是否已修正視窗標題版本號。
3. 是否有新的 yt-dlp / BiliBili / YouTube 相容性問題。
4. 是否需要重新打包 exe。
5. 是否需要更新 ffmpeg / ffprobe。
6. 是否有使用者回報某個網站不能下載。
7. 是否有實際下載測試，而不只是 `py_compile` 通過。

修改時請避免：

- 讓背景執行緒直接操作 Tkinter。
- 把播放清單檔名補零拿掉。
- 把 H.264 轉檔音訊 mapping 改回所有音軌。
- 把 ALAC 縮圖清理改回只在成功路徑清理。
- 把 yt-dlp 內部欄位當成唯一真相。
- 新增只寫不讀的狀態欄位。
- 在 UI 或註解中誇大 WAV / ALAC 對有損來源的音質提升。

## 18. 建議的修改回報格式

每次交付新版時，建議使用以下格式：

```text
版本：vX.X.X
基準檔案：downloader.py

修改內容：
1. ...
2. ...
3. ...

修正原因：
1. ...
2. ...
3. ...

覆蓋檔案：
- downloader.py
- README.md，如有更新

是否需要重新打包 exe：是 / 否
是否需要更新 yt-dlp：是 / 否
是否需要更新 ffmpeg：是 / 否
是否需要重新設定：是 / 否

測試結果：
- py_compile：通過 / 未測
- GUI 啟動：通過 / 未測
- YouTube 單支：通過 / 未測
- 播放清單：通過 / 未測
- MP3：通過 / 未測
- WAV：通過 / 未測
- ALAC：通過 / 未測
- 取消流程：通過 / 未測

已知限制：
- ...
```

## 19. 最重要的維護原則

本專案目前最重要的原則是：

1. 穩定優先，不要為了理論最高畫質犧牲太多使用者體驗。
2. UI 不要卡死，背景執行緒不要直接碰 Tkinter。
3. 檔案結果追蹤要以 yt-dlp hook 為主，不要退回只靠掃資料夾猜測。
4. 對有損來源轉 WAV / ALAC 的說明必須誠實。
5. 對 BiliBili 412 這類網站限制，要明確告訴使用者這通常是 metadata 分析被拒絕，不是轉檔失敗。
6. 每次修改都要說清楚「改了什麼」、「為什麼改」、「要覆蓋哪些檔案」、「要不要重新打包」。
