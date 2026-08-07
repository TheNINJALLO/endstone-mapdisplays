import numpy as np
from endstone import Player
from endstone.map import MapRenderer, MapCanvas, MapView
from typing import cast, Any

class StaticMapRenderer(MapRenderer):
    """
    A renderer that displays a static numpy array image on a map.
    """
    def __init__(self, image_buffer: np.ndarray, plugin) -> None:
        super().__init__(is_contextual=False)
        self.plugin = plugin
        self.buffer = np.zeros((128, 128, 4), dtype=np.uint8)
        self.drawn = False
        self.set_image(image_buffer)

    def set_image(self, array: np.ndarray):
        """
        Updates the internal buffer with a new image array.
        Array should be 128x128x3 (RGB) or 128x128x4 (RGBA).
        """
        if array is None or list(array.shape[:2]) != [128, 128]:
            # fallback to empty if bad shape
            self.buffer.fill(0)
            self.drawn = False
            return

        if array.shape[2] == 3:
            rgba = np.empty((128, 128, 4), dtype=np.uint8)
            rgba[:, :, :3] = array
            rgba[:, :, 3] = 255
            np.copyto(self.buffer, rgba)
        else:
            np.copyto(self.buffer, array)
        self.drawn = False

    def render(self, view: MapView, canvas: MapCanvas, player: Player) -> None:
        """
        Draws the static buffer to the map for the player.
        """
        if not self.drawn:
            canvas.draw_image(0, 0, cast(Any, self.buffer))
            self.drawn = True

            # Safely unregister this renderer on the next server tick so the main loop stops ticking it.
            # The canvas pixels will remain cached on the client and server MapView.
            def remove():
                view.remove_renderer(self)
            self.plugin.server.scheduler.run_task(self.plugin, remove)
