from pathlib import Path

from PIL import Image

from app.services.reportgen.composite import _RENDER_CACHE, _fitted_layer, _stack, image_render_cache


def test_repeated_layers_preserve_pixels_and_cache_is_scoped_to_one_report(tmp_path):
    source = Path(__file__).parent.parent / "app/services/reportgen/template_assets/mga_logo.png"
    uncached = _stack([source, source], width=100)
    @image_render_cache
    def render():
        first = _fitted_layer(source, uncached.size)
        assert _fitted_layer(source, uncached.size) is first
        result = _stack([source, source], width=100)
        assert result.tobytes() == uncached.tobytes()
        return result
    result = render()
    assert _RENDER_CACHE.get() is None
    assert result.tobytes() == uncached.tobytes()


def test_fast_lossless_png_encoding_preserves_all_pixels(tmp_path):
    source = Path(__file__).parent.parent / "app/services/reportgen/template_assets/mga_logo.png"
    image = _stack([source], width=100).convert("RGB")
    expected = tmp_path / "optimized.png"
    actual = tmp_path / "fast.png"
    image.save(expected, optimize=True)
    image.save(actual, compress_level=1)
    with Image.open(expected) as left, Image.open(actual) as right:
        assert left.tobytes() == right.tobytes()
