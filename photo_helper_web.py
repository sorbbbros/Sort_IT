"""Five-mode Photo AI Agent web UI backend for Windows and Docker."""
import json
import base64
import html
import os
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import asdict, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen


def restart_with_project_venv():
    if getattr(sys, "frozen", False) or os.environ.get("SORT_IT_VENV_ACTIVE") == "1" or not sys.platform.startswith("win"):
        return
    py = Path(__file__).resolve().parent / ".ocr-venv" / "Scripts" / "python.exe"
    if py.is_file() and Path(sys.executable).resolve() != py.resolve():
        env = os.environ.copy()
        env["SORT_IT_VENV_ACTIVE"] = "1"
        script = str(Path(__file__).resolve())
        os.execve(str(py), [str(py), script, *sys.argv[1:]], env)


restart_with_project_venv()
import photo_helper_v2 as core

HERE = Path(__file__).resolve().parent
UI_FILE = HERE / "app_resources" / "sort_it_ui.html"
if not UI_FILE.is_file():
    UI_FILE = HERE / "sort_it_ui.html"
UI = UI_FILE
MODES = {"clean", "plan"}
DEFAULT_CATEGORIES = ["Screenshots", "Люди", "Дети", "Животные", "Природа",
                      "Город и путешествия", "Еда", "Документы", "Транспорт",
                      "Дом и интерьер", "Покупки", "Прочее"]


class Cancelled(Exception):
    pass


core.Cancelled = Cancelled


class State:
    def __init__(self):
        self.cfg = core.load_settings()
        self.cfg.ollama_model = "qwen3.5:9b"
        if self.cfg.ollama_url.casefold().startswith("http://localhost:"):
            self.cfg.ollama_url = self.cfg.ollama_url.replace("localhost", "127.0.0.1", 1)
        if self.cfg.mode not in MODES:
            self.cfg.mode = "clean"
        self.cfg.copy_instead = getattr(self.cfg, "copy_instead", True)
        self.cfg.topics_table_only = getattr(self.cfg, "topics_table_only", True)
        self.running = False
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.log = []
        self.done = self.total = 0
        self.current = ""
        self.candidates = set()
        self.kept = set()
        self.results = []
        self.browse_jobs = {}


S = State()


def container_path(value):
    value = str(value or "")
    if os.environ.get("SORT_IT_HEADLESS") == "1" and value[:2].casefold() == "q:":
        return "/q/" + value[2:].replace("\\", "/").lstrip("/")
    return value


def apply(data):
    for item in fields(core.Config):
        if item.name in data and data[item.name] is not None:
            setattr(S.cfg, item.name, data[item.name])
    c = S.cfg
    c.mode = c.mode if c.mode in MODES else "clean"
    c.photos_dir = container_path(c.photos_dir)
    c.trash_dir = str(Path(c.photos_dir) / "\u041d\u0430 \u0443\u0434\u0430\u043b\u0435\u043d\u0438\u0435") if c.photos_dir else ""
    c.plan_dir = container_path(c.plan_dir)
    c.ollama_model = "qwen3.5:9b"
    c.use_ai = True
    c.analyze_photos = True
    c.analyze_videos = True
    c.exclude_system_files = True
    if os.environ.get("SORT_IT_HEADLESS") == "1":
        c.ollama_url = c.ollama_url.replace("http://127.0.0.1:", "http://host.docker.internal:", 1)
        c.ollama_url = c.ollama_url.replace("http://localhost:", "http://host.docker.internal:", 1)
    elif c.ollama_url.casefold().startswith("http://localhost:"):
        c.ollama_url = c.ollama_url.replace("localhost", "127.0.0.1", 1)
    return c


