"""Unit tests for synthetic layer stimulus composition (no network)."""

from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.services.ppt_images import ImageCache, _parse_aspect
from app.services.synthetic_study_adapter import build_study_data_for_synthetic
from app.synthetic.layer_stimulus import (
    StimulusComposer,
    build_compose_elements,
    describe_layer_stack,
    encode_composed_image_for_vision,
    enrich_shown_element,
    is_layer_study,
    is_probably_image_url,
    is_shown_flag,
    iter_shown_elements,
    normalize_study_type,
    normalize_transform,
    normalize_z_index,
    resolve_aspect_ratio,
    resolve_layer_name,
)


def _solid(color, size=(80, 80)) -> Image.Image:
    return Image.new("RGBA", size, color)


class LayerStimulusHelpersTests(unittest.TestCase):
    def test_normalize_study_type_variants(self):
        self.assertEqual(normalize_study_type("layer"), "layer")
        self.assertEqual(normalize_study_type("LAYER"), "layer")
        self.assertEqual(normalize_study_type("StudyType.layer"), "layer")
        self.assertEqual(normalize_study_type(SimpleNamespace(value="layer")), "layer")
        self.assertEqual(normalize_study_type(None), "")

    def test_is_layer_study_from_type_or_layout(self):
        self.assertTrue(is_layer_study({"study_type": "layer"}))
        self.assertTrue(is_layer_study({"layer_layout": {"Logo": {"z_index": 1}}}))
        self.assertFalse(is_layer_study({"study_type": "grid"}))
        self.assertFalse(is_layer_study({}))
        self.assertFalse(is_layer_study(None))

    def test_is_shown_flag(self):
        self.assertTrue(is_shown_flag(1))
        self.assertTrue(is_shown_flag("1"))
        self.assertTrue(is_shown_flag(True))
        self.assertTrue(is_shown_flag(1.0))
        self.assertFalse(is_shown_flag(0))
        self.assertFalse(is_shown_flag(None))
        self.assertFalse(is_shown_flag("no"))

    def test_normalize_transform_clamps_and_defaults(self):
        self.assertEqual(
            normalize_transform(None),
            {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0},
        )
        self.assertEqual(
            normalize_transform('{"x": 10, "y": 20, "width": 30, "height": 40}'),
            {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0},
        )
        bad = normalize_transform({"x": -5, "y": "nope", "width": 250, "height": 0})
        self.assertEqual(bad["x"], 0.0)
        self.assertEqual(bad["width"], 100.0)
        self.assertEqual(bad["height"], 100.0)

    def test_normalize_z_index(self):
        self.assertEqual(normalize_z_index("3"), 3)
        self.assertEqual(normalize_z_index(None), 0)
        self.assertEqual(normalize_z_index("x"), 0)

    def test_resolve_layer_name_from_key(self):
        self.assertEqual(resolve_layer_name("Logo_2", {}), "Logo")
        self.assertEqual(resolve_layer_name("x", {"layer_name": " Badge "}), "Badge")
        self.assertEqual(resolve_layer_name("x", {"category_name": "Cap"}), "Cap")

    def test_iter_shown_without_elements_shown_map(self):
        task = {
            "elements_shown_content": {
                "Logo_1": {"url": "https://cdn.example.com/a.png", "layer_name": "Logo"},
            }
        }
        pairs = iter_shown_elements(task)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0], "Logo_1")

    def test_iter_shown_from_layers_shown_in_task(self):
        task = {
            "elements_shown": {"Cap_1": 1, "Logo_1": 0},
            "layers_shown_in_task": {
                "Cap_1": {"url": "https://cdn.example.com/cap.png"},
                "Logo_1": {"url": "https://cdn.example.com/logo.png"},
            },
        }
        pairs = iter_shown_elements(task)
        self.assertEqual([key for key, _ in pairs], ["Cap_1"])

    def test_enrich_pulls_transform_from_study_layout(self):
        study = {
            "layer_layout": {
                "Logo": {
                    "z_index": 7,
                    "transform": {"x": 12, "y": 8, "width": 20, "height": 15},
                    "layer_type": "image",
                }
            }
        }
        element = {"url": "https://acct.blob.core.windows.net/studies/logo?sv=2024-11-04&sig=abc", "name": "Logo A"}
        enriched = enrich_shown_element("Logo_1", element, study)
        self.assertEqual(enriched["layer_name"], "Logo")
        self.assertEqual(enriched["z_index"], 7)
        self.assertEqual(enriched["transform"]["x"], 12.0)
        self.assertTrue(enriched["url"].startswith("https://acct.blob.core.windows.net/"))
        self.assertEqual(enriched["element_type"], "image")

    def test_task_transform_wins_over_layout(self):
        study = {"layer_layout": {"Logo": {"z_index": 1, "transform": {"x": 0, "y": 0, "width": 100, "height": 100}}}}
        element = {
            "url": "https://cdn.example.com/logo.png",
            "layer_name": "Logo",
            "z_index": 9,
            "transform": {"x": 5, "y": 6, "width": 10, "height": 11},
        }
        enriched = enrich_shown_element("Logo_1", element, study)
        self.assertEqual(enriched["z_index"], 9)
        self.assertEqual(enriched["transform"]["width"], 10.0)

    def test_is_probably_image_url(self):
        azure = "https://acct.blob.core.windows.net/studies/a1b2c3d4_upload?sv=2024-11-04&sig=abc"
        self.assertTrue(is_probably_image_url(azure))
        self.assertTrue(is_probably_image_url("https://acct.blob.core.windows.net/studies/guid-only"))
        self.assertTrue(is_probably_image_url("https://cdn.example.com/a.png"))
        self.assertFalse(is_probably_image_url("https://cdn.example.com/notes.txt"))
        self.assertFalse(is_probably_image_url("https://cdn.example.com/page", element_type="text", layer_mode=False))
        self.assertTrue(is_probably_image_url("https://cdn.example.com/page", element_type="image"))

    def test_describe_stack_includes_background_and_transform(self):
        task = {
            "elements_shown": {"Logo_1": 1},
            "elements_shown_content": {"Logo_1": {"url": "https://cdn.example.com/a.png", "layer_name": "Logo"}},
        }
        study = {
            "background_image_url": "https://cdn.example.com/bg.png",
            "layer_layout": {"Logo": {"z_index": 2, "transform": {"x": 10, "y": 20, "width": 30, "height": 40}}},
        }
        text = describe_layer_stack(task, study)
        self.assertIn("Background image: yes", text)
        self.assertIn('Layer "Logo"', text)
        self.assertIn("z-index=2", text)
        self.assertIn("x=10%", text)

    def test_resolve_aspect_ratio(self):
        self.assertEqual(resolve_aspect_ratio({"aspect_ratio": "16:9"}), "16:9")
        self.assertEqual(resolve_aspect_ratio({"audience_segmentation": {"aspect_ratio": "1:1"}}), "1:1")
        self.assertEqual(resolve_aspect_ratio({}), "9 / 16")

    def test_parse_aspect_common_ratios(self):
        self.assertEqual(_parse_aspect("9 / 16"), (1080, 1920))
        self.assertEqual(_parse_aspect("16:9"), (1920, 1080))
        self.assertEqual(_parse_aspect("4:3"), (1440, 1080))
        self.assertEqual(_parse_aspect("3:4"), (1080, 1440))

    def test_encode_jpeg_data_url(self):
        buf = io.BytesIO()
        Image.new("RGBA", (200, 400), (10, 20, 30, 255)).save(buf, format="PNG")
        data_url = encode_composed_image_for_vision(buf.getvalue())
        self.assertTrue(data_url.startswith("data:image/jpeg;base64,"))
        self.assertGreater(len(data_url), 100)

    def test_build_compose_elements_skips_missing_urls(self):
        task = {
            "elements_shown": {"Logo_1": 1, "Text_1": 1},
            "elements_shown_content": {
                "Logo_1": {"url": "https://cdn.example.com/a.png", "layer_name": "Logo"},
                "Text_1": {"name": "Claim only"},
            },
        }
        elements = build_compose_elements(task, {"study_type": "layer"})
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["image_url"], "https://cdn.example.com/a.png")

    def test_azure_blob_sas_url_is_kept_intact_for_download(self):
        azure = "https://acct.blob.core.windows.net/studies/a1b2_logo.png?sv=2024-11-04&sig=abc"
        task = {
            "elements_shown": {"Logo_1": 1},
            "elements_shown_content": {"Logo_1": {"url": azure, "layer_name": "Logo"}},
        }
        elements = build_compose_elements(task, {"study_type": "layer"})
        self.assertEqual(elements[0]["image_url"], azure)
        self.assertTrue(is_probably_image_url(azure))


