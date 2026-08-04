"""Unit tests for PPT image composition (no network)."""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from PIL import Image

from app.services.ppt_images import (
    ImageCache,
    compose_design_preview,
    compose_element_thumb,
    compose_layer_design,
    compose_mosaic_design,
)


def _solid(color, size=(80, 80)) -> Image.Image:
    return Image.new("RGBA", size, color)


class PptImagesTests(unittest.TestCase):
    def test_compose_element_thumb_text_card(self):
        png = compose_element_thumb(
            {"name": "Clean claim", "element_type": "text"},
            cache=ImageCache(),
            size=200,
        )
        img = Image.open(io.BytesIO(png))
        self.assertEqual(img.size, (200, 200))

    def test_compose_layer_stacks_by_z_index(self):
        cache = ImageCache()
        red = _solid((255, 0, 0, 255), (100, 100))
        blue = _solid((0, 0, 255, 255), (100, 100))
        bg = _solid((200, 200, 200, 255), (200, 400))

        def fake_get(url):
            mapping = {
                "https://example.com/bg.png": bg,
                "https://example.com/red.png": red,
                "https://example.com/blue.png": blue,
            }
            image = mapping.get(url)
            return image.copy() if image is not None else None

        with patch.object(ImageCache, "get", side_effect=fake_get):
            png = compose_layer_design(
                [
                    {
                        "name": "Blue",
                        "image_url": "https://example.com/blue.png",
                        "z_index": 2,
                        "transform": {"x": 10, "y": 10, "width": 80, "height": 80},
                    },
                    {
                        "name": "Red",
                        "image_url": "https://example.com/red.png",
                        "z_index": 1,
                        "transform": {"x": 0, "y": 0, "width": 100, "height": 100},
                    },
                ],
                background_url="https://example.com/bg.png",
                aspect_ratio="9 / 16",
                cache=cache,
            )
        img = Image.open(io.BytesIO(png))
        self.assertEqual(img.size, (1080, 1920))
        # Center pixel should come from the topmost (blue) layer after stacking.
        # Sample near center of the fit box.
        pixel = img.getpixel((540, 960))
        self.assertEqual(pixel[2], 255)  # blue channel dominant

    def test_compose_mosaic_and_design_preview_grid(self):
        cache = ImageCache()
        green = _solid((0, 180, 0, 255), (120, 120))

        def fake_get(url):
            return green.copy() if url else None

        with patch.object(ImageCache, "get", side_effect=fake_get):
            mosaic = compose_mosaic_design(
                [
                    {"name": "A", "image_url": "https://example.com/a.png", "z_index": 0},
                    {"name": "B", "image_url": "https://example.com/b.png", "z_index": 1},
                ],
                cache=cache,
                size=400,
            )
            preview = compose_design_preview(
                {
                    "elements": [
                        {"name": "A", "image_url": "https://example.com/a.png", "z_index": 0},
                    ]
                },
                study_type="grid",
                background_url=None,
                aspect_ratio="1 / 1",
                cache=cache,
            )
        self.assertGreater(len(mosaic), 500)
        self.assertGreater(len(preview), 500)


if __name__ == "__main__":
    unittest.main()
