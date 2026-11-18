"""
Sort IT — ядро обработки и Windows-приложения.
Основной интерфейс запускается через photo_helper_web.py.

Умеет:
  1. Очистка — найти точные дубли, похожие кадры и смазанные фото.
  2. План сортировки — проанализировать фото и видео и сохранить
     редактируемый план тематических папок без перемещения исходников.

Запуск:  py photo_helper_v2.py
Нужные библиотеки:  py -m pip install opencv-python-headless pillow pillow-heif numpy
"""

import base64
import csv
import hashlib
import html
import http.server
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from io import BytesIO
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
except ImportError as exc:
    # Серверный HTML-режим работает без графического дисплея.
    # Не создаём Tk-окно при импорте в Docker/headless-среде.
    if os.environ.get("SORT_IT_HEADLESS") == "1" or not os.environ.get("DISPLAY"):
        raise RuntimeError(
            "Не удалось загрузить библиотеки обработки изображений: "
            f"{exc}. Установите зависимости из requirements.txt.") from exc
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "Не хватает библиотек",
        "Выполни в PowerShell:\n\n"
        "py -m pip install opencv-python-headless pillow pillow-heif numpy\n\n"
        "и запусти программу снова.",
    )
    sys.exit(1)

try:
    import pillow_heif  # чтобы открывались фото с iPhone (HEIC)
    pillow_heif.register_heif_opener()
except ImportError:
    pass

EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
PHOTO_EXTENSIONS = EXTENSIONS
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".3gp", ".webm", ".mts", ".m2ts"}
MEDIA_EXTENSIONS = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS
SETTINGS_FILE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Sort IT" / "settings.json"
POP = np.array([bin(i).count("1") for i in range(65536)], dtype=np.uint8)

PROMPT = (
    "Это несколько фотографий из одной подборки. Предложи короткое название "
    "папки на русском языке (2-4 слова), которое описывает общую тему или событие. "
    "Если это скриншоты интерфейса, сайта или приложения, ответь строго Screenshots. "
    "Ответь только названием, без кавычек и пояснений."
)


# ============================ настройки ============================

@dataclass
class Config:
    mode: str = "clean"                 # clean / plan
    photos_dir: str = ""
    trash_dir: str = ""
    sorted_dir: str = ""
    screenshots_dir: str = ""
    plan_dir: str = ""
    # очистка
    do_exact: bool = True
    do_similar: bool = True
    do_blur: bool = True
    blur_threshold: int = 30
    similar_threshold: int = 5
    # план
    use_ai: bool = True
    analyze_photos: bool = True
    analyze_videos: bool = True
    exclude_system_files: bool = True
    ollama_model: str = "qwen3.5:9b"
    ollama_url: str = "http://127.0.0.1:11434/api/generate"
    # План сортировки
    # Названия тематических категорий генерируются ИИ автоматически.


def load_settings():
    cfg = Config()
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        for f in fields(Config):
            if f.name in data:
                setattr(cfg, f.name, data[f.name])
    except Exception:
        pass
    cfg.ollama_model = "qwen3.5:9b"
    if cfg.ollama_url.casefold().startswith("http://localhost:"):
        cfg.ollama_url = cfg.ollama_url.replace("localhost", "127.0.0.1", 1)
    cfg.use_ai = True
    cfg.analyze_photos = True
    cfg.analyze_videos = True
    cfg.exclude_system_files = True
    return cfg


def save_settings(cfg):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


_gallery_servers = []


class GalleryHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, allowed=None, **kwargs):
        self.allowed = {Path(p).resolve() for p in (allowed or [])}
        super().__init__(*args, directory=directory, **kwargs)

    def do_POST(self):
        if self.path not in {"/delete", "/api/clean/delete"}:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            selected = [Path(p).resolve() for p in payload.get("paths", [])]
            deleted, missing, deleted_paths = 0, 0, []
            for path in selected:
                if path not in self.allowed:
                    continue
                if path.is_file():
                    path.unlink()
                    deleted += 1
                    deleted_paths.append(str(path))
                else:
                    missing += 1
            body = json.dumps({"deleted": deleted, "missing": missing,
                               "paths": deleted_paths}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            body = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *_args):
        pass