class StimulusComposerTests(unittest.TestCase):
    def test_compose_uses_background_z_index_and_caches(self):
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

        task = {
            "elements_shown": {"Red_1": 1, "Blue_1": 1},
            "elements_shown_content": {
                "Red_1": {"url": "https://example.com/red.png", "layer_name": "Red"},
                "Blue_1": {"url": "https://example.com/blue.png", "layer_name": "Blue"},
            },
        }
        study = {
            "study_type": "layer",
            "background_image_url": "https://example.com/bg.png",
            "aspect_ratio": "9 / 16",
            "layer_layout": {
                "Red": {"z_index": 1, "transform": {"x": 0, "y": 0, "width": 100, "height": 100}},
                "Blue": {"z_index": 2, "transform": {"x": 10, "y": 10, "width": 80, "height": 80}},
            },
        }
        composer = StimulusComposer(image_cache=cache)
        with patch.object(ImageCache, "get", side_effect=fake_get):
            first = composer.compose_data_url(task, study)
            second = composer.compose_data_url(task, study)
        self.assertTrue(first.startswith("data:image/jpeg;base64,"))
        self.assertEqual(first, second)

    def test_compose_returns_none_when_downloads_fail(self):
        composer = StimulusComposer()
        task = {
            "elements_shown": {"Logo_1": 1},
            "elements_shown_content": {"Logo_1": {"url": "https://example.com/missing.png"}},
        }
        with patch.object(ImageCache, "get", return_value=None):
            self.assertIsNone(composer.compose_data_url(task, {"study_type": "layer"}))

    def test_compose_returns_none_when_no_shown_images(self):
        composer = StimulusComposer()
        task = {"elements_shown": {}, "elements_shown_content": {}}
        self.assertIsNone(composer.compose_data_url(task, {"study_type": "layer"}))


