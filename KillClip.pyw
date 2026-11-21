"""KillClip – clippt FiveM-Kills automatisch (live über NVIDIA Instant Replay) und aus Aufnahmen."""
import base64
import hashlib
import json
import os
import re
import subprocess
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


def _unblock_bundle():
    """Entfernt die „aus dem Internet“-Markierung von den mitgelieferten DLLs.

    Aus einer heruntergeladenen ZIP entpackt, trägt jede Datei einen Zone.Identifier. .NET lädt
    pythonnets Python.Runtime.dll dann nicht ("Failed to resolve Python.Runtime.Loader.Initialize")
    und die Oberfläche startet nicht. Muss vor dem ersten Laden von pywebview/pythonnet laufen.
    """
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return
    for dirpath, _dirs, files in os.walk(base):
        for name in files:
            if name.lower().endswith((".dll", ".exe", ".pyd")):
                try:
                    os.remove(os.path.join(dirpath, name) + ":Zone.Identifier")
                except OSError:
                    pass   # nicht markiert oder kein Schreibrecht


_unblock_bundle()

import webview  # noqa: E402

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
UI_FILE = Path(getattr(sys, "_MEIPASS", ROOT)) / "killclip" / "ui" / "index.html"
ICON_FILE = Path(getattr(sys, "_MEIPASS", ROOT)) / "assets" / "killclip.ico"
APP_ID = "einfachfelix.KillClip"
WEB_DATA = Path(os.environ.get("LOCALAPPDATA", ROOT)) / "KillClip" / "webview"

from killclip import autoedit, beats, clips, link, marker, montage, ncs, obs, preview, titlecard  # noqa: E402
from killclip.batch import (KILLS_DIR, _load_manifest, find_clips, find_videos, is_own_extra,  # noqa: E402
                            retrim_many, scan_many)
from killclip.config import Config, key_name  # noqa: E402
from killclip.job_sort import job_dirs  # noqa: E402
from killclip.detector import list_presets  # noqa: E402
from killclip.live import LiveClipper  # noqa: E402
from killclip.media import MediaServer  # noqa: E402

THUMB_DIR = Path(os.environ.get("LOCALAPPDATA", ROOT)) / "KillClip" / "thumbs"
CLIP_NAME = re.compile(r"^(?P<label>kill\d*(?:_death)?|death)_(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")


SPECIAL_DIRS = {"montagen", "musik", "tode"}
CLIP_ROOTS: list = []   # Ausgabe- und Kills-Ordner (für „Job = Unterordner“)


def clip_info(path: Path, pre_s: float) -> dict | None:
    """Metadaten + Vorschaubild (gecacht) für die Galerie."""
    m = CLIP_NAME.match(path.stem)
    if not m:
        return None
    label = m["label"]
    kills = int(label[4:].split("_")[0] or 1) if label.startswith("kill") else 0
    when = datetime.strptime(m["ts"], "%Y-%m-%d_%H-%M-%S")
    try:
        length = clips.duration(path)
    except Exception:
        return None

    key = hashlib.sha1(f"{path}|{path.stat().st_mtime}".encode()).hexdigest()
    thumb_file = THUMB_DIR / f"{key}.jpg"
    if not thumb_file.exists():
        try:
            THUMB_DIR.mkdir(parents=True, exist_ok=True)
            thumb_file.write_bytes(clips.thumbnail(path, min(pre_s, max(0.0, length - 0.5))))
        except Exception:
            thumb_file = None
    thumb = None
    if thumb_file and thumb_file.exists():
        thumb = "data:image/jpeg;base64," + base64.b64encode(thumb_file.read_bytes()).decode()

    parent = path.parent
    job = parent.name if not parent.name.startswith("_") and parent.name.lower() not in SPECIAL_DIRS and \
        any(parent.parent == Path(r) for r in CLIP_ROOTS) else ""
    return {"path": str(path), "name": path.name, "label": label, "kills": kills, "job": job,
            "when": when.strftime("%d.%m.%Y · %H:%M:%S"), "sort": m["ts"],
            "duration": length, "thumb": thumb}


