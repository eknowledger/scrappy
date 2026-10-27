"""The Save plan: exactly what a Save would write, replace and leave alone. No Qt."""
from scrappy.controller import CurrentAssets
from scrappy.models import AssetCandidate, CurrentAsset
from scrappy.ui import logic
from scrappy.ui.plan import PicturePick, VideoPick, build_plan

SYS = "/roms/switch"
NEW = AssetCandidate(slot="logo", url="https://x/logo.png", label="Transparent logo", source="Steam")
CUR_LOGO = AssetCandidate(slot="logo", url="current://logo", label="Current", source="current", current=True, width=600, height=300)


STANDARD = {"logo": "images/Game-marquee.png", "boxart": "images/Game-thumb.png", "image": "images/Game-image.jpg",
            "video": "videos/Game-video.mp4"}


def current_asset(slot, standard=True, name=None, size=9000, linked=True, **kw):
    std = f"{SYS}/{STANDARD[slot]}"
    path = f"{SYS}/images/{name}" if name else std
    cand = AssetCandidate(slot=slot, url=f"current://{slot}", label="Current", source="current", current=True,
                          width=kw.pop("width", 600), height=kw.pop("height", 300), duration=kw.pop("duration", 0.0))
    return CurrentAsset(cand, path, std, standard, size, b"png", duration=cand.duration, tag_linked=linked)


def current(**kw):
    pictures = {"logo": None, "boxart": None, "image": None}
    video = kw.pop("video", None)
    pictures.update(kw.pop("pictures", {}))
    fields = {tag: "" for tag in ("name", "desc", "releasedate", "developer", "publisher", "genre", "players")}
    fields.update(kw.pop("fields", {}))
    return CurrentAssets(fields=fields, pictures=pictures, video=video)


def pick(slot="logo", included=True, cand=NEW, data=b"\x89PNGnew", note="600×300 PNG · transparent ✓", warning="", image="QI"):
    return PicturePick(slot=slot, included=included, candidate=cand, data=data, note=note, warning=warning, image=image)


def plan(cur=None, pictures=(), video=None, texts=None, **kw):
    return build_plan("Game", SYS, cur or current(), list(pictures), video, texts or {}, **kw)


def left(p):
    return {item.name: item.reason for item in p.left_alone}


# ---------------------------------------------------------------- pictures

def test_a_new_picture_for_an_empty_slot_is_created():
    p = plan(pictures=[pick()])
    item = p.pictures[0]
    assert item.slot == "logo" and item.name == "Logo" and item.tag == "marquee" and item.data == b"\x89PNGnew"
    assert item.label == "Transparent logo" and item.source == "Steam" and item.image == "QI"
    assert item.current is None and item.current_image is None and item.replaces == "" and item.size_text.endswith("B")
    assert p.replace == set() and p.replaced_files == [] and not p.is_empty


def test_a_new_picture_over_a_current_one_replaces_the_standard_file():
    cur = current(pictures={"logo": current_asset("logo")})
    p = plan(cur, [pick()])
    item = p.pictures[0]
    assert p.replace == {"marquee"} and item.replaces == "images/Game-marquee.png" and p.replaced_files == ["images/Game-marquee.png"]
    assert item.current is cur.pictures["logo"] and "600×300" in item.current_size_text
    assert item.repoints == ""


def test_the_current_picture_picture_is_passed_on_for_the_side_by_side_view():
    cur = current(pictures={"logo": current_asset("logo")})
    p = build_plan("Game", SYS, cur, [pick()], None, {}, current_images={"logo": "OLD-QIMAGE"})
    assert p.pictures[0].current_image == "OLD-QIMAGE"


def test_replacing_art_with_another_name_repoints_and_keeps_the_old_file():
    cur = current(pictures={"logo": current_asset("logo", standard=False, name="my art.png")})
    p = plan(cur, [pick()])
    item = p.pictures[0]
    assert p.replace == {"marquee"} and item.replaces == "" and p.replaced_files == []
    assert item.repoints == "images/my art.png"


def test_a_non_standard_current_plus_a_standard_file_replaces_the_standard_one_too():
    cur = current(pictures={"logo": current_asset("logo", standard=True, name="my art.png")})
    item = plan(cur, [pick()]).pictures[0]
    assert item.replaces == "images/Game-marquee.png" and item.repoints == "images/my art.png"


def test_current_shown_writes_nothing_even_when_included():
    cur = current(pictures={"logo": current_asset("logo")})
    p = plan(cur, [pick(cand=CUR_LOGO, data=b"\x89PNGcurrent")])           # data present: still never written
    assert p.pictures == [] and p.replace == set() and p.is_empty
    assert "Current" in left(p)["Logo"]


def test_a_current_marked_candidate_is_never_written_whatever_the_panel_says():
    forged = AssetCandidate(slot="logo", url="https://x/y.png", label="x", source="Steam", current=True)
    assert plan(pictures=[pick(cand=forged)]).pictures == []


