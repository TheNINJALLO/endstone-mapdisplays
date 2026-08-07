import os
import io
import numpy as np
import requests
from PIL import Image, ImageFilter, ImageEnhance

def load_image(source: str, cols: int, rows: int, plugin_data_dir: str) -> np.ndarray:
    """
    Loads an image from a URL or a local file relative to plugin_data_dir.
    Resizes to (cols * 128) x (rows * 128) and returns an RGB numpy array.
    """
    target_w = cols * 128
    target_h = rows * 128

    img = None
    if source.startswith("http://") or source.startswith("https://"):
        try:
            response = requests.get(source, timeout=10)
            response.raise_for_status()
            img = Image.open(io.BytesIO(response.content)).convert("RGB")
        except Exception as e:
            raise RuntimeError(f"Failed to fetch image from URL: {e}")
    else:
        local_path = os.path.join(plugin_data_dir, source)
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"Image not found at path: {local_path}")
        img = Image.open(local_path).convert("RGB")

    if img is None:
        raise ValueError(f"Could not decode image from source: {source}")

    # Resize to exactly fit the full map grid using high-quality Lanczos resampling
    resized = img.resize((target_w, target_h), Image.LANCZOS)

    # Sharpen to recover edge detail lost during downscaling — maps are only 128px
    # per tile so sharpness matters a lot for readability
    resized = resized.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=3))

    # Small contrast boost to help colors pop against Minecraft's limited map palette
    resized = ImageEnhance.Contrast(resized).enhance(1.15)

    return np.array(resized, dtype=np.uint8)

def slice_image(full_image: np.ndarray, cols: int, rows: int) -> list[list[np.ndarray]]:
    """
    Slices the full map grid image into 128x128 blocks.
    Returns a 2D list: grid[row][col].
    """
    grid = []
    for r in range(rows):
        row_list = []
        for c in range(cols):
            sub_frame = full_image[r*128:(r+1)*128, c*128:(c+1)*128]
            row_list.append(sub_frame)
        grid.append(row_list)
    return grid