class Api:
    """Wird von der Oberfläche (JS) aufgerufen. Interne Felder mit _ sind für JS unsichtbar."""

    def __init__(self):
        self._cfg = Config.load()
        self._window = None
        self._live: LiveClipper | None = None
        self._cancel = threading.Event()
        self._scanning = False
        self._last_progress = 0.0
        self._media = MediaServer()
        self._known: set[str] = set()   # Clips, die die Oberfläche gerade anzeigt
        self._preview_cancel = threading.Event()
        preview.listen(lambda img, text: self._emit(type="detect", img=img, text=text))

    # --- nach Python -> JS ------------------------------------------------------------
    def _emit(self, **event):
        if self._window:
            try:
                self._window.evaluate_js(f"window.kc && kc.on({json.dumps(event, ensure_ascii=False)})")
            except Exception:
                pass

    def _log(self, msg):
        self._emit(type="log", msg=msg)

    def _status(self, text, on=False, hot=False):
        self._emit(type="status", text=text, on=on, hot=hot)

    def _settings(self):
        c = self._cfg
        return {"live_enabled": c.live_enabled, "clip_deaths": c.clip_deaths, "preset": c.preset,
                "presets": list_presets(), "pre_s": int(c.pre_s), "post_s": int(c.post_s),
                "replay_buffer_s": c.replay_buffer_s, "detect_mode": c.detect_mode,
                "clip_source": c.clip_source, "obs_password_set": bool(c.obs_password),
                "obs_local": bool(obs.local_settings()),
                "marker": self._marker_info(),
                "hotkey": [key_name(k) for k in c.replay_hotkey], "hotkey_alt": 0x12 in c.replay_hotkey}

    def _push_clip(self, path: Path, fresh=False):
        info = clip_info(path, self._cfg.pre_s)
        if info:
            self._known.add(info["path"])
            self._emit(type="clip", clip=info, fresh=fresh)

    def _watch_gone(self):
        # Im Explorer gelöschte/verschobene Clips aus Galerie und Montage nehmen
        while True:
            time.sleep(3)
            gone = [p for p in list(self._known) if not Path(p).is_file()]
            if gone:
                self._known.difference_update(gone)
                self._emit(type="clips_gone", paths=gone)

    # --- von JS ------------------------------------------------------------------------
    def ready(self):
        self._emit(type="settings", settings=self._settings())
        self._status("Live: aus")
        if self._cfg.live_enabled:
            self._start_live()

        def load_gallery():
            folders = [Path(self._cfg.output_dir), *map(Path, self._cfg.kill_folders)]
            folders += [clips.deaths_root(f) for f in folders]
            CLIP_ROOTS[:] = [str(f) for f in folders]
            files = set()
            for folder in dict.fromkeys(folders):
                if folder.is_dir():
                    files.update(p for d in (folder, *job_dirs(folder)) for p in d.glob("*.mp4"))
            # Kills und Tode getrennt begrenzen, damit viele Kills die Tode nicht verdrängen
            for death in (False, True):
                group = [f for f in files if clips.is_death_label(f.stem) == death]
                for f in sorted(group, key=lambda p: p.name, reverse=True)[:200]:
                    self._push_clip(f)

        threading.Thread(target=load_gallery, daemon=True).start()
        threading.Thread(target=self._watch_gone, daemon=True).start()

    def save_settings(self, changes: dict):
        for key in ("clip_deaths", "preset", "pre_s", "post_s", "detect_mode", "clip_source", "obs_password"):
            if key in changes:
                setattr(self._cfg, key, changes[key])
        self._cfg.save()
        if self._live:  # Live mit neuen Werten neu starten
            self._stop_live()
            self._start_live()

    def test_obs(self):
        """„OBS testen“: verbinden, Replay-Puffer starten (bei Bedarf einschalten), Länge melden."""
        try:
            with obs.ObsClient(port=self._cfg.obs_port, password=self._cfg.obs_password) as client:
                seconds = client.ensure_replay_buffer()
                version = client.request("GetVersion").get("obsVersion", "?")
        except (obs.ObsError, OSError) as exc:
            return {"ok": False, "msg": str(exc)}
        self._cfg.replay_buffer_s = seconds
        msg = f"OBS {version} verbunden – Replay-Puffer läuft ({seconds} s)"
        self._log(msg)
        return {"ok": True, "msg": msg, "seconds": seconds}

    def set_live(self, on: bool):
        self._cfg.live_enabled = bool(on)
        self._cfg.save()
        self._start_live() if on else self._stop_live()

    def pick_folder(self, deaths=False):
        res = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=self._cfg.replay_dir)
        if not res:
            return None
        folder = Path(res[0] if isinstance(res, (list, tuple)) else res)
        videos = self._scan_targets(find_videos(folder), deaths)
        manifest = _load_manifest(Path(self._cfg.output_dir), deaths)
        new = [v for v in videos if manifest.get(str(v)) != v.stat().st_size]
        out_name = str(Path(self._cfg.output_dir) / "Tode") if deaths else self._cfg.output_dir
        return {"path": str(folder), "name": folder.name or str(folder), "videos": len(videos),
                "new": len(new), "out_name": out_name}

    def pick_files(self):
        res = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=True, directory=self._cfg.replay_dir,
            file_types=("Videos (*.mp4;*.mkv;*.mov)", "Alle Dateien (*.*)"))
        if not res:
            return False
        files = [Path(f) for f in res]
        self._start_scan(files, Path(self._cfg.output_dir), skip_done=False)
        return True

    def pick_death_clips(self):
        """Modus „Tode suchen“: nur einzelne Clips/Videos, jeder eigene Tod wird ein eigener Clip."""
        res = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=True, directory=self._cfg.replay_dir,
            file_types=("Videos (*.mp4;*.mkv;*.mov)", "Alle Dateien (*.*)"))
        if not res:
            return False
        self._start_scan([Path(f) for f in res], Path(self._cfg.output_dir), skip_done=False, deaths_only=True)
        return True

    def scan_folder(self, folder: str, deaths=False):
        folder = Path(folder)
        self._start_scan(find_videos(folder), Path(self._cfg.output_dir), skip_done=True, deaths_only=bool(deaths))

    def _scan_targets(self, files: list[Path], deaths: bool) -> list[Path]:
        """Kill-Scan: nichts aus dem Ausgabeordner. Tode-Suche: Kill-Clips ja, eigene Tod-Clips/Backups nein."""
        out_dir = Path(self._cfg.output_dir).resolve()
        if deaths:
            return [p for p in files if not is_own_extra(p, out_dir)]
        return [p for p in files if not p.resolve().is_relative_to(out_dir)]

    def retrim_all(self):
        """Alle Clips der Galerie auf den aktuellen Vor-/Nachlauf kürzen."""
        if self._scanning:
            return False
        folders = dict.fromkeys(Path(f) for f in [self._cfg.output_dir, *self._cfg.kill_folders])
        files = sorted({c for f in folders if f.is_dir() for c in find_clips(f)})
        if not files:
            return False
        self._scanning = True
        self._cancel.clear()
        self._emit(type="scan_start")

        def work():
            self._log(f"Kürze {len(files)} Clip(s) auf {self._cfg.pre_s:.0f} s davor / {self._cfg.post_s:.0f} s danach")
            try:
                n = retrim_many(files, self._cfg, self._log, lambda p: self._emit(type="progress", **p),
                                self._cancel, on_clip=self._push_clip)
                msg = f"{n} Clip(s) gekürzt – Originale in _Original"
            except Exception as exc:
                msg = f"Fehler beim Kürzen: {exc}"
            self._scanning = False
            self._emit(type="scan_done", msg=msg)

        threading.Thread(target=work, daemon=True).start()
        return True

    # --- Montage -------------------------------------------------------------------------
    def media_url(self, path: str):
        """URL für <video>/<audio> (spulbar) – nur für Dateien, die die Oberfläche anfragt."""
        return self._media.url(path)

    def pick_music(self):
        res = self._window.create_file_dialog(
            webview.FileDialog.OPEN, file_types=("Musik (*.mp3;*.m4a;*.wav;*.ogg;*.flac;*.aac)", "Alle Dateien (*.*)"))
        if not res:
            return None
        path = Path(res[0] if isinstance(res, (list, tuple)) else res)
        return {"path": str(path), "name": path.name, "url": self._media.url(path)}

    def clip_kills(self, path: str):
        """Kill-Zeitpunkte in einem Clip (für die roten Marken im Clip-Player)."""
        try:
            kills = autoedit.kill_times(path, self._cfg.preset, self._cfg.pre_s, threading.Event())
            return {"ok": True, "kills": kills, "pre_s": self._cfg.pre_s, "post_s": self._cfg.post_s,
                    "url": self._media.url(path)}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    def plan_preview(self, items: list, music: str, style: str, speed: float = 1.0, intro: str = "teaser"):
        """Schnittplan für „Vorschau“ – Kills suchen dauert beim ersten Mal ~1 s pro Clip."""
        self._preview_cancel.set()
        self._preview_cancel = cancel = threading.Event()
        items = [{**it, "start": float(it["start"]), "end": float(it["end"] or 0)} for it in items
                 if Path(it["path"]).is_file()]
        items = [it for it in items if it["end"] - it["start"] >= 1.0]
        if not items:
            return {"ok": False, "msg": "Keine Clips in der Montage"}
        try:
            music = montage.sped_up(music, speed)
            plan = autoedit.preview_plan(items, music, style, self._cfg.preset, self._cfg.pre_s, cancel,
                                         lambda i, n, name: self._emit(type="plan_progress", i=i, n=n, name=name),
                                         intro)
        except Exception as exc:
            return {"ok": False, "msg": f"Vorschau fehlgeschlagen: {exc}"}
        if cancel.is_set():
            return {"ok": False, "msg": "abgebrochen"}
        for s in plan["shots"]:
            s["url"] = self._media.url(s["path"])
        return {"ok": True, **plan, "music_url": self._media.url(music)}

    def title_preview(self, text: str, items: list):
        """Standbild der Titelkarte für die Schnitt-Vorschau."""
        try:
            sub = autoedit.subtitle_for([{**it, "kills": it.get("kills") or [1]} for it in items])
            return {"ok": True, "img": titlecard.preview_png(text, sub, self._cfg.preset)}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    def analyze_music(self, path: str, speed: float = 1.0):
        """Tempo + Drop für die Anzeige unter der Musik (bei Sped Up für die schnelle Version)."""
        try:
            return {"ok": True, **beats.analyze(montage.sped_up(path, speed)).as_dict()}
        except Exception as exc:
            return {"ok": False, "msg": f"Beat-Erkennung fehlgeschlagen: {exc}"}

    def ncs_genres(self):
        return list(ncs.GENRES)

    def ncs_search(self, query: str, genre: str, page: int = 1):
        try:
            return {"ok": True, "tracks": ncs.search(query, genre, page)}
        except Exception as exc:
            return {"ok": False, "msg": f"NCS nicht erreichbar: {exc}"}

    def ncs_take(self, url: str, artist: str, track: str):
        try:
            path = ncs.download(url, artist, track, Path(self._cfg.output_dir) / "Musik")
        except Exception as exc:
            return {"ok": False, "msg": f"Download fehlgeschlagen: {exc}"}
        self._log(f"Musik geladen: {path.name} (Quellenangabe liegt daneben als .txt)")
        return {"ok": True, "path": str(path), "name": path.name, "url": self._media.url(path)}

    def music_link(self, url: str):
        """Musik von einem Link (SoundCloud & Co.) in den Musik-Ordner laden."""
        try:
            path = link.download(url, Path(self._cfg.output_dir) / "Musik")
        except Exception as exc:
            return {"ok": False, "msg": f"Link geht nicht: {exc}"}
        self._log(f"Musik geladen: {path.name}")
        return {"ok": True, "path": str(path), "name": path.name, "url": self._media.url(path)}

    def render_montage(self, items: list, music: str | None, music_vol: float, game_vol: float, auto=None):
        if self._scanning or not items:
            return False
        self._scanning = True
        self._cancel.clear()
        self._emit(type="scan_start")

        def work():
            path = None
            try:
                self._log(f"Montage: {len(items)} Clip(s){' + Musik' if music else ''}")
                opts = {**auto, "preset": self._cfg.preset, "pre_s": self._cfg.pre_s} if auto else None
                path = montage.render(items, Path(self._cfg.output_dir), music, float(music_vol), float(game_vol),
                                      self._log, lambda p: self._emit(type="progress", **p), self._cancel, opts)
                msg = f"Montage gespeichert: {path.name}" if path else "Montage abgebrochen"
            except Exception as exc:
                msg = f"Montage-Fehler: {exc}"
            self._scanning = False
            self._emit(type="scan_done", msg=msg)
            if path:
                self._emit(type="montage_done", path=str(path))

        threading.Thread(target=work, daemon=True).start()
        return True

    # --- Im Bild markieren (Kill-Notify / Killdot) -----------------------------------------
    def _marker_info(self):
        try:
            return marker.info(self._cfg.preset)
        except Exception:
            return {"notify": None, "killdot": False}

    def pick_video(self):
        res = self._window.create_file_dialog(
            webview.FileDialog.OPEN, directory=self._cfg.replay_dir,
            file_types=("Videos (*.mp4;*.mkv;*.mov)", "Alle Dateien (*.*)"))
        if not res:
            return None
        return str(res[0] if isinstance(res, (list, tuple)) else res)

    def marker_frame(self, path: str, t: float):
        try:
            return marker.frame_preview(path, float(t))
        except Exception as exc:
            return {"error": str(exc)}

    def save_marker(self, kind: str, path: str, t: float, box: list):
        try:
            msg = marker.save(self._cfg.preset, kind, path, float(t), box)
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}
        if self._cfg.detect_mode != "beide":      # bei „Beides“ bleibt es bei Beides
            self._cfg.detect_mode = "killdot" if kind == "killdot" else "notify"
        self._cfg.save()
        self._log(msg)
        self._emit(type="settings", settings=self._settings())
        if self._live:  # Live mit neuer Erkennung neu starten
            self._stop_live()
            self._start_live()
        return {"ok": True, "msg": msg}

    def cancel_scan(self):
        self._cancel.set()

    def open_output(self, deaths=False):
        folder = self._cfg.ensure_output()
        if deaths:
            folder = clips.deaths_root(folder)
            folder.mkdir(exist_ok=True)
        os.startfile(folder)

    def open_path(self, path: str):
        os.startfile(path)

    def reveal(self, path: str):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])

    # --- intern ------------------------------------------------------------------------
    def _start_scan(self, files: list[Path], out_dir: Path, skip_done: bool, deaths_only=False):
        out_dir = Path(self._cfg.output_dir).resolve()
        files = self._scan_targets(files, deaths_only)
        if self._scanning:
            return
        self._scanning = True
        self._cancel.clear()
        self._emit(type="scan_start")
        if str(out_dir) not in self._cfg.kill_folders:
            self._cfg.kill_folders = [str(out_dir), *self._cfg.kill_folders][:20]
            self._cfg.save()

        def progress(p):
            now = time.monotonic()
            if now - self._last_progress > 0.25 or p["pct"] >= 100:
                self._last_progress = now
                self._emit(type="progress", **p)

        def work():
            what = "Tode suchen" if deaths_only else "Scan"
            self._log(f"{what}: {len(files)} Video(s) → {out_dir}")
            try:
                n = scan_many(files, out_dir, self._cfg, self._log, progress, self._cancel,
                              on_clip=lambda p, _s: self._push_clip(p, fresh=True), skip_done=skip_done,
                              deaths_only=deaths_only)
                if self._cancel.is_set():
                    msg = f"{what} abgebrochen"
                elif deaths_only:
                    msg = f"Tode-Suche fertig – {n} Tod-Clip(s) in {out_dir.name}"
                else:
                    msg = f"Scan fertig – {n} Clip(s) in {out_dir.name}"
            except Exception as exc:
                msg = f"Scan-Fehler: {exc}"
            self._scanning = False
            self._emit(type="scan_done", msg=msg)

        threading.Thread(target=work, daemon=True).start()

    def _start_live(self):
        if self._live:
            return
        self._live = LiveClipper(self._cfg, self._log, self._status,
                                 on_clip=lambda p, _s: self._push_clip(p, fresh=True))
        self._live.start()

    def _stop_live(self):
        if self._live:
            self._live.stop()
            self._live.join(timeout=3)  # dxcam-Instanz freigeben, bevor eine neue startet
            self._live = None
        self._status("Live: aus")

    def _shutdown(self):
        self._cancel.set()
        self._media.close()
        if self._live:
            self._live.stop()


