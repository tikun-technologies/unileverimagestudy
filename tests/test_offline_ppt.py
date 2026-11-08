import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from app.services.offline_ppt import offline_report_source, report_files


def test_ships_the_actual_live_generator_fonts_and_artwork():
    source = report_files()
    root = Path(__file__).parent.parent / "app/services/reportgen"
    assert source["format"] == "reportgen-v1"
    for name, encoded in source["files"].items():
        assert base64.b64decode(encoded) == (root / name).read_bytes()
    assert "build.py" in source["files"]
    assert "combinations.py" in source["files"]
    assert "fonts/Karla-Regular.ttf" in source["files"]
    assert "template_assets/cover_Picture_17.png" in source["files"]


def test_retains_live_image_bytes_credit_and_constraints(tmp_path):
    study = SimpleNamespace(id=uuid4(), main_question="  How   appealing? ")
    exporter = SimpleNamespace(name="Analyst", email="a@example.test")
    data = {"elements": [{"content": "https://assets.test/element.png"}, {"content": "https://assets.test/video.mp4"}], "background": "https://assets.test/element.png"}
    image = b"\x89PNG\r\n\x1a\nexact-source-bytes"
    def fetch_image(destination, url):
        destination.write_bytes(image)
    with patch("app.services.appeal_report._constraint_study_data", return_value={"design_constraints": ["rule"]}), \
         patch("app.services.appeal_report._image_cache_dir", return_value=tmp_path), \
         patch("app.services.reportgen.study.fetch_image", side_effect=fetch_image) as fetch:
        result = offline_report_source(study, data, exporter)
    fetch.assert_called_once()
    encoded = result["artwork"]["https://assets.test/element.png"].split(",")[1]
    assert base64.b64decode(encoded) == image
    assert result["preparedBy"] == "Analyst | a@example.test"
    assert result["mainQuestion"] == "How appealing?"
    assert result["constraintStudy"] == {"design_constraints": ["rule"]}
