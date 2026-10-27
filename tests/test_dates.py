import pytest

from scrappy.dates import from_gamelist, to_gamelist


@pytest.mark.parametrize("text, expected", [
    ("2026-02-18", "20260218T000000"),
    ("2026/2/8", "20260208T000000"),
    ("2026.02.18", "20260218T000000"),
    ("  2026-02-18  ", "20260218T000000"),
    ("2026", "20260101T000000"),
    ("2026-03", "20260301T000000"),
    ("20260218", "20260218T000000"),
    ("20260218T000000", "20260218T000000"),               # already in the gamelist format
    ("2026-02-18T10:30:00Z", "20260218T000000"),           # the time of day is dropped
    ("2026-02-18 10:30", "20260218T000000"),
    ("2024-02-29", "20240229T000000"),                     # leap day
])
def test_to_gamelist_numeric_formats(text, expected):
    assert to_gamelist(text) == expected


@pytest.mark.parametrize("text, expected", [               # the shapes the Steam store page uses
    ("Feb 18, 2026", "20260218T000000"),
    ("18 Feb, 2026", "20260218T000000"),
    ("February 18, 2026", "20260218T000000"),
    ("Sept. 5, 2026", "20260905T000000"),
    ("Feb 2026", "20260201T000000"),
])
def test_to_gamelist_steam_formats(text, expected):
    assert to_gamelist(text) == expected


@pytest.mark.parametrize("text", [
    "", None, "   ", "soon", "Coming soon", "To be announced", "Q3 2026", "TBA 2026",
    "2026-02-30", "2025-02-29", "2026-13-01", "2026-00-10", "2026-01-00", "2026-04-31",   # not real calendar days
    "1969-12-31", "2101-01-01", "0000-01-01",                                              # outside what ES can show
    "2026-02-18abc", "abc2026-02-18", "2026-02-18-05", "2026-2-18-5",                      # trailing / leading junk
    "18.02.2026", "02/18/2026",                                                            # day/month first: ambiguous, refused
    "202", "20261", "2026021", "٢٠٢٦-٠٢-١٨",                                                # not four ASCII digits
    "2026-02-18\n2026-02-19", 20260218, 2026, ["2026"], {"a": 1},
])
def test_to_gamelist_rejects_garbage(text):
    assert to_gamelist(text) == ""


@pytest.mark.parametrize("value, expected", [
    ("20260218T000000", "2026-02-18"),
    ("20260218", "2026-02-18"),
    ("20260218T101500", "2026-02-18"),
    ("", ""), (None, ""), ("garbage", ""), ("2026-02-18", ""), ("20261399T000000", ""),
    ("19000101T000000", ""), (123, ""),
])
def test_from_gamelist(value, expected):
    assert from_gamelist(value) == expected


def test_roundtrip_is_stable():
    for text in ("2026-02-18", "Feb 18, 2026", "2026"):
        once = to_gamelist(text)
        assert to_gamelist(from_gamelist(once)) == once
