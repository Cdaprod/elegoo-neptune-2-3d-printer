from __future__ import annotations

import io

import pytest

from mks_wifi_bridge.gcode_fabrication import (
    MAX_SEGMENTS,
    FabricationPreviewService,
    ModelPreviewError,
    reconstruct,
    render_svg,
)


def fixture_gcode() -> str:
    return (
        ";TYPE:WALL-OUTER\nG90\nM82\n"
        "G1 X0 Y0 Z0.2 E0\nG1 X20 E1\nG1 Y20 E2\n"
        "G1 X0 E3\nG1 Y0 E4\n"
        "G92 E0\nM83\nG91\n;TYPE:SUPPORT\n"
        "G1 X4 Y0 Z0.2 E0.5\nG1 X-4 Y0 E-0.2\n"
        "G90\nG2 X20 Y0 I10 J0 E1.2\n"
    )


def test_reconstruction_is_geometry_not_embedded_tft_thumbnail() -> None:
    model = reconstruct(io.StringIO(fixture_gcode()))
    assert model["source"] == "reconstructed_gcode_extrusion"
    assert model["stats"]["extrusion_segments"] > 5
    assert model["stats"]["arcs_represented_as_chords"] == 0
    assert model["bounds"][1][0] >= 20
    assert any(segment[7] == 2 for segment in model["segments"])
    for view in ("iso", "top", "front"):
        image = render_svg(model, view)
        assert image.startswith(b'<svg xmlns="http://www.w3.org/2000/svg"')
        assert b"G-CODE / DEPOSITED GEOMETRY" in image
        assert b"<path" in image
    with pytest.raises(ModelPreviewError):
        render_svg(model, "side")


def test_relative_extrusion_and_retraction_never_draw_travel() -> None:
    text = "G90\nM83\nG1 X5 E1\nG1 X15 E-0.4\nG0 X25 Y25\nG1 X35 E0.3\n"
    model = reconstruct(io.StringIO(text))
    assert model["stats"]["extrusion_segments"] == 2
    assert model["bounds"][0][0] == 0
    assert model["bounds"][1][0] == 35
    with pytest.raises(ModelPreviewError, match="No deposited"):
        reconstruct(io.StringIO("G90\nG0 X20 Y20\nG1 X35 F1800\n"))


def test_progressive_sampling_covers_entire_large_gcode() -> None:
    commands = ["G90", "M83"]
    commands.extend(f"G1 X{n % 200} Y{n // 200} E0.1" for n in range(16000))
    model = reconstruct(io.StringIO("\n".join(commands)))
    assert model["stats"]["extrusion_segments"] == 15999
    assert model["stats"]["rendered_segments"] <= MAX_SEGMENTS
    assert model["stats"]["sampling_stride"] > 1
    assert model["bounds"][1][1] == 79


def test_cached_source_is_read_only_and_invalidated_after_change(tmp_path) -> None:
    source = tmp_path / "models"
    source.mkdir()
    path = source / "bracket.gcode"
    path.write_text(fixture_gcode())
    original = path.read_bytes()
    preview = FabricationPreviewService(source, tmp_path / "cache")
    first = preview.geometry("bracket.gcode")
    assert first["cache_key"] == preview.geometry("bracket.gcode")["cache_key"]
    assert preview.preview("bracket.gcode", "iso").startswith(b"<svg")
    assert path.read_bytes() == original
    assert list((tmp_path / "cache").glob("*.json"))

    with path.open("a") as target:
        target.write("\nG1 X30 E0.5\n")
    changed = preview.geometry("bracket.gcode")
    assert changed["cache_key"] != first["cache_key"]
    assert changed["bounds"][1][0] >= 30


def test_rejects_traversal_symlinks_and_oversized_input(tmp_path) -> None:
    source = tmp_path / "models"
    source.mkdir()
    outside = tmp_path / "outside.gcode"
    outside.write_text(fixture_gcode())
    (source / "sample.gcode").write_text(fixture_gcode())
    preview = FabricationPreviewService(source, tmp_path / "cache")
    for path in ("../outside.gcode", "/tmp/outside.gcode", "folder//sample.gcode", "x.txt"):
        with pytest.raises(ModelPreviewError):
            preview.geometry(path)
    (source / "link.gcode").symlink_to(outside)
    with pytest.raises(ModelPreviewError):
        preview.geometry("link.gcode")
    (source / "huge.gcode").write_bytes(b"G" * (50 * 1024 * 1024 + 1))
    with pytest.raises(ModelPreviewError):
        preview.geometry("huge.gcode")


def test_unsupported_arc_encoding_is_exposed_not_claimed_exact() -> None:
    code = "M83\nG1 X0 Y0 Z0.2\nG2 X10 Y0 R5 E0.5\n"
    model = reconstruct(io.StringIO(code))
    assert model["stats"]["arcs_represented_as_chords"] == 1
    assert model["stats"]["approximate"] is True


def test_raster_thumbnail_is_cached_geometry_and_invalidates_with_source(tmp_path):
    from PIL import Image

    source = tmp_path / "part.gcode"
    source.write_text(fixture_gcode())
    service = FabricationPreviewService(tmp_path, tmp_path / "cache")
    png = service.raster_preview("part.gcode")
    image = Image.open(io.BytesIO(png))
    assert image.size == (768, 576) and len(image.getcolors(768 * 576)) > 20
    assert service.raster_preview("part.gcode") == png
    source.write_text(fixture_gcode().replace("X20", "X40"))
    assert service.raster_preview("part.gcode") != png
    with pytest.raises(ModelPreviewError):
        service.raster_preview("part.gcode", "unsupported")
