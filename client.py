#!/usr/bin/env python3
from hashlib import sha256
from json5 import load, dump
from json import loads, dumps
from subprocess import run
from os import walk
from sys import argv, exit
from time import time
from pathlib import Path
from shutil import which
import asyncio
import logging
import argparse
import websockets

# try to import mpv
try:
    from mpv import MPV
except ImportError as e:
    print("Missing dependency: python-mpv (pip install python-mpv)")
    raise

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
log.debug("PROGRAMM STARTED.")

# init config
with open("config.jsonc", "r+") as f:
    CONFIG:dict = load(f)

MUSIC_DIR = Path(CONFIG["media_folder"])
RECONNECT_DELAY = CONFIG["reconnect_delay"]
MAXDELAY_TILL_RESYNC = CONFIG["maxdelay_till_resync"]
VIDEO_WINDOW = CONFIG["video_window"]
WS_SERVER = CONFIG["ws_server"]

# build maps: hash -> path, hash -> filename
def scan_musics():
    exts = {
        ".mp3", ".ogg", ".webm", ".flac", ".wav", ".m4a",
        ".mp4", ".mkv", ".avi",  ".mov",  ".wmv", ".flv", ".mpg",".mpeg"
    }
    mapping = {}
    names = {}
    for root, _, files in walk(MUSIC_DIR):
        for fname in files:
            p = Path(root) / fname
            if p.suffix.lower() in exts:
                hash = sha256()
                with open(p, "rb") as f:
                    while True:
                        chunk = f.read(8192)
                        if not chunk:
                            break
                        hash.update(chunk)
                digest = hash.hexdigest()
                mapping[digest] = str(p)
                names[digest] = p
    return mapping, names

MUSIC_PATHS, MUSIC_NAMES = scan_musics()
_PLAYING = False
try:
    ws_server = argv[1]
except Exception:
    ws_server = WS_SERVER
ws_url = f"ws://{ws_server}"

player = MPV(ytdl=False, video=VIDEO_WINDOW)

current_hash = None
current_path = None
current_duration = 0.0
paused = False
_last_seeked_to = None

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


# attempt to seek robustly
def seek_player(position):
    log.info(f"Seeking to {position}s")
    global player
    try:
        # try direct method
        if hasattr(player, "seek"):
            player.seek(position, reference="absolute+exact")
            return True
        # fallback to command interface
        try:
            player.command("seek", str(position), "absolute+exact")
            return True
        except Exception:
            pass
    except Exception:
        pass
    return False

# monitor task: detect end-of-song and send position updates when requested
async def monitor_and_report(ws):
    global current_hash, current_duration, paused, player
    while True:
        await asyncio.sleep(0.5)
        try:
            # check current playback pos
            pos = None
            try:
                pos = getattr(player, "time_pos", None)
                # sometimes property is a function or bytes, normalize
                if callable(pos):
                    pos = pos()
            except Exception:
                pos = None

            # try to get duration from mpv if we don't have it yet
            if current_path and (not current_duration or current_duration == 0.0):
                # try reading mpv property 'duration'
                try:
                    dur = getattr(player, "duration", None)
                    if callable(dur):
                        dur = dur()
                        current_duration = float(dur) # pyright: ignore[reportArgumentType]
                    else:
                        dur = None
                except Exception:
                    pass
                # fallback: use mutagen
                if (not current_duration or current_duration == 0.0) and current_path:
                    d = get_media_duration(current_path)
                    if d and d > 0:
                        current_duration = float(d)

            # detect end (allow small tolerance)
            if current_hash and current_duration and pos is not None:
                # sometimes pos jumps slightly past duration at EOF; use tolerance
                if pos >= max(0.0, current_duration - 0.6): # pyright: ignore[reportOperatorIssue]
                    # ended
                    try:
                        msg = {"type": "position", "hash": current_hash, "position": current_duration, "duration": current_duration, "event": "ended"}
                        await ws.send(dumps(msg))
                    except Exception:
                        pass
                    # clear state (mpv might go idle)
                    current_hash = None
                    current_path = None
                    current_duration = 0.0
                    paused = False
                    # stop mpv to be safe
                    try:
                        player.stop()
                    except Exception:
                        pass
                    continue

            # optionally send periodic heartbeat position (disabled by default)
            # If server asks for position, it'll send a request; we respond in the receive loop.

        except asyncio.CancelledError:
            break
        except Exception:
            # ignore monitor errors
            await asyncio.sleep(0.1)
            continue

