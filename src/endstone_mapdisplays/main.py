"""
endstone-mapdisplays  —  Server-local video & image display system for Endstone.

Folder layout (relative to plugins/mapdisplays/):
    videos/         ← drop MP4 / WEBM / MKV here
    images/         ← drop PNG / JPG here
    idle.webm       ← replaceable idle animation (auto-copied from package on first run)
    config.json     ← plugin configuration (world_folder, display_fps, etc.)
    displays.json   ← persisted display state
    resourcepack/   ← auto-generated Bedrock resource pack with extracted OGG audio
        manifest.json
        sounds.json
        sounds/mapdisplays/<stem>.ogg

Stream support:
    /setdisplay <id> stream <url>  — stream any YouTube/Twitch/HTTP URL via yt-dlp.
    Sound is intentionally disabled for streams (no stable sync is possible).

Auto resource pack registration:
    Set 'world_folder' in config.json to the path of your world folder.
    The plugin will automatically update world_resource_packs.json and bump the
    manifest version each time audio is added or removed.
"""


import asyncio as aio
import json
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, cast

import av
import numpy as np
from PIL import Image

from endstone import Player
from endstone import asyncio as endstone_aio
from endstone.event import PlayerJoinEvent, event_handler
from endstone.inventory import ItemStack, MapMeta
from endstone.map import MapCanvas, MapRenderer, MapView
from endstone.plugin import Plugin

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

_VALID_VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi"}
_VALID_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}

# UUID used for our resource pack in manifest.json and world_resource_packs.json
_RP_HEADER_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
_RP_MODULE_UUID = "b2c3d4e5-f6a7-8901-bcde-f12345678901"

_DEFAULT_CONFIG: dict = {
    # How often to decode/advance video frames (frames per second).
    # Affects audio sync timing. Higher = smoother but more CPU.
    "display_fps": 10,

    # How often to actually SEND map packets to players (frames per second).
    # This directly controls main-thread scheduler load.
    # 4 fps is visually indistinguishable on 128px maps and is much lighter.
    # Lower this first if you see server lag (try 2 or 3).
    "send_fps": 4,
}


def _resize_rgb(img: np.ndarray, width: int, height: int) -> np.ndarray:
    """Resize an HxWx3 uint8 RGB array using Pillow (no OpenCV needed)."""
    if img.shape[:2] == (height, width):
        return img
    pil = Image.fromarray(img, mode="RGB").resize((width, height), Image.BILINEAR)
    return np.asarray(pil)


# ──────────────────────────────────────────────────────────────────────────────
# Renderer — one 128×128 tile of a display
# ──────────────────────────────────────────────────────────────────────────────

class CabinetMapRenderer(MapRenderer):
    """Thread-safe renderer for a single 128×128 map tile."""

    def __init__(self) -> None:
        super().__init__(is_contextual=False)
        self._lock = threading.Lock()
        self._buffer = np.zeros((128, 128, 4), dtype=np.uint8)
        self._has_frame = False
        self._frame_id = -1

    def push(self, rgb_crop: np.ndarray, frame_id: int) -> bool:
        """
        Push a new 128×128 RGB or RGBA crop.
        Returns True if the frame was new (i.e. the display needs a send_map call).
        """
        if frame_id == self._frame_id:
            return False
        with self._lock:
            if rgb_crop.ndim == 3 and rgb_crop.shape[2] == 3:
                buf = np.empty((128, 128, 4), dtype=np.uint8)
                buf[:, :, :3] = rgb_crop
                buf[:, :, 3] = 255
                np.copyto(self._buffer, buf)
            else:
                np.copyto(self._buffer, rgb_crop[:128, :128])
            self._has_frame = True
            self._frame_id = frame_id
        return True

    def render(self, view: MapView, canvas: MapCanvas, player: Player) -> None:
        with self._lock:
            if self._has_frame:
                canvas.draw_image(0, 0, cast(Any, self._buffer))


# ──────────────────────────────────────────────────────────────────────────────
# Display States
# ──────────────────────────────────────────────────────────────────────────────

class DisplayState(ABC):
    """Abstract base for anything that can drive a map display."""

    @abstractmethod
    def get_full_frame(self) -> tuple[np.ndarray, int]:
        """Return (HxWx3 RGB frame, frame_id). frame_id increments on each new frame."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Signal the state to stop any background threads."""
        ...

    @property
    def duration(self) -> float | None:
        """Duration in seconds; None if infinite / not applicable."""
        return None