def self_test() -> str:
    """Für die gepackte EXE: prüft Abhängigkeiten ohne Fenster (Ergebnis in selftest.txt)."""
    from killclip import ocr
    from killclip.detector import Detector, Preset
    import dxcam  # noqa: F401
    import numpy as np
    lines = [f"ffmpeg: {clips.tool('ffmpeg')}", f"ffprobe: {clips.tool('ffprobe')}"]
    subprocess.run([clips.tool("ffmpeg"), "-version"], check=True, capture_output=True, creationflags=clips.NO_WINDOW)
    cfg = Config.load()
    Detector(Preset.load(cfg.preset), {"kill", "death"})
    Detector(Preset.load(cfg.preset), {"kill"}, "killdot").states[0].trigger.template_for(720)
    lines.append(f"preset: {cfg.preset} ok, ausgabe: {cfg.output_dir}")
    lines.append(f"ocr: {ocr.read_lines(np.zeros((60, 200, 3), np.uint8), 720)!r}")
    lines.append(f"ui: {UI_FILE.is_file()}  encoder: {montage.encoder()[1]}")
    import websocket  # noqa: F401  (OBS-Anbindung mit eingepackt?)
    lines.append(f"obs: modul ok, lokale OBS-Konfig: {bool(obs.local_settings())}")
    return "\n".join(lines)


