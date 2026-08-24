"""
Barcode recognition helper using zxing-cpp and Pillow.
Attempts to auto-detect barcodes from store crew photos with multi-orientation and image enhancement fallbacks.
"""

import os
from typing import Optional, List
from PIL import Image, ImageEnhance, ImageOps
import zxingcpp


def detect_barcode_from_image(image_path: str) -> Optional[str]:
    """
    Attempts to read 1D or 2D barcodes from an image file.
    Tries:
    1. Direct read on the original image.
    2. Rotation at 90, 180, 270 degrees.
    3. Grayscale and high-contrast enhancement.
    Returns the decoded barcode string, or None if not found/unreadable.
    """
    if not os.path.exists(image_path):
        return None

    try:
        with Image.open(image_path) as img:
            # 1. Direct pass
            results = zxingcpp.read_barcodes(img)
            if results:
                for r in results:
                    if r.text and r.text.strip():
                        return r.text.strip()

            # 2. Try with standard EXIF auto-rotation & grayscale
            img_normalized = ImageOps.exif_transpose(img)
            if img_normalized is None:
                img_normalized = img

            gray = img_normalized.convert("L")
            results = zxingcpp.read_barcodes(gray)
            if results:
                for r in results:
                    if r.text and r.text.strip():
                        return r.text.strip()

            # 3. Contrast enhancement
            enhancer = ImageEnhance.Contrast(gray)
            enhanced = enhancer.enhance(2.0)
            results = zxingcpp.read_barcodes(enhanced)
            if results:
                for r in results:
                    if r.text and r.text.strip():
                        return r.text.strip()

            # 4. Rotations (90, 180, 270)
            for angle in [90, 180, 270]:
                rotated = img_normalized.rotate(angle, expand=True)
                results = zxingcpp.read_barcodes(rotated)
                if results:
                    for r in results:
                        if r.text and r.text.strip():
                            return r.text.strip()

    except Exception as e:
        print(f"[Barcode Reader] Warning reading image {image_path}: {e}")
        return None

    return None