class IdleState(DisplayState):
    """
    Loops a webm/mp4 animation as an idle screen.

    Resolution order:
    1. data_folder/idle.webm     (user-replaceable)
    2. Package resource fallback (resources/idle.webm baked into wheel)
    """

    _FALLBACK_RESOURCE = "resources/idle.webm"

    def __init__(self, width: int, height: int, logger: Any, data_folder: Path) -> None:
        self._width = width
        self._height = height
        self._logger = logger
        self._lock = threading.Lock()
        self._frame = np.zeros((height, width, 3), dtype=np.uint8)
        self._frame_id = 0
        self._running = True

        # Prefer the user-replaceable copy in the data folder
        data_copy = data_folder / "idle.webm"
        if data_copy.exists():
            self._path: str | None = str(data_copy)
        else:
            try:
                from importlib.resources import files as _res_files
                res = _res_files("endstone_mapdisplays").joinpath(self._FALLBACK_RESOURCE)
                self._path = str(res)
            except Exception:
                self._path = None

        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="mapdisplay-idle"
        )
        self._thread.start()

    def _loop(self) -> None:
        back_off = 1.0
        # Cap idle animation to a sensible fps — no need to run at native video fps
        _MAX_FPS = 10.0
        while self._running:
            if not self._path:
                time.sleep(1.0)
                continue
            try:
                with av.open(self._path) as container:
                    stream = container.streams.video[0]
                    raw_fps = float(stream.average_rate) if stream.average_rate else _MAX_FPS
                    fps = min(raw_fps, _MAX_FPS)
                    frame_time = 1.0 / fps
                    skip = max(1, round(raw_fps / fps))  # decode every Nth frame
                    i = 0
                    for frame in container.decode(video=0):
                        if not self._running:
                            return
                        i += 1
                        if i % skip != 0:
                            continue  # drop frames we don't need
                        img = frame.to_ndarray(format="rgb24")
                        img = _resize_rgb(img, self._width, self._height)
                        with self._lock:
                            self._frame = img
                            self._frame_id += 1
                        time.sleep(frame_time)
                back_off = 1.0  # clean loop — reset back-off
            except Exception as exc:
                self._logger.warning(f"[MapDisplays] IdleState decode error: {exc}")
                time.sleep(back_off)
                back_off = min(back_off * 2, 30.0)

    def get_full_frame(self) -> tuple[np.ndarray, int]:
        with self._lock:
            return self._frame, self._frame_id

    def stop(self) -> None:
        self._running = False


class ImageState(DisplayState):
    """Displays a static image (PNG, JPG, etc.) — no background thread needed."""

    def __init__(self, width: int, height: int, logger: Any, path: Path) -> None:
        self._logger = logger
        self._frame = np.zeros((height, width, 3), dtype=np.uint8)
        self._frame_id = 0
        try:
            img = (
                Image.open(path)
                .convert("RGB")
                .resize((width, height), Image.LANCZOS)
            )
            self._frame = np.asarray(img)
            self._frame_id = 1
        except Exception as exc:
            logger.error(f"[MapDisplays] ImageState failed to load '{path}': {exc}")

    def get_full_frame(self) -> tuple[np.ndarray, int]:
        return self._frame, self._frame_id

    def stop(self) -> None:
        pass  # stateless — nothing to clean up


class VideoFileState(DisplayState):
    """
    Streams a local video file, looping indefinitely.

    Tracks video duration for sound loop synchronisation.
    Calls on_loop() each time the video restarts; the plugin uses this to
    restart the Bedrock sound event so audio stays in sync.
    """

    def __init__(
        self,
        width: int,
        height: int,
        logger: Any,
        path: Path
    ) -> None:
        self._width = width
        self._height = height
        self._logger = logger
        self._path = path
        self._lock = threading.Lock()
        self._frame = np.zeros((height, width, 3), dtype=np.uint8)
        self._frame_id = 0
        self._running = True
        self._duration: float | None = None

        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name=f"mapdisplay-video-{path.stem}",
        )
        self._thread.start()

    @property
    def duration(self) -> float | None:
        return self._duration

    def _loop(self) -> None:
        back_off = 1.0
        # Cap to a map-friendly fps — decoding 30fps into 128px maps wastes CPU
        _MAX_FPS = 10.0
        while self._running:
            try:
                with av.open(str(self._path)) as container:
                    v_stream = container.streams.video[0]
                    raw_fps = float(v_stream.average_rate) if v_stream.average_rate else _MAX_FPS
                    fps = min(raw_fps, _MAX_FPS)
                    frame_time = 1.0 / fps
                    skip = max(1, round(raw_fps / fps))  # how many encoded frames to skip

                    # Capture duration once
                    if self._duration is None and container.duration:
                        self._duration = float(container.duration) / 1_000_000.0

                    deadline = time.perf_counter()
                    i = 0
                    for frame in container.decode(video=0):
                        if not self._running:
                            return
                        i += 1
                        if i % skip != 0:
                            continue  # skip frames we don't need
                        img = frame.to_ndarray(format="rgb24")
                        img = _resize_rgb(img, self._width, self._height)

                        # Frame-rate limiter
                        now = time.perf_counter()
                        wait = deadline - now
                        if wait > 0:
                            time.sleep(wait)
                        deadline = time.perf_counter() + frame_time

                        with self._lock:
                            self._frame = img
                            self._frame_id += 1

                # Video ended naturally — fire loop callback, then continue loop
                if self._running and self.on_loop is not None:
                    try:
                        self.on_loop()
                    except Exception:
                        pass
                back_off = 1.0

            except Exception as exc:
                self._logger.warning(
                    f"[MapDisplays] VideoFileState '{self._path.name}' error: {exc}"
                )
                time.sleep(back_off)
                back_off = min(back_off * 2, 30.0)

    def get_full_frame(self) -> tuple[np.ndarray, int]:
        with self._lock:
            return self._frame, self._frame_id

    def stop(self) -> None:
        self._running = False


