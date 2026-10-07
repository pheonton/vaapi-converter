#!/usr/bin/env python3
"""
VAAPI Converter — a desktop front-end for hardware video encoding on Linux.

Every video encode runs on the GPU through VAAPI; there is no software encoder
path. If you want CPU encoding, HandBrake already does that well.

Requires: Python 3.8+ (tkinter included), ffmpeg/ffprobe built with VAAPI, and a
GPU render node under /dev/dri.
Run with:  python3 ffmpeg_gui.py
"""

import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "VAAPI Converter"

# --- Format catalogue -------------------------------------------------------
# Each container lists the codecs it can actually hold, so the UI can't build
# an invalid command (e.g. H.264 inside a WebM file).

CONTAINERS = {
    "MP4 (.mp4)": {
        "ext": ".mp4",
        "video": ["hevc", "av1", "h264", "copy"],
        "audio": ["copy", "aac", "libmp3lame", "none"],
    },
    "Matroska (.mkv)": {
        "ext": ".mkv",
        "video": ["hevc", "av1", "vp9", "h264", "copy"],
        "audio": ["copy", "aac", "libmp3lame", "libopus", "flac", "none"],
    },
    "WebM (.webm)": {
        "ext": ".webm",
        "video": ["av1", "vp9", "copy"],
        "audio": ["libopus", "copy", "none"],
    },
}

CODEC_LABELS = {
    "copy": "Keep as-is (no re-encode)",
    "h264": "H.264 — widest compatibility",
    "hevc": "H.265 / HEVC — smaller files",
    "av1": "AV1 — smallest files",
    "vp9": "VP9 — for WebM",
    "aac": "AAC",
    "libmp3lame": "MP3",
    "libopus": "Opus",
    "flac": "FLAC (lossless)",
    "none": "No audio",
}

# Hardware encoders that can produce 10-bit. H.264 High10 isn't implemented by
# any current VAAPI driver, so it's absent deliberately.
TEN_BIT_CAPABLE = {"hevc", "av1", "vp9"}

# The VAAPI encoder backing each logical codec.
ENCODERS = {
    "h264": "h264_vaapi",
    "hevc": "hevc_vaapi",
    "av1": "av1_vaapi",
    "vp9": "vp9_vaapi",
}

RESOLUTIONS = {
    "Same as source": None,
    "2160p (4K)": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}

FRAMERATES = ["Same as source", "60", "30", "25", "24", "15"]
VIDEO_BITRATES = ["2M", "4M", "6M", "8M", "12M", "20M", "40M"]

AUDIO_BITRATES = ["320k", "256k", "192k", "128k", "96k", "64k"]

# Named quality tiers. MP3 gets true VBR (-q:a, the classic V-numbers); AAC and
# Opus are already variable-rate around a target, so for them a tier is a target.
AUDIO_QUALITY = {
    "Transparent": {"libmp3lame": ("-q:a", "0"), "aac": ("-b:a", "256k"),
                    "libopus": ("-b:a", "160k")},
    "High": {"libmp3lame": ("-q:a", "2"), "aac": ("-b:a", "192k"),
             "libopus": ("-b:a", "128k")},
    "Standard": {"libmp3lame": ("-q:a", "4"), "aac": ("-b:a", "128k"),
                 "libopus": ("-b:a", "96k")},
    "Small": {"libmp3lame": ("-q:a", "6"), "aac": ("-b:a", "96k"),
              "libopus": ("-b:a", "64k")},
}

MP3_AVERAGE_KBPS = {"0": 245, "2": 190, "4": 165, "6": 115}

# Downmix choices. The mix levels are swresample's own, so they work on any
# input layout — unlike a hand-written pan matrix, which must match the source.
CHANNEL_MODES = {
    "Keep original": [],
    "Stereo": ["-ac", "2"],
    "Stereo (dialogue boost)":
        ["-ac", "2", "-af", "aresample=center_mixlev=1.0:surround_mixlev=0.5"],
    "Stereo (Pro Logic II)": ["-ac", "2", "-af", "aresample=matrix_encoding=dplii"],
    "Mono": ["-ac", "1"],
}

# Each container accepts a different subtitle format.
SUBTITLE_CODEC = {".mkv": "copy", ".mp4": "mov_text", ".webm": "webvtt"}

MEDIA_TYPES = [
    ("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.wmv *.flv *.ts "
                    "*.mpg *.mpeg *.mts *.m2ts *.3gp *.ogv"),
    ("All files", "*.*"),
]

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def label_for(codec: str) -> str:
    return CODEC_LABELS.get(codec, codec)


def codec_from_label(label: str) -> str:
    for code, text in CODEC_LABELS.items():
        if text == label:
            return code
    return label


def detect_scale(root: tk.Tk) -> float:
    """Work out the desktop's scale factor — Tk won't do it on its own."""
    for name in ("FFMPEG_GUI_SCALE", "GDK_SCALE"):
        value = os.environ.get(name)
        if value:
            try:
                return max(1.0, float(value))
            except ValueError:
                pass
    try:  # GNOME/KDE write the effective DPI here on X11
        out = subprocess.run(["xrdb", "-query"], capture_output=True, text=True,
                             timeout=5, creationflags=NO_WINDOW)
        for line in out.stdout.splitlines():
            if line.lower().startswith("xft.dpi:"):
                return max(1.0, float(line.split(":", 1)[1].strip()) / 96)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        return max(1.0, round(root.winfo_fpixels("1i") / 96, 2))
    except tk.TclError:
        return 1.0


def apply_scaling(root: tk.Tk, scale: float):
    if scale <= 1.01:
        return
    root.tk.call("tk", "scaling", 96 * scale / 72)
    for name in tkfont.names(root):
        try:
            font = tkfont.nametofont(name, root)
        except tk.TclError:
            continue
        size = font.cget("size")
        # Negative sizes are pixels and ignore scaling; convert them to points.
        if size < 0:
            font.configure(size=max(6, round(-size * 72 / 96)))
    style = ttk.Style(root)
    for widget in ("TCheckbutton", "TRadiobutton"):
        style.configure(widget, indicatorsize=round(12 * scale))