def validate(c):
    if c.mode not in MODES:
        return "Выберите одну из двух задач."
    if not c.photos_dir or not Path(c.photos_dir).is_dir():
        return "Не найдена исходная папка."
    if c.mode == "clean":
        if not c.trash_dir:
            return "Укажите папку для отчёта очистки."
        if not (c.do_exact or c.do_similar or c.do_blur):
            return "Включите хотя бы один параметр очистки."
    elif c.mode == "plan":
        if not c.plan_dir:
            return "Укажите папку для сохранения плана."
        if not (c.analyze_photos or c.analyze_videos):
            return "Выберите фото, видео или оба типа."
        if c.use_ai and not c.ollama_model:
            return "Укажите модель Ollama или отключите использование ИИ."
    outputs = [c.trash_dir, c.plan_dir, c.sorted_dir, c.screenshots_dir]
    src = Path(c.photos_dir).resolve()
    for value in outputs:
        if value and Path(value).resolve() == src:
            return "Папка назначения не должна совпадать с исходной папкой."
    return None


def log(message):
    with S.lock:
        S.log.append(time.strftime("[%H:%M:%S] ") + str(message))


def progress(done, total, current=""):
    with S.lock:
        S.done, S.total, S.current = done, total, str(current)


def cleanup_files(c):
    root = Path(c.photos_dir)
    excluded = [Path(x) for x in (c.trash_dir, c.plan_dir, c.sorted_dir, c.screenshots_dir) if x]
    ignored = {"$recycle.bin", "system volume information", "__macosx", ".trash", "@eaDir"}
    return sorted(p for p in root.rglob("*") if p.is_file()
                  and p.suffix.lower() in core.PHOTO_EXTENSIONS
                  and not any(x == p or x in p.parents for x in excluded)
                  and not any(part.casefold() in ignored for part in p.relative_to(root).parts))


def run_clean(c):
    files = cleanup_files(c)
    seen, analyzed, candidates = {}, [], []
    log(f"Проверяю фотографий: {len(files)}")
    for i, path in enumerate(files, 1):
        if S.stop.is_set():
            raise Cancelled()
        progress(i, len(files), path.name)
        try:
            if c.do_exact:
                digest = core.file_hash(path)
                original = seen.get(digest)
                if original is not None:
                    if path.resolve() != original.resolve():
                        candidates.append({"path": str(path), "reason": "Точный дубликат",
                                           "detail": f"Content hash matches full path: {original}",
                                           "kept": str(original)})
                    continue
                seen[digest] = path
            if c.do_similar or c.do_blur:
                score, dhash = core.analyze(path)
                analyzed.append({"path": path, "score": score, "dhash": dhash,
                                 "size": path.stat().st_size})
        except Exception as exc:
            log(f"Не удалось проверить {path.name}: {exc}")
    flagged = set()
    if c.do_similar and analyzed:
        hashes = core.np.array([x["dhash"] for x in analyzed], dtype=core.np.uint64)
        assigned = core.np.zeros(len(analyzed), dtype=bool)
        for i, item in enumerate(analyzed):
            if S.stop.is_set():
                raise Cancelled()
            if assigned[i]:
                continue
            dist = core.hamming_all(hashes[i], hashes)
            group = core.np.where((dist <= c.similar_threshold) & ~assigned)[0]
            assigned[group] = True
            if len(group) > 1:
                best = max(group, key=lambda j: (analyzed[j]["score"], analyzed[j]["size"]))
                for j in group:
                    if j != best:
                        p, winner = analyzed[j]["path"], analyzed[best]["path"]
                        candidates.append({"path": str(p), "reason": "Похожее фото",
                                           "detail": f"Лучший кадр: {winner.name}", "kept": str(winner)})
                        flagged.add(p)
    if c.do_blur:
        for item in analyzed:
            if item["path"] not in flagged and item["score"] < c.blur_threshold:
                candidates.append({"path": str(item["path"]), "reason": "Смазанное фото",
                                   "detail": f"Резкость {item['score']:.1f}", "kept": ""})
    destination = Path(c.trash_dir)
    destination.mkdir(parents=True, exist_ok=True)
    core.write_csv(destination / "report.csv", [["Файл", "Причина", "Детали", "Оставить"]] +
                   [[x["path"], x["reason"], x["detail"], x["kept"]] for x in candidates])
    with S.lock:
        S.candidates = {str(Path(x["path"]).resolve()) for x in candidates}
        S.kept = {str(Path(x["kept"]).resolve()) for x in candidates if x["kept"]}
        S.results = candidates
    make_cleanup_gallery(destination, candidates)
    for item in candidates:
        log(f"Проверен файл: {item['path']} | {item['reason']} | {item['detail']}")
    log(f"Созданы отчёт и HTML-обзор: {destination}")
    log(f"Кандидатов найдено: {len(candidates)}. Исходники не перемещались и не удалялись.")
    progress(len(files), len(files), "Готово")