class StreamState(DisplayState):
    """
    Streams video from any URL supported by yt-dlp (YouTube, Twitch, direct HLS,
    plain HTTP video streams, etc.).

    Resolution order:
    1. yt-dlp extracts the best direct stream URL (preferred — handles all platforms)
    2. Falls back to av.open(url) directly (works for raw HTTP/HLS/RTSP streams)

    Sound is intentionally NOT supported — audio sync across a network stream is
    not reliably achievable with the Bedrock sound event system.
    """

    # Prefer low-resolution streams to reduce server CPU load
    _YDL_FORMAT = (
        "bestvideo[height<=144][ext=mp4]/"
        "bestvideo[height<=240][ext=mp4]/"
        "bestvideo[height<=144]/"
        "bestvideo[height<=360]/"
        "best[height<=144]/best"
    )

    def __init__(
        self,
        width: int,
        height: int,
        logger: Any,
        url: str,
        data_folder: Path,
    ) -> None:
        self._width = width
        self._height = height
        self._logger = logger
        self._url = url
        self._data_folder = data_folder

        self._lock = threading.Lock()
        self._frame = np.zeros((height, width, 3), dtype=np.uint8)
        self._frame_id = 0
        self._running = True

        # Show idle animation while the URL is being resolved
        self._idle = IdleState(width, height, logger, data_folder)
        self._stream_url: str | None = None
        self._resolving = True

        self._resolve_thread = threading.Thread(
            target=self._resolve_url, daemon=True, name="mapdisplay-stream-resolve"
        )
        self._resolve_thread.start()

        self._decode_thread = threading.Thread(
            target=self._decode_loop, daemon=True, name="mapdisplay-stream-decode"
        )
        self._decode_thread.start()

    # sound_name intentionally returns None — no audio for streams

    def _resolve_url(self) -> None:
        """Try yt-dlp first; fall back to using the raw URL directly with av."""
        try:
            import yt_dlp  # optional dependency

            ydl_opts = {
                "format": self._YDL_FORMAT,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                # No cookiesfrombrowser — fails on headless servers.
                # Public videos work without it; age-restricted content will fail gracefully.
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self._url, download=False)
                if info:
                    # Prefer direct URL; for formats list, pick the first entry
                    url = info.get("url")
                    if not url and info.get("formats"):
                        url = info["formats"][0].get("url")
                    if url:
                        self._stream_url = url
                        self._logger.info(f"[MapDisplays] Stream URL resolved via yt-dlp.")
                        self._resolving = False
                        return

        except ImportError:
            self._logger.warning(
                "[MapDisplays] yt-dlp not installed — attempting direct stream open."
            )
        except Exception as exc:
            self._logger.warning(
                f"[MapDisplays] yt-dlp resolution failed: {exc} — trying direct open."
            )

        # Fallback: let av try to open the URL directly (works for plain HLS/HTTP)
        self._stream_url = self._url
        self._logger.info("[MapDisplays] Using URL directly with av (no yt-dlp resolution).")
        self._resolving = False

    def _decode_loop(self) -> None:
        back_off = 2.0
        while self._running:
            # While resolving, serve idle frames
            if self._resolving or not self._stream_url:
                frame, fid = self._idle.get_full_frame()
                with self._lock:
                    self._frame = frame
                    self._frame_id = fid
                time.sleep(0.05)
                continue

            try:
                self._logger.info("[MapDisplays] StreamState: opening stream…")
                # Low-latency flags for HTTP/HLS streams
                options = {
                    "fflags": "nobuffer",
                    "flags": "low_delay",
                    "analyzeduration": "1000000",
                    "probesize": "65536",
                }
                with av.open(self._stream_url, options=options) as container:
                    v_stream = container.streams.video[0]
                    v_stream.thread_type = "AUTO"
                    fps = float(v_stream.average_rate) if v_stream.average_rate else 20.0
                    frame_time = 1.0 / fps
                    deadline = time.perf_counter()

                    for packet in container.demux(v_stream):
                        if not self._running:
                            return
                        try:
                            frames = list(packet.decode())
                            if not frames:
                                continue

                            # Drop stale frames if we're falling behind
                            now = time.perf_counter()
                            if now > deadline and len(frames) > 1:
                                frames = [frames[-1]]

                            for frame in frames:
                                if not self._running:
                                    return
                                img = frame.to_ndarray(format="rgb24")
                                img = _resize_rgb(img, self._width, self._height)
                                with self._lock:
                                    self._frame = img
                                    self._frame_id += 1

                            deadline += frame_time
                            gap = deadline - time.perf_counter()
                            if gap > 0:
                                time.sleep(gap)
                            else:
                                deadline = time.perf_counter()

                        except Exception:
                            continue  # skip bad packets

                # Stream ended — re-resolve (live streams reconnect; VODs restart)
                self._logger.info("[MapDisplays] Stream ended — reconnecting…")
                self._resolving = True
                self._stream_url = None
                self._resolve_thread = threading.Thread(
                    target=self._resolve_url, daemon=True, name="mapdisplay-stream-resolve"
                )
                self._resolve_thread.start()
                back_off = 2.0

            except Exception as exc:
                self._logger.warning(f"[MapDisplays] StreamState error: {exc}")
                time.sleep(back_off)
                back_off = min(back_off * 2, 60.0)

    def get_full_frame(self) -> tuple[np.ndarray, int]:
        with self._lock:
            return self._frame, self._frame_id

    def stop(self) -> None:
        self._running = False
        self._idle.stop()


