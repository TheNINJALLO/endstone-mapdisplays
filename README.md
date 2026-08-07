# MapDisplays — Announcements Edition

An Endstone plugin that renders static images across one or more item-frame maps.
Use it for server announcements, spawn boards, montly banners — anything you can push as an image.

---

## Commands

All management is done through the `/display` command.

### `/display create <name> <cols> <rows>`
Creates a new named display board and gives you the physical map items.
- `name` — a unique identifier for this display (e.g. `announcements`, `spawnboard`)
- `cols` — number of maps wide
- `rows` — number of maps tall

Place the maps into item frames on your wall from **top-left → top-right, then next row**.

**Example:** `/display create spawnboard 3 2` → gives you 6 maps for a 3-wide × 2-tall grid.

---

### `/display set <name> <source>`
Loads an image onto an existing display. Works in two ways:

#### From a URL
```
/display set announcements https://i.imgur.com/abc123.png
```
Any direct image link works (Imgur, Discord CDN, your own web host, etc.)

#### From a local file
```
/display set announcements announcement_april.png
```
Upload the file to your server at:
```
/home/container/plugins/endstone-mapdisplays/
```
Then use just the filename as the `source`.

---

### `/display give <name> [player]`
Gives the map items for an existing display to yourself or another player.
Use this to hand out the `announcements` map to new players, or replace lost maps.

**Examples:**
```
/display give announcements
/display give announcements Steve
```

---

## Folder Structure (on the server)

```
plugins/endstone-mapdisplays/
├── announcement_april.png   ← upload your local images here
├── spawn_banner.jpg
└── data/
    └── displays.json        ← auto-generated, do not edit manually
```

---

## Typical Workflows

### Monthly Announcement Map
1. `/display create announcements 1 1`
2. Hand the map to players (or distribute via kit plugin)
3. Each month, upload your new image to the plugin folder and run:
   `/display set announcements april_news.png`
   — or paste a direct URL instead —
   `/display set announcements https://yourhost.com/april.png`

### Large Spawn Board (e.g. 3×2 item frames)
1. Build your item frame grid on the wall
2. `/display create spawnboard 3 2` — you get 6 maps
3. Place maps into frames top-left → bottom-right
4. `/display set spawnboard https://link.to/banner.png`

---

## Notes
- Images are automatically scaled to fit the display dimensions
- Displays persist across server restarts via `displays.json`
- The image is re-applied on every server startup from the last `source`
- Both `.png` and `.jpg` formats are supported