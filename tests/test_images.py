import io
import os

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from scrappy.processing import images
from scrappy.processing.images import (BOX_SIZE, IMAGE_SIZE, LOGO_SIZE, has_real_transparency, make_boxart_png,
                                   make_image_jpg, make_logo_png, make_text_logo, open_image, remove_background,
                                   write_atomic)


def encode(im, fmt="PNG", **kw):
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


def test_make_image_jpg_size_and_format():
    data = make_image_jpg(Image.new("RGB", (1920, 1080), (200, 30, 30)))
    im = Image.open(io.BytesIO(data))
    assert im.format == "JPEG" and im.size == IMAGE_SIZE == (640, 360) and im.mode == "RGB"


def test_make_image_jpg_crops_portrait_source():
    data = make_image_jpg(Image.new("RGB", (600, 900), (10, 200, 10)))
    assert Image.open(io.BytesIO(data)).size == (640, 360)


def test_make_image_jpg_crops_the_centre():
    src = Image.new("RGB", (400, 900), (255, 0, 0))
    ImageDraw.Draw(src).rectangle((0, 300, 399, 599), fill=(0, 0, 255))   # the centre band of a portrait
    out = np.asarray(Image.open(io.BytesIO(make_image_jpg(src))))
    assert out[180, 320, 2] > 200 and out[180, 320, 0] < 60
    assert out[2, 320, 2] > 200                                            # band fills the whole 16:9 crop


def test_make_image_jpg_flattens_transparency_on_dark_grey():
    out = np.asarray(Image.open(io.BytesIO(make_image_jpg(Image.new("RGBA", (64, 36), (255, 255, 255, 0))))))
    assert abs(int(out[10, 10, 0]) - 16) <= 3


def test_make_image_jpg_tiny_and_extreme_sources():
    for size in ((1, 1), (3, 2), (1, 1000), (5000, 2)):
        assert Image.open(io.BytesIO(make_image_jpg(Image.new("RGB", size, (9, 9, 9))))).size == IMAGE_SIZE


def test_outputs_are_deterministic():
    src = Image.effect_noise((300, 500), 40).convert("RGB")
    assert make_image_jpg(src) == make_image_jpg(src)
    assert make_boxart_png(src)[0] == make_boxart_png(src)[0]
    assert make_logo_png(src) == make_logo_png(src)
    assert make_text_logo("Some Game") == make_text_logo("Some Game")


def test_boxart_png_size_alpha_and_crop_flag():
    data, cropped = make_boxart_png(Image.new("RGB", (600, 900), (1, 2, 3)))
    im = Image.open(io.BytesIO(data))
    assert im.format == "PNG" and im.size == BOX_SIZE == (396, 640) and im.mode == "RGBA"
    assert cropped is False
    _, cropped_wide = make_boxart_png(Image.new("RGB", (1920, 1080)))
    assert cropped_wide is True


def test_boxart_keeps_alpha_of_transparent_source():
    src = Image.new("RGBA", (396, 640), (255, 0, 0, 0))
    ImageDraw.Draw(src).rectangle((100, 100, 300, 500), fill=(255, 0, 0, 255))
    arr = np.asarray(Image.open(io.BytesIO(make_boxart_png(src)[0])))
    assert arr[0, 0, 3] == 0 and arr[300, 200, 3] == 255


def test_has_real_transparency():
    assert not has_real_transparency(Image.new("RGB", (10, 10), (1, 1, 1)))
    assert not has_real_transparency(Image.new("RGBA", (10, 10), (1, 1, 1, 255)))
    assert has_real_transparency(Image.new("RGBA", (10, 10), (1, 1, 1, 0)))


def test_has_real_transparency_palette_and_la():
    assert not has_real_transparency(Image.new("P", (10, 10)))
    pal = Image.new("P", (10, 10), 0)
    pal.info["transparency"] = 0
    assert has_real_transparency(pal)
    assert has_real_transparency(Image.new("LA", (10, 10), (5, 0)))
    assert not has_real_transparency(Image.new("LA", (10, 10), (5, 255)))


def test_remove_background_makes_corners_transparent():
    im = Image.new("RGB", (200, 100), (255, 255, 255))
    ImageDraw.Draw(im).rectangle((60, 30, 140, 70), fill=(200, 0, 0))
    out = remove_background(im)
    a = np.asarray(out)[:, :, 3]
    assert a[0, 0] == 0 and a[99, 199] == 0 and a[50, 100] == 255
    assert has_real_transparency(out)