class AdapterLayerLayoutTests(unittest.TestCase):
    def test_build_study_data_includes_layout_and_background(self):
        layer = SimpleNamespace(
            layer_id="L1",
            name="Logo",
            order=1,
            layer_type="image",
            z_index=4,
            transform={"x": 5, "y": 6, "width": 20, "height": 15},
            images=[
                SimpleNamespace(image_id="i1", id="uuid-1", name="Logo A", url="https://cdn.example.com/a.png", order=1),
            ],
        )
        study = SimpleNamespace(
            id="11111111-1111-1111-1111-111111111111",
            title="Pack",
            background="Study text background",
            background_image_url="https://cdn.example.com/bg.png",
            main_question="How appealing?",
            language="en",
            orientation_text="Look at the pack",
            study_type="layer",
            rating_scale={"min_value": 1, "max_value": 5},
            audience_segmentation={"aspect_ratio": "9:16", "number_of_respondents": 10},
            classification_questions=[],
            layers=[layer],
            categories=[],
            elements=[],
            tasks={"1": []},
        )
        data = build_study_data_for_synthetic(study, tasks={"1": []})
        self.assertEqual(data["study_type"], "layer")
        self.assertEqual(data["background_image_url"], "https://cdn.example.com/bg.png")
        self.assertEqual(data["aspect_ratio"], "9:16")
        self.assertEqual(data["layer_layout"]["Logo"]["z_index"], 4)
        self.assertEqual(data["layer_layout"]["Logo"]["transform"]["x"], 5.0)
        self.assertEqual(data["elements"][0]["url"], "https://cdn.example.com/a.png")
        self.assertEqual(data["background"], "Study text background")


if __name__ == "__main__":
    unittest.main()