# handle incoming websocket messages
async def handle_ws_messages(ws):
    global current_hash, current_path, current_duration, paused, player, _last_seeked_to

    async for raw in ws:
        try:
            data = loads(raw)
        except Exception:
            continue

        log.debug(f"SERVER SENT WS: {data}")
        # server broadcasts a "play" message with current info
        if data.get("type") == "play":
            curr = data.get("current", {}) or {}
            h = curr.get("hash")
            if not h:
                # if null, stop playback
                try:
                    player.stop()
                except Exception:
                    pass
                current_hash = None
                current_path = None
                current_duration = 0.0
                paused = False
                continue
            

            if h == current_hash:
                # update pause state or seek if server provided position
                pos = curr.get("position", None)
                if pos is not None:
                    # seek if drift significant
                    try:
                        current_pos = getattr(player, "time_pos", None) or 0.0
                        log.debug(f"where i at: {current_pos}")
                        if current_pos is None:
                            current_pos = 0.0
                        if abs(current_pos - pos) > MAXDELAY_TILL_RESYNC:
                            log.debug(f"Syncing the song because too much delay ({current_pos-pos}s) to be exact.")
                            seek_player(pos)
                    except Exception:
                        pass
                # set paused if requested
                if curr.get("paused", False):
                    try:
                        player.pause = True
                        paused = True
                    except Exception:
                        pass
                else:
                    try:
                        player.pause = False
                        paused = False
                    except Exception:
                        pass
                continue

            # new track announced
            if h in MUSIC_PATHS:
                log.info("Playing ")
                path = MUSIC_PATHS[h]
                current_hash = h
                current_path = path
                current_duration = curr.get("duration", 0.0) or 0.0
                paused = curr.get("paused", False)

                # start playback
                try:
                    player.stop()
                except Exception:
                    pass
                try:
                    player.play(path)
                except Exception:
                    # fallback: loadfile via command
                    try:
                        player.command("loadfile", path, "replace")
                    except Exception:
                        pass

                # give mpv a moment to load, then get duration from mutagen/mpv if available
                await asyncio.sleep(0.2)
                if not current_duration:
                    d = get_media_duration(path)
                    if d:
                        current_duration = d
                # if server provided position, seek
                pos = curr.get("position", 0.0) or 0.0
                if pos:
                    # try to seek
                    log.debug("seek0 273")
                    seek_player(pos)
                    _last_seeked_to = time()
                # apply pause state
                try:
                    player.pause = bool(paused)
                except Exception:
                    pass

                # inform server of duration & initial position
                try:
                    mpv_dur = getattr(player, "duration", None)
                    mpv_dur = mpv_dur() if callable(mpv_dur) else mpv_dur
                    if isinstance(mpv_dur, (int, float)) and mpv_dur:
                        current_duration = float(mpv_dur)
                except Exception:
                    pass

                try:
                    msg = {"type": "position", "hash": current_hash, "position": getattr(player, "time_pos", 0) or 0, "duration": current_duration}
                    await ws.send(dumps(msg))
                except Exception:
                    pass

            else:
                # server sent hash we don't have
                log.warning(f"Received unknown hash from server: {h}")

        elif data.get("type") == "rebroadcast":
            # server telling current state on connect
            curr = data.get("current", {}) or {}
            # handle similarly to play: if there's a current hash, optionally start playing
            h = curr.get("hash")
            if h:
                # reuse same handling: enqueue a synthetic play message
                synth = {"type": "play", "current": curr}
                # directly process
                await handle_single_play_message(synth, ws)
            else:
                # nothing playing
                try:
                    player.stop()
                except Exception:
                    pass
                current_hash = None
                current_path = None
                current_duration = 0.0
                paused = False

        # server requests position explicitly (support several possible request shapes)
        elif data.get("type") in ("request_position", "position_request", "get_position") or data.get("request") == "position":
            pos = getattr(player, "time_pos", None) or 0.0
            dur = current_duration or 0.0
            try:
                await ws.send(dumps({"type": "position", "hash": current_hash, "position": pos, "duration": dur}))
            except Exception:
                pass

        # ignore other message types, but support generic 'ping' -> respond 'pong'
        elif data.get("type") == "ping":
            try:
                await ws.send(dumps({"type": "pong"}))
            except Exception:
                pass

