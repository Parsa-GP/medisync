import asyncio
import logging
import websockets
from httpx import AsyncClient
from subprocess import run
from shutil import which
from time import time
from os import walk, path
from hashlib import sha256
from json import loads, dumps
from quart import Quart, request, jsonify, render_template, url_for, abort
from pathlib import Path

import websockets.asyncio
import websockets.asyncio.server

# init logging
logging._levelToName = {
    logging.CRITICAL: 'CRIT',
    logging.ERROR: 'EROR',
    logging.WARNING: 'WARN',
    logging.INFO: 'INFO',
    logging.DEBUG: 'DBUG',
    logging.NOTSET: 'NSET',
}
log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('[%(levelname)s] %(name)s: %(message)s'))
log.addHandler(handler)
log.info("SERVER STARTED.")

MUSIC_DIR = "media"
SUPPORTED_EXTS = [
    # audio formats
    ".mp3", ".ogg", ".webm", ".flac", ".wav", ".m4a",
    # video formats
    ".mp4", ".mkv", ".avi",  ".mov",  ".wmv",  ".flv", ".mpg",".mpeg"
]
SYNC_THRESHOLD = 5

def get_media_duration(filepath: str) -> float:
    """
    Get duration (in seconds) of a local audio/video file using ffprobe.
    """
    if not which("ffprobe"):
        log.error("ffmpeg is not installed. Make sure you have ffmpeg or the ffprobe binary is in the PATH.")
        exit(1)

    filepath = str(Path(filepath).expanduser().resolve())
    cmd = ["ffprobe", "-v", "error","-show_entries", "format=duration", "-of", "json", filepath]
    result = run(cmd, capture_output=True, text=True)
    data = loads(result.stdout)
    return float(data["format"]["duration"]) if "format" in data else -1.0

def hash_file(path: str) -> str:
    h = sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

# Scan musics
musics = {}
for root, _, files in walk(MUSIC_DIR):
    for f in files:
        if any(f.lower().endswith(ext) for ext in SUPPORTED_EXTS):
            p = path.join(root, f)
            filehash = hash_file(p)
            musics[filehash] = {"name": f, "path": p, "duration":get_media_duration(p)}

log.debug(musics)

queue = []
current = {"hash": None, "start": 0, "duration": 0, "paused": False, "position": 0}
clients = set()
doAutoplay = True

# ---------------- websocket ----------------
async def ws_handler(websocket: websockets.asyncio.server.ServerConnection):
    clients.add(websocket)
    log.info("NEW CLIENT CONNECTED: "+str(websocket.id))
    log.debug("All Clients: "+ str([i.id for i in clients]))
    try:
        await websocket.send(dumps({"type": "rebroadcast", "current": current}))
        async for msg in websocket:
            data = loads(msg)
            if data.get("type") == "position":
                current["position"] = data["position"]
    finally:
        clients.remove(websocket)

async def broadcaster():
    while True:
        if current["hash"]:
            if current["position"] > current["duration"]:
                log.debug("SONG ENDED!!~")
                current["paused"] = True
                await asyncio.sleep(SYNC_THRESHOLD)
                log.debug("trying to play next if there is one...")
                async with AsyncClient() as client:
                    await client.post("http://127.0.0.1:5000/api/play")
                continue
            current["position"] = current["position"]+SYNC_THRESHOLD
            msg = dumps({"type": "play", "current": current})
            await asyncio.gather(*[c.send(msg) for c in clients], return_exceptions=True)
        await asyncio.sleep(SYNC_THRESHOLD)

# ---------------- web API ----------------
app = Quart(__name__)

@app.route("/")
async def index():
    return await render_template("index.html")

@app.route("/api/musics")
async def api_musics():
    log.debug("api_musics()")
    log.debug(musics)
    return jsonify(musics)

@app.route("/api/queue", methods=["GET", "POST", "DELETE"])
async def manage_queue():
    global queue
    if request.method == "GET":
        return jsonify(queue)
    data = await request.get_json()
    if request.method == "POST":
        h = data.get("hash")
        if h in musics:
            queue.append(h)
        return jsonify(queue)
    if request.method == "DELETE":
        h = data.get("hash")
        queue = [x for x in queue if x != h]
        return jsonify(queue)
    else:
        return "Use GET, POST or DELETE to fetch/change data."

@app.route("/api/queue/reorder", methods=["POST"])
async def reorder_queue():
    global queue
    data = await request.get_json()
    f, t = data.get("from"), data.get("to")
    if 0 <= f < len(queue) and 0 <= t < len(queue):
        item = queue.pop(f)
        queue.insert(t, item)
    return jsonify(queue)

@app.route("/api/autoplay_get", methods=["GET"])
async def autoplay_get():
    global doAutoplay
    return jsonify(doAutoplay)

@app.route("/api/autoplay_set", methods=["POST"])
async def autoplay_set():
    global doAutoplay
    data = await request.get_json()
    doAutoplay = data
    log.debug("FROM CLIENT, CNAGE AUTOPLAY: "+str(data))
    return jsonify(doAutoplay)

@app.route("/api/play", methods=["POST"])
async def play():
    global current
    if queue:
        h = queue.pop(0)
        current = {"hash": h, "start": time(), "duration": musics[h]["duration"], "paused": False, "position": 0}
    return jsonify(current)

@app.route("/api/pause", methods=["POST"])
async def pause():
    if current["paused"]:
        current["paused"] = False
    else:
        current["paused"] = True
    return jsonify(current)

@app.route("/api/rebroadcast", methods=["POST"])
async def rebroadcast():
    return jsonify(current)

@app.route("/api/current")
async def current_playing():
    return jsonify(current)

# ---------------- main ----------------
async def main():
    ws_server = await websockets.serve(ws_handler, "0.0.0.0", 6789)
    asyncio.create_task(broadcaster())
    await app.run_task(host="0.0.0.0", port=5000)

if __name__ == "__main__":
    asyncio.run(main())