def open_gallery(trash, allowed):
    handler = lambda *args, **kwargs: GalleryHandler(
        *args, directory=str(trash), allowed=allowed, **kwargs)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    _gallery_servers.append(server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    open_path(f"http://127.0.0.1:{server.server_port}/обзор.html")



    pass


def file_hash(path):
    """SHA-256 of file bytes; names alone never classify duplicates."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path):
    """Читает таблицу, сохранённую из Excel (UTF-8 или обычная Windows-кодировка)."""
    for enc in ("utf-8-sig", "cp1251"):
        try:
            with open(path, newline="", encoding=enc) as f:
                return list(csv.reader(f, delimiter=";"))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Не удалось прочитать таблицу {path}")


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f, delimiter=";").writerows(rows)


def sanitize(name):
    """Убирает символы, которые нельзя использовать в названии папки."""
    return re.sub(r'[\\/:*?"<>|«»]', "", name).strip(" .")[:60]


def transfer(src, dst_dir, copy=False):
    """Переносит или копирует файл, не затирая одноимённые."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    n = 1
    while dst.exists():
        dst = dst_dir / f"{src.stem}_{n}{src.suffix}"
        n += 1
    if copy:
        shutil.copy2(str(src), str(dst))
    else:
        shutil.move(str(src), str(dst))
    return dst


def analyze(path):
    """Возвращает (резкость, хэш внешнего вида). Резкость: меньше = более смазано."""
    img = Image.open(path)
    img.draft("L", (1000, 1000))          # ускоряет чтение больших JPEG
    img = img.convert("L")
    img.thumbnail((1000, 1000))           # одинаковый размер для честного сравнения
    score = float(cv2.Laplacian(np.array(img), cv2.CV_64F).var())

    small = np.array(img.resize((9, 8), Image.LANCZOS), dtype=np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    dhash = int.from_bytes(np.packbits(bits).tobytes(), "big")
    return score, dhash


def hamming_all(h, arr):
    """Сколько бит отличается между хэшем h и каждым хэшем из массива."""
    x = arr ^ h
    total = np.zeros(len(arr), dtype=np.uint8)
    for shift in (0, 16, 32, 48):
        part = (x >> np.uint64(shift)) & np.uint64(0xFFFF)
        total += POP[part.astype(np.intp)]
    return total


def get_date(path):
    """Дата съёмки из самого файла, а если её нет, то дата изменения файла."""
    try:
        exif = Image.open(path).getexif()
        raw = None
        try:
            ifd = exif.get_ifd(0x8769)
            raw = ifd.get(36867) or ifd.get(36868)
        except Exception:
            pass
        raw = raw or exif.get(306)
        if raw:
            return datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime)


class Helper:
    def __init__(self, cfg, log, progress, stop_event):
        self.cfg = cfg
        self.log = log
        self.progress = progress
        self.stop = stop_event
        self.ai_ok = True

    def check(self):
        if self.stop.is_set():
            raise Cancelled()

    def tick(self, i, total):
        if i % 10 == 0 or i == total:
            self.progress(i, total)

    def list_photos(self):
        c = self.cfg
        root = Path(c.photos_dir)
        excluded = [Path(v) for v in (c.trash_dir, c.plan_dir) if v]
        ignored_dirs = {"$recycle.bin", "system volume information", "__macosx", ".trash", "@eaDir"}
        ignored_suffixes = {".tmp", ".temp", ".part", ".crdownload", ".db", ".ini"}
        return sorted(
            p for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in EXTENSIONS
            and not any(x == p or x in p.parents for x in excluded)
            and not any(part.casefold() in ignored_dirs for part in p.relative_to(root).parts)
            and p.suffix.lower() not in ignored_suffixes
            and not p.name.startswith((".~", "~$"))
        )

    def clean(self):
        c = self.cfg
        trash = Path(c.trash_dir)
        files = self.list_photos()
        total = len(files)
        self.log(f"Найдено фото: {total}")

        seen, items, rows = {}, [], []
        need_analysis = c.do_similar or c.do_blur

        for i, path in enumerate(files, 1):
            self.check()
            self.tick(i, total)
            try:
                if c.do_exact:
                    h = file_hash(path)
                    if h in seen:
                        rows.append((path, "точный дубль", f"копия {seen[h].name}", "", seen[h]))
                        continue
                    seen[h] = path
                if need_analysis:
                    score, dhash = analyze(path)
                    items.append({"path": path, "score": score,
                                  "dhash": dhash, "size": path.stat().st_size})
            except Exception as e:
                self.log(f"Пропущен {path.name}: {e}")

        flagged = set()
        if c.do_similar and items:
            self.log("Ищу похожие кадры...")
            hashes = np.array([it["dhash"] for it in items], dtype=np.uint64)
            assigned = np.zeros(len(items), dtype=bool)
            for i in range(len(items)):
                if i % 200 == 0:
                    self.check()
                if assigned[i]:
                    continue
                dist = hamming_all(hashes[i], hashes)
                members = np.where((dist <= c.similar_threshold) & ~assigned)[0]
                assigned[members] = True
                if len(members) > 1:
                    best = max(members, key=lambda k: (items[k]["score"], items[k]["size"]))
                    for k in members:
                        if k != best:
                            rows.append((items[k]["path"], "похожий кадр",
                                         f"лучше: {items[best]['path'].name}", "",
                                         items[best]["path"]))
                            flagged.add(items[k]["path"])

        if c.do_blur:
            for it in items:
                if it["path"] not in flagged and it["score"] < c.blur_threshold:
                    sc = int(round(it["score"]))
                    rows.append((it["path"], "смазанное", f"резкость {sc}", sc, None))

        # Если «оставленный» файл сам признан лишним (например, он тоже похож на
        # другой кадр), показываем в паре настоящий эталон, а не его
        kept_of = {p: k for p, _, _, _, k in rows}

        def final_kept(k):
            visited = set()
            while k in kept_of and kept_of[k] is not None and k not in visited:
                visited.add(k)
                k = kept_of[k]
            return k

        rows = [(p, r, d, sc, final_kept(k) if k else None) for p, r, d, sc, k in rows]

        trash.mkdir(parents=True, exist_ok=True)
        report = trash / "report.csv"
        order = {"смазанное": 0, "похожий кадр": 1, "точный дубль": 2}
        rows.sort(key=lambda r: (order[r[1]], r[3] if r[3] != "" else 0))
        write_csv(report, [["Файл", "Причина", "Детали", "Резкость", "Оставлен"]] +
                  [[str(p), r, d, sc, str(k) if k else ""] for p, r, d, sc, k in rows])

        # Очистка готовит обзор кандидатов; оригиналы остаются на месте.
        if rows:
            self.log("Готовлю обзор кандидатов с миниатюрами. Исходники не изменяются...")
            gallery_items = [(p, reason, detail, k) for p, reason, detail, _sc, k in rows]
            self.make_gallery(trash, gallery_items, moved=False)
        else:
            (trash / "обзор.html").unlink(missing_ok=True)
            shutil.rmtree(trash / "превью", ignore_errors=True)

        counts = {}
        for _, reason, _, _, _ in rows:
            counts[reason] = counts.get(reason, 0) + 1
        self.log("")
        self.log(f"Найдено подозрительных: {len(rows)}")
        for reason, n in counts.items():
            self.log(f"  {reason}: {n}")
        self.log(f"Исходные файлы не изменялись. Отчёт: {report}")
        if rows:
            self.log(f"Обзор для просмотра и выборочного удаления: {trash / 'обзор.html'}")

    def make_gallery(self, trash, items, moved=False):
        prev_dir = Path(trash) / "превью"
        shutil.rmtree(prev_dir, ignore_errors=True)
        prev_dir.mkdir(parents=True, exist_ok=True)
        groups = {"точный дубль": [], "похожий кадр": [], "смазанное": []}
        for path, reason, detail, kept in items:
            groups.setdefault(reason, []).append((Path(path), detail, kept))
        titles = {"точный дубль": "Точные дубликаты", "похожий кадр": "Похожие фотографии",
                  "смазанное": "Смазанные фотографии"}
        html_parts, allowed = [], []
        for reason, title in titles.items():
            records = groups.get(reason, [])
            if not records:
                continue
            cards = []
            for index, (path, detail, kept) in enumerate(records):
                self.check()
                full_path = path.resolve()
                allowed.append(str(full_path))
                thumb = f"candidate-{index}.jpg"
                try:
                    with Image.open(path) as source_image:
                        image = ImageOps.exif_transpose(source_image).convert("RGB")
                        image.thumbnail((320, 320))
                        image.save(prev_dir / thumb, "JPEG", quality=82)
                    image_html = f'<img src="превью/{thumb}" loading="lazy">'
                except Exception:
                    image_html = '<div class="empty">Предпросмотр недоступен</div>'
                detail_html = html.escape(str(detail or ""))
                keeper_html = f'<small>Оставить: {html.escape(Path(kept).name)}</small>' if kept else ""
                escaped_path = html.escape(str(full_path), quote=True)
                cards.append(
                    '<article class="card"><label><input class="candidate" type="checkbox" '
                    f'data-path="{escaped_path}"> Удалить</label>{image_html}'
                    f'<strong>{html.escape(path.name)}</strong><small>{detail_html}</small>{keeper_html}</article>')
            html_parts.append(f'<section><h2>{title} ({len(records)})</h2><div class="grid">'
                              + "".join(cards) + "</div></section>")
        write_csv(Path(trash) / "report.csv",
                  [["Файл", "Причина", "Детали", "Оставить"]] +
                  [[str(path), reason, str(detail or ""), str(kept or "")]
                   for path, reason, detail, kept in items])
        document = (
            '<!doctype html><html lang="ru"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Обзор кандидатов</title><style>'
            'body{margin:0;background:#f3f6fb;color:#182433;font:15px Segoe UI,Arial}'
            'header{position:sticky;top:0;background:#fff;padding:16px 24px;box-shadow:0 2px 8px #0002;z-index:2}'
            'main{padding:20px 24px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px}'
            '.card{background:white;border:1px solid #dce3eb;border-radius:14px;padding:12px;display:grid;gap:8px}'
            '.card img,.empty{width:100%;height:190px;object-fit:contain;background:#edf1f5;border-radius:8px}'
            'button{background:#1769d2;color:white;border:0;border-radius:8px;padding:10px 16px;cursor:pointer}'
            'small{color:#596779;overflow-wrap:anywhere}</style><header><h1>Кандидаты на удаление</h1>'
            '<button id="delete">Удалить выбранные</button> <button id="all">Выбрать все</button> '
            '<button id="none">Снять выбор</button> <span id="status">Исходные файлы не удалены</span></header><main>'
            + "".join(html_parts) + '</main><script>'
            'const boxes=()=>[...document.querySelectorAll(".candidate")];'
            'document.querySelector("#all").onclick=()=>boxes().forEach(x=>x.checked=true);'
            'document.querySelector("#none").onclick=()=>boxes().forEach(x=>x.checked=false);'
            'document.querySelector("#delete").onclick=async()=>{const selected=boxes().filter(x=>x.checked);'
            'if(!selected.length)return alert("Сначала выберите файлы.");'
            'if(!confirm("Удалить выбранные файлы без возможности восстановления ("+selected.length+")?"))return;'
            'try{const r=await fetch("/api/clean/delete",{method:"POST",headers:{"Content-Type":"application/json"},'
            'body:JSON.stringify({paths:selected.map(x=>x.dataset.path)})});const result=await r.json();'
            'if(!r.ok||result.error)throw Error(result.error||r.statusText);'
            'selected.forEach(x=>{if(result.paths.includes(x.dataset.path))x.closest(".card").remove()});'
            'document.querySelector("#status").textContent="Удалено: "+result.deleted;'
            '}catch(e){alert("Ошибка удаления: "+e.message)}};</script></html>')
        (Path(trash) / "обзор.html").write_text(document, encoding="utf-8")

    def restore(self, reasons=None):
        """Возвращает перенесённые файлы на прежние места (по журналу перенесено.csv)."""
        trash = Path(self.cfg.trash_dir)
        log_path = trash / "перенесено.csv"
        if not log_path.exists():
            self.log("Журнала переноса нет: возвращать нечего.")
            return
        rows = read_csv(log_path)
        data = [r for r in rows[1:] if len(r) >= 3]
        total = len(data)
        left, restored, gone, renamed = [], 0, 0, 0

        for i, r in enumerate(data, 1):
            self.check()
            self.tick(i, total)
            orig, now, reason = Path(r[0]), Path(r[1]), r[2]
            if reasons and reason not in reasons:
                left.append(r)
                continue
            if not now.exists():     # ты уже удалил или переместил его сам
                gone += 1
                continue
            dst = orig
            if dst.exists():         # на прежнем месте уже лежит другой файл
                n = 1
                while dst.exists():
                    dst = orig.with_name(f"{orig.stem}_возвращено{n}{orig.suffix}")
                    n += 1
                renamed += 1
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(now), str(dst))
            restored += 1

        write_csv(log_path, [rows[0]] + left)
        self.log("")
        self.log(f"Возвращено файлов: {restored}")
        if gone:
            self.log(f"Пропущено (их уже нет в папке «На удаление»): {gone}")
        if renamed:
            self.log(f"Возвращено под новым именем (место было занято): {renamed}")
        if left:
            self.log(f"В «На удаление» осталось по журналу: {len(left)}")
        else:
            (trash / "обзор.html").unlink(missing_ok=True)
            shutil.rmtree(trash / "превью", ignore_errors=True)
            self.log("Журнал пуст, устаревший обзор удалён.")

    # ---------- план ----------

    def ask_ai_name(self, paths):
        """Просит нейросеть назвать подборку по нескольким фото из неё."""
        c = self.cfg
        if not (c.use_ai and self.ai_ok):
            return ""
        step = max(1, len(paths) // 4)
        images = []
        for p in paths[::step][:4]:
            try:
                img = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
                img.thumbnail((768, 768))
                buf = BytesIO()
                img.save(buf, "JPEG", quality=85)
                images.append(base64.b64encode(buf.getvalue()).decode())
            except Exception:
                continue
        if not images:
            return ""
        payload = {"model": c.ollama_model, "prompt": PROMPT,
                   "images": images, "stream": False}
        try:
            req = urllib.request.Request(
                c.ollama_url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600) as r:
                text = json.loads(r.read().decode("utf-8"))["response"]
        except Exception as e:
            self.log(f"Нейросеть недоступна ({e}). Дальше названия будут только по датам.")
            self.ai_ok = False
            return ""
        text = text.strip()
        return sanitize(text.splitlines()[0]) if text else ""

    def make_plan(self):
        """Анализирует фото и видео, сохраняет редактируемый план без копирования/перемещения."""
        c = self.cfg
        files = self.list_media()
        total = len(files)
        if c.use_ai:
            self.check_model()
        self.log(f"Найдено файлов для анализа: {total}.")
        category_files = {"Screenshots": []}
        file_rows = [["Файл", "Категория", "Тип"]]
        screenshot_helper = self.is_screenshot
        for i, path in enumerate(files, 1):
            self.check()
            self.progress(i, total)
            try:
                if path.suffix.lower() in PHOTO_EXTENSIONS and screenshot_helper(path, Path(c.photos_dir)):
                    category = "Screenshots"
                else:
                    category = self.classify_media_file(path)
                category = sanitize(category) or "Прочее"
                category_files.setdefault(category, []).append(path)
                kind = "Видео" if path.suffix.lower() in VIDEO_EXTENSIONS else "Фото"
                file_rows.append([str(path), category, kind])
                self.log(f"[{i}/{total}] {path.name} → {category}")
            except Exception as exc:
                self.log(f"Пропущен {path.name}: {exc}")
        category_files.setdefault("Screenshots", [])
        group_rows = [["Категория", "Тип", "Файлов", "Название папки"]]
        category_ids = {}
        for index, (name, members) in enumerate(category_files.items(), 1):
            category_id = f"C{index:03d}"
            category_ids[name] = category_id
            kinds = {"Фото" if p.suffix.lower() in PHOTO_EXTENSIONS else "Видео" for p in members}
            kind_text = ", ".join(sorted(kinds)) if kinds else "—"
            group_rows.append([category_id, kind_text, len(members), name])
        file_rows = [file_rows[0]] + [[row[0], category_ids.get(row[1], category_ids["Прочее"]), row[2]]
                                      for row in file_rows[1:]]
        plan_dir = Path(c.plan_dir)
        plan_dir.mkdir(parents=True, exist_ok=True)
        write_csv(plan_dir / "группы.csv", group_rows)
        write_csv(plan_dir / "файлы.csv", file_rows)
        self.log("")
        self.log(f"План готов: {len(group_rows) - 1} категорий, {len(file_rows) - 1} файлов.")
        self.log(f"Сохранён в {plan_dir}. Исходные фото и видео не перемещались и не копировались.")

    # ---------- сортировка ----------

    def sort_files(self):
        c = self.cfg
        plan_dir = Path(c.plan_dir)
        groups_path, files_path = plan_dir / "группы.csv", plan_dir / "файлы.csv"
        if not groups_path.exists() or not files_path.exists():
            self.log("Плана нет. Сначала запусти режим «План».")
            return

        names = {r[0]: r[3] for r in read_csv(groups_path)[1:] if len(r) >= 4}
        rows = [r for r in read_csv(files_path)[1:] if len(r) >= 2]
        (Path(c.sorted_dir) / "Screenshots").mkdir(parents=True, exist_ok=True)
        total = len(rows)
        done = skipped = 0
        for i, row in enumerate(rows, 1):
            self.check()
            self.tick(i, total)
            src = Path(row[0])
            if not src.exists():
                skipped += 1
                continue
            folder = sanitize(names.get(row[1], "")) or "Без названия"
            transfer(src, Path(c.sorted_dir) / folder, copy=c.copy_instead)
            done += 1
        self.log("")
        self.log(f"Готово. Разложено: {done}, пропущено (файла нет): {skipped}")
        self.log(f"Результат: {c.sorted_dir}")

    def list_media(self):
        """Поддерживаемые фото и видео, исключая результаты и временные файлы."""
        c = self.cfg
        source = Path(c.photos_dir)
        skip = [Path(value) for value in (c.trash_dir, c.plan_dir) if value]
        ignored_dirs = {"$recycle.bin", "system volume information", "__macosx",
                        ".trash", "@eaDir"}
        ignored_suffixes = {".tmp", ".temp", ".part", ".crdownload", ".db", ".ini"}
        allowed = set()
        if c.analyze_photos:
            allowed.update(PHOTO_EXTENSIONS)
        if c.analyze_videos:
            allowed.update(VIDEO_EXTENSIONS)
        result = []
        for p in source.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in allowed:
                continue
            rel_parts = p.relative_to(source).parts
            if any(s == p or s in p.parents for s in skip):
                continue
            if c.exclude_system_files and (
                    any(part.casefold() in ignored_dirs for part in rel_parts)
                    or p.name.startswith(".~") or p.name.startswith("~$")
                    or p.suffix.lower() in ignored_suffixes):
                continue
            result.append(p)
        return sorted(result)

    def classify_media_file(self, path):
        """Ask Qwen for an automatically proposed category title for image/video."""
        c = self.cfg
        if path.suffix.lower() in PHOTO_EXTENSIONS and self.is_screenshot(path, Path(c.photos_dir)):
            return "Screenshots"
        frames = []
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            cap = cv2.VideoCapture(str(path))
            if not cap.isOpened():
                raise ValueError("Cannot open video")
            count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
            duration = count / fps if fps else 0
            try:
                for second in sorted(set((0.0, duration / 2, max(0.0, duration - 0.5)))):
                    self.check()
                    cap.set(cv2.CAP_PROP_POS_MSEC, second * 1000)
                    ok, frame = cap.read()
                    if not ok:
                        continue
                    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    image.thumbnail((768, 768))
                    buf = BytesIO()
                    image.save(buf, "JPEG", quality=78)
                    frames.append(base64.b64encode(buf.getvalue()).decode("ascii"))
            finally:
                cap.release()
        else:
            image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
            image.thumbnail((768, 768))
            buf = BytesIO()
            image.save(buf, "JPEG", quality=80)
            frames.append(base64.b64encode(buf.getvalue()).decode("ascii"))
        if not frames:
            return "Прочее"
        prompt = (
            "Suggest a concise Russian folder title, one to four words, for the main "
            "topic shown in the image" +
            (" or these key video frames" if path.suffix.lower() in VIDEO_EXTENSIONS else "") +
            ". Return only the folder title. If it is an application, website, chat, or "
            "interface screenshot, return exactly Screenshots."
        )
        payload = {"model": c.ollama_model, "prompt": prompt, "images": frames,
                   "stream": False, "options": {"temperature": 0.2, "num_predict": 24}}
        req = urllib.request.Request(c.ollama_url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as response:
            answer = json.loads(response.read().decode("utf-8")).get("response", "")
        label = sanitize(answer.strip().splitlines()[0]) if answer.strip() else "Прочее"
        return "Screenshots" if label.casefold() == "screenshots" else (label or "Прочее")

    def is_screenshot(self, path, source):
        """Определяет скриншот по пути, имени и типичным размерам экранов."""
        parts = {part.casefold() for part in path.relative_to(source).parts}
        if {"screenshots", "скриншоты", "screen shots", "снимки экрана"} & parts:
            return True
        name = path.stem.casefold().replace("_", " ").replace("-", " ")
        if any(word in name for word in ("screenshot", "скриншот", "screen shot", "снимок экрана")):
            return True
        if path.suffix.lower() != ".png":
            return False
        try:
            with Image.open(path) as img:
                width, height = img.size
            ratio = max(width, height) / max(min(width, height), 1)
            return (ratio >= 1.55 and max(width, height) >= 1000 and
                    min(width, height) >= 600)
        except Exception:
            return False

    def ask_screenshot_label(self, path):
        """Определяет приложение или тему скриншота через Ollama."""
        c = self.cfg
        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        img.thumbnail((1000, 1000))
        buf = BytesIO()
        img.save(buf, "JPEG", quality=82)
        prompt = (
            "Это скриншот телефона или компьютера. Определи приложение, сайт или "
            "главную тему скриншота. Ответь только коротким названием папки на "
            "русском или английском языке, 1-3 слова, без пояснений. "
            "Примеры: YouTube, Instagram, Telegram, Банк, Покупки, Путешествия, "
            "Рецепты, Документы, Новости, Прочее.")
        payload = {
            "model": c.ollama_model, "prompt": prompt, "stream": False,
            "images": [base64.b64encode(buf.getvalue()).decode()],
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 12},
        }
        req = urllib.request.Request(
            c.ollama_url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as response:
            text = json.loads(response.read().decode("utf-8"))["response"]
        return sanitize(text.splitlines()[0]) if text.strip() else "Прочее"

    def sort_screenshots(self):
        """Анализирует только скриншоты и раскладывает их по приложениям или темам."""
        c = self.cfg
        source = Path(c.photos_dir)
        destination = Path(c.screenshots_dir)
        files = [p for p in self.list_media() if self.is_screenshot(p, source)]
        if not files:
            self.log("Скриншоты не найдены.")
            return
        self.check_model()
        total = len(files)
        done = skipped = 0
        self.log(f"Найдено скриншотов: {total}. Определяю приложение или тему...")
        for i, src in enumerate(files, 1):
            self.check()
            self.tick(i, total)
            try:
                folder = self.ask_screenshot_label(src) or "Прочее"
                transfer(src, destination / folder, copy=c.copy_instead)
                done += 1
            except (OSError, ValueError) as exc:
                skipped += 1
                self.log(f"Пропущен {src.name}: {exc}")
            except Exception as exc:
                skipped += 1
                self.log(f"Не удалось определить {src.name}: {exc}")
        self.log("")
        self.log(f"Скриншоты разложены: {done}, пропущено: {skipped}.")
        self.log(f"Результат: {destination}")

    def sort_media(self):
        """Раскладывает фото, видео и скриншоты по отдельным папкам."""
        c = self.cfg
        if c.screenshots_only:
            self.sort_screenshots()
            return
        source = Path(c.photos_dir)
        destination = Path(c.sorted_dir)
        (destination / "Скриншоты").mkdir(parents=True, exist_ok=True)
        files = self.list_media()
        total = len(files)
        counts = {"Фото": 0, "Видео": 0, "Скриншоты": 0}
        skipped = 0

        for i, src in enumerate(files, 1):
            self.check()
            self.tick(i, total)
            relative = src.relative_to(source)
            parts = [part.casefold() for part in relative.parts]
            try:
                if src.suffix.lower() in PHOTO_EXTENSIONS and self.is_screenshot(src, source):
                    marker = next((index for index, part in enumerate(relative.parts)
                                   if part.casefold() in {"screenshots", "скриншоты", "screen shots", "снимки экрана"}), None)
                    after = relative.parts[marker + 1:-1] if marker is not None else ()
                    app_parts = [p for p in after if p.casefold() not in {"приложения", "applications"}]
                    app = sanitize(app_parts[0]) if app_parts else "Прочие"
                    target_dir = destination / "Скриншоты" / "Приложения" / (app or "Прочие")
                    category = "Скриншоты"
                elif src.suffix.lower() in VIDEO_EXTENSIONS:
                    target_dir = destination / "Видео"
                    category = "Видео"
                else:
                    target_dir = destination / "Фото"
                    category = "Фото"
                transfer(src, target_dir, copy=c.copy_instead)
                counts[category] += 1
            except (OSError, ValueError) as exc:
                skipped += 1
                self.log(f"Пропущен {src.name}: {exc}")

        self.log("")
        self.log(f"Типовая сортировка завершена: фото {counts['Фото']}, "
                 f"видео {counts['Видео']}, скриншоты {counts['Скриншоты']}, "
                 f"пропущено {skipped}.")
        self.log(f"Результат: {destination}")


    def check_model(self):
        """Проверяет, что Ollama запущена и нужная модель скачана."""
        c = self.cfg
        base = c.ollama_url.rsplit("/api/", 1)[0]
        try:
            with urllib.request.urlopen(base + "/api/tags", timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            names = [m.get("name", "") for m in data.get("models", [])]
        except Exception:
            raise RuntimeError(
                "Ollama не отвечает. Запусти Ollama (значок в трее или команда "
                "`ollama serve`) и повтори.")
        want = c.ollama_model
        if not any(n == want or n == want + ":latest" or n.split(":")[0] == want
                   for n in names):
            raise RuntimeError(
                f"Модель {want} не скачана. Выполни в PowerShell: ollama pull {want}")

    def ask_topic(self, path, cats, prompt):
        c = self.cfg
        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        img.thumbnail((640, 640))
        buf = BytesIO()
        img.save(buf, "JPEG", quality=80)
        payload = {
            "model": c.ollama_model, "prompt": prompt, "stream": False,
            "images": [base64.b64encode(buf.getvalue()).decode()],
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 20},
        }
        req = urllib.request.Request(
            c.ollama_url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            text = json.loads(r.read().decode("utf-8"))["response"]
        return match_category(text, cats)


def match_category(text, cats):
    """Находит в ответе нейросети одну из тем списка."""
    t = re.sub(r"[\"'«».,!:;*]", "", text.strip().lower())
    for cat in cats:
        if t == cat.lower():
            return cat
    found = [(t.find(cat.lower()), cat) for cat in cats if cat.lower() in t]
    return min(found)[1] if found else "Без темы"


# ============================ окно ============================

def open_path(p):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as e:
        messagebox.showerror("Не удалось открыть", str(e))


def main():
    """Launch the single supported HTML application UI."""
    import photo_helper_web
    return photo_helper_web.main()

if __name__ == "__main__":
    main()
