import os
import re
import uuid
import threading
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, render_template, request, jsonify, send_file, abort
import yt_dlp
import imageio_ffmpeg

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # Only accept small form requests.

# In-memory job state: suitable for a local demo, not multi-worker production.
JOBS = {}
JOBS_LOCK = threading.Lock()

ALLOWED_SCHEMES = {"http", "https"}
BLOCKED_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def validate_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.hostname:
        raise ValueError("أدخل رابطاً صحيحاً يبدأ بـ http:// أو https://")
    host = parsed.hostname.lower().rstrip(".")
    if host in BLOCKED_HOSTS or host.endswith(".local"):
        raise ValueError("هذا النطاق غير مسموح.")
    # Basic protection against local/private IP targets.
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("عناوين الشبكات الداخلية غير مسموحة.")
    except ValueError as exc:
        if "غير مسموحة" in str(exc):
            raise
        # Host is a domain name, not an IP address.
    return url


def run_download(job_id: str, url: str, kind: str, quality: str):
    outtmpl = str(DOWNLOAD_DIR / f"{job_id}_%(title).80s.%(ext)s")
   opts = {
    "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
    "outtmpl": outtmpl,
        "noplaylist": True,
        "restrictfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 25,
        "retries": 2,
        "max_filesize": 1024 * 1024 * 1024,  # 1 GiB limit
        "progress_hooks": [],
    }

    if kind == "audio":
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
    else:
        format_map = {
            "best": "bestvideo*+bestaudio/best",
            "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "720": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            "480": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
        }
        opts["format"] = format_map.get(quality, format_map["best"])
        opts["merge_output_format"] = "mp4"

    def hook(data):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            status = data.get("status")
            if status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                downloaded = data.get("downloaded_bytes", 0)
                job["progress"] = int(downloaded * 100 / total) if total else None
                job["message"] = "جارٍ التنزيل..."
            elif status == "finished":
                job["progress"] = 100
                job["message"] = "اكتمل التنزيل، جارٍ تجهيز الملف..."

    opts["progress_hooks"] = [hook]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            requested = info.get("requested_downloads") or []
            candidates = []
            for item in requested:
                fp = item.get("filepath")
                if fp:
                    candidates.append(Path(fp))
            # Locate final output (including post-processed MP3 / merged MP4).
            candidates.extend(sorted(DOWNLOAD_DIR.glob(f"{job_id}_*"), key=lambda p: p.stat().st_mtime, reverse=True))
            final_path = next((p for p in candidates if p.exists() and p.is_file()), None)
            if final_path is None:
                # Some postprocessors change extension; search by job prefix.
                final_path = next((p for p in DOWNLOAD_DIR.glob(f"{job_id}_*") if p.is_file()), None)
            if final_path is None:
                raise RuntimeError("تم التنزيل لكن لم أتمكن من العثور على الملف الناتج.")
            with JOBS_LOCK:
                JOBS[job_id].update({
                    "status": "done",
                    "progress": 100,
                    "message": "جاهز للتنزيل",
                    "filename": final_path.name,
                    "title": info.get("title") or final_path.name,
                })
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id].update({
                "status": "error",
                "message": str(exc)[:500],
            })


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/download")
def create_download():
    data = request.get_json(silent=True) or request.form
    try:
        url = validate_url(data.get("url", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    kind = data.get("kind", "video")
    quality = data.get("quality", "best")
    if kind not in {"video", "audio"}:
        return jsonify(error="نوع الملف غير صالح."), 400
    if quality not in {"best", "1080", "720", "480"}:
        quality = "best"

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "working",
            "progress": 0,
            "message": "تم استلام الرابط...",
        }

    thread = threading.Thread(
        target=run_download,
        args=(job_id, url, kind, quality),
        daemon=True,
    )
    thread.start()
    return jsonify(job_id=job_id), 202


@app.get("/api/status/<job_id>")
def download_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify(error="لم يتم العثور على طلب التنزيل."), 404
        result = dict(job)
    if result.get("status") == "done":
        result["download_url"] = f"/files/{job_id}"
    return jsonify(result)


@app.get("/files/<job_id>")
def get_file(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or job.get("status") != "done":
            abort(404)
        filename = job.get("filename")
    path = (DOWNLOAD_DIR / filename).resolve()
    if path.parent != DOWNLOAD_DIR.resolve() or not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)


if __name__ == "__main__":
    # Local development only. Use a production WSGI server when deploying.
    app.run(host="127.0.0.1", port=5000, debug=False)
