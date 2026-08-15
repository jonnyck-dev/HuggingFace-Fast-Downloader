# 🚀 HuggingFace Fast Downloader

Descarga modelos, datasets y spaces de [HuggingFace](https://huggingface.co) a **máxima velocidad** usando descarga paralela por chunks, con **cero dependencias** (solo Python estándar).

## 🖥️ Compatibilidad

| Modo | Windows | Linux | macOS |
|---|---|---|---|
| Código Python (`gui.py`, `webui.py`, CLI) | ✅ | ✅ | ✅ |
| Ejecutable portable (release) | ✅ `.exe` | ✅ `-linux` | ✅ `-macos` (GitHub Actions) |

> ⚠️ El `.exe` es **solo para Windows** (PyInstaller compila nativo por plataforma, no cruza).
> En Linux y macOS puedes usar el binario portable de la release o ejecutar con Python:
> `python gui.py` (ventana nativa) o `python webui.py` (navegador). Los binarios de
> macOS — y los de todas las plataformas en releases futuras — se compilan solos con
> **GitHub Actions** al publicar un tag `v*`.

## ✨ Características

| | |
|---|---|
| ⚡ **Descarga paralela** | Cada archivo se parte en trozos y se baja con N conexiones simultáneas (HTTP `Range`) |
| 🧵 **Hilos automáticos** | Detecta el número óptimo de conexiones según tu CPU (sobreescribible) |
| 🔁 **Resume** | Si la descarga se corta, reanuda desde donde quedó, sin volver a empezar |
| 🛡 **Protección de archivos** | Si un archivo ya existe en el destino, te pregunta: renombrar, sobrescribir u omitir |
| ✅ **Verificación SHA-256** | Cuando HuggingFace expone el hash (LFS oid), verifica la integridad automáticamente |
| 📦 **Repos completos** | Pega la URL de un repo y baja **todos** sus archivos, o elige cuáles |
| 🔑 **Repos gated** | Soporta tokens de HuggingFace |
| 🪟 **App de escritorio** | Ventana nativa de Windows (Tkinter, cero dependencias). Cerrar la ventana cierra el proceso y cancela las descargas |
| 🖥 **CLI + Web UI** | Además de la app de escritorio, tienes terminal y una interfaz web opcional |

## 📦 Requisitos

- **Python 3.8+** (no hace falta instalar nada más: `urllib` + `http.server`, todo estándar)

## 🚀 Uso rápido (app de escritorio — recomendado)

**Windows:** descarga el ejecutable portable desde [Releases](https://github.com/jonnyck-dev/HuggingFace-Fast-Downloader/releases), haz doble clic y listo (no necesitas Python ni instalar nada). Al cerrar la ventana se cierra el proceso.

**Con Python:**

```bash
python gui.py
```

Flujo en la ventana:

1. **Pega la URL** del modelo o del archivo → *Detectar archivos*
2. **Marca** los archivos que quieras (por defecto, todos)
3. **Elige la carpeta** de destino con *Examinar* (los hilos se detectan solos)
4. 🚀 **Iniciar descarga** — progreso en tiempo real con velocidad, ETA, log y verificación
5. Si un archivo **ya existe** en el destino, te pregunta: *renombrar* (por defecto), *sobrescribir* u *omitir*

## 🌐 Uso en el navegador (opcional)

```bash
python webui.py
```

Se abre el navegador en `http://127.0.0.1:8000` con la misma funcionalidad.

## 💻 Uso por terminal (CLI)

```bash
# Descargar un repositorio completo
python hf_fast_download.py "https://huggingface.co/Qwen/Qwen2.5-7B" "G:\IA\models"

# Descargar un archivo directo (formato compatible con el script original)
python hf_fast_download.py "https://huggingface.co/adrepale/LTX2.3-10Eros-LoRA/resolve/main/10Eros_v1_Delta.safetensors" "G:\models\"

# Solo ver qué archivos tiene un repo
python hf_fast_download.py "https://huggingface.co/Qwen/Qwen2.5-7B" --list

# Descargar solo ciertos archivos (admite * glob)
python hf_fast_download.py "https://huggingface.co/Qwen/Qwen2.5-7B" "G:\IA\models" --file="*.safetensors"

# Repos gated / privados (token)
set HF_TOKEN=hf_xxxx          # PowerShell:  $env:HF_TOKEN="hf_xxxx"
python hf_fast_download.py "https://huggingface.co/meta-llama/Llama-3.1-8B" "G:\IA\models"
```

Opciones del CLI:

| Opción | Descripción |
|---|---|
| `--threads=N` | Conexiones paralelas (por defecto: **automático** según CPU) |
| `--token=...` | Token HF (o variable de entorno `HF_TOKEN`) |
| `--revision=...` | Rama/revisión del repo (por defecto: `main`) |
| `--file=patrón` | Descargar solo archivos que coincidan (repetible, admite `*`) |
| `--list` | Solo listar los archivos sin descargar |
| `--no-resume` | No reanudar chunks parciales |
| `--no-verify` | Omitir verificación SHA-256 |
| `--overwrite` | Sobrescribir archivos que ya existen (por defecto se omiten si están completos) |

## 🧠 ¿Cómo funciona?

1. **Lista los archivos** del repo usando la API pública de HuggingFace (`/api/models/.../tree`).
2. **Consulta el tamaño** de cada archivo y lo divide en **N trozos** (N = hilos).
3. **Descarga los trozos en paralelo** con peticiones HTTP `Range` (una conexión por hilo).
4. **Une los trozos** en el archivo final y **verifica SHA-256** si HuggingFace lo expone.

Si algo falla a mitad de camino, los trozos parciales quedan guardados en una carpeta oculta
`.chunks_*` y la siguiente ejecución **reanuda** exactamente desde donde quedó. Cuando todo
termina correctamente, esa carpeta se elimina sola.

## 📁 Estructura del proyecto

```
HuggingFace Fast Downloader/
├── hf_downloader.py     # Motor de descarga (librería)
├── gui.py               # App de escritorio (ventana nativa, Tkinter)
├── webui.py             # Interfaz web opcional (navegador)
├── index.html           # Frontend web (en español)
├── hf_fast_download.py  # Interfaz de línea de comandos
├── README.md
└── LICENSE
```

## 🤝 Contribuir

¿Ideas? ¿Bugs? Abre un *issue* o manda un *pull request*. El proyecto está pensado para
seguir siendo de **cero dependencias** — si añades algo, intenta mantenerlo así.

## 📄 Licencia

[MIT](LICENSE)

## ☕ ¿Te ha sido útil?

Invítame un café: <https://ko-fi.com/jonnyckdev> 💜