# small helper to handle a synthetic play message (same logic re-used)
async def handle_single_play_message(msg, ws):
    # reuse the same logic as in handle_ws_messages for 'play' and 'rebroadcast'
    inner = msg.get("current", {}) or {}
    h = inner.get("hash")
    global current_hash, current_path, current_duration, paused
    if not h:
        try:
            player.stop()
        except Exception:
            pass
        current_hash = None
        current_path = None
        current_duration = 0.0
        paused = False
        return

    if h in MUSIC_PATHS:
        path = MUSIC_PATHS[h]
        current_hash = h
        current_path = path
        current_duration = inner.get("duration", 0.0) or 0.0
        paused = inner.get("paused", False)

        try:
            player.stop()
        except Exception:
            pass
        try:
            player.play(path)
        except Exception:
            try:
                player.command("loadfile", path, "replace")
            except Exception:
                pass

        await asyncio.sleep(0.2)
        if not current_duration:
            d = get_media_duration(path)
            if d:
                current_duration = d

        pos = inner.get("position", 0.0) or 0.0
        if pos:
            log.debug("seek0 386")
            seek_player(pos)
        try:
            player.pause = bool(paused)
        except Exception:
            pass

        try:
            msg = {"type": "position", "hash": current_hash, "position": getattr(player, "time_pos", 0) or 0, "duration": current_duration}
            await ws.send(dumps(msg))
        except Exception:
            pass
    else:
        log.warning(f"Received unknown hash from server: {h}")
        log.info(h in MUSIC_PATHS)
        log.info(h)
        log.info(MUSIC_PATHS)

# main connection loop (reconnects automatically)
async def main_loop():
    global player
    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                log.info(f"Connected to {ws_url}")
                # on connect send a hello identifying available hashes
                try:
                    await ws.send(dumps({"type": "hello", "available": list(MUSIC_PATHS.keys())[:50]}))
                except Exception:
                    pass

                monitor_task = asyncio.create_task(monitor_and_report(ws))
                receiver_task = asyncio.create_task(handle_ws_messages(ws))

                done, pending = await asyncio.wait(
                    [monitor_task, receiver_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
        except (ConnectionRefusedError, OSError) as e:
            log.warning(f"Could not connect to server {ws_url}: {e}; retrying in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)
            continue
        except websockets.exceptions.ConnectionClosedError as e:
            log.warning(f"Connection closed: {e}; reconnecting in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)
            continue
        except Exception as e:
            log.error(f"Websocket loop failed: {e}")
            await asyncio.sleep(RECONNECT_DELAY)
            continue

if __name__ == "__main__":
    if not MUSIC_PATHS:
        log.info("No music files found in musics/ — create the directory and place files (*.mp3,*.ogg,*.webm, etc.)")
        exit(1)
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        log.info("exiting")
        try:
            player.stop()
        except Exception:
            pass
        raise
