# Test fixtures

Answers from the Steam and YouTube services, stored so the tests run offline.

| Files | What | How made |
|---|---|---|
| `steam_search_openttd.json`, `steam_details_1536610.json`, `steam_browse_1536610.json` | OpenTTD (a free, open-source game on Steam): search, store details, library art | captured with `tools/capture_fixtures.py openttd` |
| `steam_search_portal2.json`, `steam_details_620.json`, `steam_browse_620.json` | Portal 2, a Valve game used as a public sample: an older game with library box art and many trailers | captured with `tools/capture_fixtures.py portal2` |
| `steam_search_nothing.json`, `steam_details_missing.json`, `steam_browse_missing.json` | a search with no result, an app id that does not exist | captured with `tools/capture_fixtures.py missing` |
| `youtube_search_supertuxkart.jsonl` | the flat `yt-dlp` search result for "SuperTuxKart official trailer" (the project's own channel) | captured with `tools/capture_fixtures.py youtube` |
| `steam_search_dlc.json`, `steam_details_9000002.json`, `steam_browse_9000002.json`, `steam_browse_9000001.json` | an add-on (DLC) and its base game, with no library art of their own | **anonymised, not captured**: the structure of a real Steam answer for an add-on, with the game name, developer, publisher, description text, app ids, image hashes and movie ids replaced by invented ones |

The add-on case is anonymised because no free game with an add-on and no library art was at hand; the tests that use it
(`tests/test_steam.py`) check the shape of the data (types, fields, hosts, counts), not facts about a real game.

Captured files are stored as Steam or yt-dlp returned them, except that `appdetails` drops fields the parser never reads
(`DETAILS_DROP` in the tool) and the YouTube answer drops `__x_forwarded_for_ip`. Re-capturing needs network access to
`store.steampowered.com`, `api.steampowered.com` and, for YouTube, `yt-dlp`.
