#!/usr/bin/env python3
"""
hf_fast_download.py — Descargador ultrarrápido para HuggingFace (CLI).

Generalización del script original: ahora acepta tanto URLs de archivo
directo como URLs de repositorio completo (baja todos los archivos),
detecta los hilos automáticamente según tu CPU, reanuda descargas
interrumpidas y verifica SHA-256 cuando HuggingFace lo expone.

Uso:
  python hf_fast_download.py <URL> [destino] [opciones]

Ejemplos:
  # Archivo directo (mantiene el formato del script original)
  python hf_fast_download.py "https://huggingface.co/adrepale/LTX2.3-10Eros-LoRA/resolve/main/10Eros_v1_Delta.safetensors" "G:\\models\\"

  # Repositorio completo (descarga TODOS los archivos)
  python hf_fast_download.py "https://huggingface.co/Qwen/Qwen2.5-7B" "G:\\models"

  # Solo listar los archivos de un repo sin descargar
  python hf_fast_download.py "https://huggingface.co/Qwen/Qwen2.5-7B" --list

  # Repos gated / privados: token por env var o flag
  set HF_TOKEN=hf_xxxxx
  python hf_fast_download.py "https://huggingface.co/meta-llama/Llama-3.1-8B" "G:\\models"

Opciones:
  --threads=N     Número de conexiones paralelas (por defecto: automático)
  --token=...     Token de HuggingFace (o variable de entorno HF_TOKEN)
  --revision=...  Rama/revisión del repo (por defecto: main)
  --file=...      Descargar solo archivos que coincidan (repetible, admite * glob)
  --list          Solo listar los archivos del repo
  --no-resume     No reanudar chunks parciales
  --no-verify     Omitir verificación SHA-256
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys

# Permitir importar hf_downloader desde el mismo directorio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hf_downloader as hd


def print_files_table(files):
    if not files:
        print("  (el repositorio no contiene archivos)")
        return
    name_w = max(len(f["path"]) for f in files)
    print()
    print(f"  {'ARCHIVO'.ljust(name_w)}  {'TAMAÑO'.rjust(12)}")
    print("  " + "-" * (name_w + 15))
    for f in files:
        size = hd.format_size(f["size"]) if f["size"] else "desconocido"
        print(f"  {f['path'].ljust(name_w)}  {size.rjust(12)}")
    total = sum(f["size"] for f in files)
    print("  " + "-" * (name_w + 15))
    print(f"  {'TOTAL'.ljust(name_w)}  {hd.format_size(total).rjust(12)}")
    print()


def run_job(job, out):
    """Ejecuta el trabajo imprimiendo progreso por consola."""
    last_done = set()
    state = job.state

    def on_progress(st):
        done = {f["path"] for f in st.files if f.get("fstate") == "done"}
        nonlocal last_done
        for path in done - last_done:
            print(f"  ✓ {path}")
        last_done = done
        if st.state in ("done", "error", "cancelled"):
            return
        pct = (st.downloaded / st.total * 100) if st.total else 0
        line = (
            f"\r  {hd.format_size(st.downloaded)}/{hd.format_size(st.total)} "
            f"({pct:.1f}%) | {hd.format_speed(st.speed)} | "
            f"ETA: {hd.format_time(st.eta)}   "
        )
        print(line, end="", flush=True)

    job.on_progress = on_progress
    job.run()
    print("\n")
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="hf_fast_download.py",
        description="Descargador ultrarrápido para HuggingFace (paralelo + resume + verificación).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("url", help="URL del repo o archivo en huggingface.co")
    parser.add_argument(
        "dest",
        nargs="?",
        default=None,
        help="Carpeta de destino (por defecto: ./downloads). Para un archivo directo "
        "puede ser una ruta de archivo completa, como en el script original.",
    )
    parser.add_argument("--threads", type=int, default=None, help="Conexiones paralelas (por defecto: automático)")
    parser.add_argument("--token", default=None, help="Token HF (o variable de entorno HF_TOKEN)")
    parser.add_argument("--revision", default=None, help="Rama/revisión (por defecto: main)")
    parser.add_argument("--file", action="append", default=[], help="Solo descargar archivos que coincidan (repetible, admite * )")
    parser.add_argument("--list", action="store_true", help="Solo listar los archivos del repo")
    parser.add_argument("--no-resume", action="store_true", help="No reanudar chunks parciales")
    parser.add_argument("--no-verify", action="store_true", help="Omitir verificación SHA-256")
    parser.add_argument("--overwrite", action="store_true", help="Sobrescribir archivos que ya existen en el destino (por defecto se omiten si están completos)")
    args = parser.parse_args(argv)

    token = args.token if args.token is not None else os.environ.get("HF_TOKEN", "")
    threads = args.threads
    if threads is not None and not (1 <= threads <= 128):
        print("  --threads debe estar entre 1 y 128")
        return 1

    print(f"\n{'=' * 60}")
    print("HuggingFace Fast Downloader")
    print(f"{'=' * 60}")

    if not token:
        print("  Nota: HF_TOKEN no definido — los repos gated/privados fallarán.")

    try:
        files, kind, org, repo, rev = hd.list_repo_files(args.url, token, args.revision)
    except ValueError as e:
        print(f"  Error: {e}")
        return 1

    kind_label = {"model": "modelo", "dataset": "dataset", "space": "space"}[kind]
    print(f"  Repo: {org}/{repo} ({kind_label}) — revisión '{rev}'")
    print(f"  Hilos: {threads if threads else hd.auto_threads()} (auto: {hd.auto_threads()})")

    if args.file:
        wanted = set()
        for pattern in args.file:
            wanted.update(f["path"] for f in files if fnmatch.fnmatch(f["path"], pattern))
        if not wanted:
            print("  Ningún archivo coincide con --file. Lista disponible:")
            print_files_table(files)
            return 1
        files = [f for f in files if f["path"] in wanted]

    if args.list:
        print(f"  {len(files)} archivo(s):")
        print_files_table(files)
        return 0

    if not files:
        print("  El repositorio no tiene archivos para descargar.")
        return 1

    print_files_table(files)

    # -- destino ---------------------------------------------------------
    if args.dest:
        dest = args.dest
    else:
        dest = os.path.join("downloads", org + "__" + repo)

    # Compatibilidad con el script original: si la URL es de archivo directo
    # y el destino parece una ruta de archivo, usarlo como archivo final.
    kind2, org2, repo2, rev2, filepath, mode = hd.parse_hf_url(args.url)
    dest_dir = dest
    if mode == "file" and len(files) == 1 and os.path.splitext(dest)[1]:
        files[0]["save_as"] = os.path.basename(dest)
        dest_dir = os.path.dirname(dest) or "."

    job_key = hd.repo_key(kind, org, repo, rev) if mode == "repo" else os.path.basename(files[0]["path"])
    conflict_policy = {}
    if args.overwrite:
        conflict_policy = {f["path"]: "overwrite" for f in files}
    job = hd.DownloadJob(
        files,
        dest_dir,
        job_key=job_key,
        kind=kind,
        org=org,
        repo=repo,
        rev=rev,
        threads=threads,
        token=token,
        resume=not args.no_resume,
        verify=not args.no_verify,
        conflict_policy=conflict_policy,
    )

    # las URLs ya las resuelve el motor; solo reportar destino
    print(f"  Destino: {os.path.abspath(dest_dir)}")
    print()

    state = run_job(job, sys.stdout)

    if state.state == "done":
        print("  " + state.message)
        print("\nSUCCESS!")
        return 0
    if state.state == "cancelled":
        print("  " + state.message)
        return 130
    if state.error:
        print(f"  {state.error}")
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