def run_with_helper(c, method_name):
    helper = core.Helper(c, log, progress, S.stop)
    getattr(helper, method_name)()
    if c.mode == "plan":
        loaded = plan_load(c.plan_dir)
        with S.lock:
            S.results = loaded.get("groups", [])
    else:
        with S.lock:
            S.results = []


def run_plan(c):
    helper = core.Helper(c, log, progress, S.stop)
    files = helper.list_media()
    helper.check_model()
    categories = {"Screenshots": []}
    samples = {"Screenshots": []}
    file_rows = [["Файл", "Категория", "Тип"]]
    total = len(files)
    log(f"Найдено файлов для плана: {total}. Начинаю анализ.")
    for index, path in enumerate(files, 1):
        if S.stop.is_set():
            raise Cancelled()
        progress(index, total, path.name)
        try:
            if path.suffix.lower() in core.PHOTO_EXTENSIONS and helper.is_screenshot(path, Path(c.photos_dir)):
                category = "Screenshots"
            else:
                category = core.sanitize(helper.classify_media_file(path)) or "Прочее"
            category = "Screenshots" if category.casefold() == "screenshots" else category
            categories.setdefault(category, [])
            samples.setdefault(category, [])
            kind = "Видео" if path.suffix.lower() in core.VIDEO_EXTENSIONS else "Фото"
            file_rows.append([str(path), category, kind])
            categories[category].append(kind)
            if len(samples[category]) < 5:
                samples[category].append(str(path))
            log(f"[{index}/{total}] {path.name} ? {category}")
        except Exception as exc:
            log(f"Не удалось классифицировать {path.name}: {exc}")
    groups = [["ID", "Тип", "Файлов", "Название папки"]]
    result = []
    ids = {}
    for i, (name, kinds) in enumerate(categories.items(), 1):
        key = f"C{i:03d}"
        ids[name] = key
        kind = ", ".join(sorted(set(kinds))) or "?"
        groups.append([key, kind, len(kinds), name])
        result.append({"id": key, "name": name, "count": len(kinds),
                       "kind": kind, "samples": samples[name]})
    file_rows = [file_rows[0]] + [[row[0], ids[row[1]], row[2]] for row in file_rows[1:]]
    out = Path(c.plan_dir)
    out.mkdir(parents=True, exist_ok=True)
    core.write_csv(out / "группы.csv", groups)
    core.write_csv(out / "файлы.csv", file_rows)
    with S.lock:
        S.results = result
    log(f"План сохранён в {out}. Исходные файлы не изменялись.")
    progress(total, total, "Готово")


def start_task(c):
    with S.lock:
        if S.running:
            return "Задача уже выполняется."
        S.running = True
        S.done = S.total = 0
        S.current = "Подготовка"
        S.candidates.clear()
        S.kept.clear()
        S.results = []
    S.stop.clear()
    log(f"Запущена операция {c.mode}. Исходная папка: {c.photos_dir}")
    def worker():
        try:
            if c.mode == "clean":
                run_clean(c)
            else:
                run_plan(c)
        except Cancelled:
            log("Операция остановлена пользователем.")
        except Exception as exc:
            log(f"Ошибка: {exc}")
        finally:
            with S.lock:
                S.running = False
    threading.Thread(target=worker, daemon=True).start()
    return None


