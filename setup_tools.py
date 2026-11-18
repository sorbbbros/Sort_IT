"""Start the existing local Ollama installation on the owner's PC."""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

MODEL = "qwen3.5:9b"
MODELS_DIRECTORY = Path(r"Q:\AI\Ollama\ollama-models")
OLLAMA_APP = Path(r"Q:\AI\Ollama\Ollama\ollama app.exe")


def ready():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as response:
            return MODEL in [item["name"] for item in json.load(response).get("models", [])]
    except Exception:
        return False


def check_tools():
    os.environ["OLLAMA_MODELS"] = str(MODELS_DIRECTORY)
    if ready():
        return
    if OLLAMA_APP.is_file():
        subprocess.Popen([str(OLLAMA_APP)], env=os.environ.copy(),
                         creationflags=subprocess.CREATE_NO_WINDOW,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        for _ in range(30):
            if ready():
                return
            time.sleep(1)
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showwarning("Sort IT", "Ollama или модель qwen3.5:9b недоступны.\n"
                           "Проверьте Ollama и каталог Q:\\AI\\Ollama\\ollama-models.\n"
                           "Приложение откроется. Очистка доступна без ИИ.")
    root.destroy()
