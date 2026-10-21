# stream-flask-core

Stream a video library from your laptop to your phone's browser over the local network or a VPN, with seeking that actually works.

It's a single-file Flask server. The interesting part is that it implements **HTTP 206 Partial Content** itself instead of handing files to `send_file`.

## Why

Mobile browsers only let you scrub through a video if the server honours `Range` requests. Serving whole files made seeking stall over a VPN, because the phone kept waiting on bytes it didn't need. So `/stream` parses byte ranges and returns only the slice that was asked for.

## How it works

- **Range requests:** `/stream/<path>` parses `Range: bytes=start-end` and returns `206` with `Content-Range`, `Content-Length` and `Accept-Ranges: bytes`. Out-of-bounds ranges get `416`. Requests without a `Range` header get the full file, streamed.
- **Chunking:** files are read and yielded in **512 KB** chunks. Smaller chunks mean lower seek latency, larger ones mean better throughput; 512 KB was the balance over VPN. Open-ended ranges (`bytes=N-`) are capped at 16 chunks (8 MB) per response, so a seek returns quickly instead of streaming to the end of the file.
- **Path safety:** every requested path is resolved against `VIDEO_ROOT` and rejected if the resolved path escapes it, so `../` traversal returns `404`. Only `.mp4`, `.webm` and `.mkv` are served.
- **Thumbnails:** `ffmpeg` grabs a frame at the 1-second mark the first time a video's thumbnail is requested. The cache key includes the file's modification time, so a replaced video gets a fresh thumbnail.
- **Library API:** `/api/folders`, `/api/videos/<folder>` and `/api/all-videos` back the browse UI.

## Run it

```bash
pip install flask
# ffmpeg must be on PATH for thumbnails

VIDEO_ROOT=/path/to/videos python app.py
```

On startup it prints a LAN address (port 5000) to open on your phone. `VIDEO_ROOT` defaults to `~/Videos`.

## Limits

- **Trusted networks only.** There's no authentication, and the server currently starts with Flask's debug mode on and binds to `0.0.0.0`. Don't expose it to the internet.
- **No transcoding.** The browser has to support the file's codec, so `.mkv` playback varies by browser.
