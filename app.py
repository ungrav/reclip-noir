import os
import re
import math
import uuid
import glob
import json
import subprocess
import threading
import urllib.request
from flask import Flask, request, jsonify, send_file, render_template, Response

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Auth paths
BROWSER_PROFILE = "/browser-data/.config"         # Perfil de linuxserver/chromium
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "http://pot-provider:4416")
YTDLP_COMMON_ARGS = [
    "--remote-components", "ejs:github",
    "--extractor-args", f"youtubepot-bgutilhttp:base_url={POT_PROVIDER_URL}",
]

jobs = {}


def parse_ytdlp_json(stdout):
    """Parse the first JSON object emitted by yt-dlp."""
    first_error = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            first_error = exc

    if first_error:
        raise first_error
    raise ValueError("yt-dlp returned no data")


# ─── Auth Helpers ────────────────────────────────────────────────────────────

def _parse_clip_value(value, field_name):
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} inválido")
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} inválido")
    if parsed < 0:
        raise ValueError(f"{field_name} no puede ser negativo")
    return parsed


def _validate_clip_range(clip_start, clip_end):
    start = _parse_clip_value(clip_start, "Inicio")
    end = _parse_clip_value(clip_end, "Final")
    if start is None and end is None:
        return None, None
    if start is None:
        start = 0
    if end is not None and end <= start:
        raise ValueError("El final debe ser mayor que el inicio")
    return start, end


def _format_clip_time(seconds):
    value = float(seconds)
    return str(int(value)) if value.is_integer() else f"{value:.3f}".rstrip("0").rstrip(".")


def _clip_suffix(clip_start, clip_end):
    if clip_start is None and clip_end is None:
        return ""
    start = _format_clip_time(clip_start or 0)
    end = _format_clip_time(clip_end) if clip_end is not None else "end"
    return f"_{start}s-{end}s"


def _is_proxy_ffmpeg_clip_error(stderr):
    text = stderr.lower()
    return (
        "ffmpeg exited with code 251" in text
        or "ffmpeg exited with code -11" in text
        or "unexpected tls packet" in text
        or "error opening input" in text
    )


def _remove_job_files(job_id):
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")):
        try:
            os.remove(f)
        except OSError:
            pass

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
        "this video is not available", "po token", "n challenge solving failed",
    ]
    return any(kw in stderr.lower() for kw in keywords)


def _get_auth_strategies():
    """
    Devuelve la cadena de estrategias de auth en orden de prioridad:
    1. POT automático (sin args — bgutil plugin lo maneja solo)
    2. Browser sidecar (cookies de Chromium en el contenedor browser)
    """
    strategies = [[]]  # Estrategia 1: POT solo (plugin automático)

    # Estrategia 2: Browser sidecar — si el perfil de Chromium existe
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

def run_download(job_id, url, format_choice, format_id, proxy, clip_start=None, clip_end=None):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    base_cmd = ["yt-dlp", "--no-playlist", *YTDLP_COMMON_ARGS, "-o", out_template, "--embed-metadata", "--embed-thumbnail"]
    if clip_start is not None or clip_end is not None:
        section_start = _format_clip_time(clip_start or 0)
        section_end = _format_clip_time(clip_end) if clip_end is not None else "inf"
        base_cmd += ["--download-sections", f"*{section_start}-{section_end}"]

    if format_choice == "audio":
        audio_format = "bestaudio[ext=m4a]/bestaudio" if (clip_start is not None or clip_end is not None) else "bestaudio"
        base_cmd += ["-f", audio_format, "-x", "--audio-format", "mp3"]
    elif format_id:
        audio_format = "bestaudio[ext=m4a]/bestaudio" if (clip_start is not None or clip_end is not None) else "bestaudio"
        base_cmd += ["-f", f"{format_id}+{audio_format}/best", "--merge-output-format", "mp4"]
    else:
        base_cmd += ["-f", "bestvideo[vcodec^=avc]+bestaudio[ext=m4a]/bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    try:
        result = _run_with_fallback(base_cmd, url, proxy=proxy, timeout=300)
        if (
            result.returncode != 0
            and proxy
            and (clip_start is not None or clip_end is not None)
            and _is_proxy_ffmpeg_clip_error(result.stderr)
        ):
            _remove_job_files(job_id)
            result = _run_with_fallback(base_cmd, url, proxy=None, timeout=300)

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
            suffix = _clip_suffix(clip_start, clip_end)
            job["filename"] = f"{safe_title}{suffix}{ext}" if safe_title else os.path.basename(chosen)
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


import socket

def get_docker_gateway():
    try:
        with open('/proc/net/route') as f:
            for line in f.readlines():
                fields = line.strip().split()
                if fields[1] != '00000000' or not int(fields[3], 16) & 2:
                    continue
                return socket.inet_ntoa(bytes.fromhex(fields[2])[::-1])
    except:
        pass
    return "host.docker.internal"

@app.route("/proxy.pac")
def proxy_pac():
    """
    PAC file para Chromium Sidecar.
    """
    gateway_ip = get_docker_gateway()
    pac_content = f"""
function FindProxyForURL(url, host) {{
    return "PROXY {gateway_ip}:8891; PROXY {gateway_ip}:8892; DIRECT";
}}
"""
    return Response(pac_content, mimetype="application/x-ns-proxy-autoconfig")


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

    # Normalizar m.youtube.com a www.youtube.com para mayor compatibilidad
    url = url.replace("://m.youtube.com", "://www.youtube.com")

    base_cmd = ["yt-dlp", "--no-playlist", *YTDLP_COMMON_ARGS, "-j"]
    try:
        result = _run_with_fallback(base_cmd, url, proxy=proxy, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        stdout_clean = result.stdout.strip()
        if not stdout_clean:
            return jsonify({"error": "La URL no parece contener un video válido."}), 400
            
        try:
            info = parse_ytdlp_json(stdout_clean)
        except json.JSONDecodeError:
            return jsonify({"error": "Respuesta inválida al procesar el video."}), 400

        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            vcodec = f.get("vcodec", "none")
            if height and vcodec != "none":
                tbr = f.get("tbr") or 0
                is_avc = 1 if (vcodec.startswith("avc") or vcodec.startswith("h264")) else 0
                score = (is_avc * 1000000) + tbr
                
                current = best_by_height.get(height, {})
                current_vcodec = current.get("vcodec", "")
                current_is_avc = 1 if (current_vcodec.startswith("avc") or current_vcodec.startswith("h264")) else 0
                current_score = (current_is_avc * 1000000) + (current.get("tbr") or 0)
                
                if height not in best_by_height or score > current_score:
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
    try:
        clip_start, clip_end = _validate_clip_range(data.get("clip_start"), data.get("clip_end"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if not url:
        return jsonify({"error": "No se proporcionó URL"}), 400

    # Normalizar m.youtube.com a www.youtube.com
    url = url.replace("://m.youtube.com", "://www.youtube.com")

    try:
        proxy = _get_working_proxy(allow_direct)
    except ValueError as e:
        if str(e) == "vpn_required":
            return jsonify({"error": "VPN no disponible", "vpn_required": True}), 403
        return jsonify({"error": "Error interno validando proxy"}), 500

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id, proxy, clip_start, clip_end))
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
        "browser": os.path.exists(chromium_path),
    })



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
