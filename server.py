import os, hashlib, asyncio, json, time, subprocess, logging, shutil, requests
from quart import Quart, request, jsonify, render_template_string
import websockets, httpx
from pathlib import Path

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
log.debug("SERVER STARTED.")

MUSIC_DIR = "musics"
SUPPORTED_EXTS = [
    ".mp3", ".ogg", ".webm", ".flac", ".wav", ".m4a",
    ".mp4", ".mkv", ".avi",  ".mov",  ".wmv",  ".flv", ".mpg",".mpeg"
]

def get_media_duration(filepath: str) -> float:
    """
    Get duration (in seconds) of a local audio/video file using ffprobe.
    """
    if not shutil.which("ffprobe"):
        log.error("ffmpeg is not installed. Make sure you have ffmpeg or the ffprobe binary is in the PATH.")
        exit(1)

    filepath = Path(filepath).expanduser().resolve()
    result = subprocess.run(
        [
            "ffprobe", "-v", "error","-show_entries",
            "format=duration", "-of", "json", str(filepath)
        ],
        capture_output=True,
        text=True
    )
    data = json.loads(result.stdout)
    return float(data["format"]["duration"]) if "format" in data else None

def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

# Scan musics
musics = {}
for root, _, files in os.walk(MUSIC_DIR):
    for f in files:
        if any(f.lower().endswith(ext) for ext in SUPPORTED_EXTS):
            p = os.path.join(root, f)
            filehash = hash_file(p)
            musics[filehash] = {"name": f, "duration":get_media_duration(os.path.join(MUSIC_DIR, f))}  # show filename in UI

log.debug(musics)

queue = []
current = {"hash": None, "start": 0, "duration": 0, "paused": False, "position": 0}
clients = set()
doAutoplay = True

# ---------------- websocket ----------------
async def ws_handler(websocket):
    clients.add(websocket)
    try:
        await websocket.send(json.dumps({"type": "rebroadcast", "current": current}))
        async for msg in websocket:
            data = json.loads(msg)
            if data.get("type") == "position":
                current["position"] = data["position"]
    finally:
        clients.remove(websocket)

async def broadcaster():
    while True:
        if current["hash"]:
            if current["position"] > current["duration"]:
                log.debug("SONG ENDED!!~")
                await asyncio.sleep(2)
                log.debug("trying to play next if there is one...")
                async with httpx.AsyncClient() as client:
                    await client.post("http://127.0.0.1:5000/api/play")
                continue
            current["position"] = current["position"]+2
            msg = json.dumps({"type": "play", "current": current})
            await asyncio.gather(*[c.send(msg) for c in clients], return_exceptions=True)
        await asyncio.sleep(2)

# ---------------- web API ----------------
app = Quart(__name__)

@app.route("/")
async def index():
    return await render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Music Sync Server</title>
        <h2>Now Playing</h2>
        <div id="now-playing">Nothing</div>

        <style>
            body { font-family: sans-serif; margin: 2em; }
            #music-list, #queue-list { border: 1px solid #ccc; padding: 1em; min-height: 100px; }
            .music-item, .queue-item { padding: 0.5em; border: 1px solid #aaa; margin: 0.2em; cursor: pointer; background: #f9f9f9; }
            .dragging { opacity: 0.5; }
            button { margin-left: 1em; }
        </style>
    </head>
    <body>
        <h1>Music Sync Server</h1>

        <h2>All Musics</h2>
        <div id="music-list"></div>

        <h2>Queue</h2>
        <div id="queue-list"></div>
        <button onclick="play()">Play Next</button>
        <button onclick="pause()">Pause</button>
        <label>
            <input type="checkbox" id="autoplay-toggle" checked>
            Autoplay next song
        </label>

        <script>
            async function atp_get() {
                let res = await fetch('/api/autoplay_get');
                console.log(res);
                let data = await res.json();
                return data
            }
            const atp = document.getElementById("autoplay-toggle");
            
            console.log(atp_get());
            //atp.checked = false
            atp.addEventListener("change", e => {
                fetch('/api/autoplay_set', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify( e.target.checked )
                });
            });
            let musics = {};
                                        
            function fancyname(name, hash, duration) {
                return name+" ("+hash.slice(0,8)+") ["+duration+"]";
            }
            async function loadMusics() {
                let res = await fetch('/api/musics');
                let data = await res.json();
                const container = document.getElementById("music-list");
                container.innerHTML = "";
                for (const [h, music_info] of Object.entries(data)) {
                    const duration = Math.round(music_info.duration/40) + ":" + Math.round(music_info.duration%60)
                    let div = document.createElement("div");
                    div.className = "music-item";
                    div.textContent = fancyname(music_info.name, h, duration);
                    div.onclick = async () => { await fetch('/api/queue', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({hash:h})}); refreshQueue(); };
                    container.appendChild(div);
                }
                musics = data;
            }

            async function refreshQueue() {
              let res = await fetch('/api/queue');
              let data = await res.json();
              const container = document.getElementById("queue-list");
              container.innerHTML = "";
              data.forEach((h, i) => {
                  let div = document.createElement("div");
                  div.className = "queue-item";
                  div.draggable = true;
                  div.dataset.index = i;
                  div.textContent = fancyname(musics[h].name, h, musics[h].duration);

                  let btn = document.createElement("button");
                  btn.textContent = "Delete";
                  btn.onclick = async (e) => { 
                      e.stopPropagation();
                      await fetch('/api/queue', {method:'DELETE', headers:{'Content-Type':'application/json'}, body: JSON.stringify({hash:h})}); 
                      refreshQueue(); 
                  };
                  div.appendChild(btn);

                  div.addEventListener('dragstart', (e)=>{div.classList.add('dragging'); e.dataTransfer.setData('text/plain', i);});
                  div.addEventListener('dragend', (e)=>{div.classList.remove('dragging');});
                  div.addEventListener('dragover', e=>e.preventDefault());
                  div.addEventListener('drop', async e=>{
                      e.preventDefault();
                      let from = parseInt(e.dataTransfer.getData('text/plain'));
                      let to = parseInt(div.dataset.index);
                      await fetch('/api/queue/reorder', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({from,to})});
                      refreshQueue();
                  });

                  container.appendChild(div);
              });

              // Update now playing
              let npRes = await fetch('/api/current');
              let npData = await npRes.json();
              console.log(npData)
              console.log(musics)
              document.getElementById("now-playing").textContent = npData.hash ? fancyname(npData.hash, musics[npData.hash].name, musics[npData.hash].duration)  : "Nothing";
          }


            async function play() { await fetch('/api/play', {method:'POST'}); refreshQueue(); }
            async function pause() { await fetch('/api/pause', {method:'POST'}); refreshQueue(); }

            loadMusics();
            refreshQueue();
        </script>
    </body>
    </html>
    """)

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
        current = {"hash": h, "start": time.time(), "duration": musics[h]["duration"], "paused": False, "position": 0}
    return jsonify(current)

@app.route("/api/pause", methods=["POST"])
async def pause():
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