def test_remove_background_keeps_enclosed_interior():
    # white background, red ring with a white centre: the centre is not connected to the corners
    im = Image.new("RGB", (200, 200), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle((40, 40, 160, 160), fill=(200, 0, 0))
    d.rectangle((80, 80, 120, 120), fill=(255, 255, 255))
    a = np.asarray(remove_background(im))[:, :, 3]
    assert a[0, 0] == 0 and a[100, 100] == 255 and a[60, 60] == 255


def test_remove_background_ignores_corner_that_differs_from_the_others():
    im = Image.new("RGB", (200, 100), (255, 255, 255))
    ImageDraw.Draw(im).rectangle((0, 0, 20, 20), fill=(0, 0, 200))        # logo touches the top-left corner
    a = np.asarray(remove_background(im))[:, :, 3]
    assert a[5, 5] == 255 and a[99, 199] == 0


def test_remove_background_without_uniform_corners_changes_nothing():
    im = Image.new("RGB", (100, 100), (255, 255, 255))
    d = ImageDraw.Draw(im)
    for xy, c in (((0, 0), (255, 0, 0)), ((99, 0), (0, 255, 0)), ((0, 99), (0, 0, 255)), ((99, 99), (0, 0, 0))):
        d.point(xy, fill=c)
    assert not has_real_transparency(remove_background(im))


def test_remove_background_tolerates_jpeg_noise():
    im = Image.new("RGB", (200, 100), (250, 250, 250))
    ImageDraw.Draw(im).rectangle((60, 30, 140, 70), fill=(10, 10, 10))
    noisy = Image.open(io.BytesIO(encode(im, "JPEG", quality=40)))
    a = np.asarray(remove_background(noisy))[:, :, 3]
    assert a[0, 0] == 0 and a[50, 100] == 255 and (a[:10, :] == 0).all()


def test_make_logo_png_trims_and_fits_canvas():
    im = Image.new("RGBA", (1000, 1000), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle((400, 450, 600, 550), fill=(255, 0, 0, 255))
    out = Image.open(io.BytesIO(make_logo_png(im)))
    assert out.size == LOGO_SIZE == (600, 300) and out.mode == "RGBA"
    arr = np.asarray(out)
    assert arr[0, 0, 3] == 0
    ys, xs = np.where(arr[:, :, 3] > 0)
    assert xs.min() > 0 and xs.max() < 599 and ys.min() > 0 and ys.max() < 299


def test_make_logo_png_rejects_fully_transparent_input():
    with pytest.raises(ValueError):
        make_logo_png(Image.new("RGBA", (50, 50), (0, 0, 0, 0)))


def test_make_text_logo():
    out = Image.open(io.BytesIO(make_text_logo("Tidepool Tactics: Remastered")))
    assert out.size == LOGO_SIZE
    arr = np.asarray(out.convert("RGBA"))
    assert arr[0, 0, 3] == 0 and (arr[:, :, 3] > 0).sum() > 500


def test_make_text_logo_stays_inside_canvas_and_is_centred():
    for title in ("A", "Tidepool Tactics: Remastered", "Supercalifragilisticexpialidocious" * 3, "x " * 80):
        arr = np.asarray(Image.open(io.BytesIO(make_text_logo(title))))[:, :, 3]
        ys, xs = np.where(arr > 0)
        assert xs.min() > 0 and xs.max() < 599 and ys.min() > 0 and ys.max() < 299, title
        assert abs((xs.min() + xs.max()) / 2 - 299.5) <= 3 and abs((ys.min() + ys.max()) / 2 - 149.5) <= 3, title


def test_make_text_logo_centres_each_line():
    arr = np.asarray(Image.open(io.BytesIO(make_text_logo("Tidepool Tactics: Remastered"))))[:, :, 3]
    rows = np.where((arr > 0).any(axis=1))[0]
    top_half = arr[rows.min():(rows.min() + rows.max()) // 2]
    xs = np.where((top_half > 0).any(axis=0))[0]
    bottom = arr[(rows.min() + rows.max()) // 2 + 20:]
    xb = np.where((bottom > 0).any(axis=0))[0]
    assert abs((xs.min() + xs.max()) / 2 - (xb.min() + xb.max()) / 2) <= 3


def test_make_text_logo_rejects_blank_title():
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            make_text_logo(bad)


def test_make_text_logo_without_any_truetype_font(monkeypatch):
    monkeypatch.setattr(images, "FONT_CANDIDATES", [])
    arr = np.asarray(Image.open(io.BytesIO(make_text_logo("Hello World"))))
    assert (arr[:, :, 3] > 0).sum() > 100


def test_make_text_logo_with_only_the_bitmap_font(monkeypatch):
    def no_truetype(*a, **k):
        raise OSError("no fonts")

    monkeypatch.setattr(ImageFont, "truetype", no_truetype)
    monkeypatch.setattr(ImageFont, "load_default", lambda size=None: ImageFont.load_default_imagefont())
    arr = np.asarray(Image.open(io.BytesIO(make_text_logo("Hello World"))))
    assert arr.shape[:2] == (300, 600) and (arr[:, :, 3] > 0).sum() > 50


def test_open_image_from_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, "PNG")
    assert open_image(buf.getvalue()).size == (4, 4)


def test_open_image_applies_exif_orientation():
    src = Image.new("RGB", (40, 20), (255, 0, 0))
    ImageDraw.Draw(src).rectangle((0, 0, 9, 9), fill=(0, 0, 255))           # marker top-left
    exif = Image.Exif()
    exif[0x0112] = 6                                                          # rotate 90 CW to display
    im = open_image(encode(src, "JPEG", exif=exif, quality=95))
    assert im.size == (20, 40)
    assert im.getpixel((15, 5))[2] > 200                                      # marker moved to top-right


@pytest.mark.parametrize("mode", ["P", "L", "1", "LA", "CMYK", "RGBA", "RGB"])
def test_open_image_normalises_modes(mode):
    src = Image.new("RGB", (8, 8), (10, 200, 30)).convert(mode)
    fmt = "TIFF" if mode == "CMYK" else "PNG"
    out = open_image(encode(src, fmt))
    assert out.mode in ("RGB", "RGBA") and out.size == (8, 8)
    assert (out.mode == "RGBA") == (mode in ("RGBA", "LA"))


def test_open_image_palette_transparency_becomes_alpha():
    pal = Image.new("P", (8, 8), 0)
    pal.putpalette([255, 0, 0] + [0, 0, 0] * 255)
    out = open_image(encode(pal, transparency=0))
    assert out.mode == "RGBA" and out.getpixel((0, 0))[3] == 0


def test_open_image_cmyk_jpeg():
    out = open_image(encode(Image.new("CMYK", (8, 8), (0, 255, 255, 0)), "JPEG"))
    assert out.mode == "RGB" and out.getpixel((4, 4))[0] > 200 and out.getpixel((4, 4))[2] < 80


def test_open_image_16bit_grayscale_is_scaled_not_clipped():
    arr = np.full((8, 8), 40000, dtype=np.uint16)
    out = open_image(encode(Image.fromarray(arr), "PNG"))
    assert out.mode == "RGB"
    assert 150 <= out.getpixel((2, 2))[0] <= 160                              # 40000 / 257 ~ 155, not 255


def test_open_image_animated_uses_first_frame():
    frames = [Image.new("RGB", (16, 16), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=50, loop=0)
    out = open_image(buf.getvalue())
    assert out.size == (16, 16) and out.getpixel((3, 3))[0] > 200 and out.getpixel((3, 3))[1] < 60


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_open_image_rejects_decompression_bombs(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    data = encode(Image.new("RGB", (40, 40)))                                  # 1600 px: between 1x and 2x limit
    with pytest.raises(Image.DecompressionBombError):
        open_image(data)
    with pytest.raises(Image.DecompressionBombError):
        open_image(encode(Image.new("RGB", (100, 100))))


def test_open_image_rejects_garbage():
    with pytest.raises(OSError):
        open_image(b"definitely not an image")


def test_write_atomic_replaces_file_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "x-image.jpg"
    target.write_bytes(b"old")
    write_atomic(target, b"new")
    assert target.read_bytes() == b"new" and os.listdir(tmp_path) == ["x-image.jpg"]


def test_write_atomic_failure_keeps_old_file_and_cleans_tmp(tmp_path, monkeypatch):
    target = tmp_path / "x-image.jpg"
    target.write_bytes(b"old")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_atomic(target, b"new")
    assert target.read_bytes() == b"old" and os.listdir(tmp_path) == ["x-image.jpg"]


def test_write_atomic_does_not_follow_a_planted_tmp_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_bytes(b"precious")
    (tmp_path / "x-image.jpg.tmp").symlink_to(victim)               # the old, predictable temp name
    write_atomic(tmp_path / "x-image.jpg", b"new")
    assert victim.read_bytes() == b"precious" and (tmp_path / "x-image.jpg").read_bytes() == b"new"


def test_write_atomic_keeps_the_mode_of_the_file_it_replaces(tmp_path):
    target = tmp_path / "x-image.jpg"
    target.write_bytes(b"old")
    target.chmod(0o640)
    write_atomic(target, b"new")
    assert (target.stat().st_mode & 0o777) == 0o640


# ---------------------------------------------------------------- hardening

def test_open_image_is_thread_safe_and_leaves_warning_filters_alone():
    import threading
    import warnings

    data = encode(Image.new("RGB", (8, 8), (1, 2, 3)))
    before = list(warnings.filters)
    errors = []

    def work():
        try:
            for _ in range(300):
                assert open_image(data).size == (8, 8)
        except Exception as e:          # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and warnings.filters == before


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_open_image_refuses_between_one_and_two_times_the_limit(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(Image.DecompressionBombError):
        open_image(encode(Image.new("RGB", (40, 40))))                       # 1600 px
    assert open_image(encode(Image.new("RGB", (30, 30)))).size == (30, 30)   # 900 px is fine


def _sample(fmt):
    im = Image.new("RGB", (16, 16), (10, 200, 30))
    if fmt == "MPO":
        buf = io.BytesIO()
        im.save(buf, "MPO", save_all=True, append_images=[im.copy()])
        return buf.getvalue()
    return encode(im, fmt)


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF", "MPO"])
def test_open_image_allowed_formats(fmt):
    assert open_image(_sample(fmt)).size == (16, 16)


@pytest.mark.parametrize("fmt", ["PPM", "ICO", "PDF", "EPS", "DDS", "PCX", "TGA"])
def test_open_image_rejects_formats_outside_the_allow_list(fmt):
    im = Image.new("RGB", (16, 16), (1, 2, 3))
    buf = io.BytesIO()
    try:
        im.save(buf, fmt)
    except (OSError, KeyError, ValueError):
        pytest.skip(f"Pillow cannot write {fmt} here")
    with pytest.raises(Image.UnidentifiedImageError, match="unsupported"):
        open_image(buf.getvalue())


def test_open_image_rejects_postscript_payload():
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n(x) show\n"
    with pytest.raises(Image.UnidentifiedImageError):
        open_image(eps)


def test_remove_background_gives_up_when_flood_pass_cap_is_hit(monkeypatch):
    im = Image.new("RGB", (200, 100), (255, 255, 255))
    ImageDraw.Draw(im).rectangle((60, 30, 140, 70), fill=(200, 0, 0))
    assert has_real_transparency(remove_background(im))                      # normal cap: works
    monkeypatch.setattr(images, "FLOOD_PASS_FACTOR", 0)                      # at most 1 pass
    out = remove_background(im)
    assert not has_real_transparency(out) and out.size == (200, 100)


@pytest.mark.parametrize("n, pitch", [(1181, 20), (601, 2)])
def test_remove_background_serpentine_worst_case_completes(n, pitch):
    im = Image.new("RGB", (n, n), (0, 0, 0))
    d = ImageDraw.Draw(im)
    # corridors separated by walls, joined alternately at the right/left end: one flood pass per corridor
    # (pitch 2 is the extreme: n/2 passes, half of the allowed n; n-1 is a multiple of the pitch so the
    # corridors, not the walls, make up most of the border)
    for row in range(0, n, pitch):
        d.line((0, row, n - 1, row), fill=(255, 255, 255))
    for i, row in enumerate(range(0, n - pitch, pitch)):
        d.line((n - 1 if i % 2 == 0 else 0, row, n - 1 if i % 2 == 0 else 0, row + pitch), fill=(255, 255, 255))
    last = n - 1                                                              # the very last corridor
    a = np.asarray(remove_background(im))[:, :, 3]
    assert a[last, n // 2] < 128                                              # (thin corridors are blurred a little)


def test_remove_background_tie_breaks_on_whole_border():
    # a "T": the bar touches both top corners, so the corner colours tie 2-2
    im = Image.new("RGB", (200, 100), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 199, 20), fill=(0, 0, 200))
    d.rectangle((90, 20, 110, 99), fill=(0, 0, 200))
    a = np.asarray(remove_background(im))[:, :, 3]
    assert a[5, 5] == 255 and a[5, 194] == 255 and a[60, 100] == 255          # logo survives
    assert a[90, 10] == 0 and a[90, 190] == 0 and a[50, 40] == 0              # background gone


def test_remove_background_tie_breaks_with_logo_on_two_adjacent_side_corners():
    im = Image.new("RGB", (200, 100), (255, 255, 255))
    ImageDraw.Draw(im).rectangle((0, 0, 30, 99), fill=(0, 0, 200))            # left bar touches both left corners
    a = np.asarray(remove_background(im))[:, :, 3]
    assert a[50, 10] == 255 and a[50, 150] == 0