# ──────────────────────────────────────────────────────────────────────────────
# MapDisplay — a grid of tiles driven by a DisplayState
# ──────────────────────────────────────────────────────────────────────────────

class MapDisplay:
    """Manages a rows×cols grid of CabinetMapRenderers backed by a single DisplayState."""

    def __init__(
        self,
        plugin: "EntryForPlugin",
        display_id: int,
        cols: int,
        rows: int,
        creator_name: str = "",
    ) -> None:
        self.plugin = plugin
        self.display_id = display_id
        self.cols = cols
        self.rows = rows
        self.width = cols * 128
        self.height = rows * 128
        self.creator_name = creator_name  # player who ran /getdisplay
        self._last_send_time: float = 0.0  # throttle main-thread send_map calls

        self._state_lock = threading.Lock()
        self._state: DisplayState = IdleState(
            self.width, self.height, plugin.logger, Path(plugin.data_folder)
        )
        self._state_name = "idle"
        self._state_arg: str | None = None

        # Pre-build a flat (renderer, view) list — avoids re-allocation every frame
        self._grid: list[list[tuple[CabinetMapRenderer, MapView]]] = []
        self._tiles: list[tuple[CabinetMapRenderer, MapView]] = []

        for r in range(rows):
            row: list[tuple[CabinetMapRenderer, MapView]] = []
            for c in range(cols):
                renderer = CabinetMapRenderer()
                view = plugin.server.create_map(
                    plugin.server.level.get_dimension("Overworld")
                )
                # Remove default renderer so only our renderer runs
                for old in list(view.renderers):
                    view.remove_renderer(old)
                view.add_renderer(renderer)
                row.append((renderer, view))
                self._tiles.append((renderer, view))
            self._grid.append(row)

    # ── Properties ──────────────────────────────────────────────────────────

    @property
    def state(self) -> DisplayState:
        with self._state_lock:
            return self._state

    @property
    def map_ids(self) -> list[list[int]]:
        return [
            [self._grid[r][c][1].id for c in range(self.cols)]
            for r in range(self.rows)
        ]

    # ── State management ────────────────────────────────────────────────────

    def set_state(
        self,
        new_state: DisplayState,
        name: str,
        arg: str | None = None,
    ) -> None:
        """Thread-safe state swap. Stops the old state after releasing the lock."""
        with self._state_lock:
            old_state = self._state
            self._state = new_state
            self._state_name = name
            self._state_arg = arg
        # Stop old state outside the lock so it doesn't deadlock with its own lock
        try:
            old_state.stop()
        except Exception:
            pass

    # ── Frame push ──────────────────────────────────────────────────────────

    def update(self) -> None:
        """
        Pull the current frame from the active state and push to tiles.

        Decoupled rates:
        - Decode/canvas update: runs at display_fps (for audio timing accuracy)
        - Packet send (run_task): gated by send_fps (default 4fps)

        This prevents the main game thread from being flooded with send_map
        calls, which delays commands, forms, and game ticks.
        """
        with self._state_lock:
            state = self._state

        full_frame, frame_id = state.get_full_frame()

        # Always update in-memory canvas so the renderer stays current,
        # but collect which views have a genuinely new frame.
        updated_views = []
        for r in range(self.rows):
            for c in range(self.cols):
                renderer, view = self._grid[r][c]
                crop = full_frame[r * 128:(r + 1) * 128, c * 128:(c + 1) * 128]
                if renderer.push(crop, frame_id):
                    updated_views.append(view)

        if not updated_views:
            return  # frame unchanged — no work needed

        # Rate-limit packet sends to send_fps (independent of decode/canvas rate).
        # This is the primary control for main-thread scheduler pressure.
        send_fps = float(self.plugin._config.get("send_fps", 4))
        send_fps = max(1.0, min(send_fps, 20.0))
        now = time.monotonic()
        if now - self._last_send_time < 1.0 / send_fps:
            return  # too soon — canvas updated but skip this packet burst
        self._last_send_time = now

        # One single run_task per display — sends ALL updated tiles together.
        def _send_batch(views=updated_views, plg=self.plugin):
            for player in plg.server.online_players:
                for v in views:
                    try:
                        player.send_map(v)
                    except Exception:
                        pass
        self.plugin.server.scheduler.run_task(self.plugin, _send_batch)

    # ── Inventory helpers ───────────────────────────────────────────────────

    def give_maps_to(self, player: Player) -> None:
        """Give all map items (one per tile) to a player."""
        for r in range(self.rows):
            for c in range(self.cols):
                _, view = self._grid[r][c]
                item = ItemStack("minecraft:filled_map")
                meta = item.item_meta
                if isinstance(meta, MapMeta):
                    meta.map_view = view
                    item.set_item_meta(meta)
                player.inventory.add_item(item)

    def send_all_maps_to(self, player: Player) -> None:
        """Force-send every current frame to a specific player (e.g. on join)."""
        for _, view in self._tiles:
            try:
                player.send_map(view)
            except Exception:
                pass

    # ── Persistence helper ──────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "id": self.display_id,
            "cols": self.cols,
            "rows": self.rows,
            "state_name": self._state_name,
            "state_arg": self._state_arg,
            "creator_name": self.creator_name,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Plugin Entry Point