def scan_source(data):
    previous_source = str(S.cfg.photos_dir or "")
    c = apply(data)
    if not c.photos_dir or not Path(c.photos_dir).is_dir():
        return {"error": "Исходная папка не найдена."}
    root = Path(c.photos_dir)
    c.trash_dir = str(root / "\u041d\u0430 \u0443\u0434\u0430\u043b\u0435\u043d\u0438\u0435")
    if previous_source and Path(previous_source).resolve() != root.resolve():
        c.trash_dir = str(root / "На удаление")
        c.plan_dir = str(root / "План сортировки")
        c.sorted_dir = str(root / "Сортировано")
    if not c.trash_dir:
        c.trash_dir = str(root / "На удаление")
    if not c.plan_dir:
        c.plan_dir = str(root / "План сортировки")
    if not c.sorted_dir:
        c.sorted_dir = str(root / "Сортировано")
    probe = core.Config()
    probe.photos_dir = str(root)
    probe.trash_dir = c.trash_dir
    probe.plan_dir = c.plan_dir
    probe.sorted_dir = c.sorted_dir
    probe.screenshots_dir = c.screenshots_dir
    probe.analyze_photos = True
    probe.analyze_videos = True
    probe.exclude_system_files = True
    probe_helper = core.Helper(probe, lambda *_: None, lambda *_: None, threading.Event())
    files = probe_helper.list_media()
    photos = sum(p.suffix.lower() in core.PHOTO_EXTENSIONS for p in files)
    videos = sum(p.suffix.lower() in core.VIDEO_EXTENSIONS for p in files)
    screenshots = sum(probe_helper.is_screenshot(p, root)
                      for p in files if p.suffix.lower() in core.PHOTO_EXTENSIONS)
    core.save_settings(c)
    log(f"Просканирована папка: {root}. Фото: {photos}, видео: {videos}, всего: {len(files)}")
    return {"count": len(files), "photos": photos, "videos": videos,
            "screenshots": screenshots, "bytes": sum(p.stat().st_size for p in files if p.exists()),
            "trash_dir": c.trash_dir, "plan_dir": c.plan_dir, "sorted_dir": c.sorted_dir}


def _plan_files(plan_dir):
    directory = Path(plan_dir)
    return directory / "группы.csv", directory / "файлы.csv", directory / "темы.csv"


def _backup(path):
    backup = path.with_name(path.name + ".bak")
    if path.exists() and not backup.exists():
        backup.write_bytes(path.read_bytes())


def plan_load(plan_dir):
    gp, fp, _ = _plan_files(plan_dir)
    log(f"Загрузка плана: {plan_dir}")
    if not gp.exists() or not fp.exists():
        return {"error": "Плана пока нет."}
    counts, samples, kinds = {}, {}, {}
    for row in core.read_csv(fp)[1:]:
        if len(row) < 2:
            continue
        key = row[1]
        counts[key] = counts.get(key, 0) + 1
        kinds.setdefault(key, set()).add(row[2] if len(row) > 2 else "Фото")
        samples.setdefault(key, [])
        if len(samples[key]) < 5:
            samples[key].append({"path": row[0], "kind": row[2] if len(row) > 2 else "Фото"})
    groups = []
    for row in core.read_csv(gp)[1:]:
        if len(row) < 4:
            continue
        key = row[0]
        groups.append({"id": key, "name": row[3], "count": counts.get(key, 0),
                       "kind": ", ".join(sorted(kinds.get(key, set()))) or row[1],
                       "samples": samples.get(key, [])})
    if not any(g["name"].casefold() == "screenshots" for g in groups):
        groups.append({"id": "C-SCREENSHOTS", "name": "Screenshots", "count": 0,
                       "kind": "—", "samples": []})
    return {"groups": groups}


def plan_save(data):
    gp, fp, _ = _plan_files(data.get("plan_dir", ""))
    if not gp.exists() or not fp.exists():
        return {"error": "План не найден. Сначала выполните анализ."}
    groups = data.get("groups") or []
    if not any(str(g.get("name", "")).strip().casefold() == "screenshots" for g in groups):
        return {"error": "Категория Screenshots обязательна."}
    remap = data.get("remap") or {}
    names = {str(g["id"]): core.sanitize(str(g.get("name", ""))) or "Без названия" for g in groups}
    rows = core.read_csv(fp)
    if not rows:
        return {"error": "Файл плана пуст."}
    _backup(fp)
    _backup(gp)
    out = [rows[0]]
    for row in rows[1:]:
        if len(row) < 2:
            continue
        destination = remap.get(row[1], row[1])
        if destination not in names:
            destination = next((g["id"] for g in groups if names[str(g["id"])].casefold() == "screenshots"), None)
        if destination is not None:
            out.append([row[0], destination] + row[2:])
    core.write_csv(fp, out)
    counts, kinds = {}, {}
    for row in out[1:]:
        counts[row[1]] = counts.get(row[1], 0) + 1
        kinds.setdefault(row[1], set()).add(row[2] if len(row) > 2 else "Фото")
    summary = [["Категория", "Тип", "Файлов", "Название папки"]]
    for group in groups:
        key = str(group["id"])
        summary.append([key, ", ".join(sorted(kinds.get(key, set()))) or "—",
                        counts.get(key, 0), names[key]])
    core.write_csv(gp, summary)
    for row in out[1:]:
        log(f"Сохранён план для файла: {row[0]} | Категория: {names.get(row[1], row[1])}")
    log(f"План сохранён: {gp.parent}")
    return {"error": None}


