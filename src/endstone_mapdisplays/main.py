from endstone.plugin import Plugin
from endstone import Player
from endstone.event import event_handler, PlayerJoinEvent
from endstone.inventory import ItemStack, MapMeta
import os
import threading

from endstone_mapdisplays.config_manager import ConfigManager, DisplayConfig
from endstone_mapdisplays.renderer import StaticMapRenderer
from endstone_mapdisplays.image_loader import load_image, slice_image

class EntryForPlugin(Plugin):
    api_version = "0.11"

    commands = {
        "display": {
            "description": "manage static map displays and announcements",
            "usages": [
                "/display create <name: str> <cols: int> <rows: int> [broadcast: bool]",
                "/display set <name: str> <source: str>",
                "/display give <name: str> [start: int] [player: player]",
                "/display broadcast <name: str>",
                "/display delete <name: str>",
                "/display list"
            ],
            "permissions": ["mapdisplay.command.manage"],
        }
    }

    def _get_data_dir(self):
        return os.path.join(self.data_folder, "data")

    def on_enable(self) -> None:
        if not os.path.exists(self.data_folder):
            os.makedirs(self.data_folder)

        self.config_manager = ConfigManager(self._get_data_dir())
        self.displays = self.config_manager.load_displays()

        self.logger.info(f"Loaded {len(self.displays)} static map displays.")
        self.register_events(self)

        # Re-apply all saved images after startup
        self.server.scheduler.run_task(self, self._load_all_display_images)

    def on_disable(self) -> None:
        self.config_manager.save_displays(self.displays)
        self.logger.info("Saved static map displays.")

    def _load_all_display_images(self):
        for name, config in self.displays.items():
            if config.source:
                self._apply_image_to_display(name, config.source, save=False)

    def _push_all_maps_to_player(self, player: Player, broadcast_only: bool = False):
        """Send display maps to a specific player.
        If broadcast_only=True, only sends displays marked as broadcast (e.g. announcements).
        """
        for config in self.displays.values():
            if broadcast_only and not config.broadcast:
                continue
            for r in range(config.rows):
                for c in range(config.cols):
                    if r < len(config.map_ids) and c < len(config.map_ids[r]):
                        view = self.server.get_map(config.map_ids[r][c])
                        if view:
                            player.send_map(view)

    def _push_all_maps_to_all_players(self, broadcast_only: bool = False):
        """Broadcast display maps to all online players."""
        for player in self.server.online_players:
            self._push_all_maps_to_player(player, broadcast_only=broadcast_only)

    def _push_display_maps_to_player(self, player: Player, config: DisplayConfig):
        """Send specific display maps to a player."""
        for r in range(config.rows):
            for c in range(config.cols):
                if r < len(config.map_ids) and c < len(config.map_ids[r]):
                    view = self.server.get_map(config.map_ids[r][c])
                    if view:
                        player.send_map(view)

    def _push_display_maps_to_all_players(self, config: DisplayConfig):
        """Broadcast specific display maps to all online players."""
        for player in self.server.online_players:
            self._push_display_maps_to_player(player, config)

    @event_handler
    def on_player_join(self, event: PlayerJoinEvent):
        """Push only broadcast displays (e.g. announcements) to players on join."""
        # Capture the name string only — the PlayerJoinEvent object (and its player
        # reference) is freed after this handler returns, so capturing event.player
        # directly in a delayed lambda causes a SIGSEGV when the task fires.
        player_name = event.player.name
        def push():
            player = self.server.get_player(player_name)
            if player:
                self._push_all_maps_to_player(player, broadcast_only=True)
        self.server.scheduler.run_task(self, push, delay=40)

    def _apply_image_to_display(self, name: str, source: str, save: bool = True):
        config = self.displays.get(name)
        if not config:
            return

        def loading_task():
            try:
                full_img = load_image(source, config.cols, config.rows, self.data_folder)
                grid_images = slice_image(full_img, config.cols, config.rows)

                def apply_renderers():
                    for r in range(config.rows):
                        for c in range(config.cols):
                            if r < len(config.map_ids) and c < len(config.map_ids[r]):
                                map_id = config.map_ids[r][c]
                                map_view = self.server.get_map(map_id)
                                if map_view:
                                    renderer = StaticMapRenderer(grid_images[r][c], self)
                                    for old_renderer in list(map_view.renderers):
                                        map_view.remove_renderer(old_renderer)
                                    map_view.add_renderer(renderer)

                    # Push updated maps of this display to all online players so they see it immediately
                    self._push_display_maps_to_all_players(config)

                    self.logger.info(f"Successfully applied image '{source}' to display '{name}'")
                    config.source = source
                    if save:
                        self.config_manager.save_displays(self.displays)

                self.server.scheduler.run_task(self, apply_renderers)
            except Exception as e:
                self.logger.error(f"Failed to load image for display '{name}': {e}")

        threading.Thread(target=loading_task, daemon=True).start()

    def on_command(self, sender, command, args: list[str]) -> bool:
        if command.name != "display":
            return False

        if not args:
            sender.send_message("Usage: /display <create|set|give>")
            return False

        subcmd = args[0].lower()
        if subcmd == "create":
            if not isinstance(sender, Player):
                sender.send_message("Only players can create displays to receive the maps.")
                return True
            if len(args) < 4:
                sender.send_message("Usage: /display create <name> <cols> <rows>")
                return True

            name = args[1]
            try:
                cols = int(args[2])
                rows = int(args[3])
            except ValueError:
                sender.send_message("Cols and rows must be integers.")
                return True

            # Optional 5th arg: broadcast=true makes this an announcement display
            broadcast = len(args) > 4 and args[4].lower() in ("true", "yes", "1")

            if name in self.displays:
                sender.send_message(f"Display '{name}' already exists.")
                return True

            map_ids = []
            for r in range(rows):
                row_ids = []
                for c in range(cols):
                    view = self.server.create_map(self.server.level.get_dimension("Overworld"))
                    row_ids.append(view.id)
                map_ids.append(row_ids)

            config = DisplayConfig(name, cols, rows, "", map_ids, broadcast=broadcast)
            self.displays[name] = config
            self.config_manager.save_displays(self.displays)

            if broadcast:
                # Announcement display — give a copy to every online player
                for player in self.server.online_players:
                    given, skipped, _ = self._give_display_maps(config, player)
                    if skipped > 0:
                        player.send_message(f"[MapDisplays] Inventory full! {skipped} maps skipped. Free space and run: /display give {name}")
                sender.send_message(f"Created announcement display '{name}' ({cols}x{rows}) and distributed maps to all online players.")
            else:
                given, skipped, next_start = self._give_display_maps(config, sender)
                total = cols * rows
                if skipped > 0:
                    sender.send_message(f"Created '{name}' ({cols}x{rows}). Gave {given}/{total} maps. Inventory full! Run: /display give {name} {next_start} for the rest.")
                else:
                    sender.send_message(f"Created display '{name}' ({cols}x{rows}). All {total} maps added to your inventory.")
            return True

        elif subcmd == "set":
            if len(args) < 3:
                sender.send_message("Usage: /display set <name> <image_url_or_file>")
                return True

            name = args[1]
            source = args[2]
            if name not in self.displays:
                sender.send_message(f"Display '{name}' does not exist.")
                return True

            sender.send_message(f"Loading image for '{name}'...")
            self._apply_image_to_display(name, source)
            return True

        elif subcmd == "give":
            if len(args) < 2:
                sender.send_message("Usage: /display give <name> [start: int] [player]")
                return True

            name = args[1]
            if name not in self.displays:
                sender.send_message(f"Display '{name}' does not exist.")
                return True

            config = self.displays[name]
            total = config.cols * config.rows
            target = sender
            start = 0

            # Parse optional args: start index (int) and/or player name (str)
            remaining = args[2:]
            if remaining and remaining[0].isdigit():
                start = int(remaining[0])
                remaining = remaining[1:]
            if remaining:
                target_player = self.server.get_player(remaining[0])
                if not target_player:
                    sender.send_message(f"Player '{remaining[0]}' not found.")
                    return True
                target = target_player

            if not isinstance(target, Player):
                sender.send_message("Target must be a player.")
                return True

            given, skipped, next_start = self._give_display_maps(config, target, start=start)
            if skipped > 0:
                sender.send_message(f"Gave maps {start+1}-{start+given} of {total} to {target.name}. Run: /display give {name} {next_start} for the next batch.")
            elif given == 0:
                sender.send_message(f"No maps to give from index {start} — display only has {total} maps total.")
            else:
                sender.send_message(f"Gave maps {start+1}-{start+given} of {total} to {target.name}. All done!")
            return True

        elif subcmd == "broadcast":
            if len(args) < 2:
                sender.send_message("Usage: /display broadcast <name>")
                return True

            name = args[1]
            if name not in self.displays:
                sender.send_message(f"Display '{name}' does not exist.")
                return True

            config = self.displays[name]
            total = config.cols * config.rows
            full_count = 0
            for player in self.server.online_players:
                given, skipped, _ = self._give_display_maps(config, player)
                if skipped > 0:
                    full_count += 1
                    player.send_message(f"[MapDisplays] Your inventory is full! {skipped}/{total} maps could not be given. Free space and run: /display give {name}")
            online = len(list(self.server.online_players))
            if full_count > 0:
                sender.send_message(f"Broadcast '{name}' to {online} player(s). {full_count} had full inventories and were notified.")
            else:
                sender.send_message(f"Sent all {total} maps for '{name}' to {online} online player(s).")
            return True

        elif subcmd == "delete":
            if len(args) < 2:
                sender.send_message("Usage: /display delete <name>")
                return True

            name = args[1]
            if name not in self.displays:
                sender.send_message(f"Display '{name}' does not exist.")
                return True

            del self.displays[name]
            self.config_manager.save_displays(self.displays)
            sender.send_message(f"Deleted display '{name}'. Maps already in players' inventories will remain but will no longer update.")
            return True

        elif subcmd == "list":
            if not self.displays:
                sender.send_message("No displays created yet.")
                return True
            lines = ["--- Displays ---"]
            for name, cfg in self.displays.items():
                tag = " [announcement]" if cfg.broadcast else ""
                src = cfg.source if cfg.source else "(no image set)"
                lines.append(f"  {name}{tag} - {cfg.cols}x{cfg.rows} - {src}")
            sender.send_message("\n".join(lines))
            return True

        else:
            sender.send_message(f"Unknown subcommand '{subcmd}'. Use: create, set, give, broadcast, delete, list")
            return True

    def _give_display_maps(self, config: DisplayConfig, player: Player, start: int = 0) -> tuple[int, int, int]:
        """
        Gives map items to the player starting from a flat index offset.
        Returns (given, skipped, next_start) where next_start is the index to use for the next batch.
        """
        contents = list(player.inventory.contents)
        free_slots = sum(1 for slot in contents if slot is None)

        given = 0
        skipped = 0
        flat_index = 0
        next_start = start

        for r in range(config.rows):
            for c in range(config.cols):
                if r < len(config.map_ids) and c < len(config.map_ids[r]):
                    if flat_index < start:
                        flat_index += 1
                        continue
                    if free_slots <= 0:
                        skipped += 1
                        flat_index += 1
                        continue
                    map_id = config.map_ids[r][c]
                    view = self.server.get_map(map_id)
                    if view:
                        item = ItemStack("minecraft:filled_map")
                        meta = item.item_meta
                        if isinstance(meta, MapMeta):
                            meta.map_view = view
                            item.set_item_meta(meta)
                        player.inventory.add_item(item)
                        free_slots -= 1
                        given += 1
                        next_start = flat_index + 1
                    flat_index += 1

        return given, skipped, next_start