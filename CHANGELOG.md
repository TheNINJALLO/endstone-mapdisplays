# Changelog

All notable changes to `endstone-mapdisplays` will be documented here.

---

## [0.2.2] — 2026-04-03

### Fixed

#### Event Handler Registration Error
- Removed `from __future__ import annotations` from the top of `main.py`.
  This import makes **all** type annotations lazy strings at runtime (PEP 563),
  which broke Endstone's `register_events()` introspection — it saw
  `'PlayerJoinEvent'` (a string) instead of the actual class and refused to
  register the handler, logging:
  > `Plugin attempted to register an invalid event handler signature: on_player_join`
  Python 3.11 supports `X | None` union syntax natively so the import is not needed.

### Performance

Three root causes of server lag when playing videos were fixed:

#### 1. Batched `send_map` calls (largest impact)
- **Before:** One `scheduler.run_task` call was scheduled **per tile per frame**.
  A 2×2 display at 20 fps = **80 scheduler interrupts/sec** on the main thread.
- **After:** All updated tile views are collected, then a **single** `run_task` fires
  per display per frame sending every tile at once. At 10 fps = **10 calls/sec** (8× less).
- Also returns immediately from `update()` if no tile frames have changed (e.g. static images
  no longer generate any scheduler calls at all between state changes).

#### 2. Video decode capped to 10 fps
- `VideoFileState` and `IdleState` previously decoded at the video's **native fps** (e.g. 24–30 fps).
  128×128 px map tiles have no meaningful quality gain above 10 fps.
- Both now cap to `_MAX_FPS = 10.0` and use **frame-skip** (`i % skip`) so PyAV still
  reads the file sequentially but only processes and stores every Nth frame.
  Resize and lock overhead reduced by ~2–3×.

#### 3. Update loop driven by `display_fps` config
- The `_update_loop` coroutine was hardcoded to `sleep(0.05)` (20 fps) even though
  `config.json` already had a `display_fps` field — it was completely ignored.
- Now reads `display_fps` from config on every iteration (clamped 1–20 fps).
- Default `display_fps` lowered from `20` to `10` in `_DEFAULT_CONFIG`.
- Tunable live: edit `plugins/mapdisplays/config.json` and run `/reloadconfig`.

---

## [0.2.1] — 2026-04-03

### Added

#### Stream Support (YouTube / Twitch / HTTP / RTMP)
- New command: `/setdisplay <id> stream <url>` — stream any URL supported by yt-dlp
  (YouTube, Twitch, direct HLS, plain HTTP video streams, RTMP, etc.)
- `StreamState` class handles URL resolution via `yt-dlp`, then decodes with `av` (PyAV).
  Falls back to direct `av.open(url)` if yt-dlp is unavailable or the URL is a plain stream.
- Shows idle animation while the stream URL is being resolved in a background thread.
- Stream state is persisted in `displays.json` and fully restored on server restart.
- When a stream ends naturally, the state auto-reconnects (useful for live streams).
- `yt-dlp` added back to dependencies (was removed in v0.2.0).

### Changed
- `/setdisplay` usages updated to include the new `stream` mode.
- `_cmd_setdisplay` error message updated to list all four valid modes.

### Notes
- **Sound is intentionally disabled for streams.** Reliable audio sync is not achievable
  over a network stream with the Bedrock sound event system.
- The `cookiesfrombrowser` option from the original `YoutubeState` (v0.1.0) is **not**
  restored. It fails on headless servers. Public YouTube videos work without it.
  Age-restricted or private content will fail gracefully with a console warning.
- Prefer low-resolution formats (`height<=144`, `height<=240`) to minimize server CPU load
  during stream decoding.

---

## [0.2.0] — 2026-04-03

### Summary
Complete rewrite of the plugin. Replaces the YouTube + OpenCV + yt-dlp pipeline with a
purely server-local, file-based architecture. Adds a full auto-generated sound system,
six new commands, display persistence across restarts, and closes all known stability issues
from v0.1.0.

---

### Breaking Changes

- **YouTube playback removed.** The `/getdisplay <cols> <rows> [youtube_url]` third argument
  is no longer accepted. Video files must be placed locally in `plugins/mapdisplays/videos/`.
- **`opencv-python` and `yt-dlp` dependencies dropped.** These do not need to be installed on
  the server anymore.

---

### Added

#### Commands
| Command | Description |
|---|---|
| `/setdisplay <id> video <file>` | Play a local video file from `plugins/mapdisplays/videos/` |
| `/setdisplay <id> image <file>` | Display a static image from `plugins/mapdisplays/images/` |
| `/setdisplay <id> idle` | Reset a display to the idle animation |
| `/listvideos` | List all available video and image files, including audio extraction status |
| `/stopdisplay <id>` | Stop a display and reset it to idle |
| `/getmaps` | Re-receive map items for all active displays (use after a server restart) |
| `/listdisplays` | List all active displays and their current state |

