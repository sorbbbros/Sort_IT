"""Windows entry point for source and bundled Sort IT."""
import base64
import json
import os
import sys
import traceback
from pathlib import Path


def pick_folder():
    import tkinter as tk
    from tkinter import filedialog
    initial = base64.b64decode(sys.argv[2]).decode("utf-8")
    destination = Path(base64.b64decode(sys.argv[3]).decode("utf-8"))
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(parent=root, title="Sort IT — Выберите папку",
                                          initialdir=initial if Path(initial).is_dir() else None,
                                          mustexist=True)
        result = {"path": selected or "", "error": ""}
    except Exception as exc:
        result = {"path": "", "error": str(exc)}
    finally:
        root.destroy()
    destination.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--self-test":
        import tempfile
        import photo_helper_web as web
        from PIL import Image
        import cv2
        import numpy
        import pillow_heif
        import tkinter
        base = Path(tempfile.mkdtemp(prefix="sortit-check-"))
        image = base / "sample.png"
        Image.new("RGB", (32, 32), "green").save(image)
        web.make_cleanup_gallery(base, [{"path": str(image), "kept": "", "reason": "test", "detail": "test"}])
        assert (base / "обзор.html").is_file()
        assert web.UI.is_file()
        assert (web.HERE / "app_resources" / "gallery.css").is_file()
        Path(sys.argv[2]).write_text(json.dumps({"ok": True, "opencv": cv2.__version__, "numpy": numpy.__version__, "ui": True, "gallery": True}), encoding="utf-8")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--pick-folder":
        pick_folder()
        return
    data = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Sort IT"
    data.mkdir(parents=True, exist_ok=True)
    log_file = (data / "startup.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log_file
    try:
        import setup_tools
        setup_tools.check_tools()
        import photo_helper_web
        return photo_helper_web.main()
    except Exception:
        traceback.print_exc()
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Sort IT", "Ошибка запуска. Подробности в " + str(data / "startup.log"))
        root.destroy()
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
