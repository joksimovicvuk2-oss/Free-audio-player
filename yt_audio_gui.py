import os, sys, re, glob, time, random, ctypes, threading
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk
import tkinter.messagebox as messagebox
import yt_dlp

DOWNLOAD_FOLDER = os.path.join(os.path.expanduser('~'), 'Downloads', 'PC_Audio_Library')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

def _resource_path(filename):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, filename)

winmm = ctypes.windll.winmm
winmm.mciSendStringW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, wintypes.HANDLE]
winmm.mciSendStringW.restype = ctypes.c_ulong

def _extract_id3_album(filepath):
    try:
        with open(filepath, "rb") as f:
            header = f.read(10)
        if len(header) < 10 or header[:3] != b"ID3":
            return None
        size = ((header[6] & 0x7F) << 21) | ((header[7] & 0x7F) << 14) | ((header[8] & 0x7F) << 7) | (header[9] & 0x7F)
        with open(filepath, "rb") as f:
            f.seek(10)
            tag_data = f.read(min(size, 262144))
        idx = tag_data.find(b"TALB")
        while idx != -1 and idx + 10 < len(tag_data):
            frame_size = int.from_bytes(tag_data[idx + 4:idx + 8], "big")
            if 1 < frame_size < 4096 and idx + 11 + frame_size <= len(tag_data):
                enc = tag_data[idx + 10]
                raw = tag_data[idx + 11:idx + 10 + frame_size]
                val = raw.decode("utf-16", errors="ignore").strip("\x00").strip() if enc in (1, 2) else raw.decode("latin-1", errors="ignore").strip("\x00").strip()
                if val:
                    return val
            idx = tag_data.find(b"TALB", idx + 4)
    except Exception:
        pass
    return None

def detect_album(filepath):
    id3_album = _extract_id3_album(filepath)
    if id3_album:
        return id3_album
    name = os.path.splitext(os.path.basename(filepath))[0]
    if "_" in name and name.rsplit("_", 1)[-1].isdigit():
        name = name.rsplit("_", 1)[0]
    if "[" in name and "]" in name:
        inside = name.split("[", 1)[1].split("]", 1)[0].strip()
        if inside and len(inside) <= 45:
            return inside
    if " - " in name:
        parts = [p.strip() for p in name.split(" - ") if p.strip()]
        if len(parts) >= 3:
            return parts[1][:45]
    return "Singles & Misc"

def _fmt_bytes(num_bytes):
    if not num_bytes or num_bytes <= 0:
        return "?"
    units = ["B", "KB", "MB", "GB"]
    val = float(num_bytes)
    for u in units:
        if val < 1024.0 or u == units[-1]:
            return f"{val:.1f} {u}"
        val /= 1024.0
    return f"{val:.1f} B"

def _fmt_time(secs):
    if secs is None or secs < 0:
        return "--:--"
    secs = int(secs)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

class InternalPlayer:
    def __init__(self):
        self.alias = "yt_bot_track"
        self.playing = False

    def _cmd(self, command):
        buf = ctypes.create_unicode_buffer(256)
        err = winmm.mciSendStringW(command, buf, 255, 0)
        if err != 0:
            err_buf = ctypes.create_unicode_buffer(256)
            winmm.mciGetErrorStringW(err, err_buf, 255)
            raise Exception(f"MCI error {err} ({err_buf.value}) on: {command}")
        return buf.value

    def stop(self):
        try:
            winmm.mciSendStringW(f"stop {self.alias}", None, 0, 0)
            winmm.mciSendStringW(f"close {self.alias}", None, 0, 0)
        except Exception:
            pass
        self.playing = False

    def status_mode(self):
        if not self.playing:
            return "stopped"
        try:
            return self._cmd(f"status {self.alias} mode").strip().lower()
        except Exception:
            return "stopped"

    def play(self, filepath):
        self.stop()
        short_path = ctypes.create_unicode_buffer(260)
        target = short_path.value if ctypes.windll.kernel32.GetShortPathNameW(filepath, short_path, 260) > 0 else filepath
        self._cmd(f'open "{target}" type mpegvideo alias {self.alias}')
        self._cmd(f"play {self.alias}")
        self.playing = True

class YTAudioGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Doom & Sound Effects Player")
        self.root.geometry("920x580")
        self.root.configure(bg="black")
        try:
            ico_p = _resource_path("doom_audio.ico")
            if os.path.exists(ico_p):
                self.root.iconbitmap(ico_p)
        except Exception:
            pass

        self.player = InternalPlayer()
        self.last_file = None
        self.all_songs = []
        self.song_albums = {}
        self.visible_paths = []
        self.playlist_queue = []
        self.playlist_index = -1
        self.loop_song = False
        self.loop_playlist = False
        self.group_by_album = True
        self._duration_sec = None

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        logo_cv = tk.Canvas(root, width=36, height=36, bg="black", highlightthickness=0, bd=0)
        logo_cv.create_oval(2, 2, 34, 34, fill="#b91c1c", outline="#ef4444", width=2)
        logo_cv.create_polygon(14, 10, 27, 18, 14, 26, fill="white")
        logo_cv.pack(pady=(10, 0))

        tk.Label(root, text="🔥 Doom & Audio Downloader", font=("Arial", 16, "bold"), fg="#ef4444", bg="black").pack(pady=(2, 6))

        self.tab_bar = tk.Frame(root, bg="black", bd=0, highlightthickness=0)
        self.tab_bar.pack(fill=tk.X, padx=15, pady=(0, 6))

        self.dl_tab_btn = tk.Button(self.tab_bar, text="⬇ Download & Play", font=("Arial", 10, "bold"), bg="#dc2626", fg="white", relief=tk.FLAT, bd=0, highlightthickness=0, padx=14, pady=6, command=lambda: self.show_tab("dl"))
        self.dl_tab_btn.pack(side=tk.LEFT, padx=(0, 4))

        self.lib_tab_btn = tk.Button(self.tab_bar, text="🎵 Library", font=("Arial", 10, "bold"), bg="#1a1a1a", fg="white", relief=tk.FLAT, bd=0, highlightthickness=0, padx=14, pady=6, command=lambda: self.show_tab("lib"))
        self.lib_tab_btn.pack(side=tk.LEFT)

        self.container = tk.Frame(root, bg="black", bd=0, highlightthickness=0)
        self.container.pack(fill=tk.BOTH, expand=True, padx=15, pady=(0, 12))

        self.download_tab = tk.Frame(self.container, bg="black", bd=0, highlightthickness=0)
        self.library_tab = tk.Frame(self.container, bg="black", bd=0, highlightthickness=0)
        self.download_tab.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.library_tab.place(relx=0, rely=0, relwidth=1, relheight=1)

        self._build_download_tab()
        self._build_library_tab()
        self.refresh_library()
        self.show_tab("dl")
        self._poll_playback()

    def show_tab(self, name):
        if name == "dl":
            self.download_tab.tkraise()
            self.dl_tab_btn.config(bg="#dc2626")
            self.lib_tab_btn.config(bg="#1a1a1a")
        else:
            self.refresh_library()
            self.library_tab.tkraise()
            self.lib_tab_btn.config(bg="#dc2626")
            self.dl_tab_btn.config(bg="#1a1a1a")

    def on_close(self):
        self.player.stop()
        self.root.destroy()

    def _build_download_tab(self):
        sf = tk.Frame(self.download_tab, bg="black")
        sf.pack(fill=tk.X, padx=20, pady=(15, 8))
        tk.Label(sf, text="Search Song / URL:", font=("Arial", 11, "bold"), fg="white", bg="black").pack(side=tk.LEFT, padx=(0, 10))
        self.entry = tk.Entry(sf, font=("Arial", 12), width=32)
        self.entry.pack(side=tk.LEFT, padx=5)
        self.entry.insert(0, "Doom E1M1")
        self.entry.bind("<Return>", lambda e: self.start_download())
        self.dl_btn = tk.Button(sf, text="Download & Play", font=("Arial", 11, "bold"), fg="white", bg="#dc2626", relief=tk.FLAT, padx=12, pady=4, command=self.start_download)
        self.dl_btn.pack(side=tk.LEFT, padx=10)

        pf = tk.Frame(self.download_tab, bg="#111111", padx=14, pady=10)
        pf.pack(fill=tk.X, padx=20, pady=(0, 6))
        top_p = tk.Frame(pf, bg="#111111")
        top_p.pack(fill=tk.X, pady=(0, 6))
        self.stage_lbl = tk.Label(top_p, text="Idle", font=("Arial", 10, "bold"), fg="#eab308", bg="#111111")
        self.stage_lbl.pack(side=tk.LEFT)
        self.pct_lbl = tk.Label(top_p, text="0.0%", font=("Arial", 10, "bold"), fg="#38bdf8", bg="#111111")
        self.pct_lbl.pack(side=tk.RIGHT)

        p_style = ttk.Style()
        p_style.theme_use("clam")
        p_style.configure("DL.Horizontal.TProgressbar", troughcolor="#27272a", background="#22c55e", bordercolor="#111111", lightcolor="#22c55e", darkcolor="#16a34a", thickness=18)
        self.progress_bar = ttk.Progressbar(pf, style="DL.Horizontal.TProgressbar", orient="horizontal", mode="determinate", maximum=100.0, value=0.0)
        self.progress_bar.pack(fill=tk.X)

        self.detail_lbl = tk.Label(pf, text="Waiting for a download...", font=("Consolas", 9), fg="#9ca3af", bg="#111111", anchor="w")
        self.detail_lbl.pack(fill=tk.X, pady=(6, 0))

        self.status_lbl = tk.Label(self.download_tab, text="Ready. Search for a track or open the Library tab to browse downloaded songs!", font=("Arial", 11), fg="#888", bg="#111111", justify="left", anchor="nw", padx=15, pady=15)
        self.status_lbl.pack(fill=tk.BOTH, expand=True, padx=20, pady=(4, 10))

        cf = tk.Frame(self.download_tab, bg="black")
        cf.pack(pady=10)
        tk.Button(cf, text="▶ Replay Last", font=("Arial", 10, "bold"), fg="white", bg="#1d4ed8", relief=tk.FLAT, padx=10, pady=6, command=self.replay_audio).pack(side=tk.LEFT, padx=6)
        tk.Button(cf, text="🔀 Shuffle Play", font=("Arial", 10, "bold"), fg="white", bg="#7c3aed", relief=tk.FLAT, padx=10, pady=6, command=self.shuffle_play).pack(side=tk.LEFT, padx=6)
        self.loop_song_btn = tk.Button(cf, text="🔁 Loop Song: OFF", font=("Arial", 10, "bold"), fg="white", bg="#27272a", relief=tk.FLAT, padx=10, pady=6, command=self.toggle_loop_song)
        self.loop_song_btn.pack(side=tk.LEFT, padx=6)
        self.loop_playlist_btn = tk.Button(cf, text="🔁 Loop Playlist: OFF", font=("Arial", 10, "bold"), fg="white", bg="#27272a", relief=tk.FLAT, padx=10, pady=6, command=self.toggle_loop_playlist)
        self.loop_playlist_btn.pack(side=tk.LEFT, padx=6)
        tk.Button(cf, text="⏹ Stop", font=("Arial", 10, "bold"), fg="white", bg="#b91c1c", relief=tk.FLAT, padx=10, pady=6, command=self.stop_audio).pack(side=tk.LEFT, padx=6)

    def _set_progress_ui(self, pct, stage, detail, stage_color="#eab308"):
        clamped = max(0.0, min(100.0, float(pct)))
        self.progress_bar["value"] = clamped
        self.pct_lbl.config(text=f"{clamped:.1f}%")
        self.stage_lbl.config(text=stage, fg=stage_color)
        self.detail_lbl.config(text=detail)

    def _build_library_tab(self):
        top_bar = tk.Frame(self.library_tab, bg="black")
        top_bar.pack(fill=tk.X, padx=15, pady=(12, 8))
        tk.Label(top_bar, text="🔎 Filter:", font=("Arial", 10, "bold"), fg="white", bg="black").pack(side=tk.LEFT, padx=(0, 6))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *args: self.filter_library())
        self.search_entry = tk.Entry(top_bar, textvariable=self.search_var, font=("Arial", 10), bg="#1e1e1e", fg="white", insertbackground="white", relief=tk.FLAT, width=22)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8), ipady=4)

        tk.Label(top_bar, text="💿 Album:", font=("Arial", 10, "bold"), fg="white", bg="black").pack(side=tk.LEFT, padx=(0, 6))
        self.album_var = tk.StringVar(value="All Albums")
        self.album_combo = ttk.Combobox(top_bar, textvariable=self.album_var, values=["All Albums"], state="readonly", font=("Arial", 10), width=22)
        self.album_combo.pack(side=tk.LEFT, padx=(0, 8))
        self.album_combo.bind("<<ComboboxSelected>>", lambda e: self.filter_library())

        self.group_btn = tk.Button(top_bar, text="🗂 Grouped: ON", font=("Arial", 9, "bold"), fg="white", bg="#4338ca", relief=tk.FLAT, padx=10, pady=4, command=self.toggle_group_mode)
        self.group_btn.pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(top_bar, text="Clear", font=("Arial", 9, "bold"), fg="white", bg="#334155", relief=tk.FLAT, padx=10, pady=4, command=self.clear_filters).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(top_bar, text="↻ Refresh", font=("Arial", 9, "bold"), fg="white", bg="#0f766e", relief=tk.FLAT, padx=10, pady=4, command=self.refresh_library).pack(side=tk.LEFT)

        list_frame = tk.Frame(self.library_tab, bg="#111111")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)
        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.song_listbox = tk.Listbox(list_frame, selectmode=tk.EXTENDED, font=("Arial", 11), bg="#111111", fg="#e5e7eb", selectbackground="#dc2626", selectforeground="white", activestyle="none", borderwidth=0, highlightthickness=0, yscrollcommand=scrollbar.set)
        self.song_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)
        scrollbar.config(command=self.song_listbox.yview)
        self.song_listbox.bind("<Double-Button-1>", lambda e: self.play_selected_song())
        self.song_listbox.bind("<Return>", lambda e: self.play_selected_song())
        self.song_listbox.bind("<Delete>", lambda e: self.delete_selected_song())

        bottom_bar = tk.Frame(self.library_tab, bg="black")
        bottom_bar.pack(fill=tk.X, padx=15, pady=(6, 12))
        self.lib_count_lbl = tk.Label(bottom_bar, text="0 tracks", font=("Arial", 10), fg="#9ca3af", bg="black")
        self.lib_count_lbl.pack(side=tk.LEFT)
        tk.Button(bottom_bar, text="🗑 Delete Selected", font=("Arial", 10, "bold"), fg="white", bg="#991b1b", relief=tk.FLAT, padx=12, pady=6, command=self.delete_selected_song).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(bottom_bar, text="⏹ Stop", font=("Arial", 10, "bold"), fg="white", bg="#b91c1c", relief=tk.FLAT, padx=12, pady=6, command=self.stop_audio).pack(side=tk.RIGHT, padx=(8, 0))
        self.lib_loop_pl_btn = tk.Button(bottom_bar, text="🔁 Loop Playlist: OFF", font=("Arial", 10, "bold"), fg="white", bg="#27272a", relief=tk.FLAT, padx=12, pady=6, command=self.toggle_loop_playlist)
        self.lib_loop_pl_btn.pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(bottom_bar, text="🔀 Shuffle Play", font=("Arial", 10, "bold"), fg="white", bg="#7c3aed", relief=tk.FLAT, padx=12, pady=6, command=self.shuffle_play).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(bottom_bar, text="▶ Play Selected", font=("Arial", 10, "bold"), fg="white", bg="#16a34a", relief=tk.FLAT, padx=12, pady=6, command=self.play_selected_song).pack(side=tk.RIGHT)

    def clear_filters(self):
        self.search_var.set("")
        self.album_var.set("All Albums")
        self.filter_library()

    def toggle_group_mode(self):
        self.group_by_album = not self.group_by_album
        self.group_btn.config(text="🗂 Grouped: ON" if self.group_by_album else "🗂 Grouped: OFF", bg="#4338ca" if self.group_by_album else "#27272a")
        self.filter_library()

    def _sync_loop_buttons(self):
        self.loop_song_btn.config(text="🔁 Loop Song: ON" if self.loop_song else "🔁 Loop Song: OFF", bg="#059669" if self.loop_song else "#27272a")
        pl_text = "🔁 Loop Playlist: ON" if self.loop_playlist else "🔁 Loop Playlist: OFF"
        pl_bg = "#d97706" if self.loop_playlist else "#27272a"
        self.loop_playlist_btn.config(text=pl_text, bg=pl_bg)
        self.lib_loop_pl_btn.config(text=pl_text, bg=pl_bg)

    def toggle_loop_song(self):
        self.loop_song = not self.loop_song
        if self.loop_song:
            self.loop_playlist = False
        self._sync_loop_buttons()

    def toggle_loop_playlist(self):
        self.loop_playlist = not self.loop_playlist
        if self.loop_playlist:
            self.loop_song = False
            selected_paths = self._get_selected_paths()
            self.playlist_queue = list(selected_paths) if len(selected_paths) > 1 else (list(self.visible_paths) if self.visible_paths else list(self.all_songs))
            if self.playlist_queue:
                self.playlist_index = self.playlist_queue.index(self.last_file) if self.last_file in self.playlist_queue else -1
                if not self.player.playing:
                    self._play_next_in_playlist()
        self._sync_loop_buttons()

    def _play_next_in_playlist(self):
        if not self.playlist_queue:
            self.playlist_queue = list(self.visible_paths) if self.visible_paths else list(self.all_songs)
        if not self.playlist_queue:
            return
        self.playlist_index = (self.playlist_index + 1) % len(self.playlist_queue)
        nxt = self.playlist_queue[self.playlist_index]
        if os.path.exists(nxt):
            self.last_file = nxt
            self.player.play(nxt)
            self.status_lbl.config(text=f"🔁 Playlist ({self.playlist_index + 1}/{len(self.playlist_queue)}):\n{os.path.basename(nxt)}", fg="#f59e0b")

    def _poll_playback(self):
        try:
            if self.player.playing and self.player.status_mode() != "playing":
                if self.loop_song and self.last_file and os.path.exists(self.last_file):
                    self.player.play(self.last_file)
                    self.status_lbl.config(text=f"🔁 Looping Song:\n{os.path.basename(self.last_file)}", fg="#10b981")
                elif self.loop_playlist:
                    self._play_next_in_playlist()
                else:
                    self.player.playing = False
        except Exception:
            pass
        self.root.after(750, self._poll_playback)

    def refresh_library(self):
        files = glob.glob(os.path.join(DOWNLOAD_FOLDER, "*.mp3"))
        files.sort(key=os.path.getmtime, reverse=True)
        self.all_songs = files
        self.song_albums = {f: detect_album(f) for f in files}
        albums_sorted = sorted(set(self.song_albums.values()), key=lambda s: s.lower())
        current_album = self.album_var.get()
        combo_values = ["All Albums"] + albums_sorted
        self.album_combo["values"] = combo_values
        if current_album not in combo_values:
            self.album_var.set("All Albums")
        self.filter_library()

    def filter_library(self):
        query = self.search_var.get().strip().lower()
        chosen_album = self.album_var.get()
        self.song_listbox.delete(0, tk.END)
        self.visible_paths = []
        matched = [
            f for f in self.all_songs
            if (chosen_album == "All Albums" or self.song_albums.get(f, "Singles & Misc") == chosen_album)
            and (not query or query in os.path.basename(f).lower() or query in self.song_albums.get(f, "Singles & Misc").lower())
        ]
        if self.group_by_album and chosen_album == "All Albums":
            by_alb, order = {}, []
            for f in matched:
                alb = self.song_albums.get(f, "Singles & Misc")
                if alb not in by_alb:
                    by_alb[alb] = []
                    order.append(alb)
                by_alb[alb].append(f)
            order.sort(key=lambda s: (s == "Singles & Misc", s.lower()))
            for alb in order:
                for f in by_alb[alb]:
                    self.visible_paths.append(f)
                    self.song_listbox.insert(tk.END, f"[{alb}]  {os.path.basename(f)}")
        else:
            for f in matched:
                alb = self.song_albums.get(f, "Singles & Misc")
                self.visible_paths.append(f)
                self.song_listbox.insert(tk.END, f"[{alb}]  {os.path.basename(f)}")

        total, shown = len(self.all_songs), len(self.visible_paths)
        if query or chosen_album != "All Albums":
            self.lib_count_lbl.config(text=f"Showing {shown} of {total} tracks ({chosen_album})")
        else:
            self.lib_count_lbl.config(text=f"{total} tracks across {len(set(self.song_albums.values()))} albums/groups")

    def _get_selected_paths(self):
        return [self.visible_paths[i] for i in self.song_listbox.curselection() if 0 <= i < len(self.visible_paths)]

    def delete_selected_song(self):
        selected_paths = self._get_selected_paths()
        if not selected_paths:
            messagebox.showinfo("Library", "Select one or more songs from the list first.")
            return
        if len(selected_paths) == 1 and not os.path.exists(selected_paths[0]):
            messagebox.showerror("Error", "File no longer exists on disk.")
            self.refresh_library()
            return
        prompt = f"Delete this track permanently?\n\n{os.path.basename(selected_paths[0])}" if len(selected_paths) == 1 else f"Delete {len(selected_paths)} selected tracks permanently?\n\n" + "\n".join(f"• {os.path.basename(p)}" for p in selected_paths[:8]) + (f"\n...and {len(selected_paths) - 8} more" if len(selected_paths) > 8 else "")
        if not messagebox.askyesno("Delete Song(s)", prompt):
            return
        deleted, errors = [], []
        for p in selected_paths:
            if not os.path.exists(p):
                continue
            try:
                if self.last_file == p:
                    self.player.stop()
                    self.last_file = None
                os.remove(p)
                if p in self.playlist_queue:
                    self.playlist_queue.remove(p)
                deleted.append(os.path.basename(p))
            except Exception as e:
                errors.append(f"{os.path.basename(p)}: {e}")
        self.refresh_library()
        if deleted:
            self.status_lbl.config(text=f"🗑 Deleted:\n{deleted[0]}" if len(deleted) == 1 else f"🗑 Deleted {len(deleted)} selected tracks.", fg="#f87171")
        if errors:
            messagebox.showerror("Delete Error", "\n".join(errors))

    def play_selected_song(self):
        selected_paths = self._get_selected_paths()
        if not selected_paths:
            messagebox.showinfo("Library", "Select a song from the list first.")
            return
        first_path = selected_paths[0]
        if not os.path.exists(first_path):
            messagebox.showerror("Error", "File no longer exists on disk.")
            self.refresh_library()
            return
        try:
            self.last_file = first_path
            if len(selected_paths) > 1:
                self.playlist_queue = [p for p in selected_paths if os.path.exists(p)]
                self.playlist_index = 0
                self.loop_playlist = True
                self._sync_loop_buttons()
            else:
                self.playlist_queue = list(self.visible_paths) if self.visible_paths else list(self.all_songs)
                self.playlist_index = self.playlist_queue.index(first_path) if first_path in self.playlist_queue else 0
            self.player.play(first_path)
            self.status_lbl.config(text=f"▶ Playing {len(selected_paths)} selected tracks (starting with):\n{os.path.basename(first_path)}" if len(selected_paths) > 1 else f"▶ Playing inside app:\n{os.path.basename(first_path)}", fg="#22c55e")
        except Exception as e:
            messagebox.showerror("Playback Error", str(e))

    def _parse_ffmpeg_time_sec(self, line):
        m = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None

    def start_download(self):
        query = self.entry.get().strip()
        if not query:
            return
        self._duration_sec = None
        self.dl_btn.config(state=tk.DISABLED, bg="#475569")
        self._set_progress_ui(0.0, "1/2 • Downloading", "Fetching video/audio info...", "#eab308")
        self.status_lbl.config(text=f"⏳ Downloading '{query}'...", fg="#eab308")
        self.root.update()

        def dl_progress_hook(d):
            info_d = d.get("info_dict") or {}
            if info_d.get("duration"):
                try:
                    self._duration_sec = float(info_d["duration"])
                except Exception:
                    pass
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                speed, eta = d.get("speed"), d.get("eta")
                if total > 0:
                    raw_pct = (downloaded / total) * 100.0
                else:
                    pct_str = re.sub(r"\x1b\[[0-9;]*m", "", str(d.get("_percent_str", "0%"))).replace("%", "").strip()
                    try:
                        raw_pct = float(pct_str)
                    except Exception:
                        raw_pct = 0.0
                overall_pct = min(75.0, max(0.0, raw_pct * 0.75))
                detail = f"Download: {raw_pct:.1f}% ({_fmt_bytes(downloaded)} / {_fmt_bytes(total)}) | Speed: {(_fmt_bytes(speed) + '/s') if speed else '?/s'} | ETA: {_fmt_time(eta) if eta is not None else '--:--'}"
                self.root.after(0, lambda p=overall_pct, det=detail: self._set_progress_ui(p, "1/2 • Downloading Audio", det, "#eab308"))
            elif status == "finished":
                self.root.after(0, lambda: self._set_progress_ui(75.0, "2/2 • FFmpeg Converting", "Download complete (100% of stream). Starting FFmpeg MP3 encode...", "#38bdf8"))

        class FFmpegProgressLogger:
            def __init__(self, gui):
                self.gui = gui
            def debug(self, msg):
                if not msg:
                    return
                clean = re.sub(r"\x1b\[[0-9;]*m", "", str(msg))
                if "time=" in clean:
                    cur_t = self.gui._parse_ffmpeg_time_sec(clean)
                    if cur_t is not None:
                        dur = self.gui._duration_sec
                        spd_m = re.search(r"speed=\s*([0-9.]+)x", clean)
                        spd_x = float(spd_m.group(1)) if spd_m else None
                        spd_txt = f"{spd_x:.2f}x" if spd_x else "?x"
                        if dur and dur > 0:
                            ff_pct = min(100.0, max(0.0, (cur_t / dur) * 100.0))
                            overall_pct = 75.0 + (ff_pct * 0.25)
                            rem_s = ((dur - cur_t) / spd_x) if (spd_x and spd_x > 0) else None
                            det = f"FFmpeg MP3: {ff_pct:.1f}% ({_fmt_time(cur_t)} / {_fmt_time(dur)}) | Speed: {spd_txt} | ETA: {_fmt_time(rem_s) if rem_s is not None else '--:--'}"
                        else:
                            overall_pct = 85.0
                            det = f"FFmpeg MP3 encoded: {_fmt_time(cur_t)} | Speed: {spd_txt}"
                        self.gui.root.after(0, lambda p=overall_pct, d_txt=det: self.gui._set_progress_ui(p, "2/2 • FFmpeg Converting to MP3", d_txt, "#38bdf8"))
            def warning(self, msg):
                pass
            def error(self, msg):
                pass

        def bg_worker():
            try:
                unique_id = int(time.time())
                target_url = query if query.startswith("http") else f"ytsearch1:{query}"
                ydl_opts = {
                    'format': 'bestaudio',
                    'socket_timeout': 15,
                    'outtmpl': os.path.join(DOWNLOAD_FOLDER, f'%(title)s_{unique_id}.%(ext)s'),
                    'progress_hooks': [dl_progress_hook],
                    'logger': FFmpegProgressLogger(self),
                    'postprocessor_args': {'ffmpeg': ['-loglevel', 'info', '-stats']},
                    'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '128'}],
                    'quiet': True,
                    'no_warnings': True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(target_url, download=True)
                    video_info = info['entries'][0] if 'entries' in info else info
                    title = video_info.get('title', 'Unknown')
                    if video_info.get("duration"):
                        try:
                            self._duration_sec = float(video_info["duration"])
                        except Exception:
                            pass
                matches = glob.glob(os.path.join(DOWNLOAD_FOLDER, f'*_{unique_id}.mp3'))
                if not matches:
                    raise Exception("Audio conversion completed, but MP3 file could not be found.")
                self.last_file = max(matches, key=os.path.getctime)

                def finish_ok():
                    mp3_size = _fmt_bytes(os.path.getsize(self.last_file)) if os.path.exists(self.last_file) else "?"
                    self._set_progress_ui(100.0, "Done • Ready & Playing", f"Completed ({mp3_size}) — {os.path.basename(self.last_file)}", "#22c55e")
                    try:
                        self.player.play(self.last_file)
                        play_status = "▶ Playing inside app!"
                    except Exception as pe:
                        play_status = f"⚠ Downloaded, but internal play failed: {pe}"
                    self.status_lbl.config(text=f"✅ Success!\n\nTitle: {title}\nSize: {mp3_size}\nFolder: {DOWNLOAD_FOLDER}\n\n{play_status}", fg="#3b82f6")
                    self.dl_btn.config(state=tk.NORMAL, bg="#dc2626")
                    self.refresh_library()

                self.root.after(0, finish_ok)
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda: [
                    self._set_progress_ui(0.0, "Error", err_msg[:120], "#ff5555"),
                    self.status_lbl.config(text="❌ Error during download/conversion.", fg="#ff5555"),
                    self.dl_btn.config(state=tk.NORMAL, bg="#dc2626"),
                    messagebox.showerror("Error", err_msg)
                ])

        threading.Thread(target=bg_worker, daemon=True).start()

    def replay_audio(self):
        if self.last_file and os.path.exists(self.last_file):
            try:
                self.player.play(self.last_file)
                self.status_lbl.config(text=f"▶ Replaying inside app:\n{os.path.basename(self.last_file)}", fg="#3b82f6")
            except Exception as e:
                messagebox.showerror("Playback Error", str(e))

    def shuffle_play(self):
        pool = self.visible_paths if self.visible_paths else glob.glob(os.path.join(DOWNLOAD_FOLDER, "*.mp3"))
        if not pool:
            messagebox.showinfo("Shuffle", "No downloaded MP3 files found in your library yet!")
            return
        chosen = random.choice(pool)
        try:
            self.last_file = chosen
            self.playlist_queue = list(pool)
            random.shuffle(self.playlist_queue)
            self.playlist_index = self.playlist_queue.index(chosen)
            self.player.play(chosen)
            self.status_lbl.config(text=f"🔀 Shuffle Playing inside app:\n{os.path.basename(chosen)}", fg="#a855f7")
        except Exception as e:
            messagebox.showerror("Playback Error", str(e))

    def stop_audio(self):
        self.loop_song = False
        self.loop_playlist = False
        self._sync_loop_buttons()
        self.player.stop()
        self.status_lbl.config(text="⏹ Playback stopped.", fg="#9ca3af")

if __name__ == "__main__":
    r = tk.Tk()
    YTAudioGUI(r)
    r.mainloop()
