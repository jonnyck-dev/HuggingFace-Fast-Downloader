#!/usr/bin/env python3
"""
gui.py — Interfaz de escritorio (ventana nativa) para HuggingFace Fast Downloader.

Aplicación de ventana Tkinter (viene con Python, cero dependencias extra).
Cerrar la ventana cierra el proceso y cancela las descargas activas.

Uso:
  python gui.py            # abre la ventana
  python gui.py --self-test  # abre la ventana 3s y sale (para verificar builds)
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hf_downloader as hd

# ── Soporte PyInstaller: redirigir stdout/stderr a un log junto al .exe ────
IS_FROZEN = getattr(sys, "frozen", False)

def exe_dir():
    if IS_FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


if IS_FROZEN:
    try:
        _logf = open(os.path.join(exe_dir(), "HuggingFace-Fast-Downloader.log"), "a", buffering=1, encoding="utf-8")
        sys.stdout = _logf
        sys.stderr = _logf
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] --- inicio gui ---")
    except Exception:
        pass

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ── Paleta (igual que la web) ──────────────────────────────────────────────
C_BG = "#0d1117"
C_CARD = "#161b22"
C_CARD2 = "#1c2333"
C_BORDER = "#30363d"
C_TEXT = "#e6edf3"
C_MUTED = "#8b949e"
C_ACCENT = "#ff7a1a"
C_ACCENT2 = "#ffb000"
C_GREEN = "#3fb950"
C_RED = "#f85149"
C_BLUE = "#58a6ff"

FONT = "Segoe UI"
MONO = "Consolas"

DEST_DEFAULT = os.path.join(os.path.expanduser("~"), "Downloads", "HuggingFace")


class App:
    def __init__(self, root):
        self.root = root
        self.repo_info = None        # {kind, org, repo, revision, files, total_bytes}
        self.all_files = []          # todos los archivos del repo
        self.selected = set()        # rutas seleccionadas
        self.row_path = {}           # iid -> path
        self.job = None
        self._latest = None          # snapshot del estado del trabajo
        self._log_pos = 0            # líneas de log ya mostradas
        self._sel_total = 0

        self._setup_style()
        self._build_ui()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(250, self._poll)

    # ── estilos ────────────────────────────────────────────────────────
    def _setup_style(self):
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=C_BG, foreground=C_TEXT, font=(FONT, 10))
        st.configure("Card.TFrame", background=C_CARD, relief="flat")
        st.configure("TLabel", background=C_BG, foreground=C_TEXT)
        st.configure("Muted.TLabel", background=C_BG, foreground=C_MUTED)
        st.configure("CardLabel.TLabel", background=C_CARD, foreground=C_TEXT)
        st.configure("MutedCard.TLabel", background=C_CARD, foreground=C_MUTED)
        st.configure("Title.TLabel", background=C_BG, foreground=C_ACCENT, font=(FONT, 18, "bold"))
        st.configure("Step.TLabel", background=C_CARD, foreground=C_ACCENT, font=(FONT, 11, "bold"))
        st.configure(
            "TButton",
            background=C_CARD2, foreground=C_TEXT, borderwidth=1,
            focusthickness=0, padding=(12, 6),
        )
        st.map("TButton", background=[("active", "#273042")], bordercolor=[("focus", C_ACCENT)])
        st.configure("Primary.TButton", background=C_ACCENT, foreground="#1a0f00", font=(FONT, 11, "bold"))
        st.map("Primary.TButton", background=[("active", C_ACCENT2), ("disabled", "#6b4a1f")])
        st.configure(
            "TEntry", fieldbackground=C_BG, foreground=C_TEXT,
            bordercolor=C_BORDER, lightcolor=C_BORDER, darkcolor=C_BORDER, insertcolor=C_TEXT,
        )
        st.configure("TSpinbox", fieldbackground=C_BG, foreground=C_TEXT, arrowcolor=C_MUTED, bordercolor=C_BORDER)
        st.configure(
            "Treeview",
            background=C_CARD, fieldbackground=C_CARD, foreground=C_TEXT,
            bordercolor=C_BORDER, rowheight=26, font=(FONT, 9),
        )
        st.map("Treeview", background=[("selected", "#2d333b")], foreground=[("selected", C_TEXT)])
        st.configure(
            "Treeview.Heading",
            background=C_CARD2, foreground=C_TEXT, bordercolor=C_BORDER,
            font=(FONT, 9, "bold"), padding=(4, 4),
        )
        st.configure(
            "TProgressbar",
            background=C_ACCENT, troughcolor=C_BG, bordercolor=C_BORDER, lightcolor=C_ACCENT, darkcolor=C_ACCENT,
        )
        st.configure("TScrollbar", background=C_CARD2, troughcolor=C_BG, bordercolor=C_BORDER, arrowcolor=C_MUTED)
        st.configure("TNotebook", background=C_BG, bordercolor=C_BORDER)
        st.configure("TNotebook.Tab", background=C_CARD2, foreground=C_TEXT, padding=(10, 5))
        st.map("TNotebook.Tab", background=[("selected", C_CARD)])

    # ── interfaz ───────────────────────────────────────────────────────
    def _card(self, parent):
        f = ttk.Frame(parent, style="Card.TFrame", padding=14)
        return f

    def _build_ui(self):
        root = self.root
        root.title("HuggingFace Fast Downloader")
        root.configure(bg=C_BG)
        root.geometry("880x720")
        root.minsize(760, 620)

        outer = ttk.Frame(root, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="🚀  HuggingFace Fast Downloader", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Descarga modelos, datasets y spaces a máxima velocidad. Cerrar esta ventana cierra el proceso.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 12))

        # ── Paso 1: URL ────────────────────────────────────────────────
        c1 = self._card(outer)
        c1.pack(fill="x", pady=(0, 10))
        ttk.Label(c1, text="1. Pega la URL del modelo o del archivo", style="Step.TLabel").pack(anchor="w")
        row = ttk.Frame(c1, style="Card.TFrame")
        row.pack(fill="x", pady=(6, 0))
        self.url_var = tk.StringVar()
        self.url_var.set("https://huggingface.co/Qwen/Qwen2.5-7B")
        self.url_entry = ttk.Entry(row, textvariable=self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.detect_btn = ttk.Button(row, text="🔍 Detectar archivos", style="Primary.TButton", command=self.on_detect)
        self.detect_btn.pack(side="left")
        self.detect_msg = ttk.Label(c1, text="", style="MutedCard.TLabel")
        self.detect_msg.pack(anchor="w", pady=(4, 0))

        # ── Paso 2: archivos ───────────────────────────────────────────
        c2 = self._card(outer)
        c2.pack(fill="both", expand=True, pady=(0, 10))
        ttk.Label(c2, text="2. Elige qué descargar", style="Step.TLabel").pack(anchor="w")
        tools = ttk.Frame(c2, style="Card.TFrame")
        tools.pack(fill="x", pady=(6, 6))
        ttk.Button(tools, text="☑ Todos", command=lambda: self.toggle_all(True)).pack(side="left")
        ttk.Button(tools, text="☐ Ninguno", command=lambda: self.toggle_all(False)).pack(side="left", padx=(6, 0))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.render_files())
        ttk.Entry(tools, textvariable=self.filter_var).pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.sel_label = ttk.Label(c2, text="0 archivos seleccionados · 0 B", style="MutedCard.TLabel")
        self.sel_label.pack(anchor="w", pady=(0, 4))

        cols = ("check", "path", "size")
        self.tree = ttk.Treeview(c2, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("check", text="")
        self.tree.heading("path", text="Archivo")
        self.tree.heading("size", text="Tamaño")
        self.tree.column("check", width=46, stretch=False, anchor="center")
        self.tree.column("path", width=520, anchor="w")
        self.tree.column("size", width=110, anchor="e")
        vsb = ttk.Scrollbar(c2, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)

        # ── Paso 3: destino y opciones ─────────────────────────────────
        c3 = self._card(outer)
        c3.pack(fill="x", pady=(0, 10))
        ttk.Label(c3, text="3. ¿Dónde lo guardo y cómo?", style="Step.TLabel").pack(anchor="w")

        drow = ttk.Frame(c3, style="Card.TFrame")
        drow.pack(fill="x", pady=(6, 0))
        self.dest_var = tk.StringVar(value=DEST_DEFAULT)
        ttk.Entry(drow, textvariable=self.dest_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(drow, text="📁 Examinar…", command=self.on_browse).pack(side="left")

        orow = ttk.Frame(c3, style="Card.TFrame")
        orow.pack(fill="x", pady=(8, 0))
        # hilos
        f_threads = ttk.Frame(orow, style="Card.TFrame")
        f_threads.pack(side="left", padx=(0, 12))
        ttk.Label(f_threads, text="Hilos (0 = automático)", style="MutedCard.TLabel").pack(anchor="w")
        self.threads_var = tk.StringVar(value="0")
        ttk.Spinbox(f_threads, from_=0, to=128, textvariable=self.threads_var, width=6).pack(anchor="w")
        self.threads_hint = ttk.Label(f_threads, text="", style="MutedCard.TLabel")
        self.threads_hint.pack(anchor="w")
        # revisión
        f_rev = ttk.Frame(orow, style="Card.TFrame")
        f_rev.pack(side="left", padx=(0, 12))
        ttk.Label(f_rev, text="Revisión / rama", style="MutedCard.TLabel").pack(anchor="w")
        self.rev_var = tk.StringVar(value="main")
        ttk.Entry(f_rev, textvariable=self.rev_var, width=14).pack(anchor="w")
        # token
        f_tok = ttk.Frame(orow, style="Card.TFrame")
        f_tok.pack(side="left")
        ttk.Label(f_tok, text="Token (opcional, repos gated)", style="MutedCard.TLabel").pack(anchor="w")
        self.tok_var = tk.StringVar()
        ttk.Entry(f_tok, textvariable=self.tok_var, show="*", width=28).pack(anchor="w")

        # ── botón iniciar ──────────────────────────────────────────────
        self.start_btn = ttk.Button(outer, text="🚀  Iniciar descarga", style="Primary.TButton", command=self.on_start)
        self.start_btn.pack(fill="x", pady=(0, 10))

        # ── área de descarga ───────────────────────────────────────────
        c4 = self._card(outer)
        c4.pack(fill="both", expand=True)
        head = ttk.Frame(c4, style="Card.TFrame")
        head.pack(fill="x")
        self.job_title = ttk.Label(head, text="Descargas", style="Step.TLabel")
        self.job_title.pack(side="left")
        self.job_badge = ttk.Label(head, text="", style="MutedCard.TLabel")
        self.job_badge.pack(side="right")

        self.pbar = ttk.Progressbar(c4, mode="determinate", maximum=100)
        self.pbar.pack(fill="x", pady=(8, 4))
        self.stats_label = ttk.Label(c4, text="", style="MutedCard.TLabel")
        self.stats_label.pack(anchor="w")

        cols2 = ("name", "progress", "state")
        self.ftree = ttk.Treeview(c4, columns=cols2, show="headings", height=6)
        self.ftree.heading("name", text="Archivo")
        self.ftree.heading("progress", text="Progreso")
        self.ftree.heading("state", text="Estado")
        self.ftree.column("name", width=430, anchor="w")
        self.ftree.column("progress", width=130, anchor="e")
        self.ftree.column("state", width=160, anchor="w")
        self.ftree.pack(fill="x", pady=(8, 4))

        self.log = tk.Text(c4, height=7, bg="#010409", fg="#9da7b3", font=(MONO, 9),
                           relief="flat", wrap="word", state="disabled", insertbackground=C_TEXT)
        self.log.pack(fill="both", expand=True, pady=(4, 0))

        acts = ttk.Frame(c4, style="Card.TFrame")
        acts.pack(fill="x", pady=(8, 0))
        self.cancel_btn = ttk.Button(acts, text="✋ Cancelar", command=self.on_cancel)
        self.cancel_btn.pack(side="left")
        self.open_btn = ttk.Button(acts, text="📂 Abrir carpeta", command=self.on_open_folder)
        self.open_btn.pack(side="left", padx=(6, 0))
        self.clear_btn = ttk.Button(acts, text="🗑 Limpiar", command=self.on_clear)
        self.clear_btn.pack(side="left", padx=(6, 0))
        self.quit_btn = ttk.Button(acts, text="⏻ Salir", command=self.on_close)
        self.quit_btn.pack(side="right")
        self.cancel_btn.state(["disabled"])
        self.open_btn.state(["disabled"])

        ttk.Label(outer, text="☕ ¿Te ha sido útil? Invítame un café: https://ko-fi.com/jonnyckdev",
                  style="Muted.TLabel").pack(anchor="w", pady=(10, 0))

        # detectar hilos
        try:
            self.threads_hint.config(text=f"Detectado: {hd.auto_threads()}")
        except Exception:
            pass

    # ── paso 1: detectar ───────────────────────────────────────────────
    def on_detect(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Falta la URL", "Pega primero una URL de HuggingFace.", parent=self.root)
            return
        self.detect_btn.state(["disabled"])
        self.detect_msg.config(text="Consultando HuggingFace…")
        rev = self.rev_var.get().strip() or None
        tok = self.tok_var.get().strip() or ""
        threading.Thread(target=self._detect_worker, args=(url, tok, rev), daemon=True).start()

    def _detect_worker(self, url, tok, rev):
        try:
            files, kind, org, repo, revision = hd.list_repo_files(url, tok, rev)
            self.root.after(0, lambda: self._detect_done(files, kind, org, repo, revision))
        except Exception as e:
            self.root.after(0, lambda: self._detect_error(str(e)))

    def _detect_done(self, files, kind, org, repo, revision):
        self.repo_info = {"kind": kind, "org": org, "repo": repo, "revision": revision,
                          "files": files, "total_bytes": sum(f["size"] for f in files)}
        self.all_files = files
        self.selected = {f["path"] for f in files}
        self.detect_btn.state(["!disabled"])
        label = {"model": "modelo", "dataset": "dataset", "space": "space"}.get(kind, kind)
        total = hd.format_size(sum(f["size"] for f in files))
        self.detect_msg.config(
            text=f"✅ {org}/{repo} ({label}) — {len(files)} archivos · {total} · revisión '{revision}'"
        )
        self.render_files()

    def _detect_error(self, msg):
        self.detect_btn.state(["!disabled"])
        self.detect_msg.config(text="")
        messagebox.showerror("Error", f"No se pudo consultar el repositorio:\n{msg}", parent=self.root)

    # ── paso 2: lista de archivos ──────────────────────────────────────
    def _on_tree_click(self, event):
        ident = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if ident and col == "#1":  # columna del checkbox
            path = self.row_path.get(ident)
            if path:
                if path in self.selected:
                    self.selected.discard(path)
                else:
                    self.selected.add(path)
                self.render_files()
                return "break"
        return None

    def toggle_all(self, on):
        if on:
            self.selected = {f["path"] for f in self.all_files}
        else:
            self.selected = set()
        self.render_files()

    def render_files(self):
        self.tree.delete(*self.tree.get_children())
        self.row_path = {}
        q = self.filter_var.get().lower()
        shown = [f for f in self.all_files if not q or f["path"].lower().find(q) >= 0]
        for f in shown:
            checked = "☑" if f["path"] in self.selected else "☐"
            size = hd.format_size(f["size"]) if f["size"] else "desconocido"
            iid = self.tree.insert("", "end", values=(checked, f["path"], size))
            self.row_path[iid] = f["path"]
        n = len(self.selected)
        tot = sum(f["size"] for f in self.all_files if f["path"] in self.selected)
        self.sel_label.config(text=f"{n} archivo(s) seleccionado(s) · {hd.format_size(tot)}")

    # ── paso 3: destino ────────────────────────────────────────────────
    def on_browse(self):
        initial = self.dest_var.get().strip()
        d = filedialog.askdirectory(title="Elegir carpeta de destino", initialdir=initial or None)
        if d:
            self.dest_var.set(d)

    # ── descarga ───────────────────────────────────────────────────────
    def on_start(self):
        if not self.repo_info or not self.selected:
            messagebox.showwarning("Nada que descargar", "Detecta archivos y selecciona al menos uno.", parent=self.root)
            return
        files = [f for f in self.all_files if f["path"] in self.selected]
        dest = self.dest_var.get().strip() or DEST_DEFAULT
        threads = 0
        try:
            threads = int(self.threads_var.get())
        except ValueError:
            threads = 0
        token = self.tok_var.get().strip() or ""
        rev = self.rev_var.get().strip() or "main"

        # preflight: ¿archivos ya completos en destino?
        conflicts = []
        for f in files:
            if not f["size"]:
                continue
            p = os.path.join(dest, f["path"])
            if os.path.exists(p) and os.path.getsize(p) >= f["size"]:
                conflicts.append({"path": f["path"], "size": f["size"], "existing_size": os.path.getsize(p)})

        if conflicts:
            self._show_conflicts(conflicts, lambda policy: self._launch(files, dest, threads, token, rev, policy))
        else:
            self._launch(files, dest, threads, token, rev, {})

    def _show_conflicts(self, conflicts, on_result):
        top = tk.Toplevel(self.root)
        top.title("⚠️ Archivos que ya existen en el destino")
        top.configure(bg=C_BG)
        top.transient(self.root)
        top.grab_set()
        top.geometry("640x460")

        ttk.Label(top, text="Estos archivos ya están completos en la carpeta elegida. Elige qué hacer con cada uno:",
                  style="Muted.TLabel", wraplength=600).pack(anchor="w", padx=14, pady=(12, 8))

        # lista desplazable de filas (path + OptionMenu)
        canvas = tk.Canvas(top, bg=C_CARD, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(top, orient="vertical", command=canvas.yview)
        rows = ttk.Frame(canvas, style="Card.TFrame")
        rows.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=rows, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(14, 0))
        vsb.pack(side="right", fill="y", padx=(4, 14))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))

        varmap = {}
        translate = {"Renombrar": "rename", "Sobrescribir": "overwrite", "Saltar": "skip"}
        for c in conflicts:
            r = ttk.Frame(rows, style="Card.TFrame")
            r.pack(fill="x", pady=3)
            ttk.Label(r, text=c["path"], style="CardLabel.TLabel", font=(MONO, 9)).pack(side="left", fill="x", expand=True)
            ttk.Label(r, text=f"{hd.format_size(c['size'])} · existe: {hd.format_size(c['existing_size'])}",
                      style="MutedCard.TLabel").pack(side="left", padx=(6, 6))
            var = tk.StringVar(value="Renombrar")
            varmap[c["path"]] = var
            om = ttk.OptionMenu(r, var, "Renombrar", "Renombrar", "Sobrescribir", "Saltar")
            om.configure(width=11)
            om.pack(side="left")

        btns = ttk.Frame(top, padding=14)
        btns.pack(fill="x")
        for val in ("rename", "overwrite", "skip"):
            label = {"rename": "📝 Renombrar todos", "overwrite": "♻️ Sobrescribir todos", "skip": "⏭ Saltar todos"}[val]
            ttk.Button(btns, text=label,
                       command=lambda v=val: [var.set([k for k, x in translate.items() if x == v][0])
                                              for var in varmap.values()]).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Cancelar", command=top.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="Continuar descarga →", style="Primary.TButton",
                   command=lambda: self._confirm_conflicts(top, varmap, on_result)).pack(side="right")

    def _confirm_conflicts(self, top, varmap, on_result):
        translate = {"Renombrar": "rename", "Sobrescribir": "overwrite", "Saltar": "skip"}
        policy = {path: translate[var.get()] for path, var in varmap.items()}
        top.destroy()
        on_result(policy)

    def _launch(self, files, dest, threads, token, rev, policy):
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Error", f"No se pudo crear la carpeta de destino:\n{e}", parent=self.root)
            return

        kind = self.repo_info["kind"]
        org = self.repo_info["org"]
        repo = self.repo_info["repo"]
        job_key = hd.repo_key(kind, org, repo, rev)

        job = hd.DownloadJob(
            files, dest, job_key=job_key, kind=kind, org=org, repo=repo, rev=rev,
            threads=threads or None, token=token, conflict_policy=policy,
        )
        job.on_progress = lambda st: setattr(self, "_latest", st.snapshot())
        self.job = job
        self._log_pos = 0
        self._latest = job.state.snapshot()
        self.ftree.delete(*self.ftree.get_children())
        for f in files:
            self.ftree.insert("", "end", iid=f["path"], values=(f["path"], "0%", "pendiente"))
        self.log_append(f"▶ Descarga iniciada: {org}/{repo} → {os.path.abspath(dest)}")
        self.log_append(f"  Hilos: {job.threads} · Archivos: {len(files)} · Total: {hd.format_size(job.total)}")
        self.job_badge.config(text="descargando…")
        self.start_btn.state(["disabled"])
        self.cancel_btn.state(["!disabled"])
        self.open_btn.state(["disabled"])
        threading.Thread(target=job.run, daemon=True).start()

    def log_append(self, line):
        self.log.config(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    # ── polling de progreso ────────────────────────────────────────────
    def _poll(self):
        snap = self._latest
        if snap and self.job:
            pct = (snap["downloaded_bytes"] / snap["total_bytes"] * 100) if snap["total_bytes"] else 0
            self.pbar["value"] = pct
            self.stats_label.config(
                text=f"{hd.format_size(snap['downloaded_bytes'])} / {hd.format_size(snap['total_bytes'])} "
                     f"({pct:.1f}%) · ⚡ {hd.format_speed(snap['speed_bps'])} · "
                     f"ETA {hd.format_time(snap['eta_seconds'])} · ⏱ {int(snap['elapsed_seconds'])}s"
            )
            # por archivo
            for f in snap["files"]:
                iid = f["path"]
                if not self.ftree.exists(iid):
                    self.ftree.insert("", "end", iid=iid, values=(iid, "", ""))
                prog = (f["downloaded"] / f["size"] * 100) if f["size"] else (100 if f["state"] == "done" else 0)
                st = {"pending": "pendiente", "downloading": "descargando…", "combining": "uniendo…",
                      "done": "✓", "error": "✗"}.get(f["state"], f["state"])
                self.ftree.set(iid, "progress", f"{prog:.0f}%")
                self.ftree.set(iid, "state", st)
                self.ftree.item(iid, tags=(f["state"],))
            # log nuevo
            log = snap.get("log") or []
            if len(log) > self._log_pos:
                for line in log[self._log_pos:]:
                    self.log_append(line)
                self._log_pos = len(log)
            # estados finales
            if snap["state"] in ("done", "error", "cancelled"):
                if snap["state"] == "done":
                    self.job_badge.config(text="✓ completado")
                    self.log_append("  " + (snap.get("message") or "Descarga completada."))
                elif snap["state"] == "cancelled":
                    self.job_badge.config(text="✋ cancelado")
                    self.log_append("  " + (snap.get("message") or "Cancelado."))
                else:
                    self.job_badge.config(text="✗ error")
                    self.log_append("  ⚠️ " + (snap.get("error") or "Error."))
                self.start_btn.state(["!disabled"])
                self.cancel_btn.state(["disabled"])
                self.open_btn.state(["!disabled"])
                self.job = None
        self.root.after(250, self._poll)

    # ── acciones ───────────────────────────────────────────────────────
    def on_cancel(self):
        if self.job:
            self.job.cancel.set()
            self.log_append("  ✋ Cancelando…")
            self.cancel_btn.state(["disabled"])

    def on_open_folder(self):
        dest = self.dest_var.get().strip()
        if dest and os.path.isdir(dest):
            try:
                if os.name == "nt":
                    os.startfile(dest)  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    os.system(f'open "{dest}"')
                else:
                    os.system(f'xdg-open "{dest}"')
            except Exception as e:
                messagebox.showerror("Error", str(e), parent=self.root)
        else:
            messagebox.showwarning("Carpeta", "El destino no existe todavía.", parent=self.root)

    def on_clear(self):
        self.pbar["value"] = 0
        self.stats_label.config(text="")
        self.job_badge.config(text="")
        self.ftree.delete(*self.ftree.get_children())
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self.open_btn.state(["disabled"])

    def on_close(self):
        if self.job:
            self.job.cancel.set()
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="HuggingFace Fast Downloader (escritorio).")
    parser.add_argument("--self-test", action="store_true", help="Abre la ventana 3 s y sale (verificar builds).")
    args = parser.parse_args()

    root = tk.Tk()
    if IS_FROZEN:
        root.after(200, lambda: print(f"[{time.strftime('%H:%M:%S')}] ventana abierta"))
    if args.self_test:
        root.after(3000, root.destroy)
    App(root)
    root.mainloop()
    if args.self_test:
        print("SELF-TEST OK")
        os._exit(0)


if __name__ == "__main__":
    main()