# ──────────────────────────────────────────────────────────────────────────────

class EntryForPlugin(Plugin):
    # Required class-level metadata
    name = "mapdisplays"
    api_version = "0.5"

    commands = {
        "getdisplay": {
            "description": "Create a tiled map display and receive the map items",
            "usages": ["/getdisplay <cols: int> <rows: int>"],
            "permissions": ["mapdisplays.command.get"],
        },
        "setdisplay": {
            "description": "Change what a display shows",
            "usages": [
                "/setdisplay <id: int> video <file: str>",
                "/setdisplay <id: int> image <file: str>",
                "/setdisplay <id: int> stream <url: str>",
                "/setdisplay <id: int> idle",
            ],
            "permissions": ["mapdisplays.command.set"],
        },
        "listvideos": {
            "description": "List video files available in the videos folder",
            "usages": ["/listvideos"],
            "permissions": ["mapdisplays.command.get"],
        },
        "stopdisplay": {
            "description": "Stop a display and reset it to idle",
            "usages": ["/stopdisplay <id: int>"],
            "permissions": ["mapdisplays.command.set"],
        },
        "getmaps": {
            "description": "Re-receive map items for all active displays (use after server restart)",
            "usages": ["/getmaps"],
            "permissions": ["mapdisplays.command.get"],
        },
        "listdisplays": {
            "description": "List all active displays and their current state",
            "usages": ["/listdisplays"],
            "permissions": ["mapdisplays.command.get"],
        },
        "removedisplay": {
            "description": "Permanently remove a display and stop it playing",
            "usages": ["/removedisplay <id: int>"],
            "permissions": ["mapdisplays.command.admin"],
        },
        "removevideo": {
            "description": "Delete a video file from the server",
            "usages": ["/removevideo <filename: str>"],
            "permissions": ["mapdisplays.command.admin"],
        },
        "mdsreload": {
            "description": "Reload mapdisplays config.json without restarting",
            "usages": ["/mdsreload"],
            "permissions": ["mapdisplays.command.admin"],
        },
    }

    permissions = {
        "mapdisplays.command.get": {
            "description": "Receive or list map displays",
            "default": "op",
        },
        "mapdisplays.command.set": {
            "description": "Control what displays show",
            "default": "op",
        },
        "mapdisplays.command.admin": {
            "description": "Administrative commands (reload config, remove video)",
            "default": "op",
        },
    }

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def on_load(self) -> None:
        self.displays: dict[int, MapDisplay] = {}
        self._next_id: int = 0
        self._running: bool = False
        self._config: dict = dict(_DEFAULT_CONFIG)

    def on_enable(self) -> None:
        self._running = True
        self._setup_data_folder()
        self._load_config()
        self._load_persistence()
        self.register_events(self)
        endstone_aio.submit(self._update_loop())
        self.logger.info(
            f"[MapDisplays] Enabled — {len(self.displays)} display(s) restored."
        )

    def on_disable(self) -> None:
        self._running = False
        self._save_persistence()
        for d in self.displays.values():
            try:
                d.state.stop()
            except Exception:
                pass
        self.logger.info("[MapDisplays] Disabled.")

    # ── Data folder setup ────────────────────────────────────────────────────

    def _setup_data_folder(self) -> None:
        df = Path(self.data_folder)
        (df / "videos").mkdir(parents=True, exist_ok=True)
        (df / "images").mkdir(parents=True, exist_ok=True)

        # Copy idle.webm from wheel resources to data_folder so it's user-replaceable
        idle_dest = df / "idle.webm"
        if not idle_dest.exists():
            try:
                from importlib.resources import files as _res_files
                src = _res_files("endstone_mapdisplays").joinpath("resources/idle.webm")
                idle_dest.write_bytes(src.read_bytes())
                self.logger.info("[MapDisplays] Copied default idle.webm to data folder.")
            except Exception as exc:
                self.logger.warning(
                    f"[MapDisplays] Could not copy default idle.webm: {exc}"
                )


    # ── Config ───────────────────────────────────────────────────────────────

    def _load_config(self) -> None:
        """Load config.json, writing defaults if it doesn't exist."""
        path = Path(self.data_folder) / "config.json"
        if not path.exists():
            self._save_config()
            self.logger.info(
                "[MapDisplays] Created default config.json. "
                "Set 'world_folder' to enable auto resource pack registration."
            )
            return
        try:
            loaded = json.loads(path.read_text())
            # Merge loaded values over defaults so new keys are always present
            self._config = {**_DEFAULT_CONFIG, **loaded}
        except Exception as exc:
            self.logger.error(f"[MapDisplays] Failed to read config.json: {exc} — using defaults.")
            self._config = dict(_DEFAULT_CONFIG)

    def _save_config(self) -> None:
        path = Path(self.data_folder) / "config.json"
        try:
            path.write_text(json.dumps(self._config, indent=2))
        except Exception as exc:
            self.logger.error(f"[MapDisplays] Failed to write config.json: {exc}")

    def _save_persistence(self) -> None:
        try:
            data = {
                "next_id": self._next_id,
                "displays": [d.to_dict() for d in self.displays.values()],
            }
            path = Path(self.data_folder) / "displays.json"
            path.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            self.logger.error(f"[MapDisplays] Failed to save displays.json: {exc}")

    def _load_persistence(self) -> None:
        path = Path(self.data_folder) / "displays.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            self._next_id = data.get("next_id", 0)
            for entry in data.get("displays", []):
                display = self._restore_display(entry)
                if display is not None:
                    self.displays[display.display_id] = display
        except Exception as exc:
            self.logger.error(f"[MapDisplays] Failed to load displays.json: {exc}")

    def _restore_display(self, entry: dict) -> MapDisplay | None:
        """Re-create a MapDisplay from a saved entry (maps get new IDs — auto-given on join)."""
        try:
            display = MapDisplay(
                self,
                entry["id"],
                entry["cols"],
                entry["rows"],
                creator_name=entry.get("creator_name", ""),
            )

            state_name = entry.get("state_name", "idle")
            state_arg = entry.get("state_arg")

            if state_name == "video" and state_arg:
                video_path = Path(self.data_folder) / "videos" / state_arg
                if video_path.exists():
                    vs = VideoFileState(
                        display.width, display.height, self.logger, video_path)
                    display.set_state(vs, "video", state_arg)
                else:
                    self.logger.warning(
                        f"[MapDisplays] Video '{state_arg}' not found — display #{entry['id']} set to idle."
                    )
            elif state_name == "image" and state_arg:
                image_path = Path(self.data_folder) / "images" / state_arg
                if image_path.exists():
                    display.set_state(
                        ImageState(display.width, display.height, self.logger, image_path),
                        "image",
                        state_arg,
                    )
            elif state_name == "stream" and state_arg:
                ss = StreamState(
                    display.width, display.height, self.logger,
                    state_arg, Path(self.data_folder)
                )
                display.set_state(ss, "stream", state_arg)
                self.logger.info(
                    f"[MapDisplays] Display #{entry['id']} restoring stream: {state_arg}"
                )

            return display
        except Exception as exc:
            self.logger.error(
                f"[MapDisplays] Failed to restore display #{entry.get('id', '?')}: {exc}"
            )
            return None

    # ── Main update loop ─────────────────────────────────────────────────────

    async def _update_loop(self) -> None:
        """Frame push loop driven by display_fps from config. Runs on the Endstone async executor."""
        while self._running:
            fps = float(self._config.get("display_fps", 10))
            fps = max(1.0, min(fps, 20.0))  # clamp 1–20 fps
            sleep_interval = 1.0 / fps
            for display in list(self.displays.values()):
                try:
                    display.update()
                except Exception as exc:
                    self.logger.warning(
                        f"[MapDisplays] Display #{display.display_id} update error: {exc}"
                    )
            await aio.sleep(sleep_interval)

    # ── Events ───────────────────────────────────────────────────────────────

    @event_handler
    def on_player_join(self, event: PlayerJoinEvent) -> None:
        """Send current map frames, restart sound, and auto-give fresh maps to display creators."""
        player = event.player

        def _welcome():
            try:
                # Re-send current frame for every display
                for display in self.displays.values():
                    display.send_all_maps_to(player)

                # Auto-give fresh map items to display creators after a restart.
                # Map IDs change every restart (Endstone API limitation), so creators
                # need new items. Their old item frames will need to be updated manually,
                # but at least they don't have to run /getmaps.
                creator_displays = [
                    d for d in self.displays.values()
                    if d.creator_name == player.name
                ]
                if creator_displays:
                    player.send_message(
                        f"§e[MapDisplays] §7You created §f{len(creator_displays)} §7display(s). "
                        f"Fresh map items have been added to your inventory — "
                        f"replace the maps in your item frames to restore them after the restart."
                    )
                    for display in creator_displays:
                        display.give_maps_to(player)

            except Exception as exc:
                self.logger.warning(f"[MapDisplays] Join handler error for {player.name}: {exc}")

        # Small delay so the player is fully loaded before we flood them with packets
        self.server.scheduler.run_task(self, _welcome, delay=40)

    # ── Commands ─────────────────────────────────────────────────────────────

    def on_command(self, sender: Any, command: Any, args: list[str]) -> bool:
        if not isinstance(sender, Player):
            sender.send_message("§c[MapDisplays] This command is player-only.")
            return True
        try:
            n = command.name
            if n == "getdisplay":
                return self._cmd_getdisplay(sender, args)
            elif n == "setdisplay":
                return self._cmd_setdisplay(sender, args)
            elif n == "listvideos":
                return self._cmd_listvideos(sender)
            elif n == "stopdisplay":
                return self._cmd_stopdisplay(sender, args)
            elif n == "getmaps":
                return self._cmd_getmaps(sender)
            elif n == "listdisplays":
                return self._cmd_listdisplays(sender)
            elif n == "removedisplay":
                return self._cmd_removedisplay(sender, args)
            elif n == "removevideo":
                return self._cmd_removevideo(sender, args)
            elif n == "mdsreload":
                return self._cmd_reloadconfig(sender)
        except Exception as exc:
            self.logger.error(f"[MapDisplays] Command '{command.name}' error: {exc}")
            sender.send_message(f"§c[MapDisplays] Error: {exc}")
        return True

    def _cmd_getdisplay(self, player: Player, args: list[str]) -> bool:
        if len(args) < 2:
            player.send_message("§cUsage: /getdisplay <cols> <rows>")
            return True
        try:
            cols, rows = int(args[0]), int(args[1])
        except ValueError:
            player.send_message("§cCols and rows must be integers.")
            return True
        if not (1 <= cols <= 8 and 1 <= rows <= 8):
            player.send_message("§cCols and rows must each be between 1 and 8.")
            return True

        display_id = self._next_id
        self._next_id += 1
        display = MapDisplay(self, display_id, cols, rows, creator_name=player.name)
        self.displays[display_id] = display
        display.give_maps_to(player)
        self._save_persistence()

        player.send_message(
            f"§aDisplay §f#{display_id} §acreated (§f{cols}§a×§f{rows}§a). "
            f"§7You received §f{cols * rows} §7map item(s). "
            f"Place them on item frames in a §f{cols} wide §7× §f{rows} tall §7grid."
        )
        return True

    def _cmd_setdisplay(self, player: Player, args: list[str]) -> bool:
        if len(args) < 2:
            player.send_message("§cUsage: /setdisplay <id> video <file> | image <file> | idle")
            return True
        try:
            display_id = int(args[0])
        except ValueError:
            player.send_message("§cDisplay ID must be an integer.")
            return True

        display = self.displays.get(display_id)
        if display is None:
            player.send_message(f"§cNo display with ID {display_id}. Use /listdisplays.")
            return True

        mode = args[1].lower()

        if mode == "idle":
            display.set_state(
                IdleState(display.width, display.height, self.logger, Path(self.data_folder)),
                "idle",
            )
            self._save_persistence()
            player.send_message(f"§aDisplay §f#{display_id} §areset to idle.")

        elif mode == "video":
            if len(args) < 3:
                player.send_message("§cUsage: /setdisplay <id> video <filename>")
                return True
            filename = args[2]
            video_path = Path(self.data_folder) / "videos" / filename
            if not video_path.exists():
                player.send_message(
                    f"§cFile not found: §fvideos/{filename}\n"
                    f"§7Upload the file to §fplugins/mapdisplays/videos/ §7then retry."
                )
                return True

            player.send_message(f"§7Loading '§f{filename}§7'…")

            def _load_video():
                vs = VideoFileState(
                    display.width, display.height, self.logger, video_path
                )
                display.set_state(vs, "video", filename)
                self._save_persistence()

                def _notify():
                    player.send_message(
                        f"§aDisplay §f#{display_id} §anow playing: §f{filename}"
                    )
                self.server.scheduler.run_task(self, _notify)

            threading.Thread(
                target=_load_video,
                daemon=True,
                name=f"mapdisplay-load-{filename}",
            ).start()

        elif mode == "image":
            if len(args) < 3:
                player.send_message("§cUsage: /setdisplay <id> image <filename>")
                return True
            filename = args[2]
            image_path = Path(self.data_folder) / "images" / filename
            if not image_path.exists():
                player.send_message(
                    f"§cFile not found: §fimages/{filename}\n"
                    f"§7Upload the file to §fplugins/mapdisplays/images/ §7then retry."
                )
                return True
            display.set_state(
                ImageState(display.width, display.height, self.logger, image_path),
                "image",
                filename,
            )
            self._save_persistence()
            player.send_message(
                f"§aDisplay §f#{display_id} §anow showing image: §f{filename}"
            )
        elif mode == "stream":
            if len(args) < 3:
                player.send_message(
                    "§cUsage: /setdisplay <id> stream <url or YouTube video ID>\n"
                    "§7Example (video ID): §f/setdisplay 0 stream dQw4w9WgXcQ\n"
                    "§7Example (full URL): §f/setdisplay 0 stream https://youtu.be/dQw4w9WgXcQ"
                )
                return True

            # Bedrock's command parser can break URLs at ?, =, & — join all
            # remaining args back together (no spaces since URLs have none).
            raw = "".join(args[2:])

            # Expand bare YouTube video IDs (11 alphanumeric chars)
            import re as _re
            if _re.fullmatch(r"[A-Za-z0-9_\-]{11}", raw):
                url = f"https://www.youtube.com/watch?v={raw}"
                player.send_message(f"§7Expanding video ID to: §f{url}")
            # Expand youtu.be/<ID> shortlinks (may lose the slash)
            elif _re.match(r"youtu\.be/[A-Za-z0-9_\-]{11}", raw):
                vid = raw.split("/")[-1][:11]
                url = f"https://www.youtube.com/watch?v={vid}"
                player.send_message(f"§7Expanding shortlink to: §f{url}")
            elif raw.startswith("http://") or raw.startswith("https://") or raw.startswith("rtmp://"):
                url = raw
            else:
                player.send_message(
                    "§cCould not parse stream URL.\n"
                    "§7Use a YouTube video ID (e.g. §fdQw4w9WgXcQ§7) or a full URL starting with §fhttps://"
                )
                return True

            ss = StreamState(
                display.width, display.height, self.logger,
                url, Path(self.data_folder)
            )
            display.set_state(ss, "stream", url)
            self._save_persistence()
            player.send_message(
                f"§aDisplay §f#{display_id} §anow streaming.\n"
                f"§7§o(Resolving via yt-dlp — idle shown until ready.)"
            )
        else:
            player.send_message(
                "§cUnknown mode. Valid options: §fvideo§c, §fimage§c, §fstream§c, §fidle§c."
            )

        return True

    def _cmd_listvideos(self, player: Player) -> bool:
        video_dir = Path(self.data_folder) / "videos"
        files = sorted(video_dir.iterdir()) if video_dir.exists() else []
        valid = [f for f in files if f.suffix.lower() in _VALID_VIDEO_EXTS]
        images = sorted((Path(self.data_folder) / "images").iterdir()) if (Path(self.data_folder) / "images").exists() else []
        valid_img = [f for f in images if f.suffix.lower() in _VALID_IMAGE_EXTS]

        if not valid and not valid_img:
            player.send_message(
                "§7No media files found. Upload to:\n"
                "  §fplugins/mapdisplays/videos/\n"
                "  §fplugins/mapdisplays/images/"
            )
        else:
            if valid:
                player.send_message(f"§a§l{len(valid)}§r §avideos:")
                for f in valid:
                    player.send_message(f"  §f{f.name}")
            if valid_img:
                player.send_message(f"§a§l{len(valid_img)}§r §aimages:")
                for f in valid_img:
                    player.send_message(f"  §f{f.name}")
        return True

    def _cmd_stopdisplay(self, player: Player, args: list[str]) -> bool:
        if not args:
            player.send_message("§cUsage: /stopdisplay <id>")
            return True
        try:
            display_id = int(args[0])
        except ValueError:
            player.send_message("§cDisplay ID must be an integer.")
            return True

        display = self.displays.get(display_id)
        if display is None:
            player.send_message(f"§cNo display with ID {display_id}.")
            return True


        display.set_state(
            IdleState(display.width, display.height, self.logger, Path(self.data_folder)),
            "idle",
        )
        self._save_persistence()
        player.send_message(f"§aDisplay §f#{display_id} §astopped.")
        return True

    def _cmd_getmaps(self, player: Player) -> bool:
        if not self.displays:
            player.send_message("§7No active displays. Use /getdisplay to create one.")
            return True
        for display in self.displays.values():
            display.give_maps_to(player)
        player.send_message(
            f"§aGiven map items for §f{len(self.displays)} §adisplay(s). "
            f"§7Place them in item frames to restore your boards."
        )
        return True

    def _cmd_listdisplays(self, player: Player) -> bool:
        if not self.displays:
            player.send_message("§7No active displays.")
            return True
        player.send_message(f"§a§l{len(self.displays)}§r §aactive display(s):")
        for d in self.displays.values():
            state_desc = d._state_name
            if d._state_arg:
                state_desc += f": §f{d._state_arg}"
            player.send_message(
                f"  §f#{d.display_id} §7({d.cols}×{d.rows}) — {state_desc}"
            )
        return True

    def _cmd_removedisplay(self, player: Player, args: list[str]) -> bool:
        if not args:
            player.send_message("§cUsage: /removedisplay <id>")
            return True
        try:
            display_id = int(args[0])
        except ValueError:
            player.send_message("§cDisplay ID must be an integer.")
            return True

        display = self.displays.get(display_id)
        if display is None:
            player.send_message(f"§cNo display with ID {display_id}.")
            return True

        # Stop the active state (kills the decode thread)
        try:
            display.state.stop()
        except Exception:
            pass

        # Remove from active displays and persist
        del self.displays[display_id]
        self._save_persistence()

        player.send_message(
            f"§aDisplay §f#{display_id} §ahas been removed. "
            f"§7The map item frames can now be broken safely."
        )
        return True

    def _cmd_removevideo(self, player: Player, args: list[str]) -> bool:
        if not args:
            player.send_message("§cUsage: /removevideo <filename>")
            return True
        filename = args[0]
        video_path = Path(self.data_folder) / "videos" / filename
        stem = Path(filename).stem

        if not video_path.exists():
            player.send_message(f"§cVideo not found: §f{filename}")
            return True

        # Stop any displays currently showing this video first
        for display in self.displays.values():
            if display._state_name == "video" and display._state_arg == filename:
                display.set_state(
                    IdleState(display.width, display.height, self.logger, Path(self.data_folder)),
                    "idle",
                )
                player.send_message(
                    f"§7Display §f#{display.display_id} §7reset to idle (was showing this video)."
                )

        # Delete the video file
        try:
            video_path.unlink()
            player.send_message(f"§7Deleted video: §f{filename}")
        except Exception as exc:
            player.send_message(f"§cFailed to delete video file: {exc}")
            return True

        self._save_persistence()
        return True

    def _cmd_reloadconfig(self, player: Player) -> bool:
        self._load_config()
        player.send_message("§aConfig reloaded.")
        return True
