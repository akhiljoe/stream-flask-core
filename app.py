"""
StreamLocal - app.py
Local video streaming server with HTTP 206 Partial Content support.
Set VIDEO_ROOT env var to override the default ~/Videos directory.

Usage:
    VIDEO_ROOT=/path/to/videos python app.py
"""

import os
import re
import socket
import subprocess
from pathlib import Path
from urllib.parse import unquote
from flask import Flask, jsonify, request, Response, abort, render_template, send_file

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config.update(
    DEBUG=True,
    PROPAGATE_EXCEPTIONS=True,
)

VIDEO_ROOT = Path(os.environ.get("VIDEO_ROOT", os.path.expanduser("~/Videos"))).resolve()
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv"}
THUMB_DIR = Path("./thumbnails")
THUMB_DIR.mkdir(exist_ok=True)

# Tune chunk size for VPN streaming: smaller chunks = lower seek latency,
# larger chunks = better throughput. 512KB is a good middle ground.
CHUNK_SIZE = 512 * 1024

MIME_MAP = {
    ".mp4":  "video/mp4",
    ".webm": "video/webm",
    ".mkv":  "video/x-matroska",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_mime(path: Path) -> str:
    return MIME_MAP.get(path.suffix.lower(), "application/octet-stream")


def safe_resolve(relative: str) -> Path | None:
    """
    Resolve a relative path against VIDEO_ROOT and verify it stays inside it.
    Decodes percent-encoded characters (spaces, etc.) before resolving.
    Returns None if the resolved path escapes VIDEO_ROOT (traversal attempt).
    """
    try:
        decoded = unquote(relative)
        resolved = (VIDEO_ROOT / decoded).resolve()
        resolved.relative_to(VIDEO_ROOT)  # raises ValueError if outside root
        return resolved
    except (ValueError, OSError):
        return None


def video_files_in(directory: Path) -> list[Path]:
    try:
        return [
            f for f in directory.iterdir()
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
        ]
    except PermissionError:
        return []


def video_files_recursive(directory: Path) -> list[Path]:
    """Walk the entire tree under directory and return all video files."""
    results = []
    try:
        for item in directory.rglob("*"):
            if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS:
                results.append(item)
    except PermissionError:
        pass
    return sorted(results)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/folders")
def list_folders():
    if not VIDEO_ROOT.exists():
        return jsonify({"error": f"VIDEO_ROOT does not exist: {VIDEO_ROOT}"}), 500

    folders = []

    # Root-level videos always get a special "." entry when present
    if video_files_in(VIDEO_ROOT):
        folders.append({"name": "/ (root)", "path": "."})

    try:
        for item in sorted(VIDEO_ROOT.iterdir()):
            if item.is_dir() and video_files_in(item):
                folders.append({"name": item.name, "path": item.name})
    except PermissionError:
        pass

    return jsonify(folders)


@app.route("/api/all-videos")
def list_all_videos():
    """
    Recursively scan every subfolder under VIDEO_ROOT and return a flat list
    of all video files. The frontend uses this to build a global search index.
    """
    if not VIDEO_ROOT.exists():
        return jsonify([])

    videos = []
    for f in video_files_recursive(VIDEO_ROOT):
        rel = f.relative_to(VIDEO_ROOT)
        rel_str = str(rel).replace("\\", "/")
        # Build a human-readable folder label for the search result
        parts = rel.parts
        folder_label = "/".join(parts[:-1]) if len(parts) > 1 else "/ (root)"
        videos.append({
            "filename":     f.name,
            "path":         rel_str,
            "folder":       folder_label,
            "folder_path":  str(rel.parent).replace("\\", "/") if len(parts) > 1 else ".",
            "size":         f.stat().st_size,
            "mime":         get_mime(f),
            "thumb":        f"/api/thumbnail/{rel_str}",
        })

    return jsonify(videos)


@app.route("/api/thumbnail/<path:video_rel_path>")
def get_thumbnail(video_rel_path):
    video_path = safe_resolve(video_rel_path)
    if not video_path:
        abort(404)

    # Use mtime in the filename to bust the cache when the video changes
    thumb_name = f"{video_path.stem}_{int(video_path.stat().st_mtime)}.jpg"
    thumb_path = THUMB_DIR / thumb_name

    if not thumb_path.exists():
        cmd = [
            "ffmpeg", "-i", str(video_path), "-ss", "00:00:01.000",
            "-vframes", "1", "-vf", "scale=160:-1", str(thumb_path), "-y"
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if not thumb_path.exists():
        abort(404)

    return send_file(thumb_path)


@app.route("/api/videos/<path:folder_path>")
def list_videos(folder_path):
    if folder_path == ".":
        target = VIDEO_ROOT
    else:
        target = safe_resolve(folder_path)
        if not target or not target.is_dir():
            abort(404)

    videos = []
    for f in sorted(video_files_in(target)):
        rel = f.relative_to(VIDEO_ROOT)
        rel_str = str(rel).replace("\\", "/")
        videos.append({
            "filename": f.name,
            "path":     rel_str,
            "size":     f.stat().st_size,
            "mime":     get_mime(f),
            "thumb":    f"/api/thumbnail/{rel_str}",
        })

    return jsonify(videos)


@app.route("/stream/<path:filepath>")
def stream_video(filepath):
    """
    Streaming endpoint with HTTP 206 Partial Content support.
    Mobile browsers and the HTML5 <video> element require byte-range support
    for seeking to work correctly, especially over VPN/Tailscale.
    """
    file_path = safe_resolve(filepath)

    if not file_path or not file_path.is_file():
        abort(404)
    if file_path.suffix.lower() not in VIDEO_EXTENSIONS:
        abort(403)

    file_size = file_path.stat().st_size
    mime = get_mime(file_path)
    range_header = request.headers.get("Range")

    # --- No Range header: serve the full file ---
    if not range_header:
        def full_stream():
            with open(file_path, "rb") as fh:
                while chunk := fh.read(CHUNK_SIZE):
                    yield chunk

        resp = Response(full_stream(), 200, mimetype=mime, direct_passthrough=True)
        resp.headers["Content-Length"] = str(file_size)
        resp.headers["Accept-Ranges"]  = "bytes"
        return resp

    # --- Parse Range header: "bytes=start-end" ---
    match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
    if not match:
        abort(416)

    start = int(match.group(1))
    end   = int(match.group(2)) if match.group(2) else min(
        start + CHUNK_SIZE * 16 - 1, file_size - 1
    )

    if start >= file_size or end >= file_size or start > end:
        resp = Response(status=416)
        resp.headers["Content-Range"] = f"bytes */{file_size}"
        return resp

    length = end - start + 1

    def partial_stream():
        with open(file_path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    resp = Response(partial_stream(), 206, mimetype=mime, direct_passthrough=True)
    resp.headers["Content-Range"]  = f"bytes {start}-{end}/{file_size}"
    resp.headers["Content-Length"] = str(length)
    resp.headers["Accept-Ranges"]  = "bytes"
    return resp


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


if __name__ == "__main__":
    local_ip = get_local_ip()
    port = 5000
    print(f"\n" + "=" * 40)
    print(f"  StreamLocal is LIVE on your network!")
    print(f"  Connect from your phone using:")
    print(f"  http://{local_ip}:{port}")
    print("=" * 40 + "\n")

    app.run(host="0.0.0.0", port=port, debug=True)
