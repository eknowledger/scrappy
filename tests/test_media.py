from scrappy.media import media_abs_paths, media_rel_paths, rom_stem


def test_rom_stem():
    assert rom_stem("./Marmalade Moon [0100AAAA00000800][v65536].nsp") == "Marmalade Moon [0100AAAA00000800][v65536]"
    assert rom_stem("./Tidepool Tactics/Tidepool Tactics [0100AAAA00000000][v0] [example.com].nsp") == \
        "Tidepool Tactics [0100AAAA00000000][v0] [example.com]"
    assert rom_stem("./Game.wine") == "Game"
    assert rom_stem("./Dr. Quokka") == "Dr. Quokka"   # '. Quokka' is not an extension


def test_rel_paths_match_batocera_scraper():
    rel = media_rel_paths("./Marmalade Moon [0100AAAA00000800][v65536].nsp")
    assert rel == {
        "image": "./images/Marmalade Moon [0100AAAA00000800][v65536]-image.jpg",
        "thumbnail": "./images/Marmalade Moon [0100AAAA00000800][v65536]-thumb.png",
        "marquee": "./images/Marmalade Moon [0100AAAA00000800][v65536]-marquee.png",
        "video": "./videos/Marmalade Moon [0100AAAA00000800][v65536]-video.mp4",
    }


def test_abs_paths(tmp_path):
    ab = media_abs_paths(str(tmp_path), "./A.nsp")
    assert ab["image"] == str(tmp_path / "images" / "A-image.jpg")
    assert ab["video"] == str(tmp_path / "videos" / "A-video.mp4")
