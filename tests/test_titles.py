import pytest
from scrappy.titles import clean_title, title_id

CASES = {
    "Tidepool Tactics [0100AAAA00000000][v0] [example.com].nsp": "Tidepool Tactics",
    "Marmalade Moon꞉ Chapter 2 [0100AAAA00001000][v0].nsp": "Marmalade Moon: Chapter 2",
    "Hello, Orbit Otters! [0100BBBB00002000][v0][Base].nsp": "Hello, Orbit Otters!",
    "Lantern Lagoon - Night Shift [0100BBBB00003000][v0][Base].nsp": "Lantern Lagoon - Night Shift",
    "Starling Skyway Switch Edition [0100AAAA00004000] + (v1114112 UPD) + (38 DLC) (eshop).xci": "Starling Skyway Switch Edition",
    "Bramble’s Bandits 3[0100BBBB00005000][US][v0].nsp": "Bramble’s Bandits 3",
    "Kettle Kingdom Racers [0100AAAA00006000]+[v1.1.0](example)_patched.xci": "Kettle Kingdom Racers",
    "[example.com] Orbit_Otters_Deluxe_v1.3.0_.xci": "Orbit Otters Deluxe",
    "Quasar Quokka Jump and Run Traps and Treasures 2 [0100BBBB00007000][v65536][US](example.com).nsp":
        "Quasar Quokka Jump and Run Traps and Treasures 2",
    "MARMALADE MOON 26 [0100AAAA00008000][v1.0.0][US][Base].nsp": "MARMALADE MOON 26",
    "Cinder Citadel Chronicles North.wine": "Cinder Citadel Chronicles North",
    "Tidepool Tactics Chapter 4 v1.3[0100AAAA00000800][v196608][UPD].nsp": "Tidepool Tactics Chapter 4",
    "Tidepool Tactics Chapter 4 DLC Pack [0100AAAA00001001][v0][DLC].nsp": "Tidepool Tactics Chapter 4 DLC Pack",
    "Dr. Quokka": "Dr. Quokka",
    "Super Quokka Bros. Wonder.nsp": "Super Quokka Bros. Wonder",
    "Starling Skyway Adventure [0100BBBB00009000]+[v1.1.0] (example).xci": "Starling Skyway Adventure",
    "./Tidepool Tactics/Tidepool Tactics Chapter 1 [0100AAAA0000A000][v0][US](example.com).nsp": "Tidepool Tactics Chapter 1",
    "Orbit Otters the Red-Nosed Hero (NSP)(eShop).nsp": "Orbit Otters the Red-Nosed Hero",
    "Kettle Kingdom All-Star Kart Racing [0100BBBB0000B000][v0][US](example.com).xci": "Kettle Kingdom All-Star Kart Racing",
    # only known ROM extensions are stripped
    "Dr.Quokka": "Dr.Quokka",
    "Quokka 2.0": "Quokka 2.0",
    "Vol.2": "Vol.2",
    "Dr.Quokka.nsp": "Dr.Quokka",
    "GAME.NSP": "GAME",
    # tag-only names fall back to the file stem
    "[Base].nsp": "[Base]",
}


@pytest.mark.parametrize("filename,expected", list(CASES.items()))
def test_clean_title(filename, expected):
    assert clean_title(filename) == expected


def test_title_id():
    assert title_id("Tidepool Tactics [0100aaaa00000000][v0].nsp") == "0100AAAA00000000"
    assert title_id("Dr. Quokka") is None


@pytest.mark.parametrize("filename", ["[0100AAAA0000000]", "[0100AAAA000000000]"])
def test_title_id_wrong_length(filename):
    assert title_id(filename) is None
