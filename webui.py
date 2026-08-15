#!/usr/bin/env python3
"""
webui.py — Interfaz web local para HuggingFace Fast Downloader.

Levanta un servidor HTTP (solo biblioteca estándar, cero dependencias)
que sirve la página `index.html` y una pequeña API JSON:

  GET  /                     → página principal
  GET  /api/health           → versión + hilos detectados
  GET  /api/files?url=...    → lista archivos del repo (o del archivo directo)
  GET  /api/browse?path=...  → navegación de carpetas del sistema
  POST /api/download         → inicia una descarga  {url, dest, threads, token, revision, files[]}
  GET  /api/status?id=...    → estado/progreso de un trabajo
  POST /api/cancel           → cancela un trabajo {id}
  POST /api/open_folder      → abre una carpeta en el explorador {path}

Uso:
  python webui.py [--port 8000] [--host 127.0.0.1] [--no-browser]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Permitir importar hf_downloader desde el mismo directorio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hf_downloader as hd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(SCRIPT_DIR, "index.html")

# ── Soporte para PyInstaller (ejecutable portable) ────────────────────────
IS_FROZEN = getattr(sys, "frozen", False)


def resource_path(rel):
    """Ruta de un recurso empaquetado: con PyInstaller los archivos adjuntos
    se extraen a una carpeta temporal (sys._MEIPASS) en cada ejecución."""
    base = getattr(sys, "_MEIPASS", SCRIPT_DIR)
    return os.path.join(base, rel)


def exe_dir():
    """Directorio del ejecutable (para logs / carpeta junto al .exe)."""
    if IS_FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return SCRIPT_DIR


if IS_FROZEN:
    # Sin consola (--windowed): redirigir stdout/stderr a un archivo de log
    # junto al .exe para poder depurar si algo falla.
    try:
        log_path = os.path.join(exe_dir(), "HuggingFace-Fast-Downloader.log")
        _logf = open(log_path, "a", buffering=1, encoding="utf-8")
        sys.stdout = _logf
        sys.stderr = _logf
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] --- inicio ---")
    except Exception:
        pass

JOBS = {}          # job_id -> JobHandle
JOBS_LOCK = threading.Lock()


def default_dest():
    base = os.path.expanduser("~")
    if os.name == "nt":
        candidate = os.path.join(base, "Downloads", "HuggingFace")
    else:
        candidate = os.path.join(base, "Downloads", "HuggingFace")
    return candidate


class JobHandle:
    """Envuelve un DownloadJob en un hilo de fondo + snapshot serializable."""

    def __init__(self, job_id, job, dest_dir, repo_label):
        self.id = job_id
        self.job = job
        self.dest_dir = dest_dir
        self.repo_label = repo_label
        self.created = time.time()
        self.snapshot = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        def cb(state):
            snap = state.snapshot()
            snap["id"] = self.id
            snap["repo"] = self.repo_label
            snap["dest"] = self.dest_dir
            self.snapshot = snap

        self.job.on_progress = cb
        self.job.run()
        # snapshot final (estado done/error/cancelled ya actualizado por el motor)
        self.job.on_progress(self.job.state)

    def start(self):
        self.thread.start()


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

def api_health():
    return {"ok": True, "version": hd.__version__, "threads_detected": hd.auto_threads()}


def api_files(params):
    url = (params.get("url") or [""])[0].strip()
    if not url:
        raise ValueError("Falta la URL.")
    token = (params.get("token") or [""])[0]
    revision = (params.get("revision") or [""])[0] or None
    files, kind, org, repo, rev = hd.list_repo_files(url, token, revision)
    return {
        "kind": kind,
        "org": org,
        "repo": repo,
        "revision": rev,
        "files": files,
        "total_bytes": sum(f["size"] for f in files),
        "threads_detected": hd.auto_threads(),
    }


def api_browse(params):
    path = (params.get("path") or [""])[0]

    if os.name == "nt" and (not path or path == "root"):
        drives = [f"{d}:\\" for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{d}:\\")]
        return {"path": "", "parent": None, "is_root": True, "drives": drives, "dirs": []}

    if not path:
        path = os.path.sep
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise ValueError(f"No existe la carpeta: {path}")

    dirs = sorted(
        (d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d))),
        key=str.lower,
    )
    parent = os.path.dirname(path)
    if parent == path:  # raíz de disco
        parent = None
    is_root = parent is None
    return {"path": path, "parent": parent, "is_root": is_root, "drives": [], "dirs": dirs}


def api_download(body):
    url = (body.get("url") or "").strip()
    if not url:
        raise ValueError("Falta la URL.")
    dest = (body.get("dest") or "").strip() or default_dest()
    threads = int(body.get("threads") or 0) or None
    token = body.get("token") or ""
    revision = body.get("revision") or None
    selected = body.get("files") or []

    if threads is not None and not (1 <= threads <= 128):
        raise ValueError("Los hilos deben estar entre 1 y 128.")

    files, kind, org, repo, rev = hd.list_repo_files(url, token, revision)
    if selected:
        wanted = set(selected)
        files = [f for f in files if f["path"] in wanted]
    if not files:
        raise ValueError("No hay archivos seleccionados para descargar.")

    dest = os.path.abspath(os.path.expanduser(dest))
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        raise ValueError(f"No se pudo crear la carpeta de destino: {e}")

    job_key = hd.repo_key(kind, org, repo, rev)
    job = hd.DownloadJob(
        files,
        dest,
        job_key=job_key,
        kind=kind,
        org=org,
        repo=repo,
        rev=rev,
        threads=threads,
        token=token,
    )
    job_id = uuid.uuid4().hex[:10]
    handle = JobHandle(job_id, job, dest, f"{org}/{repo}")
    with JOBS_LOCK:
        JOBS[job_id] = handle
    handle.start()

    return {
        "id": job_id,
        "repo": f"{org}/{repo}",
        "dest": dest,
        "threads": job.threads,
        "files_count": len(files),
        "total_bytes": job.total,
    }


def api_status(params):
    job_id = (params.get("id") or [""])[0]
    with JOBS_LOCK:
        handle = JOBS.get(job_id)
    if not handle:
        raise ValueError("Trabajo no encontrado.")
    if handle.snapshot is None:
        return {"id": job_id, "repo": handle.repo_label, "dest": handle.dest_dir,
                "state": "starting", "total_bytes": handle.job.total,
                "downloaded_bytes": 0, "speed_bps": 0, "eta_seconds": 0,
                "elapsed_seconds": 0, "files": [], "log": []}
    return handle.snapshot


def api_cancel(body):
    job_id = body.get("id")
    with JOBS_LOCK:
        handle = JOBS.get(job_id)
    if not handle:
        raise ValueError("Trabajo no encontrado.")
    handle.job.cancel.set()
    return {"ok": True, "id": job_id}


def api_open_folder(body):
    path = body.get("path")
    if not path:
        raise ValueError("Falta la ruta.")
    if not os.path.isdir(path):
        raise ValueError(f"No existe la carpeta: {path}")
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        os.system(f'open "{path}"')
    else:
        os.system(f'xdg-open "{path}"')
    return {"ok": True}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "HuggingFaceFastDownloader/1.0"

    # -- utilidades ------------------------------------------------------

    def _send_json(self, obj, code=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text, code=200, ctype="text/plain; charset=utf-8"):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("JSON inválido en el cuerpo de la petición.")

    def log_message(self, fmt, *args):
        # Silencio: el progreso real se ve en la interfaz web
        pass

    # -- rutas -----------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        try:
            if path == "/" or path == "/index.html":
                index = resource_path("index.html")
                if not os.path.exists(index):
                    self._send_text("index.html no encontrado junto a webui.py", 500)
                    return
                with open(index, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/health":
                self._send_json(api_health())
            elif path == "/api/files":
                self._send_json(api_files(params))
            elif path == "/api/browse":
                self._send_json(api_browse(params))
            elif path == "/api/status":
                self._send_json(api_status(params))
            else:
                self._send_json({"error": f"Ruta no encontrada: {path}"}, 404)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
        except Exception as e:
            self._send_json({"error": f"Error interno: {e}"}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_body()
            if path == "/api/download":
                self._send_json(api_download(body))
            elif path == "/api/cancel":
                self._send_json(api_cancel(body))
            elif path == "/api/open_folder":
                self._send_json(api_open_folder(body))
            else:
                self._send_json({"error": f"Ruta no encontrada: {path}"}, 404)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
        except Exception as e:
            self._send_json({"error": f"Error interno: {e}"}, 500)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Interfaz web local de HuggingFace Fast Downloader.")
    parser.add_argument("--host", default="127.0.0.1", help="IP donde escuchar (por defecto: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Puerto (por defecto: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="No abrir el navegador automáticamente")
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"

    print("=" * 56)
    print("  🚀 HuggingFace Fast Downloader — Web UI")
    print("=" * 56)
    print(f"  Abre:        {url}")
    print(f"  Hilos auto:  {hd.auto_threads()}")
    print(f"  Destino:     {default_dest()}")
    print("  Ctrl+C para salir")
    print("=" * 56)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Hasta luego 👋")
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