#### Sound System (auto-generated resource pack)
- On first playback of a video, the plugin uses **FFmpeg** (if available on `PATH`) to extract
  the audio track and convert it to OGG Vorbis format.
- The OGG file is saved to `plugins/mapdisplays/resourcepack/sounds/mapdisplays/<name>.ogg`.
- A Bedrock **resource pack** (`plugins/mapdisplays/resourcepack/`) is auto-generated containing
  `manifest.json` and `sounds.json`, ready to distribute to clients.
- The plugin calls `player.play_sound("mapdisplays.<name>")` to synchronize audio with video.
- Sound loops restart automatically (±2 ticks) when the video loops.
- Players who join mid-playback automatically receive the current frame and active sound.
- If FFmpeg is not found, video still plays silently with a clear console warning.

#### Display Persistence
- All display configurations are saved to `plugins/mapdisplays/displays.json` on disable.
- On server restart / reload, displays are fully restored with the correct video/image state.
- Map item IDs change on restore (Bedrock limitation) — use `/getmaps` to re-receive items.

#### New Display State: `ImageState`
- Play a static PNG, JPG, WEBP, or GIF (first frame) on any display.
- Uses Pillow (LANCZOS) for high-quality scaling.

#### Data Folder Architecture
All runtime content now lives in `plugins/mapdisplays/`:
```
plugins/mapdisplays/
  idle.webm              ← default idle animation (user-replaceable without rebuilding)
  videos/                ← upload MP4 / WEBM / MKV / MOV / AVI here
  images/                ← upload PNG / JPG / WEBP here
  displays.json          ← auto-managed persistence file
  resourcepack/          ← auto-generated Bedrock resource pack
    manifest.json
    sounds.json
    sounds/mapdisplays/
      <video_stem>.ogg
```

- `idle.webm` is automatically copied from the wheel on first start so it can be replaced
  without rebuilding the package.

---

### Changed

- **Architecture**: `DisplayState` is now a proper ABC with `get_full_frame()`, `stop()`,
  `duration`, and `sound_name` properties.
- **`CabinetMapRenderer.push()`**: Returns `True` only when a frame is genuinely new,
  preventing redundant `send_map` calls when content is static.
- **`MapDisplay.update()`**: Pre-builds tile grid once in `__init__` (no per-frame allocation).
  Uses default-parameter capture in the `send_map` task lambda to correctly freeze each view
  reference (fixes closure bug from v0.1.0).
- **Thread-safe state swap**: `MapDisplay.set_state()` acquires a lock during the swap and
  stops the previous state outside the lock to prevent deadlocks.
- **Plugin class attributes**: Added `name = "mapdisplays"` and `api_version = "0.5"` as
  required class-level attributes.
- **`register_events(self)`** now called correctly in `on_enable()` so `PlayerJoinEvent`
  actually fires.
- **`pyproject.toml`**: Replaced `opencv-python` and `yt-dlp` with `Pillow`. Version bumped
  to `0.2.0`.

---

### Fixed

- **Wrong import**: `from numpy.ma import isin` removed (was imported but never used; caused
  a potential `ImportError` on some NumPy builds).
- **Lambda closure bug**: Loop variables `r` and `c` were captured by reference in the
  `send_map` task lambda, causing incorrect tiles to be updated. Fixed with default-parameter
  capture.
- **No class-level plugin metadata**: Missing `name` and `api_version` could prevent proper
  plugin registration on some Endstone versions.
- **`register_events` never called**: `PlayerJoinEvent` handler was silently never registered
  in v0.1.0.
- **Decode thread error recovery**: All decode loops now catch exceptions and retry with
  exponential back-off (1s → 2s → 4s … → 30s cap) instead of silently dying and leaving a
  frozen frame.
- **Non-main-thread `send_map`**: Map packets are now always dispatched via the Endstone
  scheduler (`run_task`) onto the main thread, eliminating the race condition from v0.1.0.
- **`yt-dlp` cookies-from-browser**: The `cookiesfrombrowser=("firefox",)` option always
  failed on headless servers. This entire code path is removed.

---

### Dependencies

| Package | v0.1.0 | v0.2.0 |
|---|---|---|
| `numpy` | ✅ | ✅ |
| `av` (PyAV) | ✅ | ✅ |
| `Pillow` | ❌ | ✅ **new** |
| `opencv-python` | ✅ | ❌ **removed** |
| `yt-dlp` | ✅ | ❌ **removed** |

**Runtime optional**: `ffmpeg` on system `PATH` — required only for audio extraction.

---

## [0.1.0] — Initial Release

- YouTube video streaming via `yt-dlp` + `PyAV`
- Multi-tile map display grid
- `IdleState` with looping webm animation
- `/getdisplay <cols> <rows> [youtube_url]` command