def test_unticked_include_leaves_the_slot_alone():
    cur = current(pictures={"logo": current_asset("logo")})
    p = plan(cur, [pick(included=False)])
    assert p.pictures == [] and "Include" in left(p)["Logo"]


def test_a_picture_that_could_not_be_loaded_is_left_out_and_says_so():
    p = plan(pictures=[pick(data=None)])
    assert p.pictures == [] and "could not be loaded" in left(p)["Logo"]


def test_an_empty_slot_with_nothing_chosen_is_listed_as_left_alone():
    p = plan(pictures=[pick(cand=None, data=None)])
    assert "nothing" in left(p)["Logo"].lower()


def test_warnings_travel_with_the_item():
    item = plan(pictures=[pick(warning="this picture has a solid background")]).pictures[0]
    assert item.warning == "this picture has a solid background"


def test_slots_keep_the_order_logo_box_art_image():
    p = plan(pictures=[pick("image", cand=AssetCandidate(slot="image", url="u", label="s", source="Steam")),
                       pick("logo")])
    assert [i.slot for i in p.pictures] == ["logo", "image"]


# ---------------------------------------------------------------- the trailer

def vid(**kw):
    cand = kw.pop("cand", AssetCandidate(slot="video", url="https://x/t.m3u8", label="Trailer", source="Steam", kind="hls",
                                         duration=kw.pop("duration", 0.0)))
    return VideoPick(included=kw.pop("included", True), candidate=cand, start=kw.pop("start", 5.0), end=kw.pop("end", 65.0),
                     thumb=kw.pop("thumb", None))


def test_a_new_trailer_lists_source_start_end_and_length():
    p = plan(video=vid())
    v = p.video
    assert (v.source, v.label, v.start, v.end, v.length) == ("Steam", "Trailer", 5.0, 65.0, 60.0)
    assert "0:05" in v.start_text and "1:05" in v.end_text and v.length_text == "1:00 (60 s)"
    assert v.current is None and v.replaces == "" and p.replace == set() and not p.is_empty


def test_the_length_is_cut_at_the_maximum():
    v = plan(video=vid(start=0.0, end=500.0), max_seconds=120).video
    assert v.length == 120.0


def test_a_whole_trailer_uses_its_known_length_or_says_up_to():
    assert plan(video=vid(end=None, start=0.0, duration=45.0)).video.length == 45.0
    assert plan(video=vid(end=None, start=0.0, duration=900.0), max_seconds=120).video.length == 120.0
    unknown = plan(video=vid(end=None, start=0.0, duration=0.0), max_seconds=120).video
    assert unknown.length is None and "up to 2:00" in unknown.length_text and "end" in unknown.end_text.lower()


def test_replacing_a_trailer_shows_the_current_one():
    cur = current(video=current_asset("video", duration=83.0, size=14_000_000, width=0, height=0))
    p = plan(cur, video=vid())
    v = p.video
    assert p.replace == {"video"} and v.current is cur.video and "1:23" in v.current_text and "14" in v.current_text
    assert v.replaces == "videos/Game-video.mp4" and p.replaced_files == ["videos/Game-video.mp4"]


def test_current_trailer_shown_or_unticked_writes_nothing():
    cur = current(video=current_asset("video"))
    cur_cand = cur.video.candidate
    shown = plan(cur, video=VideoPick(included=True, candidate=cur_cand, start=0.0, end=None))
    assert shown.video is None and "Current" in left(shown)["Trailer"]
    off = plan(cur, video=vid(included=False))
    assert off.video is None and "Include" in left(off)["Trailer"]
    none = plan(video=None)
    assert none.video is None and "Trailer" in left(none)


def test_a_thumbnail_is_passed_on():
    assert plan(video=vid(thumb="THUMB")).video.thumb == "THUMB"


# ---------------------------------------------------------------- text

def test_only_changed_text_fields_are_listed_with_old_and_new():
    cur = current(fields={"name": "Cinder Citadel", "desc": "Old text.", "releasedate": "20200102T000000"})
    p = plan(cur, texts={"name": "Cinder Citadel", "desc": "New text.", "releasedate": "20260218T000000", "genre": "Rogue", "developer": "  "})
    rows = {t.tag: t for t in p.texts}
    assert list(rows) == ["desc", "releasedate", "genre"]                      # the gamelist order; name/developer unchanged
    assert (rows["desc"].label, rows["desc"].old, rows["desc"].new) == ("Description", "Old text.", "New text.")
    assert (rows["releasedate"].old, rows["releasedate"].new) == ("2020-01-02", "2026-02-18")
    assert rows["genre"].old == ""
    assert p.fields == {"desc": "New text.", "releasedate": "20260218T000000", "genre": "Rogue"}