def make_cleanup_gallery(directory, items):
    u = lambda codes: "".join(chr(int(code, 16)) for code in codes.split())
    directory = Path(directory)
    preview_name = u("043f 0440 0435 0432 044c 044e")
    gallery_name = u("043e 0431 0437 043e 0440") + ".html"
    prev = directory / preview_name
    prev.mkdir(parents=True, exist_ok=True)
    html_items = []
    displayed = set()
    for item in items:
        duplicate = Path(item["path"])
        original = Path(item["kept"]) if item.get("kept") else None
        pair = [(duplicate, "Кандидат на удаление", item.get("reason", ""), item.get("detail", ""))]
        if original and original.is_file():
            pair.append((original, "Оригинал / лучший кадр", "Также доступен для выбора", ""))
        for path, role, reason, detail in pair:
            resolved = str(path.resolve())
            if resolved in displayed:
                continue
            displayed.add(resolved)
            index = len(displayed) - 1
            try:
                image = core.ImageOps.exif_transpose(core.Image.open(path)).convert("RGB")
                image.thumbnail((320, 320))
                image.save(prev / f"{index}.jpg", "JPEG", quality=80)
                thumb = f'<img src="/api/clean/preview?index={index}">'
            except Exception:
                thumb = '<div>Предпросмотр недоступен</div>'
            details = f"{role} | {reason}" + (f" | {detail}" if detail else "")
            html_items.append(
                f'<article><label><input type="checkbox" class="candidate" '
                f'data-path="{html.escape(resolved, quote=True)}"> Выбрать для удаления</label>'
                f'{thumb}<b>{html.escape(path.name)}</b>'
                f'<small>{html.escape(details)}</small>'
                f'<small>{html.escape(str(path.parent))}</small></article>')
    title = u("041e 0447 0438 0441 0442 043a 0430")
    heading = u("041a 0430 043d 0434 0438 0434 0430 0442 044b 0020 043e 0447 0438 0441 0442 043a 0438")
    delete = u("0423 0434 0430 043b 0438 0442 044c 0020 0432 044b 0431 0440 0430 043d 043d 044b 0435")
    all_text = u("0412 044b 0431 0440 0430 0442 044c 0020 0432 0441 0435")
    none_text = u("0421 043d 044f 0442 044c 0020 0432 044b 0431 043e 0440")
    status_text = u("0418 0441 0445 043e 0434 043d 044b 0435 0020 0444 0430 0439 043b 044b 0020 043d 0435 0020 0443 0434 0430 043b 0435 043d 044b")
    page = ('<!doctype html><html lang="ru"><meta charset="utf-8"><title>' + title + '</title>'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/gallery.css">'
            '<header><div class="brand">SORT <span>IT</span></div><h1>' + heading + '</h1><button id="delete">' + delete + '</button> <button id="all">' + all_text + '</button> <button id="none">' + none_text + '</button> <span id="status">' + status_text + '</span></header><main>'
            + "".join(html_items) + '</main><script>const b=()=>[...document.querySelectorAll(".candidate")];document.querySelector("#all").onclick=()=>b().forEach(x=>x.checked=true);document.querySelector("#none").onclick=()=>b().forEach(x=>x.checked=false);document.querySelector("#delete").onclick=async()=>{let a=b().filter(x=>x.checked);if(!a.length)return alert("Сначала выберите файлы.");if(!confirm("Удалить выбранные файлы безвозвратно ("+a.length+")?"))return;try{let r=await fetch("/api/clean/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({paths:a.map(x=>x.dataset.path)})});let j=await r.json();if(!r.ok)throw Error(j.error||r.statusText);a.forEach(x=>{if(j.paths.includes(x.dataset.path))x.closest("article").remove()});document.querySelector("#status").textContent="Удалено файлов: "+j.deleted+"; отчёт обновлён"}catch(e){alert("Ошибка удаления: "+e.message)}};</script></html>')
    (directory / gallery_name).write_text(page, encoding="utf-8")


