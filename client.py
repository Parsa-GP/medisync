#!/usr/bin/env python3
"""
client.py - connect to server websocket, play announced tracks (by hash) from local musics/,
report position/duration on request, and detect song end.

Requires:
  pip install python-mpv websockets mutagen
"""

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
import logging

import websockets

# try to import mpv and mutagen
try:
    from mpv import MPV
except Exception as e:
    print("missing dependency: python-mpv (pip install python-mpv)")
    raise

try:
    from mutagen import File as MutagenFile
except Exception:
    MutagenFile = None

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

# env vars
MUSIC_DIR = Path("musics")
try:
    ws_server = sys.argv[1]
except Exception:
    ws_server = "127.0.0.1:6789"
WS_URI = f"ws://{ws_server}"  # match server
RECONNECT_DELAY = 2.0

# build maps: hash -> path, hash -> filename
def scan_musics():
    exts = {".mp3", ".ogg", ".webm", ".flac", ".wav", ".m4a"}
    mapping = {}
    names = {}
    for root, _, files in os.walk(MUSIC_DIR):
        for fn in files:
            p = Path(root) / fn
            if p.suffix.lower() in exts:
                # compute sha256
                h = hashlib.sha256()
                with open(p, "rb") as f:
                    while True:
                        chunk = f.read(8192)
                        if not chunk:
                            break
                        h.update(chunk)
                digest = h.hexdigest()
                mapping[digest] = str(p)
                names[digest] = fn
    return mapping, names

MUSIC_PATHS, MUSIC_NAMES = scan_musics()

# mpv player
player = MPV(ytdl=False, input_default_bindings=True, input_vo_keyboard=True)
# state
current_hash = None
current_path = None
current_duration = 0.0
paused = False
_last_seeked_to = None

# helper: get duration via mutagen or mpv property fallback
def get_file_duration(path):
    # try mutagen first (more reliable)
    if MutagenFile:
        try:
            meta = MutagenFile(path)
            if meta and getattr(meta, "info", None):
                dur = getattr(meta.info, "length", None)
                if dur:
                    return float(dur)
        except Exception:
            pass
    # fallback: attempt to read player duration property after load (may be None)
    try:
        # command 'get_property' not available here; return 0 and let mpv populate later
        return 0.0
    except Exception:
        return 0.0

# attempt to seek robustly
def seek_player(position):
    log.info(f"Seeking to {position}s")
    global player
    try:
        # try direct method
        if hasattr(player, "seek"):
            # some python-mpv versions support player.seek(seconds, reference='absolute')
            try:
                player.seek(position, reference="absolute")
                return True
            except TypeError:
                # older signature maybe player.seek(seconds, 'absolute')
                try:
                    player.seek(position, "absolute")
                    return True
                except Exception:
                    pass
        # fallback to command interface
        try:
            player.command("seek", str(position), "absolute")
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
                    try:
                        pos = pos()
                    except Exception:
                        pos = None
            except Exception:
                pos = None

            # try to get duration from mpv if we don't have it yet
            if current_path and (not current_duration or current_duration == 0.0):
                # try reading mpv property 'duration'
                try:
                    dur = getattr(player, "duration", None)
                    if callable(dur):
                        try:
                            dur = dur()
                        except Exception:
                            dur = None
                    if dur:
                        current_duration = float(dur)
                except Exception:
                    pass
                # fallback: use mutagen
                if (not current_duration or current_duration == 0.0) and current_path:
                    d = get_file_duration(current_path)
                    if d and d > 0:
                        current_duration = float(d)

            # detect end (allow small tolerance)
            if current_hash and current_duration and pos is not None:
                # sometimes pos jumps slightly past duration at EOF; use tolerance
                if pos >= max(0.0, current_duration - 0.6):
                    # ended
                    try:
                        msg = {"type": "position", "hash": current_hash, "position": current_duration, "duration": current_duration, "event": "ended"}
                        await ws.send(json.dumps(msg))
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
            data = json.loads(raw)
        except Exception:
            continue

        log.info(f"SERVER SENT WS: {data}")
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
                        log.info(f"where i at: {current_pos}")
                        if current_pos is None:
                            current_pos = 0.0
                        if abs(current_pos - pos) > 0.8:
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
                    d = get_file_duration(path)
                    if d:
                        current_duration = d
                # if server provided position, seek
                pos = curr.get("position", 0.0) or 0.0
                if pos:
                    # try to seek
                    log.info("seek0 273")
                    seek_player(pos)
                    _last_seeked_to = time.time()
                # apply pause state
                try:
                    player.pause = bool(paused)
                except Exception:
                    pass

                # inform server of duration & initial position
                try:
                    # attempt to get mpv duration property
                    mpv_dur = getattr(player, "duration", None)
                    if callable(mpv_dur):
                        try:
                            mpv_dur = mpv_dur()
                        except Exception:
                            mpv_dur = None
                    if mpv_dur:
                        current_duration = float(mpv_dur)
                except Exception:
                    pass

                try:
                    msg = {"type": "position", "hash": current_hash, "position": getattr(player, "time_pos", 0) or 0, "duration": current_duration}
                    await ws.send(json.dumps(msg))
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
                await ws.send(json.dumps({"type": "position", "hash": current_hash, "position": pos, "duration": dur}))
            except Exception:
                pass

        # ignore other message types, but support generic 'ping' -> respond 'pong'
        elif data.get("type") == "ping":
            try:
                await ws.send(json.dumps({"type": "pong"}))
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
            d = get_file_duration(path)
            if d:
                current_duration = d

        pos = inner.get("position", 0.0) or 0.0
        if pos:
            log.info("seek0 386")
            seek_player(pos)
        try:
            player.pause = bool(paused)
        except Exception:
            pass

        try:
            msg = {"type": "position", "hash": current_hash, "position": getattr(player, "time_pos", 0) or 0, "duration": current_duration}
            await ws.send(json.dumps(msg))
        except Exception:
            pass
    else:
        log.warning(f"Received unknown hash from server: {h}")

# main connection loop (reconnects automatically)
async def main_loop():
    global player
    while True:
        try:
            async with websockets.connect(WS_URI) as ws:
                log.info(f"Connected to {WS_URI}")
                # on connect send a hello identifying available hashes
                try:
                    await ws.send(json.dumps({"type": "hello", "available": list(MUSIC_PATHS.keys())[:50]}))
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
            log.warning(f"Could not connect to server {WS_URI}: {e}; retrying in {RECONNECT_DELAY}s")
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
        sys.exit(1)
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        log.info("exiting")
        try:
            player.stop()
        except Exception:
            pass
        raise
