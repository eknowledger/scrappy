"""cover() and the three makers must work for any picture shape: tiny, 1 x N, N x 1, near 16:9, huge ratios."""
import io
import itertools
import random

import pytest
from PIL import Image

from scrappy.processing.images import (BOX_SIZE, IMAGE_SIZE, LOGO_SIZE, cover, cover_box, make_boxart_png, make_image_jpg,
                                   make_logo_png)

SIZES = [IMAGE_SIZE, BOX_SIZE, LOGO_SIZE, (1, 1), (7, 3)]


def check_box(w, h, size):
    left, top, right, bottom = cover_box(w, h, size)
    assert 0 <= left < right <= w and 0 <= top < bottom <= h, (w, h, size, (left, top, right, bottom))
    assert all(isinstance(v, int) for v in (left, top, right, bottom))
    return left, top, right, bottom


def test_the_old_failing_shapes_have_a_valid_box():
    # float arithmetic gave left = -2.27e-13 for these (found with a real pasted picture)
    for w, h in ((4068, 3882), (891, 2077), (1746, 2184), (3694, 1372)):
        for size in (IMAGE_SIZE, BOX_SIZE):
            check_box(w, h, size)


def test_box_is_valid_for_a_great_many_shapes():
    rnd = random.Random(7)
    pairs = [(rnd.randint(1, 6000), rnd.randint(1, 6000)) for _ in range(40000)]
    for w in (1, 2, 3, 639, 640, 641, 1919, 1920, 1921, 5000, 20000):
        for h in (1, 2, 3, 359, 360, 361, 1079, 1080, 1081, 5000, 20000):
            pairs.append((w, h))
    for w, h in pairs:
        for size in SIZES:
            check_box(w, h, size)


@pytest.mark.parametrize("w,h", [(1920, 1080), (1921, 1080), (1919, 1080), (1920, 1081), (1920, 1079), (1280, 720), (3840, 2160)])
def test_exact_and_nearly_16_9_keep_the_whole_picture_or_lose_at_most_a_pixel(w, h):
    left, top, right, bottom = check_box(w, h, IMAGE_SIZE)
    assert (right - left) >= w - 2 and (bottom - top) >= h - 2


def test_box_is_centred_and_has_the_target_shape():
    left, top, right, bottom = check_box(1000, 1000, IMAGE_SIZE)
    assert (right - left, bottom - top) == (1000, 562) and abs(left) <= 1 and abs(top - (1000 - 562) // 2) <= 1
    left, top, right, bottom = check_box(500, 100, BOX_SIZE)
    assert bottom - top == 100 and abs((right - left) / 100 - 396 / 640) < 0.02


def test_cover_resizes_odd_shapes():
    shapes = [(1, 1), (1, 5000), (5000, 1), (2, 9999), (9999, 2), (1920, 1080), (1921, 1080), (1920, 1079), (4068, 3882),
              (891, 2077), (3, 3), (640, 360), (641, 361), (100000, 3)]
    for (w, h), size in itertools.product(shapes, (IMAGE_SIZE, BOX_SIZE, LOGO_SIZE)):
        out = cover(Image.new("RGB", (w, h), (10, 20, 30)), size)
        assert out.size == size and out.getpixel((size[0] // 2, size[1] // 2)) == (10, 20, 30)


def png(w, h):
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (200, 30, 30, 255)).save(buf, "PNG")
    return Image.open(io.BytesIO(buf.getvalue()))


SHAPES = [(1, 1), (1, 3000), (3000, 1), (1920, 1080), (1921, 1080), (1919, 1080), (1920, 1081), (4068, 3882), (891, 2077),
          (2, 5000), (5000, 2), (396, 640), (600, 300), (17, 9)]


@pytest.mark.parametrize("w,h", SHAPES)
def test_makers_give_the_theme_sizes_for_any_shape(w, h):
    im = png(w, h)
    jpg = Image.open(io.BytesIO(make_image_jpg(im)))
    assert jpg.size == IMAGE_SIZE and jpg.format == "JPEG"
    box_bytes, _cropped = make_boxart_png(im)
    box = Image.open(io.BytesIO(box_bytes))
    assert box.size == BOX_SIZE and box.mode == "RGBA"
    logo = Image.open(io.BytesIO(make_logo_png(im)))
    assert logo.size == LOGO_SIZE and logo.mode == "RGBA"