def _use_app_icon():
    """Eigene Taskleisten-Gruppe statt Python-Symbol (pythonw)."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def _set_window_icon(window):
    """KillClip-Icon für Fenster + Taskleiste (WM_SETICON auf das WinForms-Fenster)."""
    if not ICON_FILE.is_file():
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)
        hwnd = ctypes.c_void_p(window.native.Handle.ToInt64())
        for which, size in ((1, 256), (0, 16)):         # ICON_BIG, ICON_SMALL
            # 0x10 = LR_LOADFROMFILE, 1 = IMAGE_ICON
            hicon = user32.LoadImageW(None, str(ICON_FILE), 1, size, size, 0x10)
            if hicon:
                user32.SendMessageW(hwnd, 0x0080, ctypes.c_void_p(which), ctypes.c_void_p(hicon))  # WM_SETICON
    except Exception:
        pass


def main():
    if "--self-test" in sys.argv:
        out = Path(sys.executable).parent / "selftest.txt"
        try:
            out.write_text("OK\n" + self_test(), encoding="utf-8")
        except Exception as exc:
            out.write_text(f"FEHLER: {exc!r}", encoding="utf-8")
        return
    _use_app_icon()
    api = Api()
    window = webview.create_window(
        "KillClip", url=str(UI_FILE), js_api=api,
        width=1200, height=780, min_size=(940, 620), background_color="#09090b",
        minimized="--minimized" in sys.argv)
    api._window = window
    window.events.closing += api._shutdown
    window.events.shown += lambda: _set_window_icon(window)

    # Videos per Drag & Drop auf die Verknüpfung -> direkt scannen
    dropped = [Path(a) for a in sys.argv[1:] if Path(a).is_file()]
    if dropped:
        window.events.loaded += lambda: api._start_scan(dropped, Path(api._cfg.output_dir), skip_done=False)

    WEB_DATA.mkdir(parents=True, exist_ok=True)
    # WebView hebt die Oberfläche im Cache auf – nach einem Update käme sonst die alte Seite
    shutil.rmtree(WEB_DATA / "EBWebView" / "Default" / "Cache", ignore_errors=True)
    webview.start(private_mode=False, storage_path=str(WEB_DATA))


if __name__ == "__main__":
    main()
