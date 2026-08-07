import json
import os

class DisplayConfig:
    def __init__(self, name: str, cols: int, rows: int, source: str = "", map_ids: list[list[int]] = None, broadcast: bool = False):
        self.name = name
        self.cols = cols
        self.rows = rows
        self.source = source
        self.map_ids = map_ids if map_ids is not None else []
        self.broadcast = broadcast  # If True, maps are pushed to all players on join

    def to_dict(self):
        return {
            "cols": self.cols,
            "rows": self.rows,
            "source": self.source,
            "map_ids": self.map_ids,
            "broadcast": self.broadcast
        }

    @classmethod
    def from_dict(cls, name: str, data: dict):
        return cls(
            name=name,
            cols=data.get("cols", 1),
            rows=data.get("rows", 1),
            source=data.get("source", ""),
            map_ids=data.get("map_ids", []),
            broadcast=data.get("broadcast", False)
        )

class ConfigManager:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.file_path = os.path.join(data_dir, "displays.json")

    def load_displays(self) -> dict[str, DisplayConfig]:
        if not os.path.exists(self.file_path):
            return {}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {k: DisplayConfig.from_dict(k, v) for k, v in data.items()}
        except Exception as e:
            print(f"Failed to load displays.json: {e}")
            return {}

    def save_displays(self, displays: dict[str, DisplayConfig]):
        os.makedirs(self.data_dir, exist_ok=True)
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump({k: v.to_dict() for k, v in displays.items()}, f, indent=4)
        except Exception as e:
            print(f"Failed to save displays.json: {e}")
