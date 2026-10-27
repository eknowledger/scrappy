# Providers: the contract today, and the plan for more sources

A provider is a source of game information: titles to match, then text and picture or video candidates for a game.

* **Part 1 describes the code as it is.** Everything in it was read from the source files named in it.
* **Part 2 is a plan. None of it is implemented.** It is written down so that the limitation "only Steam and YouTube are sources;
  games Steam does not sell get no match" ([KNOWN-LIMITATIONS.md](KNOWN-LIMITATIONS.md)) has a direction. The same steps are in
  [../ROADMAP.md](../ROADMAP.md). Nothing here is a promise, and the order can change.

## Part 1. What exists today

### 1.1 The contract

Defined in `scrappy/providers/base.py` and `scrappy/models.py`.

| Piece | What it is |
|---|---|
| `Provider` (`providers/base.py`) | The base class. A subclass sets `name` (shown in the window, stored in `Match.source`) and implements `search(title) -> list[Match]` and `fetch(match) -> GameInfo`. Both are synchronous and may block; the caller runs them on a worker thread. Both must raise `ProviderError` for any failure the person can be told about and must not leak raw network or parsing exceptions |
| `ProviderError` | An exception whose text is a plain sentence for the user. `describe_error(exc)` returns that text, or `"did not answer in time"` for an empty `TimeoutError`, or `"<ExceptionType>: <message>"` for anything else |
| `Match` (`models.py`) | One search hit: `title`, `source` (the provider's `name`), `source_id` (what `fetch` needs), `score` (0 to 1, optional) and `year` (optional text) |
| `GameInfo` | The details of one game: `name`, `description`, `release_date` (`YYYYMMDDT000000`), `developer`, `publisher`, `genre`, `players`, `source`, `source_id`, and `assets`, a list of candidates. `assets_for(slot)` filters by slot |
| `AssetCandidate` | One possible picture or video: `slot` (`logo`, `boxart`, `image` or `video`, from `models.SLOTS`), `url`, `label`, `source`, `width`, `height`, `transparent`, `kind` (`file`, `hls` or `youtube`), `thumb_url`, `duration` and `current` (reserved for what the game has now; a provider never sets it) |

What a provider can supply is therefore fixed by these types: the seven text fields the saver may write and candidates for the four
slots. There is no field for capabilities, hosts, settings or a version.

The slots map to `gamelist.xml` tags: `logo` to `marquee`, `boxart` to `thumbnail`, `image` to `image`, `video` to `video`
(`models.SLOT_TAGS`). A provider cannot write anything itself; the saver is the only writer ([ARCHITECTURE.md](ARCHITECTURE.md), section 8).

### 1.2 How a search runs, merges and ranks

`scrappy/search.py` and `Controller.search` in `scrappy/controller.py`:

1. `search_matches(providers, title, timeout, cancelled)` starts one daemon thread per provider. Each writes into its own `Future`, so
   nothing is shared between providers.
2. The timeout is one deadline for the whole search (`search_timeout` in `config.json`, 20 seconds by default), not one per provider.
   `cancelled` is polled every 50 ms. A provider that has not answered when the deadline passes is reported as "did not answer in time".
3. A provider's answer must be a list or tuple; anything in it that is not a `Match` is dropped. Any other answer, or any exception,
   counts as that provider failing.
4. The matches of all providers are ranked: a provider's `score` is used only if it is a finite number above 0 and at most 1; otherwise
   `titlematch.title_score(query, match.title)` is computed (it lowers the score for words like soundtrack or demo that were not
   asked for, and for a different number such as "Chapter 4"). Duplicates are identified by `(source, source_id)` and the best score
   wins. The sort is stable, so provider order breaks ties.
5. The result is a `SearchOutcome`: `matches`, `errors` (provider name to plain message, only for providers that failed) and `statuses`
   (one `ProviderStatus` per provider).

YouTube is **not** a `Provider` today. `Controller.search` asks `YtDlp` (`providers/youtube.py`) for video candidates on its own
thread at the same time, with its own timeout (`youtube_search_timeout`), and returns them in `SearchResults.videos` next to the matches;
its failure goes into the same `errors` dictionary under the name "YouTube".

Loading a game: the window shows the matches best first and loads the details of **the one match the person picks**
(`Controller.load_info` calls `fetch` on the provider whose `name` equals `match.source`). `search.merge_infos` (first non-empty value
of each text field wins, all candidates kept) and `search.rank_assets` (best candidates first per slot: 16:9 for `image`, portrait for
`boxart`, transparent for `logo`, then area) exist; the window uses `rank_assets`, and does not call `merge_infos`. So today a second
provider does not enrich a Steam match.

### 1.3 How errors are shown

* A provider's failure never blocks the others. In the window, `format_errors` joins them as `Provider: message; Provider: message`.
  With matches found, the status line ends with "Problems: ..."; with none, it says "Nothing found for ..." followed by the problems in
  parentheses, and suggests another spelling or Paste link / Choose file.
* A failure in `fetch` shows "Could not load details: <message>".
* `ProviderError` messages are written for the user (Steam's say, for example, that Steam could not be reached or asked to slow down).

### 1.4 What the downloader enforces about hosts

Pictures and picture-like files go through `scrappy/download.py` (`Controller.fetch_bytes`). It enforces, for every provider alike:

* `http` and `https` only, also after every redirect; no credentials in the address; no cookies; no proxy variables; an `https`
  page may not redirect to `http`.
* **Public addresses only.** The name is resolved once, every resulting address must be public (loopback, private, link-local and other
  special ranges are refused), and the connection goes to exactly the address that was checked.
* Limits on size (25 MB), time (connect, read and one total deadline), redirects and header size. After the download,
  `Controller.fetch_bytes` keeps only bytes that start like a picture, and pictures are decoded only in the formats of `ALLOWED_FORMATS`.

It does **not** restrict which public host a provider may use. Host rules live in the providers today: `steam.py` keeps only `https`
addresses on `*.steamstatic.com` and `*.akamaihd.net` (`ASSET_HOST_SUFFIXES`). For video, `Controller` trusts Steam and YouTube hosts
by name (`TRUSTED_VIDEO_HOST_SUFFIXES`) and requires any other host to resolve to public addresses only; ffmpeg then opens the address
itself, so that check is a guard against honest mistakes ([../SECURITY.md](../SECURITY.md)). A provider's own API requests (Steam's
store search and details) are made by the provider with its own `requests.Session`, not through the downloader.

### 1.5 Secrets

`scrappy/config.py` already has the rule for API keys: nothing in the program needs one today. A name in `config.json` that looks like a
secret (key, token, secret, passw, credential) is not loaded and is refused when saved. A key would go in a separate
`<data folder>/secrets.json` written with mode 0600, or in an environment variable. Keys must never appear in logs, in error messages
or in a dump of the settings.

### 1.6 Write a provider in the tree today

This is the recipe from [ARCHITECTURE.md](ARCHITECTURE.md#111-add-a-provider), with the contract above:

1. Create `scrappy/providers/<name>.py` with a subclass of `Provider`: `name`, `search(title)` and `fetch(match)`.
2. Treat every field of the answer as untrusted: check types, strip control characters, keep only `https` addresses on hosts you list,
   cap list lengths, turn HTML into plain text. Turn every failure into a `ProviderError` with a plain sentence. Use `steam.py` as the model.
3. Set `Match.score` with `titlematch.title_score(query, title)`.
4. Give `fetch` candidates with true sizes where known, `transparent=True` only when the source guarantees it, and a label that tells
   the person where a picture comes from.
5. Add the provider to the default list in `Controller.__init__` (today `[SteamProvider()]`; the `providers` argument replaces the list, and the tests use it).
6. Tests first: capture or write answers into `tests/fixtures/` (see `tests/fixtures/README.md`), test the parser against them and
   against hostile input, and test the provider with a fake session as `tests/test_steam.py` does.
7. Docs: the host in the README table "What it contacts" and in `SECURITY.md`, the `CHANGELOG.md`, and `docs/USAGE.md` if the person
   sees something new. `tests/test_docs.py` checks that every host in the code is in the README table.

`tools/real_showcase.py` has a small stand-in provider (`LibretroProvider`) that serves cached pictures for the screenshots. It is a
test helper, not a source of the program.

## Part 2. The plan (not implemented)

The steps are in order. Each can ship without the next.

### 2.1 More built-in providers

| Provider | Gives | Notes |
|---|---|---|
| libretro thumbnails | box art and in-game pictures (title screen and screenshot) for retro systems | No key. Pictures are found by game and system name, so the match depends on the ROM naming. This is the step that addresses "older console games get no match" |
| Wikidata and Wikipedia | description, release date, developer, publisher, genre | No key. Text only; useful next to a provider that has pictures |
| SteamGridDB | transparent logos, box art | Needs a free API key |
| IGDB | text and pictures for console games | Needs a free API key (it is issued through Twitch) |
| Others | whatever people ask for in an issue | Each one adds a host to the README table and to `SECURITY.md` |

For every built-in provider: the same untrusted-input rules as `steam.py`, fixtures and tests before code, and the keys of the two that
need them kept out of `config.json`, logs and settings dumps (section 1.5).

Because the window loads one match, a text-only provider is useful only after merging is wired in (see "Open questions").

### 2.2 A stable, versioned provider contract

Turn the informal contract of part 1 into one that can be depended on:

* **`provider_api`**: an integer version. A provider declares the version it was written for; the program refuses one it does not support
  and says why.
* **Capabilities**, declared per provider: `text`, `logo`, `boxart`, `image`, `video`. The window uses them to say which slots a source can
  fill, to skip it for others, and to show in the status line what was asked of whom.
* **Hosts**, declared per provider. The downloader and the video check enforce them: a request to a host the provider did not declare is
  refused, so a provider cannot reach other hosts. Public-address rules stay as they are.
* **Settings** per provider in `config.json` (for example `providers.<name>.enabled`, a timeout, a language), validated like the
  existing settings, with enable and disable per provider. Secrets stay out of `config.json`.
* **Timeouts and error isolation** per provider: its own deadline, its own failure message, and an exception, a bad answer or a hang that
  affects no other provider and is never shown as a raw traceback. Today's search already isolates failures by thread; the new part is a
  per-provider timeout and the declared limits.
* YouTube becomes a provider of the `video` capability under the same contract, instead of the special path of section 1.2.

### 2.3 Declarative providers

The first plugin kind, chosen because it needs no third-party code to run: a small JSON or YAML file that describes a REST/JSON source.

* Search and details URL templates (`{title}`, `{id}`), fixed query parameters, and the hosts used.
* A field mapping from the JSON answer to `Match` and `GameInfo` fields and to candidates (JSON paths; a few fixed transforms such as
  "join a list", "take the year", "strip HTML"). No expressions, no scripting.
* Limits applied by the program, not the file: response size, list lengths, timeouts, https only, the declared hosts.
* An optional key reference by name (read from `secrets.json` or the environment, never from the description file).

The engine runs the description through the same checks as a built-in provider, so a mistake in a file produces an error message and not a
crash.

### 2.4 Code plugins

For sources a declarative file cannot describe.

* **Place:** a drop-in folder on the box, `/userdata/system/scrappy/providers/`, not touched by the installer's updates.
* **Manifest** next to the code: name, version, `provider_api`, hosts, capabilities, author, licence, and the sha256 of the files.
* **Management:** list, install and remove from the command line and from the Tools menu. Nothing is loaded that the person did not
  enable.
* **Trust model, stated plainly.** A code plugin is Python running as root on the person's machine, with the program's permissions. The
  declared hosts are enforced for requests that go through the program's downloader and HTTP helpers; they cannot stop code that opens its
  own sockets or starts programs. So: the program shows what a plugin declares before it is enabled, requires an explicit enable step,
  pins the hashes in the manifest and refuses a changed file until it is enabled again, and never updates a plugin by itself.
* Plugins get the helper objects (a downloader bound to their declared hosts, a settings reader, a logger that redacts secrets) and are
  documented to use them.

### 2.5 A curated index

Later: a repository of plugin manifests, so people can browse and install community providers from the program (or the command line).
Entries would be reviewed before listing, carry the hash of the exact files they refer to, and be fetched only when the person asks. A
small marketplace with ratings is possible on top of that. This part needs a hosting and review policy first and has the most open
questions.

### Security considerations

* A provider's answer is untrusted input: types, lengths, control characters, bidi marks, HTML and addresses are checked before use.
* Addresses go through the downloader (public hosts only). The host list a provider declares narrows that further; it never widens it.
* A video address that ffmpeg opens is a weaker guard (ffmpeg resolves the name again); a plugin may not declare a host to make it
  trusted by name.
* API keys: only in `secrets.json` (mode 0600) or the environment; never in `config.json`, a manifest, a log line, an error message
  or an issue report. Settings dumps print a key as set or not set.
* Code plugins run as root on the box. Enabling is explicit, hashes are pinned, and nothing updates itself.
* A plugin cannot write to the library; it returns data and the saver is still the only writer. The review window still shows every
  picture and every changed detail before anything is written.
* Every new host is listed in the README table "What it contacts" and in `SECURITY.md`, as for built-in sources. A provider that is not
  enabled makes no request.
* Terms of service and rate limits of each source are respected: one request per action, no bulk scraping, and the label of each
  candidate names its source.

### Open questions

* **Merging.** The window loads one match. A text-only source (Wikidata) next to a picture source (libretro, SteamGridDB) needs either
  "the same game from several providers" matching, or loading `fetch` of several providers and calling `merge_infos`. Which game
  counts as the same, and which provider's value wins for a field, is undecided.
* **System awareness.** `Provider.search(title)` gets no system. Retro pictures are found by system and title, so the contract probably
  needs the system name (or platform) as a second argument. That is a `provider_api` change.
* **Declarative expressiveness.** How much mapping (conditions, joins of two requests, pagination) before a file becomes a program?
* **Where hosts are enforced for plugins** that use their own HTTP library, and whether to require them to use the program's helpers.
* **Keys for community providers:** one `secrets.json` entry per provider name, or a per-provider file?
* **The index:** who reviews, how entries are revoked, and whether a signature adds anything over a pinned hash.
* **Compatibility:** how long an old `provider_api` stays supported.
