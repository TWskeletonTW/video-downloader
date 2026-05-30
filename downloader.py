import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import yt_dlp
import threading
import os
import sys
import subprocess
import shutil
import requests
import re
from pathlib import Path
import time
import json
from PIL import Image, ImageTk
from io import BytesIO
from collections import deque


# ─────────────────────────────────────────────
# 應用程式版本（單一來源，視窗標題與其他位置共用）
# ─────────────────────────────────────────────
APP_VERSION = "1.1.2"


# ─────────────────────────────────────────────
# Windows 保留名稱（不可作為檔案/資料夾名稱）
# ─────────────────────────────────────────────
WINDOWS_RESERVED_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *[f'COM{i}' for i in range(1, 10)],
    *[f'LPT{i}' for i in range(1, 10)],
}


class DownloadCancelled(Exception):
    pass


class YTDLPLogger:
    def __init__(self, app):
        self.app = app

    def debug(self, msg):
        # 安全網取消機制：progress_hook 和 match_filter 是主要取消路徑，
        # 但 logger.debug 在網路等待期間也會被呼叫，可作為補充保障。
        if self.app.cancel_event.is_set():
            raise DownloadCancelled("使用者已取消")

        msg_str = str(msg)
        if not msg_str.startswith(('[download] ', '\r[download] ')):
            print(msg_str)

    def warning(self, msg):
        self.app.ytdlp_warnings.append(str(msg))
        print(f"\n[yt-dlp 警告] {msg}")

    def error(self, msg):
        self.app.ytdlp_errors.append(str(msg))
        print(f"\n[yt-dlp 錯誤] {msg}")


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def safe_folder_name(name: str, fallback: str = "Playlist") -> str:
    """
    產生安全的 Windows 資料夾名稱：
    - 移除非法字元
    - 去除結尾的點和空白（Windows 不允許）
    - 避免 Windows 保留名稱（CON、NUL、COM1、CON.txt 等）
    """
    name = re.sub(r'[\\/:*?"<>|]+', "_", str(name)).strip()
    name = name.rstrip(". ")  # Windows 不允許以點或空白結尾

    # Windows 會把 CON.txt、AUX.mp4 這類「主檔名」也是保留字的名稱視為不合法。
    # 以點開頭的名稱（例如 .CON）在 Windows 也會被保留名稱規則影響，判斷時先去掉前導點。
    trimmed_for_reserved_check = name.lstrip(".")
    stem = trimmed_for_reserved_check.split(".", 1)[0].upper() if trimmed_for_reserved_check else ""
    if name.upper() in WINDOWS_RESERVED_NAMES or stem in WINDOWS_RESERVED_NAMES:
        name = f"_{name}_"

    return name if name else fallback

def find_ffmpeg() -> str | None:
    env_ffmpeg = shutil.which("ffmpeg")
    if env_ffmpeg:
        return env_ffmpeg
    local_ffmpeg = resource_path("ffmpeg.exe")
    if os.path.exists(local_ffmpeg):
        return local_ffmpeg
    return None


def find_ffprobe(ffmpeg_path: str | None = None) -> str | None:
    env_ffprobe = shutil.which("ffprobe")
    if env_ffprobe:
        return env_ffprobe
    local_ffprobe = resource_path("ffprobe.exe")
    if os.path.exists(local_ffprobe):
        return local_ffprobe
    if ffmpeg_path:
        sibling = Path(str(ffmpeg_path)).with_name("ffprobe.exe")
        if sibling.exists():
            return str(sibling)
    return None


def format_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_count(value):
    try:
        return f"{int(value):,}"
    except Exception:
        return "未知"


class VideoDownloaderApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"影片下載器 v{APP_VERSION}")

        icon_path = resource_path("app.ico")
        if os.path.exists(icon_path):
            try:
                self.root.iconbitmap(icon_path)
            except Exception as e:
                print(f"無法載入圖示: {e}")

        self.root.geometry("660x920")
        self.root.minsize(660, 720)

        self.download_folder = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads"))
        self.video_info = {}
        self.tk_image = None
        self.audio_format_var = tk.StringVar(value="mp3")
        self.format_var = tk.StringVar(value="mp4")
        self.current_download_mode = None
        self.current_download_folder = ""
        self.current_process = None
        self.cancel_event = threading.Event()
        self.is_analyzing = False
        self.is_downloading = False
        self.is_closing = False
        self.analysis_valid = False
        self.analyzed_url = ""
        self.expected_count = 0
        self.ytdlp_errors = []
        self.ytdlp_warnings = []
        self.current_summary = None
        self.download_item_titles = {}
        self.download_item_states = {}
        self.download_item_file_map = {}
        self.download_file_key_map = {}
        self.expected_item_keys = []
        self.unknown_key_counter = 0
        self.download_thread = None
        self.download_started_at = 0.0
        self.download_output_files = []
        self.postprocessed_output_files = []

        # 硬體 H.264 編碼器（預設 libx264，背景偵測後更新）
        self.hardware_encoder = "libx264"

        # 先建立 UI，再啟動任何可能更新 UI 的背景執行緒，避免啟動時競態錯誤。
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.create_widgets()

        threading.Thread(target=self._detect_hardware_encoder_background, daemon=True).start()
        threading.Thread(target=self._check_js_runtime_background, daemon=True).start()
        threading.Thread(target=self._print_startup_info, daemon=True).start()
        threading.Thread(
            target=self._cleanup_temp_artifacts_background,
            args=(self.download_folder.get(),),
            daemon=True,
        ).start()

    # ─────────────────────────────────────────────
    # UI 工具
    # ─────────────────────────────────────────────

    def safe_after(self, callback, *args, **kwargs):
        try:
            if self.is_closing:
                return
            if self.root.winfo_exists():
                self.root.after(0, lambda: self._safe_call(callback, *args, **kwargs))
        except Exception as e:
            print(f"safe_after 執行失敗: {e}")

    def _safe_call(self, callback, *args, **kwargs):
        try:
            callback(*args, **kwargs)
        except tk.TclError:
            pass
        except Exception as e:
            print(f"_safe_call 執行失敗: {e}")

    def create_widgets(self):
        tk.Label(self.root, text="影片/播放清單網址:").pack(pady=5)

        frame_url = tk.Frame(self.root)
        frame_url.pack(pady=5)

        self.url_entry = tk.Entry(frame_url, width=56)
        self.url_entry.pack(side=tk.LEFT, padx=5)
        self.url_entry.bind("<KeyRelease>", self.on_url_changed)

        tk.Button(frame_url, text="貼上", command=self.paste_url).pack(side=tk.LEFT)

        self.analyze_btn = tk.Button(self.root, text="1. 讀取內容", command=self.start_analyze)
        self.analyze_btn.pack(pady=5)

        ttk.Separator(self.root, orient="horizontal").pack(fill="x", pady=10)

        self.thumbnail_label = tk.Label(self.root, text="[預覽圖片將顯示於此]", bg="#f0f0f0", width=45, height=10)
        self.thumbnail_label.pack(pady=5)

        info_title = tk.Label(self.root, text="影片資訊:")
        info_title.pack(pady=(8, 2), anchor="w", padx=20)

        self.info_label = tk.Label(
            self.root,
            text="尚未讀取內容",
            justify="left",
            anchor="w",
            bg="#f7f7f7",
            relief="groove",
            padx=10,
            pady=8,
            width=78,
            wraplength=580,
        )
        self.info_label.pack(fill="x", padx=20)

        tk.Label(self.root, text="下載設定:").pack(pady=8)

        frame_check = tk.Frame(self.root)
        frame_check.pack()

        rb_mp4 = tk.Radiobutton(frame_check, text="MP4 (最高解析度來源轉 H.264)", variable=self.format_var, value="mp4", command=self.toggle_ui_state)
        rb_audio = tk.Radiobutton(frame_check, text="音訊", variable=self.format_var, value="audio", command=self.toggle_ui_state)
        rb_mp4.pack(side=tk.LEFT, padx=5)
        rb_audio.pack(side=tk.LEFT, padx=5)

        self.options_container = tk.Frame(self.root)
        self.options_container.pack(pady=2, fill="x")

        # 音訊格式選單
        self.audio_frame = tk.Frame(self.options_container)
        tk.Label(self.audio_frame, text="音訊格式:").pack(pady=2)
        self.audio_combobox = ttk.Combobox(
            self.audio_frame,
            state="readonly",
            width=50,
            textvariable=self.audio_format_var,
            values=["MP3 (320kbps)", "WAV (無損 PCM)", "ALAC (.m4a)"],
        )
        self.audio_combobox.pack(pady=4)
        self.audio_combobox.current(0)
        self.audio_combobox.bind("<<ComboboxSelected>>", self.on_audio_format_changed)

        # 音質說明標籤
        self.audio_quality_label = tk.Label(self.audio_frame, text="", fg="#555555", font=("Arial", 8))
        self.audio_quality_label.pack(pady=(0, 4))

        tk.Label(self.root, text="儲存位置:").pack(pady=5)

        frame_folder = tk.Frame(self.root)
        frame_folder.pack(pady=5)

        tk.Entry(frame_folder, textvariable=self.download_folder, width=42, state="readonly").pack(side=tk.LEFT, padx=5)
        tk.Button(frame_folder, text="瀏覽...", command=self.browse_folder).pack(side=tk.LEFT)

        # 主要操作按鈕列
        button_frame = tk.Frame(self.root)
        button_frame.pack(pady=10, fill=tk.X, padx=40)

        self.download_btn = tk.Button(
            button_frame,
            text="2. 開始下載",
            command=self.start_download,
            state=tk.DISABLED,
            bg="#4CAF50",
            fg="white",
            font=("Arial", 10, "bold"),
        )
        self.download_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 8))

        self.cancel_btn = tk.Button(
            button_frame,
            text="取消下載",
            command=self.request_cancel,
            state=tk.DISABLED,
            bg="#d9534f",
            fg="white",
            font=("Arial", 10, "bold"),
        )
        self.cancel_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=8)

        self.detail_btn = tk.Button(
            button_frame,
            text="查看紀錄",
            command=self.open_current_summary,
            state=tk.DISABLED,
            font=("Arial", 10, "bold"),
        )
        self.detail_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 0))

        # 工具列（版本檢查 + 編碼器資訊）
        tool_frame = tk.Frame(self.root)
        tool_frame.pack(pady=(0, 4), fill=tk.X, padx=40)

        self.version_btn = tk.Button(
            tool_frame,
            text="檢查 yt-dlp 版本",
            command=self.check_ytdlp_version,
            font=("Arial", 8),
            relief="flat",
            fg="#444444",
        )
        self.version_btn.pack(side=tk.LEFT)

        self.encoder_label = tk.Label(
            tool_frame,
            text="H.264 編碼器：偵測中...",
            font=("Arial", 8),
            fg="#888888",
        )
        self.encoder_label.pack(side=tk.RIGHT)

        self.progress_bar = ttk.Progressbar(self.root, orient="horizontal", length=500, mode="determinate")
        self.progress_bar.pack(pady=8)

        self.speed_label = tk.Label(self.root, text="", fg="blue")
        self.speed_label.pack(side=tk.BOTTOM)

        self.status_label = tk.Label(self.root, text="準備就緒", fg="gray")
        self.status_label.pack(side=tk.BOTTOM, pady=6)

        self.toggle_ui_state()

    def _cleanup_temp_artifacts_background(self, folder):
        """清理上次異常結束可能留下的本程式暫存轉檔檔案。"""
        try:
            folder_path = Path(folder)
            if not folder_path.exists():
                return
            now = time.time()
            removed = 0
            for p in folder_path.rglob("*"):
                if not p.is_file():
                    continue
                name = p.name.lower()
                if ".__h264__." not in name and ".__alac__." not in name:
                    continue
                # 避免刪到目前剛建立的檔案，只清理 30 分鐘以前的殘留暫存檔。
                try:
                    if now - p.stat().st_mtime < 1800:
                        continue
                    p.unlink(missing_ok=True)
                    removed += 1
                except Exception as e:
                    print(f"[系統] 清理暫存檔失敗：{p} ({e})")
            if removed:
                print(f"[系統] 已清理上次殘留暫存檔：{removed} 個")
        except Exception as e:
            print(f"[系統] 清理暫存檔時發生錯誤：{e}")

    # ─────────────────────────────────────────────
    # 硬體編碼器偵測
    # ─────────────────────────────────────────────

    def _detect_hardware_encoder_background(self):
        """背景偵測最佳可用的 H.264 硬體編碼器。"""
        ffmpeg_path = find_ffmpeg()
        if not ffmpeg_path:
            self.safe_after(self.encoder_label.config, text="H.264 編碼器：ffmpeg 未找到")
            return
        encoder = self._probe_best_encoder(ffmpeg_path)
        self.hardware_encoder = encoder
        display = {
            "h264_nvenc": "NVENC（NVIDIA GPU）",
            "h264_qsv":   "Quick Sync（Intel GPU）",
            "h264_amf":   "AMF（AMD GPU）",
            "libx264":    "libx264（CPU）",
        }.get(encoder, encoder)
        print(f"[系統] H.264 編碼器：{encoder}")
        self.safe_after(self.encoder_label.config, text=f"H.264 編碼器：{display}")

    # ─────────────────────────────────────────────
    # JS Runtime 檢查（yt-dlp YouTube 格式完整性）
    # ─────────────────────────────────────────────

    def _check_js_runtime_background(self):
        """
        背景偵測系統是否有 Node.js 或 Deno。
        Node.js 或 Deno 可協助 yt-dlp 處理部分 YouTube 新機制。
        未安裝時，一般下載通常仍可運作，但少數格式或限制情境可能受影響。
        """
        has_node = shutil.which("node") is not None
        has_deno = shutil.which("deno") is not None

        if has_node:
            runtime = "node"
        elif has_deno:
            runtime = "deno"
        else:
            runtime = None

        print(f"[系統] JS Runtime 偵測：{'找到 ' + runtime if runtime else '未找到（Node.js / Deno）'}")

        if not runtime:
            self.safe_after(self._warn_no_js_runtime)


    def _print_startup_info(self):
        """
        啟動時在 console 印出完整環境資訊摘要：
        ffmpeg / ffprobe 路徑與版本、yt-dlp 版本、下載資料夾磁碟可用空間。
        """
        sep = "=" * 52
        print(f"\n{sep}")
        print("[系統] 環境資訊摘要")
        print(sep)

        # ── Python 版本 ──
        print(f"[系統] Python        : {sys.version.split()[0]}")

        # ── yt-dlp 版本 ──
        try:
            import yt_dlp.version as ytv
            print(f"[系統] yt-dlp        : {ytv.__version__}")
        except Exception:
            print("[系統] yt-dlp        : 無法取得版本")

        # ── ffmpeg 路徑與版本 ──
        ffmpeg_path = find_ffmpeg()
        if ffmpeg_path:
            ver = self._get_ffmpeg_version(ffmpeg_path)
            print(f"[系統] ffmpeg        : {ver}  ({ffmpeg_path})")
        else:
            print("[系統] ffmpeg        : ✗ 找不到！轉檔功能將無法使用")

        # ── ffprobe 路徑 ──
        ffprobe_path = find_ffprobe(ffmpeg_path)
        if ffprobe_path:
            print(f"[系統] ffprobe       : ✓  ({ffprobe_path})")
        else:
            print("[系統] ffprobe       : ✗ 找不到！轉檔進度條將無法顯示")

        # ── 下載資料夾磁碟可用空間 ──
        folder = self.download_folder.get()
        try:
            usage = shutil.disk_usage(folder)
            free_gb  = usage.free  / (1024 ** 3)
            total_gb = usage.total / (1024 ** 3)
            print(f"[系統] 下載磁碟空間  : {free_gb:.1f} GB 可用 / {total_gb:.1f} GB 總計  ({folder})")
        except Exception as e:
            print(f"[系統] 下載磁碟空間  : 無法讀取 ({e})")

        print(sep + "\n")

    def _get_ffmpeg_version(self, ffmpeg_path: str) -> str:
        """回傳 ffmpeg 的版本字串，例如 '7.1.1' 或 '2025-12-28-git-xxxx'。"""
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                [ffmpeg_path, "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="ignore",
                timeout=5, creationflags=creationflags,
            )
            first_line = (result.stdout or result.stderr or "").splitlines()[0]
            match = re.search(r"version\s+(\S+)", first_line)
            return match.group(1) if match else first_line.strip()
        except Exception:
            return "無法取得版本"

    def _warn_no_js_runtime(self):
        """在 UI 上顯示 JS runtime 缺失警告。"""
        warning_text = (
            "未偵測到 JavaScript Runtime（Node.js 或 Deno）\n\n"
            "一般下載通常仍可運作；但 YouTube 若啟用部分新機制，\n"
            "少數格式、限制影片或高規格串流可能無法完整取得。\n\n"
            "若遇到格式不足或無法下載，可安裝 Node.js 後重新啟動程式。\n"
            "Node.js：https://nodejs.org/"
        )
        # 在狀態列顯示簡短提示
        self.status_label.config(
            text="未安裝 Node.js；一般可用，少數 YouTube 格式可能受影響（點此了解）",
            fg="orange",
            cursor="hand2",
        )
        self.status_label.bind("<Button-1>", lambda e: messagebox.showwarning("JS Runtime 警告", warning_text))
        print(f"[系統] 警告：{warning_text}")

    def _probe_best_encoder(self, ffmpeg_path: str) -> str:
        """嘗試以最小測試影格驗證硬體編碼器是否可用。"""
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        candidates = [
            "h264_nvenc",
            "h264_qsv",
            "h264_amf",
        ]
        for encoder in candidates:
            try:
                cmd = [
                    ffmpeg_path, "-hide_banner", "-y",
                    # testsrc2 產生真正的 YUV 原始幀，透過 -vf format=yuv420p 確保像素格式正確。
                    # 【修正】解析度改為 320x240：NVENC H.264 要求寬高各至少 145px，
                    # 128x128 會觸發 "Frame Dimension less than the minimum supported value"。
                    "-f", "lavfi",
                    "-i", "testsrc2=size=320x240:rate=25:duration=0.2",
                    "-vf", "format=yuv420p",
                    "-c:v", encoder,
                ]
                if encoder == "h264_nvenc":
                    # 明確指定第一張 NVIDIA GPU（獨顯）
                    cmd += ["-gpu", "0"]
                cmd += ["-frames:v", "5", "-f", "null", "-"]

                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=15,          # NVENC 初始化有時需要較長時間
                    creationflags=creationflags,
                )
                ok = result.returncode == 0
                print(f"[系統] 編碼器測試 {encoder}: {'✓ 可用' if ok else '✗ 不可用'}")
                if not ok and result.stderr:
                    err_lines = [l for l in result.stderr.strip().splitlines() if l.strip()]
                    print("         完整錯誤:")
                    for l in err_lines:
                        print(f"           {l}")
                if ok:
                    return encoder
            except Exception as e:
                print(f"[系統] 編碼器測試 {encoder}: ✗ 例外 ({e})")
                continue
        return "libx264"

    # ─────────────────────────────────────────────
    # yt-dlp 版本檢查
    # ─────────────────────────────────────────────

    def check_ytdlp_version(self):
        import yt_dlp.version as ytv
        current = ytv.__version__
        self.version_btn.config(state=tk.DISABLED, text="檢查中...")

        def do_check():
            try:
                resp = requests.get("https://pypi.org/pypi/yt-dlp/json", timeout=8)
                resp.raise_for_status()
                latest = resp.json()["info"]["version"]
                if current == latest:
                    msg = f"yt-dlp 版本：{current}\n\n已是最新版本 ✓"
                else:
                    msg = (
                        f"目前版本：{current}\n"
                        f"最新版本：{latest}\n\n"
                        f"建議更新以確保相容性！\n"
                        f"請在命令列執行：\n  pip install -U yt-dlp"
                    )
            except Exception as e:
                msg = f"目前版本：{current}\n（無法連線至 PyPI：{e}）"

            self.safe_after(self._show_version_result, msg)

        threading.Thread(target=do_check, daemon=True).start()

    def _show_version_result(self, msg: str):
        messagebox.showinfo("yt-dlp 版本資訊", msg)
        self.version_btn.config(state=tk.NORMAL, text="檢查 yt-dlp 版本")

    # ─────────────────────────────────────────────
    # URL / UI 狀態
    # ─────────────────────────────────────────────

    def paste_url(self):
        try:
            clipboard = self.root.clipboard_get().strip()
            self.url_entry.delete(0, tk.END)
            self.url_entry.insert(0, clipboard)
            self.on_url_changed()
        except Exception as e:
            print(f"貼上網址失敗: {e}")

    def on_url_changed(self, event=None):
        current_url = self.url_entry.get().strip()
        if self.analyzed_url and current_url != self.analyzed_url:
            self.analysis_valid = False
            if not self.is_downloading and not self.is_analyzing:
                self.download_btn.config(state=tk.DISABLED, text="2. 開始下載")
                self.status_label.config(text="網址已變更，請重新讀取內容")

    def toggle_ui_state(self):
        mode = self.format_var.get()
        if mode == "audio":
            if not self.audio_frame.winfo_ismapped():
                self.audio_frame.pack(pady=2)
            self.audio_combobox.config(state="readonly")
            self.on_audio_format_changed()
        else:
            if self.audio_frame.winfo_ismapped():
                self.audio_frame.pack_forget()

    def on_audio_format_changed(self, event=None):
        selected = self.get_selected_audio_format()
        descriptions = {
            "mp3":  "MP3 320kbps CBR — 高品質有損壓縮，檔案較小，相容性最佳",
            "wav":  "WAV PCM — 無損格式，完整保留原始音訊資料，檔案最大",
            # 說明：ALAC 是無損容器，但 YouTube 等平台的來源本身為有損格式（Opus/AAC），
            # 轉換後音質不會高於原始有損串流，請留意。
            "alac": "ALAC (.m4a) — 無損壓縮容器；注意：若來源為有損格式（如 YouTube），實際音質不會提升",
        }
        self.audio_quality_label.config(text=descriptions.get(selected, ""))

    def get_selected_audio_format(self):
        selected = (self.audio_format_var.get() or "").strip().lower()
        if selected.startswith("alac"):
            return "alac"
        if selected.startswith("wav"):
            return "wav"
        return "mp3"

    def browse_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.download_folder.set(folder)

    # ─────────────────────────────────────────────
    # 分析影片
    # ─────────────────────────────────────────────

    def start_analyze(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showerror("錯誤", "請輸入網址")
            return
        if self.is_analyzing or self.is_downloading:
            return

        self.is_analyzing = True
        self.analysis_valid = False
        self.video_info = {}
        self.expected_count = 0
        self.thumbnail_label.config(image="", text="讀取中...", width=45, height=10)
        self.info_label.config(text="讀取中...")
        self.analyze_btn.config(state=tk.DISABLED, text="讀取中...")
        self.status_label.config(text="正在分析...")
        self.download_btn.config(state=tk.DISABLED, text="2. 開始下載")
        self.progress_bar.config(mode="indeterminate", value=0)
        self.progress_bar.start(100)

        print(f"\n{'='*50}\n[系統] 開始讀取網址: {url}\n{'='*50}")

        threading.Thread(target=self.analyze_video, args=(url,), daemon=True).start()

    def analyze_video(self, url):
        ydl_opts_flat = {
            "quiet": True,
            "nocolor": True,
            "windowsfilenames": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts_flat) as ydl:
                info = ydl.extract_info(url, download=False)

            is_playlist = self.is_playlist_info(info)

            if is_playlist:
                entries = [e for e in list(info.get("entries") or []) if e]
                info["entries"] = entries
                count = len(entries)
                display_title = f"[清單] {info.get('title', '播放清單')} (共 {count} 部影片)"

                # 【修正】優先使用播放清單本身的縮圖，fallback 才用第一支影片
                first_video = entries[0] if count > 0 else {}
                thumb_url = info.get("thumbnail") or first_video.get("thumbnail")
            else:
                # 單一影片：重新完整讀取以取得 formats、width/height 等詳細資訊
                ydl_opts_full = {
                    "quiet": True,
                    "nocolor": True,
                    "windowsfilenames": True,
                    "skip_download": True,
                }
                with yt_dlp.YoutubeDL(ydl_opts_full) as ydl:
                    info = ydl.extract_info(url, download=False)
                display_title = info.get("title", "未命名")
                thumb_url = info.get("thumbnail")

            if thumb_url:
                self.load_thumbnail(thumb_url)

            summary_text = self.build_info_text(info)
            self.safe_after(
                self.update_ui_after_analyze,
                url, info, display_title, is_playlist, summary_text
            )

        except Exception as e:
            print(f"分析影片失敗: {e}")
            self.safe_after(messagebox.showerror, "錯誤", f"無法讀取: {str(e)}")
            self.safe_after(self.reset_ui)
        finally:
            self.safe_after(self.finish_analyze_ui)

    def finish_analyze_ui(self):
        self.is_analyzing = False
        self.progress_bar.stop()
        self.progress_bar.config(mode="determinate", value=0)
        self.analyze_btn.config(state=tk.NORMAL, text="1. 讀取內容")

    def is_playlist_info(self, info):
        return bool(isinstance(info, dict) and ("entries" in info) and (info.get("entries") is not None))

    def load_thumbnail(self, url):
        try:
            response = requests.get(
                url,
                timeout=10,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
                },
            )
            response.raise_for_status()
            img = Image.open(BytesIO(response.content))
            img.thumbnail((420, 300))
            self.safe_after(self._set_thumbnail_image, img)
        except Exception as e:
            print(f"縮圖載入失敗: {e}")

    def _set_thumbnail_image(self, img):
        self.tk_image = ImageTk.PhotoImage(img)
        self.thumbnail_label.config(image=self.tk_image, width=0, height=0, text="")

    # ─────────────────────────────────────────────
    # 影片資訊文字（含可用格式摘要）
    # ─────────────────────────────────────────────

    def _get_best_format_summary(self, info: dict) -> str:
        """從 formats 清單中提取最高可用畫質與音訊資訊。"""
        formats = info.get("formats") or []
        if not formats:
            return ""

        # 含畫面的串流
        video_streams = [
            f for f in formats
            if (f.get("vcodec") or "none").lower() not in ("none", "")
            and f.get("height")
        ]

        # 純音訊串流
        audio_streams = [
            f for f in formats
            if (f.get("acodec") or "none").lower() not in ("none", "")
            and (f.get("vcodec") or "none").lower() in ("none", "")
        ]

        codec_name_map_v = {
            "avc1": "H.264", "h264": "H.264",
            "vp09": "VP9",   "vp9":  "VP9",
            "av01": "AV1",   "av1":  "AV1",
            "hvc1": "H.265", "hevc": "H.265",
        }
        codec_name_map_a = {
            "opus":    "Opus",
            "mp4a":    "AAC",
            "vorbis":  "Vorbis",
            "mp3":     "MP3",
            "flac":    "FLAC",
        }

        parts = []

        if video_streams:
            best_v = max(video_streams, key=lambda x: (
                x.get("height", 0) or 0,
                x.get("fps", 0) or 0,
            ))
            height   = best_v.get("height", 0) or 0
            fps      = best_v.get("fps") or 0
            raw_vc   = (best_v.get("vcodec") or "").split(".")[0].lower()
            vc_label = codec_name_map_v.get(raw_vc, raw_vc.upper())

            res_tag = ""
            if height >= 2160:   res_tag = "（4K）"
            elif height >= 1440: res_tag = "（2K）"
            elif height >= 1080: res_tag = "（FHD）"

            res_text = f"{height}p{res_tag}"
            if fps and fps > 30:
                res_text += f" @ {int(fps)}fps"
            if vc_label:
                res_text += f" [{vc_label}]"

            parts.append(f"最高可用畫質：{res_text}")

        if audio_streams:
            best_a  = max(audio_streams, key=lambda x: x.get("abr", 0) or x.get("tbr", 0) or 0)
            raw_ac  = (best_a.get("acodec") or "").split(".")[0].lower()
            ac_label = codec_name_map_a.get(raw_ac, raw_ac.upper())
            abr      = best_a.get("abr") or best_a.get("tbr")

            audio_text = ac_label or "未知"
            if abr:
                audio_text += f" {abr:.0f}kbps"
            parts.append(f"最佳音訊串流：{audio_text}")

        return "\n".join(parts) if parts else ""

    def build_info_text(self, info: dict) -> str:
        is_playlist = self.is_playlist_info(info)
        if is_playlist:
            entries  = [e for e in list(info.get("entries") or []) if e]
            uploader = info.get("uploader") or info.get("channel") or "未知"
            title    = info.get("title", "播放清單")
            return (
                f"標題：{title}\n"
                f"類型：播放清單\n"
                f"建立者／頻道：{uploader}\n"
                f"影片數量：{len(entries)} 部"
            )

        title        = info.get("title", "未知")
        uploader     = info.get("uploader") or info.get("channel") or "未知"
        duration     = info.get("duration")
        duration_text = format_seconds(duration) if duration else "未知"
        view_count   = format_count(info.get("view_count"))
        upload_date  = info.get("upload_date") or ""
        if len(upload_date) == 8 and upload_date.isdigit():
            upload_date_text = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
        else:
            upload_date_text = upload_date or "未知"

        width  = info.get("width")  or "未知"
        height = info.get("height") or "未知"

        base_text = (
            f"標題：{title}\n"
            f"類型：單一影片\n"
            f"頻道：{uploader}\n"
            f"長度：{duration_text}\n"
            f"解析度：{width} x {height}\n"
            f"觀看次數：{view_count}\n"
            f"上傳日期：{upload_date_text}"
        )

        # 加入可用格式摘要（第 9 點）
        fmt_summary = self._get_best_format_summary(info)
        if fmt_summary:
            return base_text + "\n" + fmt_summary
        return base_text

    def update_ui_after_analyze(self, url, info, title, is_playlist, summary_text):
        self.video_info   = info
        self.analysis_valid = True
        self.analyzed_url  = url
        self.expected_count = len(info.get("entries") or []) if is_playlist else 1
        self.info_label.config(text=summary_text)
        self.toggle_ui_state()
        self.download_btn.config(state=tk.NORMAL, text="2. 開始下載")

        short_title = title[:44] + "..." if len(title) > 44 else title
        self.status_label.config(text=f"讀取完成：{short_title}")
        print(f"[系統] 讀取完成！標題: {title}")

    def reset_ui(self):
        self.analysis_valid  = False
        self.is_analyzing    = False
        self.is_downloading  = False
        self.cancel_event.clear()
        self.current_download_mode = None
        self.current_process = None
        self.current_summary = None
        self.download_item_titles      = {}
        self.download_item_states      = {}
        self.download_item_file_map    = {}
        self.download_file_key_map     = {}
        self.expected_item_keys        = []
        self.unknown_key_counter       = 0
        self.download_thread           = None
        self.download_started_at       = 0.0
        self.download_output_files     = []
        self.postprocessed_output_files = []
        self.video_info                = {}
        self.expected_count            = 0
        self.analyzed_url              = ""
        self.progress_bar.stop()
        self.progress_bar.config(mode="determinate", value=0)
        self.analyze_btn.config(state=tk.NORMAL, text="1. 讀取內容")
        self.download_btn.config(state=tk.DISABLED, text="2. 開始下載")
        self.cancel_btn.config(state=tk.DISABLED)
        self.detail_btn.config(state=tk.DISABLED)
        self.status_label.config(text="準備就緒", fg="gray", cursor="")
        self.status_label.unbind("<Button-1>")
        self.speed_label.config(text="")
        self.info_label.config(text="尚未讀取內容")
        self.thumbnail_label.config(image="", text="[預覽圖片將顯示於此]", width=45, height=10)
        self.toggle_ui_state()

    # ─────────────────────────────────────────────
    # 下載控制
    # ─────────────────────────────────────────────

    def start_download(self):
        mode        = self.format_var.get()
        url         = self.url_entry.get().strip()
        path        = self.download_folder.get().strip()
        is_playlist = self.is_playlist_info(self.video_info)

        # Tkinter 變數只在主執行緒讀取；背景下載執行緒只接收普通字串參數。
        selected_audio = self.get_selected_audio_format() if mode == "audio" else None

        if not url:
            messagebox.showerror("錯誤", "請輸入網址"); return
        if not path:
            messagebox.showerror("錯誤", "請選擇儲存位置"); return
        if not self.analysis_valid or not self.video_info:
            messagebox.showerror("錯誤", "請先讀取內容"); return
        if url != self.analyzed_url:
            self.analysis_valid = False
            self.download_btn.config(state=tk.DISABLED)
            messagebox.showwarning("提醒", "網址已變更，請重新讀取內容後再下載")
            self.status_label.config(text="網址已變更，請重新讀取內容")
            return
        if self.is_downloading:
            return

        # 每次下載前也針對實際目標資料夾清理舊轉檔暫存檔；清理規則只處理 30 分鐘以前的本程式暫存檔。
        threading.Thread(
            target=self._cleanup_temp_artifacts_background,
            args=(path,),
            daemon=True,
        ).start()

        self.is_downloading = True
        self.cancel_event.clear()
        self.current_download_mode = mode
        self.current_summary       = None
        self.ytdlp_errors          = []
        self.ytdlp_warnings        = []
        self.download_started_at   = time.time()
        self.download_output_files = []
        self.postprocessed_output_files = []

        self.prepare_download_tracking()

        self.download_btn.config(state=tk.DISABLED, text="下載中...")
        self.cancel_btn.config(state=tk.NORMAL)
        self.detail_btn.config(state=tk.DISABLED)
        self.analyze_btn.config(state=tk.DISABLED)
        # 確保 progress bar 完全重置再開始（避免視覺閃爍）
        self.progress_bar.stop()
        self.progress_bar.config(mode="determinate", value=0)
        self.speed_label.config(text="")
        self.status_label.config(text="準備下載...")

        print(f"\n{'='*50}\n[系統] 開始執行下載任務\n{'='*50}")

        self.download_thread = threading.Thread(
            target=self.download_video,
            args=(url, path, mode, is_playlist, selected_audio, self.download_started_at),
            daemon=True,
        )
        self.download_thread.start()

    def request_cancel(self):
        if not self.is_downloading:
            return
        self.cancel_event.set()
        self.cancel_btn.config(state=tk.DISABLED)
        self.status_label.config(text="正在取消，可能需要等待目前的網路請求或轉檔步驟結束...")
        self.speed_label.config(text="")
        self._terminate_current_process("取消程序")

    def open_current_summary(self):
        if self.current_summary:
            self.show_result_detail_window(self.current_summary)
        else:
            messagebox.showinfo("提示", "目前沒有可查看的下載紀錄。")

    # ─────────────────────────────────────────────
    # yt-dlp match_filter（取消的第三道保障）
    # ─────────────────────────────────────────────

    def _match_filter(self, info_dict, *, incomplete):
        """
        在 yt-dlp 開始下載每個項目前呼叫。
        配合 progress_hook 和 YTDLPLogger.debug 形成三層取消保障。
        """
        if self.cancel_event.is_set():
            raise DownloadCancelled("使用者已取消")
        return None  # None = 允許下載

    # ─────────────────────────────────────────────
    # 追蹤系統
    # ─────────────────────────────────────────────

    def prepare_download_tracking(self):
        self.download_item_titles      = {}
        self.download_item_states      = {}
        self.download_item_file_map    = {}
        self.download_file_key_map     = {}
        self.expected_item_keys        = []
        self.unknown_key_counter       = 0
        self.download_output_files     = []
        self.postprocessed_output_files = []

        if self.is_playlist_info(self.video_info):
            entries = [e for e in list(self.video_info.get("entries") or []) if e]
            for idx, entry in enumerate(entries, start=1):
                key = self.build_download_item_key(entry, fallback_hint=f"playlist_{idx}")
                self.expected_item_keys.append(key)
                self.download_item_titles[key] = {
                    "title":          entry.get("title") or f"未命名項目 {idx}",
                    "playlist_index": entry.get("playlist_index") or idx,
                    "id":             entry.get("id"),
                }
                self.download_item_states[key] = "pending"
        elif isinstance(self.video_info, dict) and self.video_info:
            key = self.build_download_item_key(self.video_info, fallback_hint="single")
            self.expected_item_keys.append(key)
            self.download_item_titles[key] = {
                "title":          self.video_info.get("title") or "未命名",
                "playlist_index": self.video_info.get("playlist_index"),
                "id":             self.video_info.get("id"),
            }
            self.download_item_states[key] = "pending"

    def generate_fallback_item_key(self, hint="item"):
        self.unknown_key_counter += 1
        safe_hint = re.sub(r"\s+", "_", str(hint or "item")).strip("_") or "item"
        return f"fallback::{safe_hint}::{self.unknown_key_counter}"

    def build_download_item_key(self, info, fallback_hint="") -> str:
        """
        【簡化】以 extractor:video_id 作為主要唯一鍵，
        確保分析階段與下載階段能可靠地對應同一項目。
        """
        if not isinstance(info, dict):
            return self.generate_fallback_item_key(fallback_hint or "item")

        video_id  = str(info.get("id") or "").strip()
        extractor = str(
            info.get("extractor_key") or info.get("ie_key") or info.get("extractor") or ""
        ).strip().lower()

        # 最可靠：extractor + id 組合
        if video_id and extractor:
            return f"{extractor}:{video_id}"
        # 次可靠：僅 id
        if video_id:
            return f"id:{video_id}"
        # 最後手段：網址
        webpage_url = str(
            info.get("webpage_url") or info.get("original_url") or info.get("url") or ""
        ).strip()
        if webpage_url:
            return f"url:{webpage_url}"

        return self.generate_fallback_item_key(fallback_hint or "item")

    def ensure_item_tracking(self, info, file_path=None):
        fallback_hint = None
        if isinstance(info, dict):
            fallback_hint = info.get("title") or info.get("playlist_index") or info.get("id") or "item"
        key = self.build_download_item_key(info, fallback_hint=fallback_hint or "item")
        self.download_item_titles.setdefault(
            key,
            {
                "title":          (info or {}).get("title")          if isinstance(info, dict) else "未命名",
                "playlist_index": (info or {}).get("playlist_index") if isinstance(info, dict) else None,
                "id":             (info or {}).get("id")             if isinstance(info, dict) else None,
            },
        )
        self.download_item_states.setdefault(key, "pending")
        if file_path:
            self.register_file_for_key(key, file_path)
        return key

    def update_download_item_state(self, key, new_state):
        priority = {
            "pending": 0, "downloading": 1, "downloaded": 2,
            "skipped": 3, "success": 4,
            "failed_download": 4, "failed_conversion": 5,
        }
        current = self.download_item_states.get(key, "pending")
        if priority.get(new_state, 0) >= priority.get(current, 0):
            self.download_item_states[key] = new_state

    def register_file_for_key(self, key, file_path):
        resolved = self.normalize_file_path(file_path)
        if not resolved:
            return
        # 保留 key -> 最新檔案，也建立 file -> key 反查表。
        # 反查表不覆蓋舊路徑，因此下載前檔案、postprocessor 後檔案、轉檔後檔案都能對回同一項。
        self.download_item_file_map[key] = resolved
        self.download_file_key_map[resolved] = key

    def find_item_key_by_file(self, file_path) -> str | None:
        """
        優先查詢 file -> key 反查表。
        v1.1 起同一個項目可能有多個路徑：原始下載檔、postprocessor 後檔、H.264/ALAC 轉檔後檔。
        """
        resolved = self.normalize_file_path(file_path)
        if not resolved:
            return None

        key = self.download_file_key_map.get(resolved)
        if key:
            return key

        for item_key, mapped in self.download_item_file_map.items():
            if mapped == resolved:
                return item_key
        return None

    def mark_item_result_from_file(self, file_path, status, new_file_path=None, title_hint=None):
        key = self.find_item_key_by_file(file_path)
        if not key:
            fallback_info = {"title": title_hint or Path(file_path).stem}
            key = self.ensure_item_tracking(fallback_info, file_path=file_path)
        self.update_download_item_state(key, status)
        if new_file_path:
            self.register_file_for_key(key, new_file_path)
        return key

    def mark_output_files_success(self, files):
        for file_path in files or []:
            self.mark_item_result_from_file(file_path, "success", new_file_path=file_path)

    def finalize_item_states(self, mode, selected_audio, final_files, error_code=0):
        self.mark_output_files_success(final_files)
        if error_code in (0, None):
            success_like_count  = len([k for k, v in self.download_item_states.items() if v in {"success", "skipped"}])
            unmatched_downloaded = [k for k, v in self.download_item_states.items() if v == "downloaded"]
            missing_success_slots = max(0, len(final_files) - success_like_count)
            for key in unmatched_downloaded[:missing_success_slots]:
                self.download_item_states[key] = "success"

    def summarize_item_states(self, expected_count, error_code=0):
        ordered_keys = []
        for key in self.expected_item_keys + list(self.download_item_titles.keys()):
            if key not in ordered_keys:
                ordered_keys.append(key)

        success_states = {"success", "skipped"}
        failed_states  = {"failed_download", "failed_conversion"}
        success_items, failed_items = [], []

        for key in ordered_keys:
            meta  = self.download_item_titles.get(key, {"title": "未命名"})
            state = self.download_item_states.get(key, "pending")
            item  = {
                "key": key, "state": state,
                "title":          meta.get("title") or "未命名",
                "playlist_index": meta.get("playlist_index"),
                "id":             meta.get("id"),
            }
            if state in success_states:
                success_items.append(item)
            elif state in failed_states:
                failed_items.append(item)

        success_count = len(success_items)
        failed_count  = len(failed_items)
        unknown_count = max(0, expected_count - success_count - failed_count)
        if error_code not in (0, None) and unknown_count > 0:
            failed_count  += unknown_count
            unknown_count  = 0

        return success_count, failed_count, success_items, failed_items, unknown_count

    # ─────────────────────────────────────────────
    # 下載進度回呼
    # ─────────────────────────────────────────────

    def progress_hook(self, d):
        if self.cancel_event.is_set():
            raise DownloadCancelled("使用者已取消")

        status   = d.get("status")
        info     = d.get("info_dict") or {}
        filename = d.get("filename") or d.get("tmpfilename") or info.get("_filename")
        key      = self.ensure_item_tracking(info, file_path=filename if filename and status == "finished" else None)
        total_items   = max(1, self.expected_count or info.get("playlist_count") or
                           info.get("n_entries") or len(self.expected_item_keys) or 1)
        current_index = max(1, min(total_items, int(info.get("playlist_index") or 1)))

        if status == "downloading":
            self.update_download_item_state(key, "downloading")
            try:
                raw_p        = d.get("_percent_str", "0%")
                match        = re.search(r"(\d+\.?\d*)", str(raw_p))
                file_percent = float(match.group(1)) if match else 0.0
                overall_percent = ((current_index - 1) + (file_percent / 100.0)) / total_items * 100.0
                self.safe_after(self.progress_bar.config, value=overall_percent)

                status_text = (
                    f"下載中... ({current_index}/{total_items}) 單檔 {file_percent:.1f}%"
                    if total_items > 1
                    else f"下載中... {file_percent:.1f}%"
                )
                self.safe_after(self.status_label.config, text=status_text)

                speed = re.sub(r"\x1b\[[0-9;]*m", "", str(d.get("_speed_str", "N/A")))
                eta   = re.sub(r"\x1b\[[0-9;]*m", "", str(d.get("_eta_str",   "N/A")))
                self.safe_after(self.speed_label.config, text=f"速度: {speed} | 剩餘: {eta}")

                sys.stdout.write(
                    f"\r[yt-dlp 下載] 第 {current_index}/{total_items} 項"
                    f" - 進度: {file_percent:.1f}% | 速度: {speed} | 剩餘: {eta}          "
                )
                sys.stdout.flush()
            except Exception as e:
                print(f"進度條錯誤 (已忽略): {e}")

        elif status == "finished":
            self.register_completed_download(info, filename)
            for file_path in self.extract_hook_file_paths(d):
                self.remember_output_file(info, file_path, postprocessed=False)

            finished_percent = current_index / total_items * 100.0
            self.safe_after(self.progress_bar.config, value=finished_percent)
            text = (
                f"第 {current_index}/{total_items} 項下載完成，準備轉成 H.264..."
                if self.current_download_mode == "mp4"
                else f"第 {current_index}/{total_items} 項下載完成，正在處理音訊..."
            )
            self.safe_after(self.status_label.config, text=text)
            print(f"\n[yt-dlp 完成] 第 {current_index}/{total_items} 項 下載完畢！")

        elif status == "error":
            self.update_download_item_state(key, "failed_download")
            self.safe_after(self.status_label.config,
                            text=f"第 {current_index}/{total_items} 項下載失敗，繼續下一項...")
            print(f"\n[yt-dlp 錯誤] 第 {current_index}/{total_items} 項 下載失敗！")

    def register_completed_download(self, info, file_path=None):
        key = self.ensure_item_tracking(info, file_path=file_path)
        self.download_item_titles[key] = {
            "title":          info.get("title")          or self.download_item_titles.get(key, {}).get("title")          or "未命名",
            "playlist_index": info.get("playlist_index") or self.download_item_titles.get(key, {}).get("playlist_index"),
            "id":             info.get("id")             or self.download_item_titles.get(key, {}).get("id"),
        }
        self.update_download_item_state(key, "downloaded")
        if file_path:
            self.remember_output_file(info, file_path, postprocessed=False)
        return key

    def normalize_file_path(self, file_path) -> str | None:
        if not file_path:
            return None
        try:
            return str(Path(file_path).resolve())
        except Exception:
            return str(file_path)

    def is_ignored_download_artifact(self, file_path) -> bool:
        if not file_path:
            return True
        path = Path(file_path)
        suffix = path.suffix.lower()
        if suffix in {".part", ".ytdl", ".temp", ".tmp", ".download", ".jpg", ".jpeg", ".webp", ".png", ".json", ".description"}:
            return True
        name = path.name.lower()
        if ".__h264__." in name or ".__alac__." in name:
            return True
        return False

    def remember_output_file(self, info, file_path, postprocessed=False):
        resolved = self.normalize_file_path(file_path)
        if not resolved or self.is_ignored_download_artifact(resolved):
            return None

        path = Path(resolved)
        # yt-dlp hook 有時會給出尚未建立或已被移動的中間檔，這類檔案不列入最終候選。
        if not path.exists():
            return None

        key = self.ensure_item_tracking(info if isinstance(info, dict) else {}, file_path=resolved)
        bucket = self.postprocessed_output_files if postprocessed else self.download_output_files
        if resolved not in bucket:
            bucket.append(resolved)
        return key

    def extract_hook_file_paths(self, hook_data, include_requested=True) -> list:
        paths = []

        def add(value):
            if not value:
                return
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    add(item)
                return
            if isinstance(value, dict):
                for sub_key in ("filepath", "filename", "_filename", "path"):
                    add(value.get(sub_key))
                return
            value = str(value)
            if value and value not in paths:
                paths.append(value)

        if not isinstance(hook_data, dict):
            return paths

        for key in ("filepath", "filename", "_filename", "tmpfilename"):
            add(hook_data.get(key))

        info = hook_data.get("info_dict") or {}
        if isinstance(info, dict):
            for key in ("filepath", "filename", "_filename", "requested_filename"):
                add(info.get(key))

            if include_requested:
                for item in (info.get("requested_downloads") or []):
                    add(item)

            # __files_to_move 是 yt-dlp 內部欄位，不保證永久穩定；讀取失敗時只退回其他公開欄位。
            try:
                files_to_move = info.get("__files_to_move")
                if isinstance(files_to_move, dict):
                    # yt-dlp 內部常用 old -> new 的檔案搬移表；新舊都記下，再由 exists / 副檔名過濾。
                    for old_path, new_path in files_to_move.items():
                        add(old_path)
                        add(new_path)
            except Exception as e:
                print(f"[yt-dlp 追蹤] __files_to_move 讀取失敗，改用其他檔案欄位：{e}")

        return paths

    def postprocessor_hook(self, d):
        if self.cancel_event.is_set():
            raise DownloadCancelled("使用者已取消")

        status = d.get("status")
        if status != "finished":
            return

        info = d.get("info_dict") or {}
        for file_path in self.extract_hook_file_paths(d, include_requested=False):
            self.remember_output_file(info, file_path, postprocessed=True)

        key = self.ensure_item_tracking(info if isinstance(info, dict) else {})
        self.update_download_item_state(key, "downloaded")

    def get_media_exts(self, mode, allowed_exts=None):
        if allowed_exts is not None:
            return {str(ext).lower() for ext in allowed_exts}
        if mode == "audio":
            return {".mp3", ".wav", ".flac", ".m4a", ".aac", ".opus", ".ogg", ".webm", ".mka"}
        return {".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi", ".m4v"}

    def dedupe_existing_paths(self, files, folder=None, mode="mp4", allowed_exts=None):
        valid_exts = self.get_media_exts(mode, allowed_exts)
        folder_path = Path(folder).resolve() if folder else None
        result, seen = [], set()

        for file_path in files or []:
            resolved = self.normalize_file_path(file_path)
            if not resolved or resolved in seen:
                continue

            p = Path(resolved)
            if self.is_ignored_download_artifact(p):
                continue
            if p.suffix.lower() not in valid_exts:
                continue
            if not p.exists() or not p.is_file():
                continue

            if folder_path is not None:
                try:
                    p.resolve().relative_to(folder_path)
                except Exception:
                    continue

            seen.add(resolved)
            result.append(p)

        result.sort(key=lambda x: x.stat().st_mtime)
        return result

    def get_tracked_media_files(self, folder, mode, allowed_exts=None, include_raw=True):
        # postprocessor 後檔案最接近「使用者最後看到的輸出」，所以優先採用。
        tracked = self.dedupe_existing_paths(
            self.postprocessed_output_files,
            folder=folder,
            mode=mode,
            allowed_exts=allowed_exts,
        )
        if tracked or not include_raw:
            return tracked

        return self.dedupe_existing_paths(
            self.download_output_files,
            folder=folder,
            mode=mode,
            allowed_exts=allowed_exts,
        )

    def get_recent_media_files(self, folder, mode, allowed_exts=None, since=0.0):
        """
        v1.1 fallback：不再遞迴掃描整個 Downloads。
        只檢查輸出資料夾當層，且只收集本次任務開始後附近有更新的媒體檔。
        """
        folder_path = Path(folder)
        if not folder_path.exists():
            return []

        valid_exts = self.get_media_exts(mode, allowed_exts)
        since = float(since or 0.0) - 5.0
        candidates = []

        for p in folder_path.glob("*"):
            if not p.is_file():
                continue
            if self.is_ignored_download_artifact(p):
                continue
            if p.suffix.lower() not in valid_exts:
                continue
            try:
                if p.stat().st_mtime < since:
                    continue
            except Exception:
                pass
            candidates.append(p)

        candidates.sort(key=lambda x: x.stat().st_mtime)
        return candidates

    # ─────────────────────────────────────────────
    # 檔案工具
    # ─────────────────────────────────────────────

    def make_unique_path(self, path_like):
        path    = Path(path_like)
        if not path.exists():
            return path
        counter = 1
        while True:
            candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
            if not candidate.exists():
                return candidate
            counter += 1

    def finalize_transcoded_file(self, src_file, temp_file, dst_file):
        """
        將轉檔完成的暫存檔移到最終位置。
        優先使用 os.replace 做同磁碟原子替換，失敗時才以 shutil.move 後備。
        """
        src_file  = Path(src_file)
        temp_file = Path(temp_file)
        dst_file  = Path(dst_file)
        try:
            try:
                os.replace(str(temp_file), str(dst_file))
            except OSError:
                # 若未來改成不同磁碟的暫存位置，os.replace 可能失敗，改用 shutil.move 後備。
                shutil.move(str(temp_file), str(dst_file))
        except Exception:
            try:
                temp_file.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        if src_file.exists() and src_file.resolve() != dst_file.resolve():
            try:
                src_file.unlink(missing_ok=True)
            except Exception as e:
                print(f"刪除原始檔案失敗: {e}")

    def build_ffmpeg_error_message(self, prefix, log_buffer):
        meaningful = []
        for line in log_buffer:
            line = str(line).strip()
            if not line or line.startswith(("frame=", "fps=", "bitrate=", "speed=",
                                             "progress=", "out_time", "total_size=")):
                continue
            meaningful.append(line)
        tail = " | ".join(meaningful[-6:]).strip()
        return f"{prefix}：{tail}" if tail else prefix

    def collect_existing_files(self, folder):
        folder_path = Path(folder)
        if not folder_path.exists():
            return set()
        return {str(p.resolve()) for p in folder_path.rglob("*") if p.is_file()}

    def get_new_media_files(self, folder, before_files, mode, allowed_exts=None):
        """
        舊版相容用 fallback。
        v1.1 起主要依靠 yt-dlp hook 追蹤實際輸出檔；此函式不再遞迴掃描整個資料夾。
        """
        folder_path = Path(folder)
        if not folder_path.exists():
            return []

        valid_exts = self.get_media_exts(mode, allowed_exts)
        before_files = set(before_files or [])
        new_files = []

        for p in folder_path.glob("*"):
            if not p.is_file():
                continue
            if self.is_ignored_download_artifact(p):
                continue
            if p.suffix.lower() not in valid_exts:
                continue
            if str(p.resolve()) not in before_files:
                new_files.append(p)

        new_files.sort(key=lambda x: x.stat().st_mtime)
        return new_files

    # ─────────────────────────────────────────────
    # ffprobe 工具
    # ─────────────────────────────────────────────

    def _run_ffprobe(self, args: list) -> dict | None:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                args,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="ignore",
                creationflags=creationflags, check=False,
            )
            if result.returncode != 0:
                return None
            return json.loads(result.stdout or "{}")
        except Exception as e:
            print(f"ffprobe 執行失敗: {e}")
            return None

    def get_media_duration(self, src_file, ffmpeg_path) -> float | None:
        ffprobe_path = find_ffprobe(ffmpeg_path)
        if not ffprobe_path:
            return None
        data = self._run_ffprobe([
            ffprobe_path, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", str(src_file),
        ])
        if data is None:
            return None
        duration = float(data.get("format", {}).get("duration", 0) or 0)
        return duration if duration > 0 else None

    def get_media_stream_info(self, src_file, ffmpeg_path) -> dict:
        ffprobe_path = find_ffprobe(ffmpeg_path)
        if not ffprobe_path:
            return {}
        data = self._run_ffprobe([
            ffprobe_path, "-v", "error",
            "-show_entries", "stream=codec_type,codec_name",
            "-of", "json", str(src_file),
        ])
        if data is None:
            return {}
        info = {}
        for stream in (data.get("streams") or []):
            ct = stream.get("codec_type")
            if ct == "video" and "video_codec" not in info:
                info["video_codec"] = stream.get("codec_name")
            elif ct == "audio" and "audio_codec" not in info:
                info["audio_codec"] = stream.get("codec_name")
        return info

    def get_audio_stream_details(self, src_file, ffmpeg_path) -> dict:
        ffprobe_path = find_ffprobe(ffmpeg_path)
        if not ffprobe_path:
            return {}
        data = self._run_ffprobe([
            ffprobe_path, "-v", "error",
            "-select_streams", "a:0",
            "-show_entries",
            "stream=codec_name,codec_long_name,sample_rate,channels,"
            "bits_per_raw_sample,bits_per_sample,sample_fmt,bit_rate",
            "-of", "json", str(src_file),
        ])
        if data is None:
            return {}
        streams = data.get("streams") or []
        return streams[0] if streams else {}

    def infer_bit_depth(self, stream_info) -> int | None:
        for key in ("bits_per_raw_sample", "bits_per_sample"):
            value = stream_info.get(key)
            if value not in (None, "", "0", 0):
                try:
                    return int(value)
                except Exception:
                    pass
        sample_fmt = str(stream_info.get("sample_fmt") or "").lower()
        match = re.search(r"(\d+)", sample_fmt)
        if match:
            try:
                return int(match.group(1))
            except Exception:
                pass
        return None

    def format_audio_profile(self, stream_info) -> str:
        if not stream_info:
            return "未知"
        codec_name  = (stream_info.get("codec_name") or "unknown").upper()
        sample_rate = stream_info.get("sample_rate")
        channels    = stream_info.get("channels")
        bit_depth   = self.infer_bit_depth(stream_info)
        parts       = [codec_name]
        try:
            if sample_rate:
                parts.append(f"{int(sample_rate) / 1000:.1f} kHz")
        except Exception:
            pass
        if channels:
            try:
                parts.append(f"{int(channels)} 聲道")
            except Exception:
                pass
        if bit_depth:
            parts.append(f"{bit_depth}-bit")
        return " / ".join(parts)

    # ─────────────────────────────────────────────
    # 進度 UI 更新
    # ─────────────────────────────────────────────

    def update_encode_progress_ui(self, file_name, elapsed, index, total,
                                   total_duration=None, current_time=None, task_text="H.264"):
        elapsed_text = format_seconds(elapsed)
        if total_duration and current_time is not None:
            file_percent = max(0.0, min(100.0, (current_time / total_duration) * 100))
        else:
            file_percent = 0.0

        if total > 0:
            overall_percent = (
                ((index - 1) + (file_percent / 100.0)) / total * 100.0
                if (total_duration and current_time is not None)
                else ((index - 1) / total) * 100.0
            )
        else:
            overall_percent = file_percent

        self.progress_bar.config(mode="determinate", value=overall_percent)

        if total_duration and current_time is not None:
            self.status_label.config(text=f"正在轉成 {task_text}... ({index}/{total}) 單檔 {file_percent:.1f}%")
            self.speed_label.config(text=f"檔案: {file_name} | 編碼時間: {elapsed_text} | 總進度: {overall_percent:.1f}%")
            sys.stdout.write(
                f"\r[FFmpeg {task_text}] {file_name} ({index}/{total})"
                f" - {file_percent:.1f}% | 經過: {elapsed_text}          "
            )
        else:
            self.status_label.config(text=f"正在轉成 {task_text}... ({index}/{total})")
            self.speed_label.config(text=f"檔案: {file_name} | 編碼時間: {elapsed_text}")
            sys.stdout.write(
                f"\r[FFmpeg {task_text}] {file_name} ({index}/{total}) - 經過: {elapsed_text}          "
            )
        sys.stdout.flush()

    def start_indeterminate_progress(self):
        self.progress_bar.config(mode="indeterminate")
        self.progress_bar.start(120)

    def stop_indeterminate_progress(self):
        self.progress_bar.stop()
        self.progress_bar.config(mode="determinate")

    # ─────────────────────────────────────────────
    # FFmpeg 通用執行
    # ─────────────────────────────────────────────

    def _run_ffmpeg_with_progress(self, cmd, src_file, index, total,
                                   total_duration, task_text, creationflags):
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="ignore",
            bufsize=1, creationflags=creationflags,
        )
        self.current_process = process

        start_time    = time.time()
        last_out_time = 0.0
        last_update   = 0.0
        log_buffer    = deque(maxlen=60)

        if total_duration is None:
            self.safe_after(self.start_indeterminate_progress)

        try:
            if process.stdout:
                for raw_line in process.stdout:
                    if self.cancel_event.is_set():
                        try:
                            process.terminate()
                        except Exception:
                            pass
                        raise DownloadCancelled("使用者已取消")

                    line = (raw_line or "").strip()
                    if not line:
                        continue
                    log_buffer.append(line)

                    key_part, _, val_part = line.partition("=")
                    if key_part in {"out_time_ms", "out_time_us"}:
                        try:
                            last_out_time = float(val_part) / 1_000_000.0
                        except Exception:
                            pass
                    elif key_part == "progress":
                        now = time.time()
                        if (now - last_update) >= 0.2 or val_part == "end":
                            self.safe_after(
                                self.update_encode_progress_ui,
                                src_file.name, now - start_time,
                                index, total, total_duration, last_out_time, task_text,
                            )
                            last_update = now

            return_code = process.wait()
        finally:
            self.current_process = None
            if total_duration is None:
                self.safe_after(self.stop_indeterminate_progress)

        return return_code, log_buffer, start_time, last_out_time

    # ─────────────────────────────────────────────
    # FFmpeg 轉檔：H.264（支援硬體加速）
    # ─────────────────────────────────────────────

    def _build_h264_encode_args(self) -> list:
        """
        根據偵測到的硬體編碼器，回傳 ffmpeg 編碼參數。
        注意：NVENC / QSV / AMF 的品質數值不等同於 libx264 CRF，
        這裡採用的是偏向穩定與一般觀看品質的平衡設定。
        """
        encoder = self.hardware_encoder
        if encoder == "h264_nvenc":
            return [
                "-c:v", "h264_nvenc",
                "-preset", "p4",      # NVENC balanced quality/speed preset
                "-cq", "22",          # Constant quality mode
            ]
        elif encoder == "h264_qsv":
            return [
                "-c:v", "h264_qsv",
                "-global_quality", "22",
                "-look_ahead", "1",
            ]
        elif encoder == "h264_amf":
            return [
                "-c:v", "h264_amf",
                "-quality", "balanced",
                "-qp_i", "22",
                "-qp_p", "22",
                "-qp_b", "22",
            ]
        else:
            # libx264 軟體編碼：fast preset + CRF 22
            return [
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "22",
                "-threads", "0",
            ]

    def convert_file_to_h264(self, src_file, ffmpeg_path, index, total):
        src_file    = Path(src_file)
        stream_info = self.get_media_stream_info(src_file, ffmpeg_path)
        video_codec = (stream_info.get("video_codec") or "").lower()
        audio_codec = (stream_info.get("audio_codec") or "").lower()

        if video_codec == "h264" and src_file.suffix.lower() == ".mp4":
            self.safe_after(self.status_label.config,
                            text=f"已是 H.264，跳過轉檔 ({index}/{total}) {src_file.name}")
            return src_file, "skipped"

        dst_file = src_file.with_suffix(".mp4")
        if dst_file.exists() and dst_file.resolve() != src_file.resolve():
            dst_file = self.make_unique_path(dst_file)

        temp_file      = src_file.with_name(src_file.stem + f".__h264__.{int(time.time() * 1000)}.mp4")
        total_duration = self.get_media_duration(src_file, ffmpeg_path)
        creationflags  = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        encode_args    = self._build_h264_encode_args()

        # 能直接放進 MP4 的常見音訊就 copy，避免不必要的二次壓縮；
        # 其他格式才轉成 AAC，確保相容性。
        if audio_codec in {"aac", "mp3", "alac"}:
            audio_args = ["-c:a", "copy"]
        else:
            audio_args = ["-c:a", "aac", "-b:a", "192k"]

        cmd = [
            ffmpeg_path, "-y", "-v", "error",
            "-progress", "pipe:1", "-nostats",
            "-i", str(src_file),
            # 只取第一條音訊軌，避免多音軌來源中某些音訊格式無法 copy 進 MP4 而造成整檔失敗。
            "-map", "0:v:0?", "-map", "0:a:0?",
            *encode_args,
            *audio_args,
            "-movflags", "+faststart",
            str(temp_file),
        ]

        return_code, log_buffer, start_time, last_out_time = self._run_ffmpeg_with_progress(
            cmd, src_file, index, total, total_duration, "H.264", creationflags
        )

        if self.cancel_event.is_set():
            temp_file.unlink(missing_ok=True)
            raise DownloadCancelled("使用者已取消")

        if return_code != 0:
            temp_file.unlink(missing_ok=True)
            raise RuntimeError(self.build_ffmpeg_error_message("ffmpeg H.264 轉檔失敗", log_buffer))

        self.safe_after(
            self.update_encode_progress_ui,
            src_file.name, time.time() - start_time,
            index, total, total_duration, total_duration or last_out_time, "H.264",
        )
        print(f"\n[{src_file.name}] H.264 轉檔完成！（編碼器：{self.hardware_encoder}）")
        self.finalize_transcoded_file(src_file, temp_file, dst_file)
        return dst_file, "converted"

    def normalize_downloaded_videos_to_h264(self, folder, candidate_files=None, recent_since=0.0):
        ffmpeg_path = find_ffmpeg()
        if not ffmpeg_path:
            raise RuntimeError("找不到 ffmpeg，無法轉成 H.264。")

        new_video_files = self.dedupe_existing_paths(candidate_files, folder=folder, mode="mp4")
        if not new_video_files:
            new_video_files = self.get_recent_media_files(folder, "mp4", since=recent_since)

        if not new_video_files:
            return {"converted": 0, "skipped": 0, "failed": 0, "final_files": [], "failure_details": []}

        summary = {"converted": 0, "skipped": 0, "failed": 0, "final_files": [], "failure_details": []}
        total   = len(new_video_files)

        for idx, video_file in enumerate(new_video_files, start=1):
            if self.cancel_event.is_set():
                raise DownloadCancelled("使用者已取消")
            self.safe_after(self.status_label.config,
                            text=f"檢查／轉成 H.264... ({idx}/{total}) {video_file.name}")
            try:
                result_file, action = self.convert_file_to_h264(video_file, ffmpeg_path, idx, total)
                summary[action] += 1
                summary["final_files"].append(result_file)
                self.mark_item_result_from_file(
                    video_file, "skipped" if action == "skipped" else "success",
                    new_file_path=result_file, title_hint=video_file.stem
                )
            except DownloadCancelled:
                raise
            except Exception as e:
                summary["failed"] += 1
                summary["failure_details"].append(f"{video_file.name}：{e}")
                self.mark_item_result_from_file(video_file, "failed_conversion", title_hint=video_file.stem)
                print(f"\n[H.264 轉檔失敗] {video_file.name}: {e}")

        self.safe_after(self.stop_indeterminate_progress)
        self.safe_after(self.progress_bar.config, mode="determinate", value=100)
        return summary

    # ─────────────────────────────────────────────
    # FFmpeg 轉檔：ALAC（含縮圖嵌入）
    # ─────────────────────────────────────────────

    def _find_thumbnail_for_file(self, src_file: Path) -> Path | None:
        """尋找與音訊檔同名的縮圖檔案。"""
        for ext in (".jpg", ".jpeg", ".webp", ".png"):
            candidate = src_file.with_suffix(ext)
            if candidate.exists():
                return candidate
        return None

    def convert_file_to_alac(self, src_file, ffmpeg_path, index, total):
        src_file = Path(src_file)
        thumbnail_file = self._find_thumbnail_for_file(src_file)
        temp_file = None

        try:
            stream_info = self.get_audio_stream_details(src_file, ffmpeg_path)
            audio_codec = (stream_info.get("codec_name") or "").lower()

            if audio_codec == "alac" and src_file.suffix.lower() == ".m4a":
                self.safe_after(self.status_label.config,
                                text=f"已是 ALAC，跳過轉檔 ({index}/{total}) {src_file.name}")
                return src_file, "skipped"

            dst_file = src_file.with_suffix(".m4a")
            if dst_file.exists() and dst_file.resolve() != src_file.resolve():
                dst_file = self.make_unique_path(dst_file)

            temp_file = src_file.with_name(src_file.stem + f".__alac__.{int(time.time() * 1000)}.m4a")
            total_duration = self.get_media_duration(src_file, ffmpeg_path)
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            if thumbnail_file:
                # 有獨立縮圖檔：作為第二輸入嵌入。
                # m4a（ipod muxer）的 attached_pic 只接受 JPEG，
                # yt-dlp 下載的縮圖常見為 WebP，因此轉成 JPEG 後嵌入。
                cmd = [
                    ffmpeg_path, "-y", "-v", "error",
                    "-progress", "pipe:1", "-nostats",
                    "-i", str(src_file),
                    "-i", str(thumbnail_file),
                    "-map", "0:a:0",
                    "-map", "1:v",
                    "-c:v", "mjpeg",
                    "-vf", "scale='min(600,iw)':'min(600,ih)':force_original_aspect_ratio=decrease",
                    "-q:v", "3",
                    "-disposition:v:0", "attached_pic",
                    "-map_metadata", "0",
                    "-map_chapters", "0",
                    "-c:a", "alac",
                    "-movflags", "+faststart",
                    str(temp_file),
                ]
            else:
                # 無縮圖時完全不加 video mapping 和 disposition，避免 ffmpeg -22 Invalid argument。
                cmd = [
                    ffmpeg_path, "-y", "-v", "error",
                    "-progress", "pipe:1", "-nostats",
                    "-i", str(src_file),
                    "-map", "0:a:0",
                    "-map_metadata", "0",
                    "-map_chapters", "0",
                    "-c:a", "alac",
                    "-movflags", "+faststart",
                    str(temp_file),
                ]

            return_code, log_buffer, start_time, last_out_time = self._run_ffmpeg_with_progress(
                cmd, src_file, index, total, total_duration, "ALAC", creationflags
            )

            if self.cancel_event.is_set():
                if temp_file:
                    temp_file.unlink(missing_ok=True)
                raise DownloadCancelled("使用者已取消")

            if return_code != 0:
                if temp_file:
                    temp_file.unlink(missing_ok=True)
                raise RuntimeError(self.build_ffmpeg_error_message("ffmpeg ALAC 轉檔失敗", log_buffer))

            self.safe_after(
                self.update_encode_progress_ui,
                src_file.name, time.time() - start_time,
                index, total, total_duration, total_duration or last_out_time, "ALAC",
            )
            print(f"\n[{src_file.name}] ALAC 轉檔完成！")
            self.finalize_transcoded_file(src_file, temp_file, dst_file)
            return dst_file, "converted"

        finally:
            # 成功、失敗、取消、跳過都統一清理 yt-dlp 產生的縮圖檔，避免資料夾殘留。
            if thumbnail_file and thumbnail_file.exists():
                try:
                    thumbnail_file.unlink(missing_ok=True)
                except Exception as e:
                    print(f"清理縮圖檔失敗: {e}")

    def normalize_downloaded_audio_to_alac(self, folder, candidate_files=None, recent_since=0.0):
        ffmpeg_path = find_ffmpeg()
        if not ffmpeg_path:
            raise RuntimeError("找不到 ffmpeg，無法轉成 ALAC。")

        new_audio_files = self.dedupe_existing_paths(candidate_files, folder=folder, mode="audio")
        if not new_audio_files:
            new_audio_files = self.get_recent_media_files(folder, "audio", since=recent_since)

        if not new_audio_files:
            return {"converted": 0, "skipped": 0, "failed": 0,
                    "final_files": [], "audio_profiles": [], "failure_details": []}

        summary = {"converted": 0, "skipped": 0, "failed": 0,
                   "final_files": [], "audio_profiles": [], "failure_details": []}
        total   = len(new_audio_files)

        for idx, audio_file in enumerate(new_audio_files, start=1):
            if self.cancel_event.is_set():
                raise DownloadCancelled("使用者已取消")
            self.safe_after(self.status_label.config,
                            text=f"正在轉成 ALAC (.m4a)... ({idx}/{total}) {audio_file.name}")
            try:
                result_file, action = self.convert_file_to_alac(audio_file, ffmpeg_path, idx, total)
                summary[action] += 1
                summary["final_files"].append(result_file)
                summary["audio_profiles"].append({
                    "file_name": result_file.name,
                    "profile":   self.format_audio_profile(
                        self.get_audio_stream_details(result_file, ffmpeg_path)
                    ),
                })
                self.mark_item_result_from_file(
                    audio_file, "skipped" if action == "skipped" else "success",
                    new_file_path=result_file, title_hint=audio_file.stem
                )
            except DownloadCancelled:
                raise
            except Exception as e:
                summary["failed"] += 1
                summary["failure_details"].append(f"{audio_file.name}：{e}")
                self.mark_item_result_from_file(audio_file, "failed_conversion", title_hint=audio_file.stem)
                print(f"\n[ALAC 轉檔失敗] {audio_file.name}: {e}")

        self.safe_after(self.stop_indeterminate_progress)
        self.safe_after(self.progress_bar.config, mode="determinate", value=100)
        return summary

    # ─────────────────────────────────────────────
    # 主下載流程
    # ─────────────────────────────────────────────

    def finalize_download(self):
        if self.is_closing:
            return

        self.is_downloading        = False
        self.cancel_event.clear()
        self.current_download_mode = None
        self.current_process       = None
        self.download_thread       = None
        self.cancel_btn.config(state=tk.DISABLED)
        self.detail_btn.config(state=tk.NORMAL if self.current_summary else tk.DISABLED)
        self.analyze_btn.config(state=tk.NORMAL)
        self.download_btn.config(
            state=(tk.NORMAL if self.analysis_valid else tk.DISABLED),
            text="2. 開始下載"
        )

    def download_video(self, url, path, mode, is_playlist, selected_audio=None, download_started_at=None):
        ffmpeg_path = find_ffmpeg()
        if not ffmpeg_path:
            self.safe_after(messagebox.showerror, "錯誤", "找不到 ffmpeg！")
            self.safe_after(self.finalize_download)
            return

        error_code = -1
        download_started_at = download_started_at or time.time()

        try:
            if is_playlist:
                playlist_title = self.video_info.get("title", "Playlist")
                safe_name      = safe_folder_name(playlist_title, "Playlist")
                path           = os.path.join(path, safe_name)
                os.makedirs(path, exist_ok=True)
                out_tmpl = os.path.join(path, "%(playlist_index)03d - %(title)s.%(ext)s")
            else:
                os.makedirs(path, exist_ok=True)
                out_tmpl = os.path.join(path, "%(title)s.%(ext)s")

            self.current_download_folder = path
            self.current_summary         = None

            enable_thumbnail_embed = (mode == "audio" and selected_audio == "mp3")

            ydl_opts = {
                "outtmpl":           out_tmpl,
                "addmetadata":       True,
                "writethumbnail":    enable_thumbnail_embed,
                "progress_hooks":    [self.progress_hook],
                "postprocessor_hooks": [self.postprocessor_hook],
                "match_filter":      self._match_filter,
                "ignoreerrors":      True,
                "nocolor":           True,
                "windowsfilenames":  True,
                "overwrites":        False,
                "continuedl":        True,
                "retries":           10,
                "fragment_retries":  10,
                "extractor_retries": 3,
                "socket_timeout":    30,
                "ffmpeg_location":   ffmpeg_path,
                "logger":            YTDLPLogger(self),
                "postprocessors":    [],
            }

            if mode == "audio":
                ydl_opts["format"] = "bestaudio/best"

                if selected_audio == "mp3":
                    ydl_opts["postprocessors"].append({
                        "key":              "FFmpegExtractAudio",
                        "preferredcodec":   "mp3",
                        "preferredquality": "320",
                    })
                    if enable_thumbnail_embed:
                        ydl_opts["postprocessors"].append({
                            "key":                    "EmbedThumbnail",
                            "already_have_thumbnail": False,
                        })

                elif selected_audio == "wav":
                    # yt-dlp 的 WAV 輸出通常會轉成標準 PCM WAV，
                    # 來源若本身是 YouTube 的 Opus/AAC，有損來源轉 WAV 不會提升音質。
                    ydl_opts["postprocessors"].append({
                        "key":            "FFmpegExtractAudio",
                        "preferredcodec": "wav",
                    })

                else:
                    # ALAC：下載 bestaudio，後由 convert_file_to_alac 處理；保留縮圖供嵌入使用。
                    ydl_opts["writethumbnail"] = True

            else:
                # 影片模式：抓最高解析度／幀率來源，再轉成高相容性的 H.264。
                ydl_opts.update({
                    "format":               "bestvideo+bestaudio/best",
                    "merge_output_format":  "mp4",
                    "format_sort":          ["res", "fps", "vcodec:h264", "acodec:aac"],
                })

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    error_code = ydl.download([url])
            except DownloadCancelled:
                raise
            except Exception as e:
                print(f"\n[yt-dlp 下載異常] {str(e)}")
                raise

            if self.cancel_event.is_set():
                raise DownloadCancelled("使用者已取消")

            # ── 後處理：轉檔 ──
            encode_summary = {"converted": 0, "skipped": 0, "failed": 0,
                              "final_files": [], "failure_details": []}
            audio_summary  = {"converted": 0, "skipped": 0, "failed": 0,
                              "final_files": [], "audio_profiles": [], "failure_details": []}

            if mode == "mp4":
                video_candidates = self.get_tracked_media_files(path, "mp4", include_raw=False)
                encode_summary   = self.normalize_downloaded_videos_to_h264(
                    path, candidate_files=video_candidates, recent_since=download_started_at
                )
                final_files      = encode_summary["final_files"]
            else:
                if selected_audio == "alac":
                    audio_candidates = self.get_tracked_media_files(path, "audio")
                    audio_summary    = self.normalize_downloaded_audio_to_alac(
                        path, candidate_files=audio_candidates, recent_since=download_started_at
                    )
                    final_files      = audio_summary["final_files"]
                else:
                    ext_map     = {"mp3": {".mp3"}, "wav": {".wav"}}
                    final_files = self.get_tracked_media_files(
                        path, "audio", allowed_exts=ext_map.get(selected_audio), include_raw=False
                    )
                    if not final_files:
                        final_files = self.get_recent_media_files(
                            path, "audio", allowed_exts=ext_map.get(selected_audio),
                            since=download_started_at
                        )
                self.safe_after(self.progress_bar.config, value=100)

            expected_count = max(
                1,
                self.expected_count or len(self.expected_item_keys)
                or len(final_files) or 1
            )
            self.finalize_item_states(mode, selected_audio, final_files, error_code)
            success_count, failure_count, success_items, failed_items, unresolved_count = \
                self.summarize_item_states(expected_count, error_code)

            summary = {
                "path":             path,
                "mode":             mode,
                "selected_audio":   selected_audio,
                "expected":         expected_count,
                "success":          success_count,
                "failed":           failure_count,
                "unresolved":       unresolved_count,
                "converted":        encode_summary["converted"],
                "skipped_h264":     encode_summary["skipped"],
                "converted_alac":   audio_summary["converted"],
                "skipped_alac":     audio_summary["skipped"],
                "audio_profiles":   audio_summary["audio_profiles"],
                "conversion_failure_details": (
                    encode_summary.get("failure_details", []) +
                    audio_summary.get("failure_details", [])
                ),
                "error_messages":   list(dict.fromkeys(self.ytdlp_errors)),
                "warning_messages": list(dict.fromkeys(self.ytdlp_warnings)),
                "completed_items":  success_items,
                "failed_items":     failed_items,
                "item_states":      dict(self.download_item_states),
                "yt_dlp_return_code": error_code,
                "final_files":      [str(Path(p)) for p in final_files],
                "hardware_encoder": self.hardware_encoder,
            }

            self.current_summary = summary
            self.safe_after(self.on_download_complete, summary)

        except DownloadCancelled:
            self.safe_after(self.on_download_cancelled)
        except Exception as e:
            print(f"\n執行下載過程發生錯誤: {e}")
            self.safe_after(messagebox.showerror, "錯誤", f"下載失敗: {str(e)}")
            self.safe_after(self.status_label.config, text="下載失敗")
        finally:
            self.safe_after(self.finalize_download)

    # ─────────────────────────────────────────────
    # 下載完成 / 取消
    # ─────────────────────────────────────────────

    def on_download_cancelled(self):
        # 【修正】先 stop() 再 config，確保 indeterminate 動畫一定被停止
        self.progress_bar.stop()
        self.progress_bar.config(mode="determinate", value=0)
        self.status_label.config(text="已取消", fg="gray", cursor="")
        self.status_label.unbind("<Button-1>")
        self.speed_label.config(text="")
        messagebox.showinfo("已取消", "下載已取消。")

    def on_download_complete(self, summary):
        success   = summary.get("success", 0)
        failed    = summary.get("failed", 0)
        converted       = summary.get("converted", 0)
        skipped_h264    = summary.get("skipped_h264", 0)
        converted_alac  = summary.get("converted_alac", 0)
        skipped_alac    = summary.get("skipped_alac", 0)
        audio_profiles             = summary.get("audio_profiles") or []
        conversion_failure_details = summary.get("conversion_failure_details") or []
        warning_messages = summary.get("warning_messages") or []
        error_messages   = summary.get("error_messages") or []
        path             = summary.get("path", "")
        mode             = summary.get("mode", "mp4")
        selected_audio   = summary.get("selected_audio")
        yt_dlp_return_code = summary.get("yt_dlp_return_code")
        unresolved       = summary.get("unresolved", 0)
        hw_encoder       = summary.get("hardware_encoder", "libx264")

        print(f"\n{'='*50}\n[系統] 任務結束 - 成功: {success} | 失敗: {failed} | 待確認: {unresolved}\n{'='*50}")

        if failed > 0 or unresolved > 0:
            self.status_label.config(text=f"完成（有異常）：成功 {success}，失敗 {failed}，待確認 {unresolved}")
        else:
            self.status_label.config(text=f"任務完成：成功 {success}")
        self.speed_label.config(text="")
        self.progress_bar.config(mode="determinate", value=100)
        self.detail_btn.config(state=tk.NORMAL)

        detail_lines = [
            f"成功：{success}",
            f"失敗：{failed}",
            f"預期項目數：{summary.get('expected', 0)}",
        ]
        if unresolved:
            detail_lines.append(f"待確認：{unresolved}")
        if yt_dlp_return_code not in (None, ""):
            detail_lines.append(f"yt-dlp 回傳碼：{yt_dlp_return_code}")
        if mode == "mp4":
            detail_lines.append(f"重新轉成 H.264（{hw_encoder}）：{converted}")
            detail_lines.append(f"已是 H.264 直接跳過：{skipped_h264}")
        elif selected_audio == "alac":
            detail_lines.append(f"轉成 ALAC (.m4a)：{converted_alac}")
            detail_lines.append(f"已是 ALAC 直接跳過：{skipped_alac}")
            if audio_profiles:
                unique_profiles = sorted({item.get("profile", "未知") for item in audio_profiles})
                if len(unique_profiles) == 1:
                    detail_lines.append(f"實際輸出：{unique_profiles[0]}")
                else:
                    detail_lines.append(f"實際輸出規格共有 {len(unique_profiles)} 種，請查看詳細紀錄")
        if conversion_failure_details:
            detail_lines.append(f"轉檔失敗：{len(conversion_failure_details)} 項")
        if error_messages:
            detail_lines.append(f"yt-dlp 錯誤訊息：{len(error_messages)} 則")
        if warning_messages:
            detail_lines.append(f"yt-dlp 警告訊息：{len(warning_messages)} 則")

        # 自動儲存紀錄
        try:
            log_filepath = os.path.join(path, f"下載紀錄_{int(time.time())}.txt")
            with open(log_filepath, "w", encoding="utf-8") as f:
                f.write(self.build_result_detail_text(summary))
            print(f"\n[系統] 詳細紀錄已自動儲存至: {log_filepath}")
        except Exception as e:
            print(f"\n[系統] 自動儲存紀錄檔失敗: {e}")

        has_detail = bool(
            failed > 0 or unresolved > 0 or conversion_failure_details
            or error_messages or warning_messages
        )
        message = "下載完成\n\n" + "\n".join(detail_lines)
        if has_detail:
            message += "\n\n已為你開啟詳細紀錄視窗。"
            self.show_result_detail_window(summary)

        if messagebox.askyesno("完成", message + "\n\n是否開啟資料夾？"):
            self.open_folder(path)

    def open_folder(self, path):
        """開啟輸出資料夾；主要支援 Windows，也保留 macOS / Linux 後備。"""
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showwarning("提示", f"無法自動開啟資料夾：{e}")

    # ─────────────────────────────────────────────
    # 紀錄視窗
    # ─────────────────────────────────────────────

    def build_result_detail_text(self, summary) -> str:
        lines = []
        lines.append("下載結果詳細紀錄")
        lines.append("=" * 36)
        lines.append(f"儲存位置：{summary.get('path', '')}")
        lines.append(f"模式：{'影片 MP4' if summary.get('mode') == 'mp4' else '音訊'}")
        if summary.get("selected_audio"):
            lines.append(f"音訊格式：{summary.get('selected_audio')}")
        if summary.get("hardware_encoder"):
            lines.append(f"H.264 編碼器：{summary.get('hardware_encoder')}")
        if summary.get("yt_dlp_return_code") not in (None, ""):
            lines.append(f"yt-dlp 回傳碼：{summary.get('yt_dlp_return_code')}")
        lines.append(f"預期項目數：{summary.get('expected', 0)}")
        lines.append(f"成功：{summary.get('success', 0)}")
        lines.append(f"失敗：{summary.get('failed', 0)}")
        if summary.get("unresolved"):
            lines.append(f"待確認：{summary.get('unresolved')}")
        lines.append("")

        for section, label in [
            ("conversion_failure_details", "轉檔失敗明細"),
            ("error_messages",             "yt-dlp 錯誤"),
            ("warning_messages",           "yt-dlp 警告"),
        ]:
            items = summary.get(section) or []
            if items:
                lines.append(f"{label}：")
                for item in items:
                    lines.append(f"  - {item}")
                lines.append("")

        failed_items = summary.get("failed_items") or []
        if failed_items:
            lines.append("失敗項目：")
            for item in failed_items:
                prefix = f"第 {item.get('playlist_index')} 項：" if item.get("playlist_index") else ""
                lines.append(f"  - {prefix}{item.get('title') or '未命名'} [{item.get('state')}]")
            lines.append("")

        final_files = summary.get("final_files") or []
        if final_files:
            lines.append("輸出檔案：")
            for item in final_files:
                lines.append(f"  - {item}")
            lines.append("")

        completed_items = summary.get("completed_items") or []
        if completed_items:
            lines.append("成功完成的項目：")
            for item in completed_items:
                prefix = f"第 {item.get('playlist_index')} 項：" if item.get("playlist_index") else ""
                lines.append(f"  - {prefix}{item.get('title') or '未命名'}")

        return "\n".join(lines).strip()

    def show_result_detail_window(self, summary):
        try:
            detail_text = self.build_result_detail_text(summary)
            window      = tk.Toplevel(self.root)
            window.title("下載詳細紀錄")
            window.geometry("820x560")

            frame = tk.Frame(window)
            frame.pack(fill="both", expand=True, padx=10, pady=10)

            text_widget = tk.Text(frame, wrap="word")
            scrollbar   = tk.Scrollbar(frame, command=text_widget.yview)
            text_widget.configure(yscrollcommand=scrollbar.set)
            text_widget.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")
            text_widget.insert("1.0", detail_text)
            text_widget.config(state="disabled")

            btn_frame = tk.Frame(window)
            btn_frame.pack(fill="x", padx=10, pady=(0, 10))

            def copy_text():
                try:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(detail_text)
                    self.status_label.config(text="已複製詳細紀錄到剪貼簿")
                except Exception as e:
                    messagebox.showerror("錯誤", f"無法複製紀錄：{e}")

            tk.Button(btn_frame, text="複製紀錄", command=copy_text).pack(side="left")
            tk.Button(btn_frame, text="關閉", command=window.destroy).pack(side="right")
        except Exception as e:
            messagebox.showerror("錯誤", f"無法顯示詳細紀錄：{e}")

    # ─────────────────────────────────────────────
    # 關閉視窗
    # ─────────────────────────────────────────────

    def on_close(self):
        if self.is_closing:
            return

        if self.is_downloading:
            if not messagebox.askyesno("確認", "目前仍在下載或轉檔中，確定要結束程式嗎？"):
                return
            self.is_closing = True
            self.cancel_event.set()
            self._terminate_current_process("關閉視窗")
            try:
                self.status_label.config(text="正在關閉，已要求目前任務停止...", fg="gray")
                self.cancel_btn.config(state=tk.DISABLED)
                self.download_btn.config(state=tk.DISABLED)
                self.analyze_btn.config(state=tk.DISABLED)
            except Exception:
                pass
            self.root.after(200, self._wait_for_download_thread_then_destroy, time.time() + 5.0)
            return

        if self.is_analyzing:
            if not messagebox.askyesno("確認", "目前正在讀取內容，確定要結束程式嗎？"):
                return

        self.is_closing = True
        self.cancel_event.set()
        self.force_destroy_root()

    def _wait_for_download_thread_then_destroy(self, deadline):
        """關閉時給下載 worker 一小段時間善後，避免太快 destroy 造成 .part 或子程序殘留。"""
        try:
            worker = self.download_thread
            if worker is None or not worker.is_alive() or time.time() >= float(deadline):
                self.force_destroy_root()
                return
            self.root.after(200, self._wait_for_download_thread_then_destroy, deadline)
        except Exception:
            self.force_destroy_root()

    def _terminate_current_process(self, label="目前程序"):
        process = self.current_process
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
            except Exception as e:
                print(f"{label}時終止程序失敗: {e}")

    def force_destroy_root(self):
        try:
            self.root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    root = tk.Tk()
    app  = VideoDownloaderApp(root)
    root.mainloop()
