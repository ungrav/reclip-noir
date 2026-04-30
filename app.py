import os
import re
import uuid
import glob
import json
import subprocess
import threading
import urllib.request
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Auth paths
COOKIES_FILE = "/app/auth/cookies.txt"          # OAuth2 token guardado aquí
BROWSER_PROFILE = "/browser-data/config"         # Perfil de linuxserver/chromium

jobs = {}
oauth2_jobs = {}


# ─── Auth Helpers ────────────────────────────────────────────────────────────

def _get_working_proxy(allow_direct):
    """Prueba y devuelve el primer proxy activo. Lanza error si todos fallan y no allow_direct."""
    proxies = [
        "http://host.docker.internal:8891",  # NordVPN
        "http://host.docker.internal:8892"   # ProtonVPN
    ]
    for p in proxies:
        try:
            proxy_handler = urllib.request.ProxyHandler({'http': p, 'https': p})
            opener = urllib.request.build_opener(proxy_handler)
            opener.open('http://gstatic.com/generate_204', timeout=3)
            return p
        except Exception:
            continue
    
    if allow_direct:
        return None
    else:
        raise ValueError("vpn_required")


def _is_auth_error(stderr):
    """Detecta si el error es de autenticación para activar el fallback."""
    keywords = [
        "sign in to confirm", "not a bot", "http error 403",
        "requires authentication", "login required",
        "please sign in", "age-restricted", "private video",
    ]
    return any(kw in stderr.lower() for kw in keywords)


def _get_auth_strategies():
    """
    Devuelve la cadena de estrategias de auth en orden de prioridad:
    1. POT automático (sin args — bgutil plugin lo maneja solo)
    2. OAuth2 cookies (si el usuario ya hizo login TV-style)
    3. Browser sidecar (cookies de Chromium en el contenedor browser)
    """
    strategies = [[]]  # Estrategia 1: POT solo (plugin automático)

    # Estrategia 2: OAuth2 — si existe el archivo de cookies con token
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        strategies.append(["--cookies", COOKIES_FILE])

    # Estrategia 3: Browser sidecar — si el perfil de Chromium existe
    chromium_path = os.path.join(BROWSER_PROFILE, "chromium")
    if os.path.exists(chromium_path):
        strategies.append(["--cookies-from-browser", f"chromium:{chromium_path}"])

    return strategies


def _run_with_fallback(base_cmd, url, proxy=None, timeout=60):
    """
    Ejecuta base_cmd + url probando cada estrategia de auth.
    Si la estrategia falla con error de autenticación, pasa a la siguiente.
    """
    strategies = _get_auth_strategies()
    last_result = None

    if proxy:
        base_cmd = base_cmd + ["--proxy", proxy]

    for auth_args in strategies:
        cmd = base_cmd + auth_args + [url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        last_result = result
        if result.returncode == 0:
            break
        if not _is_auth_error(result.stderr):
            break  # Error distinto a auth → no reintentar

    return last_result


# ─── Download Logic ──────────────────────────────────────────────────────────

def run_download(job_id, url, format_choice, format_id, proxy):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    base_cmd = ["yt-dlp", "--no-playlist", "-o", out_template]

    if format_choice == "audio":
        base_cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        base_cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        base_cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    try:
        result = _run_with_fallback(base_cmd, url, proxy=proxy, timeout=300)

        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = result.stderr.strip().split("\n")[-1]
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Descarga completada pero no se encontró el archivo"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)

    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Descarga cancelada (límite de 5 min)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    allow_direct = data.get("allow_direct", False)
    if not url:
        return jsonify({"error": "No se proporcionó URL"}), 400

    try:
        proxy = _get_working_proxy(allow_direct)
    except ValueError as e:
        if str(e) == "vpn_required":
            return jsonify({"error": "VPN no disponible", "vpn_required": True}), 403
        return jsonify({"error": "Error interno validando proxy"}), 500

    base_cmd = ["yt-dlp", "--no-playlist", "-j"]
    try:
        result = _run_with_fallback(base_cmd, url, proxy=proxy, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)

        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({"id": f["format_id"], "label": f"{height}p", "height": height})
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Tiempo de espera agotado al obtener info del video"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")
    allow_direct = data.get("allow_direct", False)

    if not url:
        return jsonify({"error": "No se proporcionó URL"}), 400

    try:
        proxy = _get_working_proxy(allow_direct)
    except ValueError as e:
        if str(e) == "vpn_required":
            return jsonify({"error": "VPN no disponible", "vpn_required": True}), 403
        return jsonify({"error": "Error interno validando proxy"}), 500

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id, proxy))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Trabajo no encontrado"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Archivo no disponible"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


# ─── Auth Routes ─────────────────────────────────────────────────────────────

@app.route("/api/auth/status")
def auth_status():
    """Devuelve qué métodos de auth están disponibles."""
    chromium_path = os.path.join(BROWSER_PROFILE, "chromium")
    return jsonify({
        "pot": True,  # Siempre activo si el plugin está instalado
        "oauth2": os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0,
        "browser": os.path.exists(chromium_path),
    })


@app.route("/api/auth/youtube/start", methods=["POST"])
def start_youtube_oauth2():
    """Inicia el flujo OAuth2 TV-style de YouTube."""
    job_id = uuid.uuid4().hex[:8]
    oauth2_jobs[job_id] = {
        "status": "starting",
        "auth_url": None,
        "user_code": None,
        "error": None,
    }

    def run_oauth2():
        try:
            cmd = [
                "yt-dlp",
                "--username", "oauth2", "--password", "",
                "--cookies", COOKIES_FILE,
                "--skip-download",
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            ]

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            oauth2_jobs[job_id]["status"] = "waiting_code"
            auth_detected = False

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue

                # Detectar la URL y código del flujo device auth
                if ("open" in line.lower() or "visit" in line.lower()) and "http" in line:
                    url_match = re.search(r'https?://[^\s]+', line)
                    code_match = re.search(r'\b([A-Z0-9]{4}-[A-Z0-9]{4})\b', line)

                    if url_match:
                        oauth2_jobs[job_id]["auth_url"] = url_match.group(0).rstrip(".")
                    if code_match:
                        oauth2_jobs[job_id]["user_code"] = code_match.group(1)

                    oauth2_jobs[job_id]["status"] = "pending_auth"
                    auth_detected = True

            proc.wait()

            if proc.returncode == 0:
                oauth2_jobs[job_id]["status"] = "done"
            elif not auth_detected:
                oauth2_jobs[job_id]["status"] = "error"
                oauth2_jobs[job_id]["error"] = "No se pudo iniciar el flujo OAuth2"
            else:
                oauth2_jobs[job_id]["status"] = "error"
                oauth2_jobs[job_id]["error"] = "Autenticación fallida o cancelada"

        except Exception as e:
            oauth2_jobs[job_id]["status"] = "error"
            oauth2_jobs[job_id]["error"] = str(e)

    thread = threading.Thread(target=run_oauth2)
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/auth/youtube/<job_id>")
def check_youtube_oauth2(job_id):
    """Consulta el estado del flujo OAuth2."""
    job = oauth2_jobs.get(job_id)
    if not job:
        return jsonify({"status": "not_found"}), 404
    return jsonify(job)


@app.route("/api/auth/youtube/logout", methods=["POST"])
def logout_youtube():
    """Elimina el archivo de cookies para desconectarse."""
    if os.path.exists(COOKIES_FILE):
        os.remove(COOKIES_FILE)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
