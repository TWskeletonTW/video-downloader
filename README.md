# 影片下載器（Video Downloader）

> 使用 Python + Tkinter 製作的桌面 GUI 影片下載器，以 `yt-dlp` 為下載核心、`ffmpeg` 為轉檔引擎，支援單一影片與播放清單，並能轉成高相容性的 H.264 MP4 與多種音訊格式。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)

---

## 功能特色

- **單一影片 / 播放清單下載**：自動辨識，播放清單會建立專屬資料夾並補零命名（`001 - title.mp4`）。
- **MP4 影片模式**：下載最高解析度來源，再轉成相容性高的 **H.264 MP4**（強制 8-bit 4:2:0，避免 10-bit 來源在舊裝置無法播放）。
- **多種音訊格式**：MP3 320kbps、WAV（無損 PCM）、ALAC（無損容器，可嵌入縮圖）。
- **H.264 硬體加速偵測**：自動偵測並使用 NVENC（NVIDIA）／Quick Sync（Intel）／AMF（AMD），不可用時退回 `libx264`。
- **即時進度顯示**：下載速度、剩餘時間、轉檔進度一目了然。
- **影片資訊預覽**：縮圖、標題、頻道、長度、解析度、最高可用畫質與音訊格式。
- **可隨時取消**：下載與轉檔皆可中斷，並安全清理暫存檔。
- **自動產生下載紀錄**：完成後輸出含成功／失敗明細的詳細紀錄檔。
- **穩健的工程細節**：背景執行緒不直接操作 UI、Windows 保留檔名處理、異常中斷後自動清理暫存轉檔檔。

## 螢幕截圖

> 在這裡放一張程式執行畫面（建議放在 `docs/screenshot.png`）：
>
> `![程式畫面](docs/screenshot.png)`

## 技術架構

| 項目 | 使用技術 |
|------|----------|
| GUI | Tkinter / ttk |
| 下載核心 | [yt-dlp](https://github.com/yt-dlp/yt-dlp) |
| 轉檔 / 媒體檢測 | ffmpeg / ffprobe |
| 縮圖處理 | Pillow + requests |

## 環境需求

- Python 3.10 以上
- `ffmpeg` 與 `ffprobe`（放在程式同資料夾，或安裝到系統 PATH）

安裝 Python 套件：

```bash
pip install -r requirements.txt
```

## 執行方式

```bash
python downloader.py
```

> 首次使用請確認 `ffmpeg` / `ffprobe` 可被找到，否則轉檔功能無法使用。程式啟動時會在 console 印出環境摘要（Python、yt-dlp、ffmpeg 版本與磁碟空間）。

## 打包成 .exe（選用）

使用 [PyInstaller](https://pyinstaller.org/)：

```bash
pyinstaller --onefile --windowed --icon=app.ico downloader.py
```

`ffmpeg.exe` / `ffprobe.exe` 可放在執行檔旁，程式會透過 `resource_path()` 找到打包後的資源。

## 專案結構

```text
downloader.py      主程式（單檔 GUI 應用）
requirements.txt   Python 套件需求
README.md          專案說明（本檔）
DEVELOPMENT.md     開發與維護筆記（設計規則、版本歷史、測試清單）
LICENSE            MIT 授權
```

## 開發筆記

詳細的設計規則、版本歷史、執行緒模型、測試清單與已知限制，整理在 [DEVELOPMENT.md](DEVELOPMENT.md)。

## 授權

本專案採用 [MIT License](LICENSE) 授權。

## 免責聲明

本工具僅供個人學習與備份個人擁有或已獲授權之內容使用。請遵守各影音平台的服務條款與當地著作權法規，使用者需自行為其使用行為負責。