def preview_image(path):
    image = core.ImageOps.exif_transpose(core.Image.open(path)).convert("RGB")
    image.thumbnail((900, 700))
    output = BytesIO()
    image.save(output, "JPEG", quality=82)
    return output.getvalue()



class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass
    def reply(self, obj, code=200, ctype="application/json; charset=utf-8", raw=None):
        body = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self.reply(None, raw=UI.read_bytes(), ctype="text/html; charset=utf-8")
        if u.path == "/gallery.css":
            return self.reply(None, raw=(HERE / "app_resources" / "gallery.css").read_bytes(), ctype="text/css; charset=utf-8")
        if u.path == "/api/config":
            config = asdict(S.cfg)
            for key in ("categories", "use_ai", "analyze_photos", "analyze_videos", "exclude_system_files"):
                config.pop(key, None)
            config["ollama_model"] = "qwen3.5:9b"
            return self.reply(config)
        if u.path == "/api/state":
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            with S.lock:
                state = {"log": S.log[since:], "total": len(S.log), "done": S.done,
                         "max": S.total, "current": S.current, "running": S.running,
                         "results": S.results}
            return self.reply(state)
        if u.path == "/api/clean/results":
            return self.reply({"items": S.results})
        if u.path == "/api/clean/gallery":
            gallery_name = "".join(chr(x) for x in (0x043e, 0x0431, 0x0437, 0x043e, 0x0440)) + ".html"
            gallery = Path(S.cfg.trash_dir) / gallery_name
            if not gallery.is_file():
                return self.reply({"error": "Gallery not found"}, 404)
            document = gallery.read_text(encoding="utf-8")
            import re
            document = re.sub(r"<style>.*?</style>", '<link rel="stylesheet" href="/gallery.css">', document, flags=re.S)
            if 'class="brand"' not in document:
                document = document.replace("<header>", '<header><div class="brand">SORT <span>IT</span></div>', 1)
            return self.reply(None, raw=document.encode("utf-8"), ctype="text/html; charset=utf-8")
        if u.path == "/api/clean/preview":
            try:
                index = int(parse_qs(u.query).get("index", [""])[0])
                preview_name = "".join(chr(x) for x in (0x043f, 0x0440, 0x0435, 0x0432, 0x044c, 0x044e))
                preview = Path(S.cfg.trash_dir) / preview_name / f"{index}.jpg"
                if index < 0 or not preview.is_file():
                    raise ValueError("Preview not found")
                return self.reply(None, raw=preview.read_bytes(), ctype="image/jpeg")
            except (ValueError, OSError) as exc:
                return self.reply({"error": str(exc)}, 404)
        if u.path == "/api/preview":
            q = parse_qs(u.query); path = Path(container_path(q.get("path", [""])[0])).resolve()
            source = Path(S.cfg.photos_dir).resolve()
            if source not in path.parents or not path.is_file():
                return self.reply({"error": "Путь предпросмотра не разрешён."}, 403)
            try:
                blob = preview_image(path)
                return self.reply(None, raw=blob, ctype="image/jpeg")
            except Exception as exc:
                return self.reply({"error": str(exc)}, 400)
        return self.reply({"error": "not found"}, 404)
    def do_POST(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8") or "{}")
        except Exception:
            return self.reply({"error": "Некорректный JSON."}, 400)
        path = urlparse(self.path).path
        if path == "/api/browse":
            job, error = start_browse_job(data.get("initial", ""))
            return self.reply({"job": job.get("job") if job else "", "error": error, "done": False})
        if path == "/api/browse/status":
            return self.reply(browse_job_status(str(data.get("job", ""))))
        if path == "/api/scan":
            return self.reply(scan_source(data))
        if path == "/api/start":
            c = apply(data); error = validate(c)
            if not error:
                core.save_settings(c); error = start_task(c)
            return self.reply({"error": error})
        if path == "/api/clean/delete":
            requested = {str(Path(item).resolve()) for item in data.get("paths", [])}
            source = Path(S.cfg.photos_dir).resolve()
            allowed = set(S.candidates) | set(S.kept)
            deleted = []
            log(f"Запрошено удаление файлов: {len(requested)}")
            for rejected in requested - allowed:
                log(f"Удаление отклонено: {rejected}. Файл отсутствует в разрешённых результатах.")
            for item in requested & allowed:
                target = Path(item)
                if source in target.parents and target.is_file():
                    try:
                        target.unlink()
                        deleted.append(item)
                        log(f"Удалён файл: {target.name} | {target}")
                    except OSError as exc:
                        log(f"Не удалось удалить {target}: {exc}")
            if deleted:
                deleted_set = set(deleted)
                with S.lock:
                    remaining = []
                    for result in S.results:
                        if str(Path(result["path"]).resolve()) in deleted_set:
                            continue
                        updated = dict(result)
                        if updated.get("kept") and str(Path(updated["kept"]).resolve()) in deleted_set:
                            updated["kept"] = ""
                        remaining.append(updated)
                    S.results = remaining
                    S.candidates = {str(Path(x["path"]).resolve()) for x in remaining}
                    S.kept = {str(Path(x["kept"]).resolve()) for x in remaining
                              if x.get("kept") and Path(x["kept"]).is_file()}
                directory = Path(S.cfg.trash_dir)
                directory.mkdir(parents=True, exist_ok=True)
                core.write_csv(directory / "report.csv", [["Файл", "Причина", "Детали", "Оставить"]] +
                               [[x["path"], x["reason"], x["detail"], x["kept"]] for x in remaining])
                make_cleanup_gallery(directory, remaining)
            log(f"Удаление завершено. Удалено файлов: {len(deleted)}")
            return self.reply({"deleted": len(deleted), "paths": deleted})
        if path == "/api/stop":
            log("Пользователь запросил остановку обработки.")
            S.stop.set(); return self.reply({"ok": True})
        if path == "/api/plan/load":
            return self.reply(plan_load(container_path(data.get("plan_dir", ""))))
        if path == "/api/plan/save":
            data["plan_dir"] = container_path(data.get("plan_dir", ""))
            return self.reply(plan_save(data))
        if path == "/api/open":
            c = apply(data)
            log(f"Открытие обзора результатов: режим {c.mode}")
            if c.mode == "clean":
                target = Path(c.trash_dir) / "обзор.html"
                if not target.exists():
                    target = Path(c.trash_dir)
            elif c.mode == "plan":
                target = Path(c.plan_dir)
            else:
                target = Path(c.sorted_dir)
            try:
                if not target.exists():
                    return self.reply({"error": "Результаты ещё не созданы."})
                if c.mode == "clean":
                    return self.reply({"error": None, "url": "/api/clean/gallery"})
                if os.name == "nt":
                    os.startfile(str(target))
                else:
                    webbrowser.open(target.as_uri())
                return self.reply({"error": None})
            except Exception as exc:
                return self.reply({"error": str(exc)})
        return self.reply({"error": "not found"}, 404)


def _decode_windows_output(raw):
    for enc in ("utf-8", "cp1251", "oem"):
        try:
            return raw.decode(enc).strip()
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def start_browse_job(initial=""):
    """Open Tk folder picker in a GUI process; pass paths as base64 UTF-8."""
    if os.name != "nt":
        return None, "Native folder picker is only available in Windows mode."
    job = str(time.time_ns())
    output = Path(os.environ.get("TEMP", str(Path.home()))) / f"sort-it-folder-{job}.json"
    script = output.with_suffix(".py")
    pyw = Path(sys.executable).with_name("pythonw.exe")
    if not pyw.is_file():
        pyw = Path(sys.executable)
    source = "\n".join([
        "import base64,json,sys,tkinter as tk",
        "from tkinter import filedialog",
        "initial=base64.b64decode(sys.argv[1]).decode('utf-8')",
        "dest=base64.b64decode(sys.argv[2]).decode('utf-8')",
        "root=tk.Tk(); root.withdraw(); root.attributes('-topmost',True); root.update_idletasks()",
        "if initial and not __import__('os').path.isdir(initial): initial=''",
        "selected=filedialog.askdirectory(parent=root,title='Select source folder',initialdir=initial or None,mustexist=True)",
        "root.destroy()",
        "payload={'path':selected or '', 'error':''}",
        "open(dest,'w',encoding='utf-8').write(json.dumps(payload,ensure_ascii=False))",
    ])
    try:
        script.write_text(source, encoding="utf-8")
        argv = [str(pyw), str(script),
                base64.b64encode((initial or "").encode("utf-8")).decode("ascii"),
                base64.b64encode(str(output).encode("utf-8")).decode("ascii")]
        if getattr(sys, "frozen", False):
            argv = [sys.executable, "--pick-folder", argv[-2], argv[-1]]
        # Keep the picker attached to the logged-in Windows desktop. Detached
        # processes can be placed outside the interactive window station.
        process = subprocess.Popen(argv, cwd=str(HERE), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with S.lock:
            S.browse_jobs[job] = {"done": False, "path": "", "error": "", "pid": process.pid,
                                  "output": str(output), "script": str(script)}
        return {"job": job}, ""
    except Exception as exc:
        return None, f"Cannot open Windows folder dialog: {exc}"


def browse_job_status(job):
    with S.lock:
        meta = S.browse_jobs.get(str(job))
    if not meta:
        return {"done": True, "path": "", "error": "Unknown folder-picker task."}
    output = Path(meta["output"])
    if output.is_file():
        try:
            value = json.loads(output.read_text(encoding="utf-8"))
        except Exception as exc:
            value = {"path": "", "error": f"Cannot decode selected folder: {exc}"}
        with S.lock:
            S.browse_jobs.pop(str(job), None)
        output.unlink(missing_ok=True)
        Path(meta["script"]).unlink(missing_ok=True)
        log(f"Выбор папки: {value.get('path') or 'отменён'}")
        return {"done": True, "path": value.get("path", ""), "error": value.get("error", "")}
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(meta["pid"]))
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return {"done": False, "path": "", "error": ""}
    except Exception:
        pass
    with S.lock:
        S.browse_jobs.pop(str(job), None)
    Path(meta["script"]).unlink(missing_ok=True)
    return {"done": True, "path": "", "error": "Folder dialog closed without returning a folder."}


def browse(initial=""):
    """Synchronous chooser retained for non-HTTP local callers."""
    if os.name != "nt":
        return "", "Выбор папки доступен только в приложении Windows."
    job, error = start_browse_job(initial)
    if error:
        return "", error
    deadline = time.time() + 300
    while time.time() < deadline:
        result = browse_job_status(job["job"])
        if result["done"]:
            return result["path"], result["error"]
        time.sleep(0.2)
    return "", "Окно выбора папки не завершилось за 5 минут."


def main():
    host = os.environ.get("SORT_IT_HOST", "127.0.0.1")
    port = int(os.environ.get("SORT_IT_PORT", "8765"))
    url = f"http://127.0.0.1:{port}/"
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        try:
            with urlopen(url + "api/config", timeout=2) as response:
                config = json.loads(response.read().decode("utf-8"))
            if response.status == 200 and config.get("ollama_model") == "qwen3.5:9b":
                if os.environ.get("SORT_IT_HEADLESS") != "1":
                    webbrowser.open(url)
                print("Sort IT уже запущен:", url, flush=True)
                return 0
        except Exception:
            pass
        print(f"Не удалось занять порт {port}: {exc}", flush=True)
        return 1
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if os.environ.get("SORT_IT_HEADLESS") == "1":
        print("Sort IT доступен:", url, flush=True)
        try:
            while True: time.sleep(3600)
        except KeyboardInterrupt: pass
    else:
        if os.environ.get("SORT_IT_AUTOSTART") != "1":
            webbrowser.open(url)
        print("Sort IT открыт:", url, flush=True)
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt: pass
    server.shutdown(); return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