def test_text_is_never_changed_on_the_way():
    nasty = "<img src=x onerror=alert(1)> & <b>bold</b>\nsecond line"
    p = plan(texts={"desc": nasty})
    assert p.texts[0].new == nasty and p.fields["desc"] == nasty


def test_text_only_plan_is_not_empty():
    p = plan(texts={"name": "X"})
    assert not p.is_empty and p.pictures == [] and p.video is None


def test_nothing_chosen_is_an_empty_plan_with_reasons():
    cur = current(pictures={"logo": current_asset("logo")}, fields={"name": "Same"})
    p = plan(cur, [pick(cand=CUR_LOGO), pick("boxart", cand=None, data=None)], video=None, texts={"name": "Same"})
    assert p.is_empty and p.fields == {} and p.replace == set()
    assert set(left(p)) == {"Logo", "Box art", "Trailer"}


# ---------------------------------------------------------------- helpers in logic

def test_clip_length_rules():
    assert logic.clip_length(5, 65, 0, 120) == 60
    assert logic.clip_length(0, 500, 0, 120) == 120
    assert logic.clip_length(0, None, 45, 120) == 45
    assert logic.clip_length(0, None, 900, 120) == 120
    assert logic.clip_length(0, None, 0, 120) is None


def test_size_text():
    assert logic.size_text(0) == "0 B" and logic.size_text(900) == "900 B"
    assert logic.size_text(2048) == "2 KB" and logic.size_text(14_000_000) == "14 MB" and logic.size_text(1_500_000) == "1.5 MB"


# ---------------------------------------------------------------- existing files the game list does not point at

def test_an_unlinked_current_picture_that_is_included_is_linked_not_rewritten():
    cur = current(pictures={"logo": current_asset("logo", linked=False)})
    p = plan(cur, [pick(cand=CUR_LOGO, data=None)], texts={})
    assert [(i.slot, i.name, i.tag, i.path) for i in p.links] == [("logo", "Logo", "marquee", "images/Game-marquee.png")]
    assert p.link_tags == {"marquee"} and p.pictures == [] and p.replace == set() and p.replaced_files == []
    assert not p.is_empty and "Logo" not in left(p)
    assert p.links[0].current is cur.pictures["logo"] and "600×300" in p.links[0].current_text


def test_the_preview_of_a_link_comes_from_the_current_image():
    cur = current(pictures={"logo": current_asset("logo", linked=False)})
    p = build_plan("Game", SYS, cur, [pick(cand=CUR_LOGO, data=None)], None, {}, current_images={"logo": "OLD"})
    assert p.links[0].image == "OLD"


def test_an_unlinked_picture_with_include_unticked_stays_unlinked_and_says_so():
    cur = current(pictures={"logo": current_asset("logo", linked=False)})
    p = plan(cur, [pick(cand=CUR_LOGO, data=None, included=False)])
    assert p.links == [] and "game list" in left(p)["Logo"] and p.is_empty


def test_an_unlinked_picture_replaced_by_a_new_one_is_a_replacement_not_a_link():
    cur = current(pictures={"logo": current_asset("logo", linked=False)})
    p = plan(cur, [pick()])
    assert p.links == [] and p.replace == {"marquee"} and p.replaced_files == ["images/Game-marquee.png"]


def test_a_linked_current_picture_needs_nothing():
    cur = current(pictures={"logo": current_asset("logo", linked=True)})
    p = plan(cur, [pick(cand=CUR_LOGO, data=None)])
    assert p.links == [] and p.is_empty and "Logo" in left(p)


def test_an_unlinked_current_video_is_linked():
    cur = current(video=current_asset("video", linked=False, duration=45.0))
    p = plan(cur, video=VideoPick(included=True, candidate=cur.video.candidate, start=0.0, end=None))
    assert [(i.slot, i.tag, i.path) for i in p.links] == [("video", "video", "videos/Game-video.mp4")] and p.video is None
    assert "0:45" in p.links[0].current_text and p.link_tags == {"video"} and "Trailer" not in left(p)
    off = plan(cur, video=VideoPick(included=False, candidate=cur.video.candidate, start=0.0, end=None))
    assert off.links == [] and "game list" in left(off)["Trailer"]


def test_summary_counts_what_will_happen():
    cur = current(pictures={"logo": current_asset("logo"), "boxart": current_asset("boxart"), "image": current_asset("image", linked=False)},
                  fields={"name": "x"})
    picks = [pick("logo"), pick("boxart", cand=AssetCandidate(slot="boxart", url="u", label="b", source="Steam")),
             pick("image", cand=AssetCandidate(slot="image", url="current://image", current=True), data=None)]
    p = plan(cur, picks, texts={"name": "y", "genre": "g"})
    assert p.summary() == "2 files replaced, 2 details, 1 file linked"
    new = plan(pictures=[pick()])
    assert new.summary() == "1 new file"
    assert plan().summary() == ""