def parse_bitrate(text: str) -> int | None:
    """Accept 8M, 8000k or 8000000 and return bits per second."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*", text or "")
    if not match:
        return None
    scale = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2).lower()]
    bits = int(float(match.group(1)) * scale)
    return bits if bits >= 100_000 else None


def render_nodes() -> list[str]:
    """GPU render nodes VAAPI can open, e.g. /dev/dri/renderD128."""
    try:
        return sorted(str(p) for p in Path("/dev/dri").glob("renderD*"))
    except OSError:
        return []


def hms_to_seconds(value: str) -> float:
    try:
        h, m, s = value.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return 0.0


class CropDialog(tk.Toplevel):
    """Grab a frame from the video and pick how much to trim off each edge."""

    BASE_WIDTH = 720

    def __init__(self, app: "ConverterApp", source: Path, initial=None):
        super().__init__(app)
        self.title(f"Crop — {source.name}")
        self.transient(app)
        self.resizable(False, False)

        self.app = app
        self.source = source
        self.scale = getattr(app, "scale", 1.0)
        self.result = None
        self.photo = None
        self.drag_origin = None
        self.tempdir = Path(tempfile.mkdtemp(prefix="ffmpeg_gui_"))

        self.src_w, self.src_h = self._probe_size()
        if not self.src_w:
            messagebox.showerror(APP_TITLE, "Couldn't read the video's size.", parent=self)
            self.destroy()
            return

        # Preview grows with the desktop scale, but never past the screen.
        max_w = min(round(self.BASE_WIDTH * self.scale), self.winfo_screenwidth() - 80)
        max_h = self.winfo_screenheight() - round(340 * self.scale)
        self.factor = min(1.0, max_w / self.src_w, max_h / self.src_h)
        self.view_w = round(self.src_w * self.factor)
        self.view_h = round(self.src_h * self.factor)

        self.duration = app._probe_duration(source)
        self.position = tk.DoubleVar(value=min(self.duration * 0.25, max(self.duration - 1, 0)))
        self.trim_left = tk.IntVar(value=0)
        self.trim_top = tk.IntVar(value=0)
        self.trim_right = tk.IntVar(value=0)
        self.trim_bottom = tk.IntVar(value=0)
        if initial:
            w, h, x, y = initial
            self.trim_left.set(x)
            self.trim_top.set(y)
            self.trim_right.set(max(0, self.src_w - x - w))
            self.trim_bottom.set(max(0, self.src_h - y - h))
        self.hint = tk.StringVar(value="Drag on the frame to choose what to keep.")

        self._build()
        self.grab_frame()
        self._draw_rect()
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda _e: self.cancel())

    # --- layout -------------------------------------------------------------

    def _build(self):
        pad = round(10 * self.scale)
        outer = ttk.Frame(self, padding=pad)
        outer.grid(sticky="nsew")

        self.canvas = tk.Canvas(outer, width=self.view_w, height=self.view_h,
                                highlightthickness=1, highlightbackground="#888",
                                cursor="crosshair", background="#111")
        self.canvas.grid(row=0, column=0)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

        seek = ttk.Frame(outer)
        seek.grid(row=1, column=0, sticky="ew", pady=(pad, 0))
        seek.columnconfigure(1, weight=1)
        ttk.Label(seek, text="Frame at").grid(row=0, column=0)
        self.seek_scale = ttk.Scale(seek, from_=0, to=max(self.duration, 1),
                                    variable=self.position, orient="horizontal")
        self.seek_scale.grid(row=0, column=1, sticky="ew", padx=6)
        self.time_label = ttk.Label(seek, text="0:00", width=6)
        self.time_label.grid(row=0, column=2)
        ttk.Button(seek, text="Show frame", command=self.grab_frame).grid(row=0, column=3,
                                                                          padx=(6, 0))
        self.position.trace_add("write", lambda *_: self._update_time())
        self._update_time()

        fields = ttk.Frame(outer)
        fields.grid(row=2, column=0, sticky="ew", pady=(pad, 0))
        edges = (("Trim left", self.trim_left, self.src_w),
                 ("Trim top", self.trim_top, self.src_h),
                 ("Trim right", self.trim_right, self.src_w),
                 ("Trim bottom", self.trim_bottom, self.src_h))
        for i, (text, var, limit) in enumerate(edges):
            ttk.Label(fields, text=text).grid(row=0, column=i * 2,
                                              padx=(0 if i == 0 else 12, 4))
            box = ttk.Spinbox(fields, textvariable=var, from_=0, to=limit,
                              increment=2, width=6, command=self._draw_rect)
            box.grid(row=0, column=i * 2 + 1)
            box.bind("<KeyRelease>", lambda _e: self._draw_rect())

        self.size_label = ttk.Label(outer, text="")
        self.size_label.grid(row=3, column=0, sticky="w", pady=(pad, 0))

        ttk.Label(outer, textvariable=self.hint, foreground="#666", justify="left",
                  wraplength=self.view_w).grid(row=4, column=0, sticky="w", pady=(4, 0))

        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, sticky="ew", pady=(pad, 0))
        ttk.Button(actions, text="Detect black borders",
                   command=self.detect_borders).pack(side="left")
        ttk.Button(actions, text="Whole frame", command=self.reset).pack(side="left", padx=6)
        ttk.Button(actions, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(actions, text="Use this crop", command=self.apply).pack(side="right", padx=6)

    # --- ffmpeg work --------------------------------------------------------

    def _probe_size(self) -> tuple[int, int]:
        probe = self.app.ffprobe.get() or shutil.which("ffprobe")
        if not probe:
            return 0, 0
        try:
            out = subprocess.run(
                [probe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(self.source)],
                capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
            w, h = out.stdout.strip().splitlines()[0].split("x")[:2]
            return int(w), int(h)
        except (ValueError, IndexError, OSError, subprocess.SubprocessError):
            return 0, 0

    def grab_frame(self):
        """Pull one frame at the chosen time, scaled to preview size."""
        target = self.tempdir / "frame.ppm"
        cmd = [self.app.ffmpeg.get() or "ffmpeg", "-hide_banner", "-nostdin", "-y",
               "-ss", f"{self.position.get():.2f}", "-i", str(self.source),
               "-frames:v", "1", "-vf", f"scale={self.view_w}:{self.view_h}",
               "-f", "image2", str(target)]
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            subprocess.run(cmd, capture_output=True, timeout=60, creationflags=NO_WINDOW)
            self.photo = tk.PhotoImage(file=str(target))
            self.canvas.delete("frame")
            self.canvas.create_image(0, 0, anchor="nw", image=self.photo, tags="frame")
            self.canvas.tag_lower("frame")
        except (OSError, tk.TclError, subprocess.SubprocessError) as exc:
            self.hint.set(f"Couldn't read a frame there: {exc}")
        finally:
            self.config(cursor="")
        self._draw_rect()

    def detect_borders(self):
        """Let ffmpeg's cropdetect look for uniform borders around the picture."""
        cmd = [self.app.ffmpeg.get() or "ffmpeg", "-hide_banner", "-nostdin",
               "-ss", f"{self.position.get():.2f}", "-i", str(self.source),
               "-vf", "cropdetect=limit=24:round=2:reset_count=0",
               "-frames:v", "120", "-f", "null", "-"]
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                 creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as exc:
            self.hint.set(f"Detection failed: {exc}")
            return
        finally:
            self.config(cursor="")

        matches = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", out.stderr)
        if not matches:
            self.hint.set("Nothing detected here — try a brighter part of the video.")
            return
        w, h, x, y = (int(n) for n in matches[-1])
        self.trim_left.set(x)
        self.trim_top.set(y)
        self.trim_right.set(max(0, self.src_w - x - w))
        self.trim_bottom.set(max(0, self.src_h - y - h))
        self._draw_rect()
        if (w, h) == (self.src_w, self.src_h):
            self.hint.set("No borders found — this frame already fills the picture.")
        else:
            self.hint.set("Check a few other frames before applying — fades and dark scenes "
                          "can fool the detector into trimming real picture.")

    # --- crop maths ---------------------------------------------------------

    def _current_crop(self):
        """The four trims as ffmpeg's width:height:x:y, or None if nonsensical."""
        try:
            left, top = self.trim_left.get(), self.trim_top.get()
            right, bottom = self.trim_right.get(), self.trim_bottom.get()
        except tk.TclError:
            return None
        w, h = self.src_w - left - right, self.src_h - top - bottom
        if min(left, top, right, bottom) < 0 or w <= 0 or h <= 0:
            return None
        return w, h, left, top

    def _set_trims(self, left, top, right, bottom):
        even = lambda n: max(0, round(n) // 2 * 2)  # noqa: E731 - chroma needs even numbers
        self.trim_left.set(even(left))
        self.trim_top.set(even(top))
        self.trim_right.set(even(right))
        self.trim_bottom.set(even(bottom))
        self._draw_rect()

    # --- interaction --------------------------------------------------------

    def _update_time(self):
        seconds = int(self.position.get())
        self.time_label.config(text=f"{seconds // 60}:{seconds % 60:02d}")

    def _on_press(self, event):
        self.drag_origin = (self._clamp_x(event.x), self._clamp_y(event.y))

    def _on_drag(self, event):
        if not self.drag_origin:
            return
        x0, y0 = self.drag_origin
        x1, y1 = self._clamp_x(event.x), self._clamp_y(event.y)
        to_src = lambda n: n / self.factor  # noqa: E731
        self._set_trims(to_src(min(x0, x1)), to_src(min(y0, y1)),
                        self.src_w - to_src(max(x0, x1)), self.src_h - to_src(max(y0, y1)))

    def _on_release(self, _event):
        self.drag_origin = None

    def _clamp_x(self, x):
        return max(0, min(x, self.view_w))

    def _clamp_y(self, y):
        return max(0, min(y, self.view_h))

    def _draw_rect(self):
        crop = self._current_crop()
        self.canvas.delete("crop")
        if not crop:
            self.size_label.config(text="Those trims leave nothing behind.")
            return
        w, h, x, y = crop
        self.size_label.config(text=f"Keeping {w}×{h} of {self.src_w}×{self.src_h}")
        vx, vy = x * self.factor, y * self.factor
        vw, vh = w * self.factor, h * self.factor
        for coords in ((0, 0, self.view_w, vy),
                       (0, vy + vh, self.view_w, self.view_h),
                       (0, vy, vx, vy + vh),
                       (vx + vw, vy, self.view_w, vy + vh)):
            self.canvas.create_rectangle(*coords, fill="#000", stipple="gray50",
                                         outline="", tags="crop")
        self.canvas.create_rectangle(vx, vy, vx + vw, vy + vh, outline="#4da3ff",
                                     width=max(2, round(2 * self.scale)), tags="crop")

    # --- result -------------------------------------------------------------

    def reset(self):
        self._set_trims(0, 0, 0, 0)
        self.hint.set("Back to the whole frame.")

    def apply(self):
        crop = self._current_crop()
        if not crop:
            self.hint.set("Those trims don't leave a usable picture.")
            return
        w, h, x, y = crop
        self.result = None if (w, h) == (self.src_w, self.src_h) else crop
        self._close()

    def cancel(self):
        self.result = "cancelled"
        self._close()

    def _close(self):
        shutil.rmtree(self.tempdir, ignore_errors=True)
        self.destroy()


class ConverterApp(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=12)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)

        self.files: list[Path] = []
        self.scale = getattr(master, "ui_scale", 1.0)
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.process: subprocess.Popen | None = None
        self.cancelled = False

        self.ffmpeg = tk.StringVar(value=shutil.which("ffmpeg") or "")
        self.ffprobe = tk.StringVar(value=shutil.which("ffprobe") or "")
        self.container = tk.StringVar(value="MP4 (.mp4)")
        self.vcodec = tk.StringVar()
        self.acodec = tk.StringVar()
        self.quality = tk.IntVar(value=24)
        self.rate_mode = tk.StringVar(value="quality")
        self.bitrate = tk.StringVar(value="8M")
        self.ten_bit = tk.BooleanVar(value=True)
        self.depth_touched = False
        self.resolution = tk.StringVar(value="Same as source")
        self.framerate = tk.StringVar(value="Same as source")
        self.crop: tuple[int, int, int, int] | None = None
        self.abitrate = tk.StringVar(value="192k")
        self.channels = tk.StringVar(value="Stereo")
        self.audio_mode = tk.StringVar(value="quality")
        self.aquality = tk.StringVar(value="High")
        self.all_audio = tk.BooleanVar(value=True)
        self.keep_subs = tk.BooleanVar(value=True)
        nodes = render_nodes()
        self.hw_encoders: set[str] = set()
        self.ready = False
        self.vaapi_decode = tk.BooleanVar(value=True)
        self.vaapi_device = tk.StringVar(value=nodes[0] if nodes else "/dev/dri/renderD128")
        self.outdir = tk.StringVar(value="")
        self.same_folder = tk.BooleanVar(value=True)
        self.overwrite = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Add files to get started.")

        self._build_ui()
        self._on_container_change()
        for var in (self.container, self.vcodec, self.acodec,
                    self.resolution, self.framerate, self.abitrate, self.aquality,
                    self.bitrate, self.rate_mode, self.channels,
                    self.audio_mode, self.vaapi_device):
            var.trace_add("write", lambda *_: self._update_preview())
        self.quality.trace_add("write", lambda *_: self._update_preview())

        if not self.ffmpeg.get():
            self.status.set("ffmpeg not found. Set its location under Advanced.")
        self.after(100, self._drain_events)
        threading.Thread(target=self._detect_vaapi, daemon=True).start()

    # --- layout -------------------------------------------------------------

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.grid(row=0, column=0, sticky="nsew")

        left = ttk.Labelframe(panes, text="Files", padding=8)
        settings = ttk.Notebook(panes)
        panes.add(left, weight=3)
        panes.add(settings, weight=2)

        # File list
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(left, selectmode="extended", activestyle="none")
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.listbox.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.listbox.config(yscrollcommand=scroll.set)

        buttons = ttk.Frame(left)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(buttons, text="Add files…", command=self.add_files).pack(side="left")
        ttk.Button(buttons, text="Remove", command=self.remove_selected).pack(side="left", padx=6)
        ttk.Button(buttons, text="Clear", command=self.clear_files).pack(side="left")

        destination = ttk.Labelframe(left, text="Save to", padding=8)
        destination.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        destination.columnconfigure(0, weight=1)

        ttk.Checkbutton(destination, text="Next to the original file", variable=self.same_folder,
                        command=self._sync_enabled).grid(row=0, column=0, columnspan=2, sticky="w")

        self.outdir_entry = ttk.Entry(destination, textvariable=self.outdir)
        self.outdir_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.outdir_button = ttk.Button(destination, text="Choose…", command=self.choose_outdir)
        self.outdir_button.grid(row=1, column=1, padx=(6, 0), pady=(4, 0))

        ttk.Checkbutton(destination, text="Overwrite existing files", variable=self.overwrite
                        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # Settings, split across tabs so no one page is a wall of controls
        basic = ttk.Frame(settings, padding=10)
        video = ttk.Frame(settings, padding=10)
        audio = ttk.Frame(settings, padding=10)
        subs = ttk.Frame(settings, padding=10)
        advanced = ttk.Frame(settings, padding=10)
        for frame, title in ((basic, "Basic"), (video, "Video"), (audio, "Audio"),
                             (subs, "Subtitles"), (advanced, "Advanced")):
            frame.columnconfigure(1, weight=1)
            settings.add(frame, text=title)

        # --- Basic tab ---
        row = 0
        ttk.Label(basic, text="Convert to").grid(row=row, column=0, sticky="w", pady=3)
        self.container_box = ttk.Combobox(
            basic, textvariable=self.container, state="readonly",
            values=list(CONTAINERS), width=22)
        self.container_box.grid(row=row, column=1, sticky="ew", pady=3)
        self.container_box.bind("<<ComboboxSelected>>", lambda _e: self._on_container_change())
        row += 1

        ttk.Label(basic, text="Resolution").grid(row=row, column=0, sticky="w", pady=3)
        self.res_box = ttk.Combobox(basic, textvariable=self.resolution, state="readonly",
                                    values=list(RESOLUTIONS), width=22)
        self.res_box.grid(row=row, column=1, sticky="ew", pady=3)
        row += 1

        ttk.Label(basic, text="Frame rate").grid(row=row, column=0, sticky="w", pady=3)
        self.fps_box = ttk.Combobox(basic, textvariable=self.framerate, state="readonly",
                                    values=FRAMERATES, width=22)
        self.fps_box.grid(row=row, column=1, sticky="ew", pady=3)
        row += 1

        ttk.Label(basic, text="Crop").grid(row=row, column=0, sticky="w", pady=3)
        crop_row = ttk.Frame(basic)
        crop_row.grid(row=row, column=1, sticky="ew", pady=3)
        crop_row.columnconfigure(0, weight=1)
        self.crop_label = ttk.Label(crop_row, text="Whole frame")
        self.crop_label.grid(row=0, column=0, sticky="w")
        self.crop_button = ttk.Button(crop_row, text="Set…", width=6, command=self.open_crop)
        self.crop_button.grid(row=0, column=1)
        self.crop_clear = ttk.Button(crop_row, text="✕", width=3, command=self.clear_crop)
        self.crop_clear.grid(row=0, column=2, padx=(4, 0))

        # --- Video tab ---
        row = 0
        ttk.Label(video, text="Codec").grid(row=row, column=0, sticky="w", pady=3)
        self.vcodec_box = ttk.Combobox(video, textvariable=self.vcodec, state="readonly", width=22)
        self.vcodec_box.grid(row=row, column=1, sticky="ew", pady=3)
        self.vcodec_box.bind("<<ComboboxSelected>>", lambda _e: self._on_codec_change())
        row += 1

        self.depth_check = ttk.Checkbutton(video, text="Encode 10-bit", variable=self.ten_bit,
                                           command=self._on_depth_toggle)
        self.depth_check.grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        self.depth_hint = ttk.Label(video, text="", foreground="#666", justify="left")
        self.depth_hint.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        self.quality_label = ttk.Radiobutton(video, text="Quality (QP 24)",
                                             variable=self.rate_mode, value="quality",
                                             command=self._sync_enabled)
        self.quality_label.grid(row=row, column=0, sticky="w", pady=3)
        self.quality_scale = ttk.Scale(video, from_=1, to=51, orient="horizontal",
                                       command=self._on_quality)
        self.quality_scale.set(24)
        self.quality_scale.grid(row=row, column=1, sticky="ew", pady=3)
        row += 1

        ttk.Radiobutton(video, text="Target bitrate", variable=self.rate_mode, value="bitrate",
                        command=self._sync_enabled).grid(row=row, column=0, sticky="w", pady=3)
        self.bitrate_box = ttk.Combobox(video, textvariable=self.bitrate,
                                        values=VIDEO_BITRATES, width=22)
        self.bitrate_box.grid(row=row, column=1, sticky="ew", pady=3)
        self.bitrate_box.bind("<KeyRelease>", lambda _e: self._sync_enabled())
        self.bitrate_box.bind("<<ComboboxSelected>>", lambda _e: self._sync_enabled())
        row += 1

        self.quality_hint = ttk.Label(video, text="", foreground="#666", justify="left")
        self.quality_hint.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        ttk.Separator(video).grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1

        ttk.Label(video, text="GPU").grid(row=row, column=0, sticky="w", pady=3)
        device_row = ttk.Frame(video)
        device_row.grid(row=row, column=1, sticky="ew", pady=3)
        device_row.columnconfigure(0, weight=1)
        self.device_box = ttk.Combobox(device_row, textvariable=self.vaapi_device,
                                       values=render_nodes() or ["/dev/dri/renderD128"])
        self.device_box.grid(row=0, column=0, sticky="ew")
        self.test_button = ttk.Button(device_row, text="Test", width=6, command=self.test_vaapi)
        self.test_button.grid(row=0, column=1, padx=(4, 0))
        row += 1

        self.decode_check = ttk.Checkbutton(
            video, text="Decode on the GPU too (falls back automatically if unsupported)",
            variable=self.vaapi_decode, command=self._sync_enabled)
        self.decode_check.grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        self.hw_note = ttk.Label(video, text="Checking for VAAPI encoders…", foreground="#666",
                                 justify="left")
        self.hw_note.grid(row=row, column=0, columnspan=2, sticky="w")

        # --- Audio tab ---
        row = 0
        ttk.Label(audio, text="Codec").grid(row=row, column=0, sticky="w", pady=3)
        self.acodec_box = ttk.Combobox(audio, textvariable=self.acodec, state="readonly", width=22)
        self.acodec_box.grid(row=row, column=1, sticky="ew", pady=3)
        self.acodec_box.bind("<<ComboboxSelected>>", lambda _e: self._sync_enabled())
        row += 1

        ttk.Label(audio, text="Channels").grid(row=row, column=0, sticky="w", pady=3)
        self.channels_box = ttk.Combobox(audio, textvariable=self.channels, state="readonly",
                                         values=list(CHANNEL_MODES), width=22)
        self.channels_box.grid(row=row, column=1, sticky="ew", pady=3)
        self.channels_box.bind("<<ComboboxSelected>>", lambda _e: self._sync_enabled())
        row += 1

        self.channels_hint = ttk.Label(audio, text="", foreground="#666", justify="left")
        self.channels_hint.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        ttk.Radiobutton(audio, text="Quality", variable=self.audio_mode, value="quality",
                        command=self._sync_enabled).grid(row=row, column=0, sticky="w", pady=3)
        self.aq_box = ttk.Combobox(audio, textvariable=self.aquality, state="readonly",
                                   values=list(AUDIO_QUALITY), width=22)
        self.aq_box.grid(row=row, column=1, sticky="ew", pady=3)
        row += 1

        ttk.Radiobutton(audio, text="Fixed bitrate", variable=self.audio_mode, value="bitrate",
                        command=self._sync_enabled).grid(row=row, column=0, sticky="w", pady=3)
        self.abr_box = ttk.Combobox(audio, textvariable=self.abitrate, state="readonly",
                                    values=AUDIO_BITRATES, width=22)
        self.abr_box.grid(row=row, column=1, sticky="ew", pady=3)
        row += 1

        self.audio_hint = ttk.Label(audio, text="", foreground="#666", justify="left")
        self.audio_hint.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        ttk.Checkbutton(audio, text="Keep every audio track", variable=self.all_audio,
                        command=self._sync_enabled).grid(row=row, column=0, columnspan=2,
                                                         sticky="w", pady=(6, 0))

        # --- Subtitles tab ---
        ttk.Checkbutton(subs, text="Keep subtitle tracks", variable=self.keep_subs,
                        command=self._sync_enabled).grid(row=0, column=0, columnspan=2, sticky="w")
        self.tracks_hint = ttk.Label(subs, text="", foreground="#666", justify="left")
        self.tracks_hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # --- Advanced tab ---
        advanced.rowconfigure(4, weight=1)
        ttk.Label(advanced, text="ffmpeg").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(advanced, textvariable=self.ffmpeg).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(advanced, text="…", width=3,
                   command=self.choose_ffmpeg).grid(row=0, column=2)

        ttk.Label(advanced, text="ffprobe").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(advanced, textvariable=self.ffprobe).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(advanced, text="…", width=3,
                   command=self.choose_ffprobe).grid(row=1, column=2)

        ttk.Separator(advanced).grid(row=2, column=0, columnspan=3, sticky="ew", pady=10)

        header = ttk.Frame(advanced)
        header.grid(row=3, column=0, columnspan=3, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Command for the first queued file").grid(row=0, column=0,
                                                                        sticky="w")
        ttk.Button(header, text="Copy", width=6,
                   command=self.copy_command).grid(row=0, column=1)

        self.command_text = tk.Text(advanced, height=8, wrap="word", state="disabled",
                                    highlightthickness=1, highlightbackground="#999")
        self.command_text.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(4, 0))

        self.hint_labels = [self.depth_hint, self.quality_hint, self.hw_note,
                            self.channels_hint, self.audio_hint, self.tracks_hint]
        settings.bind("<Configure>", self._rewrap_hints)

        # Bottom: progress and controls
        bottom = ttk.Frame(self)
        bottom.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        bottom.columnconfigure(0, weight=1)

        self.file_bar = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.file_bar.grid(row=0, column=0, sticky="ew")
        self.start_button = ttk.Button(bottom, text="Start converting", command=self.start)
        self.start_button.grid(row=0, column=1, padx=(8, 0))

        self.overall_bar = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.overall_bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.cancel_button = ttk.Button(bottom, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_button.grid(row=1, column=1, padx=(8, 0), pady=(4, 0))

        ttk.Label(bottom, textvariable=self.status).grid(row=2, column=0, columnspan=2,
                                                         sticky="w", pady=(6, 0))

        log_frame = ttk.Labelframe(self, text="Log", padding=6)
        log_frame.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.log = tk.Text(log_frame, height=8, wrap="none", state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.config(yscrollcommand=log_scroll.set)

    # --- file handling ------------------------------------------------------

    def add_files(self):
        self.add_paths(filedialog.askopenfilenames(title="Add files", filetypes=MEDIA_TYPES))

    def add_paths(self, paths):
        """Queue files from the picker, the command line, or a file manager."""
        added = 0
        for raw in paths:
            path = Path(raw).expanduser()
            if path.is_file() and path not in self.files:
                self.files.append(path)
                self.listbox.insert("end", path.name)
                added += 1
        if added:
            self.status.set(f"{len(self.files)} file(s) queued.")
            self._update_preview()

    def remove_selected(self):
        for index in reversed(self.listbox.curselection()):
            self.listbox.delete(index)
            del self.files[index]
        self._update_preview()

    def clear_files(self):
        self.listbox.delete(0, "end")
        self.files.clear()
        self.status.set("Add files to get started.")
        self._update_preview()

    def choose_outdir(self):
        folder = filedialog.askdirectory(title="Choose output folder")
        if folder:
            self.outdir.set(folder)
            self.same_folder.set(False)
            self._sync_enabled()

    def choose_ffmpeg(self):
        path = filedialog.askopenfilename(title="Locate ffmpeg")
        if path:
            self.ffmpeg.set(path)
            probe = Path(path).with_name("ffprobe" + Path(path).suffix)
            if probe.exists() and not self.ffprobe.get():
                self.ffprobe.set(str(probe))
            self.status.set("ffmpeg set.")

    def choose_ffprobe(self):
        path = filedialog.askopenfilename(title="Locate ffprobe")
        if path:
            self.ffprobe.set(path)

    # --- option wiring ------------------------------------------------------

    def _rewrap_hints(self, event):
        """Wrap explanatory text to the panel's real width, not a guessed one."""
        width = max(event.width - 30, 140)
        for label in self.hint_labels:
            if label.cget("wraplength") != width:
                label.config(wraplength=width)

    def _on_quality(self, value):
        self.quality.set(int(float(value)))
        self.quality_label.config(text=f"Quality (QP {self.quality.get()})")

    def _on_depth_toggle(self):
        self.depth_touched = True  # stop steering it for them from here on
        self._sync_enabled()

    def _on_codec_change(self):
        self._apply_depth_default()
        self._sync_enabled()

    def _apply_depth_default(self):
        """AV1 hardware always does 10-bit, so default it on there and nowhere else."""
        if not self.depth_touched:
            self.ten_bit.set(codec_from_label(self.vcodec.get()) == "av1")

    def _rate_hint(self, reencoding: bool) -> str:
        if not reencoding:
            return ""
        if self.rate_mode.get() == "quality":
            return ("Constant quality: every file looks about the same, sizes vary. "
                    "Lower QP is better quality and a bigger file.")
        target = parse_bitrate(self.bitrate.get())
        if not target:
            return "Enter something like 8M, 8000k or 8000000."
        mb_per_min = target * 60 / 8 / 1_000_000
        return (f"Roughly {mb_per_min:.0f} MB per minute, peaking to "
                f"{target * 1.5 / 1_000_000:.1f}M in busy scenes.")

    def _depth_hint(self, codec: str) -> str:
        if codec in ("copy", ""):
            return ""
        if codec not in TEN_BIT_CAPABLE:
            return "No VAAPI driver encodes 10-bit H.264 — switch to HEVC or AV1 for that."
        if not self.ten_bit.get():
            return "8-bit plays everywhere. 10-bit mainly buys smoother gradients."
        if codec == "av1":
            return ("On by default: anything that encodes AV1 handles 10-bit too. Less banding "
                    "in skies and fades, even from an 8-bit source.")
        return ("Less banding in skies and fades, even from an 8-bit source. Needs a recent "
                "GPU and a player that handles 10-bit; falls back to 8-bit if the driver says no.")

    def _tracks_hint(self) -> str:
        if not self.keep_subs.get():
            return "Subtitles will be dropped."
        ext = CONTAINERS[self.container.get()]["ext"]
        if ext == ".mkv":
            return "MKV takes subtitles as they are, fonts included."
        if ext == ".mp4":
            return ("MP4 needs text subtitles converted to its own format. Image-based ones "
                    "(PGS, VobSub) can't convert — untick this if a file fails.")
        return "WebM only takes WebVTT. Anything else will fail to convert."

    def _channels_hint(self) -> str:
        mode = self.channels.get()
        if mode == "Keep original":
            return ("Surround is passed through. The rates below assume stereo, so a 5.1 track "
                    "needs roughly double to sound the same.")
        if mode == "Stereo":
            return "5.1 and 7.1 fold down to two channels. Already-stereo tracks are untouched."
        if mode == "Stereo (dialogue boost)":
            return ("Centre up 3 dB and surrounds down 3 dB before folding, so speech sits "
                    "forward instead of getting buried.")
        if mode == "Mono":
            return "One channel. Fine for speech, halves the size."
        return ("Surrounds are phase-encoded into the stereo pair so a Pro Logic decoder can "
                "recover them. Slightly odd on plain speakers.")

    def _audio_hint(self, codec: str) -> str:
        downmixing = self.channels.get() != "Keep original"
        if codec == "copy":
            if downmixing:
                return ("The original audio is kept untouched — downmixing needs re-encoding, "
                        "so the channel setting is ignored.")
            return "The original audio is kept untouched."
        if codec == "none":
            return "The result will have no sound."
        if codec == "libmp3lame" and not downmixing:
            return ("MP3 holds two channels at most — set Channels to Stereo or a 5.1 source "
                    "will fail to convert.")
        if codec == "flac":
            return "FLAC is lossless, so there's nothing to trade off."
        if self.audio_mode.get() == "bitrate":
            return f"Every file gets {self.abitrate.get()}, regardless of how hard it is to encode."
        option, value = AUDIO_QUALITY[self.aquality.get()][codec]
        if option == "-q:a":
            return (f"MP3 VBR V{value} — around {MP3_AVERAGE_KBPS[value]} kbps on average, "
                    f"spent where the music needs it.")
        return f"Variable rate averaging about {value}."

    def _on_container_change(self):
        spec = CONTAINERS[self.container.get()]
        video = spec["video"]
        if self.hw_encoders:
            video = [c for c in video if c == "copy" or ENCODERS[c] in self.hw_encoders]
        v_values = [label_for(c) for c in video]
        a_values = [label_for(c) for c in spec["audio"]]
        self.vcodec_box["values"] = v_values
        self.acodec_box["values"] = a_values
        if self.vcodec.get() not in v_values:
            self.vcodec.set(v_values[0] if v_values else "")
        if self.acodec.get() not in a_values:
            self.acodec.set(a_values[0] if a_values else "")
        self._apply_depth_default()
        self._sync_enabled()

    def _sync_enabled(self):
        v = codec_from_label(self.vcodec.get())
        a = codec_from_label(self.acodec.get())
        reencoding_video = v not in ("copy", "")

        by_quality = self.rate_mode.get() == "quality"
        self.quality_label.config(state="normal" if reencoding_video else "disabled")
        self.quality_scale.config(
            state="normal" if reencoding_video and by_quality else "disabled")
        self.bitrate_box.config(
            state="normal" if reencoding_video and not by_quality else "disabled")
        self.quality_hint.config(text=self._rate_hint(reencoding_video))
        for widget in (self.res_box, self.fps_box):
            widget.config(state="normal" if reencoding_video else "disabled")
        self.depth_check.config(
            state="normal" if reencoding_video and v in TEN_BIT_CAPABLE else "disabled")
        self.depth_hint.config(text=self._depth_hint(v))
        self.decode_check.config(
            state="normal" if reencoding_video and not self.crop else "disabled")
        self.crop_clear.config(state="normal" if self.crop else "disabled")

        self.channels_box.config(
            state="readonly" if a not in ("copy", "none", "") else "disabled")
        rate_applies = a not in ("copy", "none", "flac", "")
        self.aq_box.config(
            state="readonly" if rate_applies and self.audio_mode.get() == "quality" else "disabled")
        self.abr_box.config(
            state="readonly" if rate_applies and self.audio_mode.get() == "bitrate" else "disabled")
        self.channels_hint.config(text=self._channels_hint())
        self.audio_hint.config(text=self._audio_hint(a))
        self.tracks_hint.config(text=self._tracks_hint())
        self._update_preview()

    # --- command building ---------------------------------------------------

    def build_command(self, src: Path, dst: Path, sw_decode: bool = False,
                      force_8bit: bool = False) -> list[str]:
        spec = CONTAINERS[self.container.get()]
        v = codec_from_label(self.vcodec.get())
        a = codec_from_label(self.acodec.get())
        qp = str(self.quality.get())

        reencoding = v not in ("copy", "")
        hw_decode = (reencoding and self.vaapi_decode.get()
                     and not self.crop and not sw_decode)
        device = self.vaapi_device.get() or "/dev/dri/renderD128"
        height = RESOLUTIONS[self.resolution.get()]
        fps = self.framerate.get()
        crop = "crop={}:{}:{}:{}".format(*self.crop) if self.crop else None
        ten_bit = (reencoding and self.ten_bit.get() and not force_8bit
                   and v in TEN_BIT_CAPABLE)
        pix = "p010" if ten_bit else "nv12"

        cmd = [self.ffmpeg.get() or "ffmpeg", "-hide_banner", "-nostdin", "-y"]
        if hw_decode:
            cmd += ["-hwaccel", "vaapi", "-hwaccel_device", device,
                    "-hwaccel_output_format", "vaapi"]
        if reencoding:
            cmd += ["-vaapi_device", device]
        cmd += ["-i", str(src)]

        # ffmpeg otherwise picks just one stream per type, silently dropping the
        # other language tracks. Map them explicitly; '?' means "skip if absent".
        cmd += ["-map", "0:V:0"]
        if a != "none":
            cmd += ["-map", "0:a?" if self.all_audio.get() else "0:a:0?"]
        if self.keep_subs.get():
            cmd += ["-map", "0:s?", "-c:s", SUBTITLE_CODEC[spec["ext"]]]
            if spec["ext"] == ".mkv":
                cmd += ["-map", "0:t?", "-c:t", "copy"]  # attached fonts for ASS

        if not reencoding:
            cmd += ["-c:v", "copy"]
        else:
            # VAAPI encoders only accept frames already in GPU memory: either the
            # decoder put them there, or we upload them ourselves.
            filters = []
            if crop:
                filters.append(crop)
            if fps != "Same as source":
                filters.append(f"fps={fps}")
            if hw_decode:
                # Surfaces arrive in the source's own depth; normalise for the encoder.
                size = f"w=-2:h={height}:" if height else ""
                filters.append(f"scale_vaapi={size}format={pix}")
            else:
                if height:
                    filters.append(f"scale=-2:{height}")
                filters += [f"format={pix}", "hwupload"]
            if filters:
                cmd += ["-vf", ",".join(filters)]
            cmd += ["-c:v", ENCODERS[v]]
            target = parse_bitrate(self.bitrate.get())
            if self.rate_mode.get() == "bitrate" and target:
                # Peak above the target so busy scenes aren't starved; without a
                # maxrate ffmpeg would fall back to constant bitrate.
                cmd += ["-rc_mode", "VBR", "-b:v", str(target),
                        "-maxrate", str(int(target * 1.5))]
            else:
                cmd += ["-rc_mode", "CQP", "-qp", qp]
            if ten_bit and v == "hevc":
                cmd += ["-profile:v", "main10"]
            elif ten_bit and v == "vp9":
                cmd += ["-profile:v", "2"]

        if a == "none":
            cmd += ["-an"]
        elif a == "copy":
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", a]
            if a == "flac":
                pass  # lossless; rate options don't apply
            elif self.audio_mode.get() == "quality":
                cmd += list(AUDIO_QUALITY[self.aquality.get()][a])
            else:
                cmd += ["-b:a", self.abitrate.get()]
            cmd += CHANNEL_MODES[self.channels.get()]

        if spec["ext"] == ".mp4":
            cmd += ["-movflags", "+faststart"]

        cmd += ["-progress", "pipe:1", "-nostats", str(dst)]
        return cmd

    def target_path(self, src: Path) -> Path:
        ext = CONTAINERS[self.container.get()]["ext"]
        folder = src.parent if self.same_folder.get() else Path(self.outdir.get() or src.parent)
        dst = folder / (src.stem + ext)
        if dst.resolve() == src.resolve():
            dst = folder / (src.stem + "_converted" + ext)
        if dst.exists() and not self.overwrite.get():
            n = 2
            while (folder / f"{dst.stem}_{n}{ext}").exists():
                n += 1
            dst = folder / f"{dst.stem}_{n}{ext}"
        return dst

    def _update_preview(self):
        src = self.files[0] if self.files else Path("input.mp4")
        try:
            cmd = self.build_command(src, self.target_path(src) if self.files
                                     else Path("output" + CONTAINERS[self.container.get()]["ext"]))
        except (KeyError, OSError):
            return
        shown = [c for c in cmd if c not in ("-progress", "pipe:1", "-nostats")]
        text = " ".join(f'"{c}"' if " " in c else c for c in shown)
        self.command_text.config(state="normal")
        self.command_text.delete("1.0", "end")
        self.command_text.insert("1.0", text)
        self.command_text.config(state="disabled")

    def copy_command(self):
        self.clipboard_clear()
        self.clipboard_append(self.command_text.get("1.0", "end-1c"))
        self.status.set("Command copied to the clipboard.")

    def open_crop(self):
        selection = self.listbox.curselection()
        if not self.files:
            messagebox.showinfo(APP_TITLE, "Add a file first — the preview comes from one.")
            return
        source = self.files[selection[0]] if selection else self.files[0]
        dialog = CropDialog(self, source, self.crop)
        if not dialog.winfo_exists():
            return
        self.wait_window(dialog)
        if dialog.result != "cancelled":
            self.crop = dialog.result
            self._refresh_crop_label()

    def clear_crop(self):
        self.crop = None
        self._refresh_crop_label()

    def _refresh_crop_label(self):
        if self.crop:
            w, h, x, y = self.crop
            self.crop_label.config(text=f"{w}×{h} at {x},{y}")
        else:
            self.crop_label.config(text="Whole frame")
        self._sync_enabled()

    # --- VAAPI --------------------------------------------------------------

    def _detect_vaapi(self):
        """Ask ffmpeg which VAAPI encoders this build has, in the background."""
        found: set[str] = set()
        try:
            out = subprocess.run([self.ffmpeg.get() or "ffmpeg", "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=30,
                                 creationflags=NO_WINDOW)
            found = set(re.findall(r"\b(\w+_vaapi)\b", out.stdout))
        except (OSError, subprocess.SubprocessError):
            pass
        self.hw_encoders = found
        self.events.put(("hw", None))

    def _report_hw(self):
        nodes = render_nodes()
        self.device_box["values"] = nodes or ["/dev/dri/renderD128"]
        self.ready = bool(self.hw_encoders) and bool(nodes)
        if not sys.platform.startswith("linux"):
            self.hw_note.config(text="VAAPI is a Linux interface — this app can't encode here.")
        elif not self.hw_encoders:
            self.hw_note.config(text="This ffmpeg build has no VAAPI encoders, so nothing can "
                                     "be encoded. Install a build with VAAPI support.")
        elif not nodes:
            self.hw_note.config(text="No render node found in /dev/dri. Add yourself to the "
                                     "'render' group and log back in.")
        else:
            names = ", ".join(sorted(e.replace("_vaapi", "") for e in self.hw_encoders))
            self.hw_note.config(text=f"Your GPU offers: {names}. Press Test to confirm the "
                                     f"driver actually accepts the selected one.")
        self._on_container_change()

    def test_vaapi(self):
        codec = codec_from_label(self.vcodec.get())
        encoder = ENCODERS.get(codec, "h264_vaapi")
        device = self.vaapi_device.get()
        cmd = [self.ffmpeg.get() or "ffmpeg", "-hide_banner", "-nostdin", "-y",
               "-vaapi_device", device,
               "-f", "lavfi", "-i", "testsrc=size=640x480:rate=25:duration=1",
               "-vf", "format=nv12,hwupload", "-c:v", encoder, "-f", "null", "-"]

        def run():
            self.events.put(("log", f"$ {' '.join(cmd)}"))
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                     creationflags=NO_WINDOW)
            except (OSError, subprocess.SubprocessError) as exc:
                self.events.put(("status", f"Test failed: {exc}"))
                return
            if out.returncode == 0:
                self.events.put(("status", f"{encoder} works on {device}."))
            else:
                self.events.put(("log", out.stderr.strip()))
                self.events.put(("status", f"{encoder} failed on {device} — see the log."))

        threading.Thread(target=run, daemon=True).start()
        self.status.set(f"Testing {encoder} on {device}…")

    # --- running ------------------------------------------------------------

    def start(self):
        if not self.files:
            messagebox.showinfo(APP_TITLE, "Add at least one file first.")
            return
        if not (self.ffmpeg.get() or shutil.which("ffmpeg")):
            messagebox.showerror(APP_TITLE, "ffmpeg wasn't found. Set its location under Advanced.")
            return
        if (self.rate_mode.get() == "bitrate"
                and codec_from_label(self.vcodec.get()) != "copy"
                and parse_bitrate(self.bitrate.get()) is None):
            messagebox.showerror(APP_TITLE, "That bitrate doesn't look right. Try 8M, "
                                            "8000k or 8000000.")
            return
        if not self.ready and codec_from_label(self.vcodec.get()) != "copy":
            messagebox.showerror(
                APP_TITLE,
                "No usable VAAPI encoder. This app encodes on the GPU only — see the note "
                "under the GPU setting.\n\nYou can still repackage files by setting Video to "
                '"Keep as-is".')
            return
        if not self.same_folder.get() and not self.outdir.get():
            messagebox.showinfo(APP_TITLE, "Choose an output folder, or save next to the original.")
            return

        self.cancelled = False
        self.start_button.config(state="disabled")
        self.cancel_button.config(state="normal")
        self.overall_bar["value"] = 0
        self.file_bar["value"] = 0
        self.worker = threading.Thread(target=self._run_queue, args=(list(self.files),), daemon=True)
        self.worker.start()

    def cancel(self):
        self.cancelled = True
        self.status.set("Cancelling…")
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def _run_queue(self, files: list[Path]):
        done = failed = 0
        for i, src in enumerate(files):
            if self.cancelled:
                break
            self.events.put(("status", f"Converting {src.name} ({i + 1} of {len(files)})"))
            self.events.put(("file", 0))
            try:
                dst = self.target_path(src)
                ok = self._run_one(src, dst)
            except Exception as exc:  # noqa: BLE001 - surfaced in the log
                self.events.put(("log", f"[error] {src.name}: {exc}"))
                ok = False
            done += ok
            failed += not ok
            self.events.put(("overall", (i + 1) / len(files) * 100))
        self.events.put(("finished", (done, failed, self.cancelled)))

    def _probe_duration(self, src: Path) -> float:
        probe = self.ffprobe.get() or shutil.which("ffprobe")
        if not probe:
            return 0.0
        try:
            out = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(src)],
                capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
            return float(out.stdout.strip())
        except (ValueError, OSError, subprocess.SubprocessError):
            return 0.0

    def _probe_frame_rate(self, src: Path) -> float:
        """Source fps, used to turn ffmpeg's frame count into a percentage.

        Needed because ffmpeg's own out_time progress field stalls at N/A for
        most of the run whenever a sparse stream is mapped alongside it (PGS
        subtitles are the common case) — frame count keeps advancing.
        """
        probe = self.ffprobe.get() or shutil.which("ffprobe")
        if not probe:
            return 0.0
        try:
            out = subprocess.run(
                [probe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                 "-of", "default=nw=1:nk=1", str(src)],
                capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
            for line in out.stdout.strip().splitlines():
                num, _, den = line.partition("/")
                if int(den or 1) and (int(num) or 0):
                    return int(num) / int(den or 1)
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
        return 0.0

    def _run_one(self, src: Path, dst: Path) -> bool:
        dst.parent.mkdir(parents=True, exist_ok=True)
        duration = self._probe_duration(src)
        total_frames = duration * self._probe_frame_rate(src)
        reencoding = codec_from_label(self.vcodec.get()) not in ("copy", "")

        # Try as configured, then step down one capability at a time rather than
        # failing the file outright.
        attempts = [({}, "")]
        if reencoding and self.vaapi_decode.get() and not self.crop:
            attempts.append(({"sw_decode": True},
                             "the GPU couldn't decode this one, falling back to CPU decoding"))
        if reencoding and self.ten_bit.get():
            attempts.append(({"sw_decode": True, "force_8bit": True},
                             "10-bit encoding was refused, trying 8-bit"))

        previous = None
        for options, reason in attempts:
            cmd = self.build_command(src, dst, **options)
            if cmd == previous:
                continue
            previous = cmd
            if reason:
                self.events.put(("log", f"[retry] {src.name}: {reason}."))
            if self._execute(cmd, src, dst, duration, total_frames):
                return True
            if self.cancelled:
                return False
        return False

    def _execute(self, cmd: list[str], src: Path, dst: Path, duration: float,
                 total_frames: float = 0) -> bool:
        self.events.put(("log", "$ " + " ".join(cmd)))
        self.events.put(("file", 0))

        self.process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, creationflags=NO_WINDOW)

        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            if total_frames > 0 and line.startswith("frame="):
                try:
                    frame = int(line.split("=", 1)[1])
                    self.events.put(("file", min(frame / total_frames * 100, 100)))
                except ValueError:
                    pass
            elif total_frames <= 0 and line.startswith("out_time="):
                if duration > 0:
                    seconds = hms_to_seconds(line.split("=", 1)[1])
                    self.events.put(("file", min(seconds / duration * 100, 100)))
            elif line.startswith(("frame=", "fps=", "bitrate=", "total_size=", "speed=",
                                  "out_time=", "out_time_ms=", "out_time_us=", "dup_frames=",
                                  "drop_frames=", "stream_", "progress=")):
                continue
            else:
                self.events.put(("log", line))

        code = self.process.wait()
        self.process = None

        if self.cancelled:
            dst.unlink(missing_ok=True)
            self.events.put(("log", f"[cancelled] {src.name}"))
            return False
        if code != 0:
            self.events.put(("log", f"[failed] {src.name} — ffmpeg exited with {code}"))
            return False
        self.events.put(("file", 100))
        self.events.put(("log", f"[done] {dst}"))
        return True

    # --- UI updates from the worker ----------------------------------------

    def _drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "file":
                    self.file_bar["value"] = payload
                elif kind == "overall":
                    self.overall_bar["value"] = payload
                elif kind == "status":
                    self.status.set(payload)
                elif kind == "hw":
                    self._report_hw()
                elif kind == "finished":
                    self._on_finished(*payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _append_log(self, text: str):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        if float(self.log.index("end-1c").split(".")[0]) > 2000:
            self.log.delete("1.0", "500.0")
        self.log.see("end")
        self.log.config(state="disabled")

    def _on_finished(self, done: int, failed: int, cancelled: bool):
        self.start_button.config(state="normal")
        self.cancel_button.config(state="disabled")
        if cancelled:
            self.status.set(f"Stopped. {done} finished, {failed} not converted.")
        elif failed:
            self.status.set(f"{done} converted, {failed} failed — see the log.")
        else:
            self.status.set(f"{done} file(s) converted.")


def main():
    root = tk.Tk()
    root.title(APP_TITLE)
    style = ttk.Style()
    if "clam" in style.theme_names() and sys.platform.startswith("linux"):
        style.theme_use("clam")
    scale = detect_scale(root)
    apply_scaling(root, scale)
    root.ui_scale = scale
    root.geometry(f"{round(980 * scale)}x{round(720 * scale)}")
    root.minsize(round(820 * scale), round(600 * scale))
    app = ConverterApp(root)
    if len(sys.argv) > 1:
        app.add_paths(sys.argv[1:])
    root.mainloop()


if __name__ == "__main__":
    main()
