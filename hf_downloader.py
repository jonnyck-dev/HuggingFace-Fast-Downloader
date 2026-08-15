"""
hf_downloader.py — Motor de descarga paralela para HuggingFace.

Generalización de los scripts originales `hf_fast_download.py` y
`hf_resume_download.py` (Desktop/Herramientas) en un único motor
reutilizable que usan tanto el CLI como la interfaz web.

Características:
  * Descarga paralela por chunks (HTTP Range) con N conexiones.
  * Detección automática del número de hilos según la CPU.
  * Reanudación automática de chunks parciales (resume).
  * Reintentos con backoff ante errores de red.
  * Verificación SHA-256 automática cuando HuggingFace la expone (LFS oid).
  * Soporta URLs de repositorio completo (baja todos los archivos) y
    URLs de archivo directo (/resolve/...).
  * Compatible con repositorios privados o gated vía token.

Solo usa la biblioteca estándar de Python — cero dependencias.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

__version__ = "1.3.0"

USER_AGENT = "HuggingFace-Fast-Downloader/1.0.0"
MAX_RETRIES = 3
READ_SIZE = 256 * 1024          # bytes leídos por iteración de red
COMBINE_BLOCK = 8 * 1024 * 1024 # bloque al unir chunks
SHA_BLOCK = 1024 * 1024         # bloque al calcular SHA-256
MIN_CHUNK_BYTES = 512 * 1024    # tamaño mínimo por chunk (para no fragmentar archivos pequeños)
MAX_LOG_LINES = 200

HF_URL_RE = re.compile(r"^https?://huggingface\.co/(.+)$", re.IGNORECASE)
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


# ---------------------------------------------------------------------------
# Utilidades de formato
# ---------------------------------------------------------------------------

def format_size(b):
    b = float(b or 0)
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.2f} {unit}"
        b /= 1024
    return f"{b:.2f} TB"


def format_speed(bps):
    return f"{format_size(bps)}/s"


def format_time(seconds):
    seconds = float(seconds or 0)
    if seconds < 0 or seconds != seconds:  # NaN guard
        return "--"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m {seconds % 60:.0f}s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"


def sanitize(name):
    """Convierte un nombre cualquiera en algo seguro para nombres de carpeta."""
    return _SAFE_RE.sub("_", str(name))


# ---------------------------------------------------------------------------
# Detección de hilos
# ---------------------------------------------------------------------------

def auto_threads():
    """Detecta un número razonable de conexiones paralelas según la CPU.

    Regla: 2 hilos por núcleo, mínimo 4 y máximo 32. El usuario siempre
    puede sobreescribirlo con --threads o desde la interfaz web.
    """
    cores = os.cpu_count() or 4
    return min(max(cores * 2, 4), 32)


# ---------------------------------------------------------------------------
# Parseo de URLs de HuggingFace
# ---------------------------------------------------------------------------

def parse_hf_url(url):
    """Interpreta una URL de huggingface.co.

    Retorna (kind, org, repo, revision, filepath, mode):
      kind     -> "model" | "dataset" | "space"
      mode     -> "repo" (descargar todo el repo) | "file" (archivo directo)
      filepath -> ruta del archivo dentro del repo (None en modo repo)

    Ejemplos:
      https://huggingface.co/Qwen/Qwen2.5-7B
      https://huggingface.co/Qwen/Qwen2.5-7B/main
      https://huggingface.co/Qwen/Qwen2.5-7B/resolve/main/model.safetensors
      https://huggingface.co/datasets/databricks/databricks-dolly-15k
    """
    m = HF_URL_RE.match(str(url).strip())
    if not m:
        raise ValueError(
            "La URL debe apuntar a huggingface.co (modelos, datasets o spaces)."
        )
    parts = [p for p in m.group(1).split("?")[0].strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError("URL de HuggingFace incompleta: faltan org/repo.")

    if parts[0] in ("datasets", "spaces"):
        kind, org, repo = parts[0], parts[1], parts[2]
        rest = parts[3:]
    else:
        kind, org, repo = "model", parts[0], parts[1]
        rest = parts[2:]

    revision, filepath, mode = "main", None, "repo"
    if rest:
        head = rest[0]
        if head in ("resolve", "blob", "raw"):
            # URL de archivo: /resolve/{revision}/{ruta/al/archivo}
            if len(rest) >= 2:
                revision = rest[1]
            if len(rest) >= 3:
                filepath = "/".join(rest[2:])
                mode = "file"
        elif head == "tree":
            # /tree/{revision} → repo en esa revisión
            revision = rest[1] if len(rest) >= 2 else "main"
        else:
            # rama directamente en la URL: huggingface.co/org/repo/{branch}
            revision = head

    return kind, org, repo, revision, filepath, mode


def _api_base(kind, org, repo):
    if kind == "dataset":
        return f"https://huggingface.co/api/datasets/{quote(org)}/{quote(repo)}"
    if kind == "space":
        return f"https://huggingface.co/api/spaces/{quote(org)}/{quote(repo)}"
    return f"https://huggingface.co/api/models/{quote(org)}/{quote(repo)}"


def file_download_url(kind, org, repo, revision, path):
    prefix = {
        "model": f"{org}/{repo}",
        "dataset": f"datasets/{org}/{repo}",
        "space": f"spaces/{org}/{repo}",
    }[kind]
    return (
        f"https://huggingface.co/{prefix}/resolve/"
        f"{quote(revision)}/{quote(path, safe='/')}"
    )


def repo_key(kind, org, repo, revision):
    return f"{org}__{repo}__{revision}"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _headers(token):
    h = {"User-Agent": USER_AGENT}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _http_json(url, token, timeout=60):
    try:
        req = urllib.request.Request(url, headers=_headers(token))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise ValueError(
            f"Error {e.code} al consultar HuggingFace ({url.split('?')[0]}). "
            f"{body.strip()}"
        ) from e


def get_file_size(url, token=""):
    """Tamaño total de un archivo vía petición HEAD."""
    req = urllib.request.Request(url, headers=_headers(token), method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as resp:
        length = resp.headers.get("Content-Length")
        return int(length) if length else 0


def list_repo_files(url, token="", revision=None):
    """Lista los archivos de un repo (o de una URL de archivo directo).

    Retorna (files, kind, org, repo, revision) donde files es una lista de
    dicts: {"path", "size", "sha256"} (sha256 = oid LFS cuando existe).
    """
    kind, org, repo, rev, filepath, mode = parse_hf_url(url)
    if revision:
        rev = revision

    if mode == "file":
        dl = file_download_url(kind, org, repo, rev, filepath)
        size = get_file_size(dl, token)
        return [{"path": filepath, "size": size, "sha256": None}], kind, org, repo, rev

    api = f"{_api_base(kind, org, repo)}/tree/{quote(rev)}?recursive=true&expand=true"
    data = _http_json(api, token)

    files = []
    for entry in data or []:
        if entry.get("type") != "file":
            continue  # ignorar directorios y symlinks
        lfs = entry.get("lfs") or {}
        files.append(
            {
                "path": entry["path"],
                "size": int(entry.get("size") or 0),
                "sha256": lfs.get("oid") or None,
            }
        )
    files.sort(key=lambda f: f["path"])
    return files, kind, org, repo, rev


# ---------------------------------------------------------------------------
# Estado del trabajo (compartido entre hilos)
# ---------------------------------------------------------------------------

class JobState:
    def __init__(self, files, total):
        self.lock = threading.Lock()
        self.files = files           # lista de dicts compartida
        self.total = total
        self.downloaded = 0
        self.speed = 0.0
        self.eta = 0.0
        self.elapsed = 0.0
        self.start_time = None
        self.state = "running"       # running | done | error | cancelled
        self.error = None
        self.message = ""
        self.log = []

    def add_downloaded(self, idx, n):
        if n <= 0:
            return
        with self.lock:
            self.downloaded += n
            f = self.files[idx]
            f["downloaded"] = f.get("downloaded", 0) + n
            if f["downloaded"] >= f["size"]:
                f["fstate"] = "done"

    def set_file_state(self, idx, fstate):
        with self.lock:
            self.files[idx]["fstate"] = fstate

    def chunk_result(self, idx, ok):
        with self.lock:
            f = self.files[idx]
            f["chunks_total"] = f.get("chunks_total", 0) + 1
            if ok:
                f["chunks_ok"] = f.get("chunks_ok", 0) + 1

    def log_msg(self, msg):
        with self.lock:
            self.log.append(msg)
            if len(self.log) > MAX_LOG_LINES:
                del self.log[: len(self.log) - MAX_LOG_LINES]

    def snapshot(self):
        with self.lock:
            return {
                "state": self.state,
                "message": self.message,
                "error": self.error,
                "total_bytes": self.total,
                "downloaded_bytes": self.downloaded,
                "speed_bps": self.speed,
                "eta_seconds": self.eta,
                "elapsed_seconds": self.elapsed,
                "files": [
                    {
                        "path": f["path"],
                        "size": f["size"],
                        "downloaded": f.get("downloaded", 0),
                        "state": f.get("fstate", "pending"),
                    }
                    for f in self.files
                ],
                "log": list(self.log),
            }


# ---------------------------------------------------------------------------
# Descarga de un chunk (con resume y reintentos)
# ---------------------------------------------------------------------------

def _download_chunk(url, token, start, end, full_size, part_path, state, idx, cancel, resume):
    """Descarga el rango [start, end] a part_path. Retorna True si quedó completo."""
    expected = end - start + 1

    def _existing():
        return os.path.getsize(part_path) if os.path.exists(part_path) else 0

    existing = _existing()
    if existing >= expected:
        state.add_downloaded(idx, expected)  # chunk ya completo de una corrida anterior
        return True

    first_attempt = True
    for attempt in range(1, MAX_RETRIES + 1):
        if cancel.is_set():
            return False
        existing = _existing()
        if existing >= expected:
            state.add_downloaded(idx, expected)
            return True

        if first_attempt and resume and existing > 0:
            # Bytes ya bajados en una corrida anterior del mismo trabajo
            state.add_downloaded(idx, existing)
        first_attempt = False

        start_from = start + existing
        headers = _headers(token)
        headers["Range"] = f"bytes={start_from}-{end}"
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except Exception as e:
            state.log_msg(f"  {os.path.basename(part_path)}: error de conexión ({e}) — reintentando {attempt}/{MAX_RETRIES}")
            time.sleep(min(2 ** attempt, 8))
            continue

        code = resp.getcode()
        if code != 206:
            # Servidor que ignora Range: solo aceptable si es el archivo completo
            if code == 200 and start == 0 and end == full_size - 1 and existing == 0:
                state.log_msg("  Servidor ignora Range — descarga completa en 1 chunk")
            else:
                state.log_msg(f"  {os.path.basename(part_path)}: respuesta inesperada HTTP {code}")
                resp.close()
                return False

        try:
            mode = "ab" if (resume and existing > 0) else "wb"
            with open(part_path, mode) as f:
                while True:
                    if cancel.is_set():
                        return False
                    chunk = resp.read(READ_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    state.add_downloaded(idx, len(chunk))
        except Exception as e:
            if attempt < MAX_RETRIES:
                state.log_msg(f"  {os.path.basename(part_path)}: error ({e}) — reintentando {attempt + 1}/{MAX_RETRIES}")
                time.sleep(min(2 ** attempt, 8))
                continue
            state.log_msg(f"  {os.path.basename(part_path)}: falló tras {MAX_RETRIES} intentos ({e})")
            return False
        finally:
            try:
                resp.close()
            except Exception:
                pass

        if _existing() >= expected:
            return True
        state.log_msg(f"  {os.path.basename(part_path)}: tamaño incorrecto — reintentando")
    return False


def _stream_file(url, token, dest_path, state, idx, cancel):
    """Descarga sin Range (archivos de tamaño desconocido)."""
    state.set_file_state(idx, "downloading")
    for attempt in range(1, MAX_RETRIES + 1):
        if cancel.is_set():
            return False
        try:
            resp = urllib.request.urlopen(
                urllib.request.Request(url, headers=_headers(token)), timeout=60
            )
        except Exception as e:
            state.log_msg(f"  {os.path.basename(dest_path)}: error de conexión ({e}) — reintentando {attempt}/{MAX_RETRIES}")
            time.sleep(min(2 ** attempt, 8))
            continue
        try:
            with open(dest_path, "wb") as f:
                while True:
                    if cancel.is_set():
                        return False
                    chunk = resp.read(READ_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    state.add_downloaded(idx, len(chunk))
            return True
        except Exception as e:
            if attempt < MAX_RETRIES:
                state.log_msg(f"  {os.path.basename(dest_path)}: error ({e}) — reintentando {attempt + 1}/{MAX_RETRIES}")
                time.sleep(min(2 ** attempt, 8))
                continue
            state.log_msg(f"  {os.path.basename(dest_path)}: falló tras {MAX_RETRIES} intentos ({e})")
            return False
    return False


# ---------------------------------------------------------------------------
# Trabajo de descarga (orquesta archivos + chunks + hilos)
# ---------------------------------------------------------------------------

class DownloadJob:
    def __init__(
        self,
        files,
        dest_dir,
        job_key,
        kind="model",
        org="",
        repo="",
        rev="main",
        threads=None,
        token="",
        on_progress=None,
        resume=True,
        verify=True,
        conflict_policy=None,
    ):
        self.files = [dict(f, downloaded=0, fstate="pending") for f in files]
        self.total = sum(f["size"] for f in self.files)
        self.dest_dir = dest_dir
        self.job_key = job_key
        self.kind = kind
        self.org = org
        self.repo = repo
        self.rev = rev
        self.threads = threads or auto_threads()
        self.token = token or ""
        self.resume = resume
        self.verify = verify and any(f["sha256"] for f in self.files)
        self.on_progress = on_progress
        self.cancel = threading.Event()
        self.temp_dir = os.path.join(dest_dir, ".chunks_" + sanitize(job_key))
        self.conflict_policy = conflict_policy or {}
        self.state = JobState(self.files, self.total)
        self._apply_conflicts()

    # -- helpers internos ------------------------------------------------

    def _part_path(self, idx, start_byte):
        return os.path.join(self.temp_dir, f"f{idx}_c{start_byte}.part")

    def _save_path(self, f):
        # "save_as" permite guardar el archivo con otro nombre (CLI legacy / renombrar)
        rel = f.get("save_as") or f["path"]
        return os.path.join(self.dest_dir, rel)

    # -- manejo de archivos existentes (no pisar sin permiso) ----------------

    def _next_rename(self, f):
        """Calcula un nombre alternativo tipo README_1.md si ya existe."""
        rel = f.get("save_as") or f["path"]
        base, ext = os.path.splitext(rel)
        n = 1
        while True:
            cand = f"{base}_{n}{ext}"
            if not os.path.exists(os.path.join(self.dest_dir, cand)):
                return cand
            n += 1

    def _apply_conflicts(self):
        """Aplica la política ante archivos que ya existen en el destino.

        conflict_policy: {path: "overwrite" | "rename" | "skip"}
        Sin política (auto): si el archivo final ya existe y está completo,
        no se vuelve a descargar (comportamiento resume del script original).
        """
        policy = self.conflict_policy or {}
        kept = []
        for f in self.files:
            path = f["path"]
            dest = os.path.join(self.dest_dir, f.get("save_as") or path)
            exists = os.path.exists(dest)
            p = policy.get(path, "auto")

            if p == "skip":
                self.state.log_msg(f"⏭ {path}: omitido por decisión del usuario")
                continue

            if p == "rename":
                if exists:
                    new_rel = self._next_rename(f)
                    f["save_as"] = new_rel
                    self.state.log_msg(f"📝 {path}: ya existe → se guardará como {new_rel}")
                kept.append(f)
                continue

            if p == "overwrite":
                if exists:
                    self.state.log_msg(f"♻️ {path}: se sobrescribirá (decisión del usuario)")
                kept.append(f)
                continue

            # auto: reanudar — archivo completo en destino = ya descargado
            if exists:
                if f["size"] > 0 and os.path.getsize(dest) >= f["size"]:
                    f["fstate"] = "done"
                    f["downloaded"] = f["size"]
                    f["_complete"] = True
                    self.state.log_msg(f"✓ {path}: ya descargado en destino — omitido")
                    kept.append(f)
                    continue
                if f["size"] == 0:
                    f["fstate"] = "done"
                    f["_complete"] = True
                    self.state.log_msg(f"✓ {path}: ya existe en destino — omitido")
                    kept.append(f)
                    continue
            kept.append(f)

        self.files[:] = kept
        self.total = sum(f["size"] for f in self.files)
        self.state.total = self.total

    def _worker(self, q):
        while True:
            try:
                task = q.get(timeout=1)
            except queue.Empty:
                return
            try:
                idx, start, end, full_size = task
                f = self.files[idx]
                self.state.set_file_state(idx, "downloading")
                url = f.get("url") or file_download_url(self.kind, self.org, self.repo, self.rev, f["path"])
                if start < 0:
                    # tamaño desconocido → stream directo sin Range
                    ok = _stream_file(url, self.token, self._save_path(f), self.state, idx, self.cancel)
                else:
                    ok = _download_chunk(
                        url, self.token, start, end, full_size,
                        self._part_path(idx, start), self.state, idx, self.cancel, self.resume,
                    )
                self.state.chunk_result(idx, ok)
            finally:
                q.task_done()

    def _build_tasks(self, q):
        """Reparte cada archivo en chunks y los mete en la cola."""
        for idx, f in enumerate(self.files):
            if f.get("_complete"):
                continue  # ya descargado en una corrida anterior
            size = f["size"]
            if size <= 0:
                q.put((idx, -1, -1, 0))  # tamaño desconocido → stream directo
                continue
            nchunks = min(self.threads, max(1, size // MIN_CHUNK_BYTES))
            base = size // nchunks
            for c in range(nchunks):
                s = c * base
                e = (s + base - 1) if c < nchunks - 1 else size - 1
                q.put((idx, s, e, size))

    # -- flujo principal --------------------------------------------------

    def run(self):
        state = self.state
        state.start_time = time.time()

        try:
            os.makedirs(self.dest_dir, exist_ok=True)
            os.makedirs(self.temp_dir, exist_ok=True)
        except OSError as e:
            state.state = "error"
            state.error = f"No se pudo crear la carpeta de destino: {e}"
            self._final_report()
            return

        state.log_msg(f"Hilos de descarga: {self.threads}")
        state.log_msg(f"Destino: {os.path.abspath(self.dest_dir)}")
        state.log_msg(f"Archivos: {len(self.files)} — Total: {format_size(self.total)}")

        q = queue.Queue()
        self._build_tasks(q)

        stop = threading.Event()

        def reporter():
            last_t, last_b = time.time(), 0
            while not stop.wait(0.5):
                now = time.time()
                cur = state.downloaded
                dt = now - last_t
                if dt > 0:
                    state.speed = (cur - last_b) / dt
                    state.eta = (state.total - cur) / state.speed if state.speed > 0 else 0
                state.elapsed = now - state.start_time
                if self.on_progress:
                    self.on_progress(state)
                last_t, last_b = now, cur

        workers = [
            threading.Thread(target=self._worker, args=(q,), daemon=True)
            for _ in range(self.threads)
        ]
        rep = threading.Thread(target=reporter, daemon=True)
        rep.start()
        for w in workers:
            w.start()

        for w in workers:
            w.join()
        stop.set()
        rep.join(timeout=2)

        if self.cancel.is_set():
            state.state = "cancelled"
            state.message = "Descarga cancelada — los chunks parciales se conservan para reanudar."
            state.log_msg(state.message)
            self._final_report()
            return

        # ¿Algún archivo con chunks fallidos?
        failed = [
            f for f in self.files
            if f.get("chunks_total", 0) != 0
            and f.get("chunks_ok", 0) != f.get("chunks_total", 0)
        ]
        if failed:
            names = ", ".join(f["path"] for f in failed[:5])
            state.state = "error"
            state.error = (
                f"Hubo errores descargando: {names}. "
                f"Re-ejecuta con la misma carpeta para reanudar desde lo ya bajado."
            )
            state.log_msg(state.error)
            self._final_report()
            return

        # Unir chunks y verificar
        self._combine_and_verify()

        if state.state == "running":
            state.state = "done"
            state.message = f"Descarga completada en {format_time(state.elapsed)}"
            state.log_msg(state.message)
        self._final_report()

    def _combine_and_verify(self):
        state = self.state

        all_ok = True
        for idx, f in enumerate(self.files):
            if f.get("_complete"):
                state.set_file_state(idx, "done")
                continue
            dest_path = self._save_path(f)
            os.makedirs(os.path.dirname(dest_path) or self.dest_dir, exist_ok=True)

            if f["size"] <= 0:
                state.set_file_state(idx, "done")
                state.log_msg(f"✓ {f['path']}")
                continue

            state.set_file_state(idx, "combining")
            state.log_msg(f"Uniendo chunks: {f['path']}")
            try:
                with open(dest_path, "wb") as out:
                    nchunks = min(self.threads, max(1, f["size"] // MIN_CHUNK_BYTES))
                    for c in range(nchunks):
                        s = c * (f["size"] // nchunks)
                        part = self._part_path(idx, s)
                        with open(part, "rb") as p:
                            while True:
                                data = p.read(COMBINE_BLOCK)
                                if not data:
                                    break
                                out.write(data)
            except OSError as e:
                state.state = "error"
                state.error = f"Error uniendo {f['path']}: {e}"
                state.log_msg(state.error)
                all_ok = False
                continue

            # Verificación SHA-256 (si HuggingFace la expone vía LFS oid)
            sha256 = f.get("sha256")
            if sha256 and self.verify:
                state.log_msg(f"Verificando SHA-256: {f['path']} ...")
                h = hashlib.sha256()
                try:
                    with open(dest_path, "rb") as fh:
                        while True:
                            data = fh.read(SHA_BLOCK)
                            if not data:
                                break
                            h.update(data)
                except OSError as e:
                    state.state = "error"
                    state.error = f"No se pudo leer {dest_path}: {e}"
                    all_ok = False
                    continue
                if h.hexdigest() == sha256:
                    state.log_msg(f"✓ SHA-256 correcto: {f['path']}")
                    state.set_file_state(idx, "done")
                else:
                    state.log_msg(f"✗ SHA-256 NO coincide: {f['path']}")
                    state.state = "error"
                    state.error = f"El archivo {f['path']} está corrupto (SHA-256 no coincide)."
                    all_ok = False
            else:
                state.set_file_state(idx, "done")
                state.log_msg(f"✓ {f['path']}")

        if all_ok:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        else:
            state.log_msg(f"Chunks parciales conservados en: {self.temp_dir}")

    def _final_report(self):
        if self.on_progress:
            self.on_progress(self.state)
