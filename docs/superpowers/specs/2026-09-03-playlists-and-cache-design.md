# YouTube playlists + media cache — design

## Purpose

Two gaps in the Telegram transcription bot, plus one architectural
constraint that shapes how they are built.

1. **Playlists are dropped.** A bare YouTube link is only accepted when it
   carries a `?v=` id; a `/playlist?list=…` link is rejected, and a
   `watch?v=X&list=Y` link keeps only `X`. Sending a playlist should add
   every video to the batch and produce one combined summary.
2. **The media cache is unbounded and re-does work.** `media/` (forwarded
   files) is never cleaned — it holds 1.1 GB of duplicates because the
   on-disk name is prefixed with `<sender>_<message>`, so the same video
   forwarded twice is stored, and transcribed, twice. `input/` (YouTube
   audio) is deduped by title but also uncapped. There is a transcript
   cache for URLs but not for forwarded files.
3. **The code is the base for a future REST API service.** Not built in
   this change, but the module boundaries are drawn so a FastAPI layer
   drops in later over the same core, and the bot stays free of any
   dependency on the whisper-mlx pipeline code (the only contract remains
   the spool protocol and the shared bind-mounted directories).

## Constraints and decisions

- **`yt-dlp` and browser cookies stay on the host.** The bot container
  has neither. Playlist enumeration therefore goes through the host
  worker as a new spool job type, not a second `yt-dlp` in the container.
- **A playlist expands into N separate batch items**, one per video, each
  a `watch?v=<id>` URL — exactly the URL shape the worker already
  transcribes. No change to `run.sh` or the worker's transcription path;
  the batch model (one summary over all items) already gives "one
  playlist, one summary". Per-item granularity buys individual caching,
  individual retry of a failed video, and the `[i/N]` display.
- **Playlists are capped**, default 100 videos, `PLAYLIST_MAX` in the
  host `.env`. The worker enumerates the whole playlist (`--flat-playlist`
  is one API call regardless of length) and the bot slices — so the true
  length is known and the message can name it:
  `cut from 137 to 100 (PLAYLIST_MAX)`. `--playlist-end` would truncate the
  output and lose the count.
- **Content-addressed file names, no per-user prefix.** `media/` files
  become `<sha1><ext>`. Identical content collapses to one file, so a
  re-forward downloads nothing. The whitelist is the author's own ids;
  content-addressed sharing between them is fine (the old prefix was for
  uniqueness, which the full hash provides).
- **Transcripts are never evicted.** They are kilobytes of text and they
  are what makes a re-forward free even after the video blob is gone.
  Eviction scans `media/` and `input/` only.
- **FIFO by mtime.** Oldest file first, until the total is under the cap.
  `mtime` is not touched on a cache hit — that would make it LRU; FIFO
  was the explicit choice. `CACHE_MAX_GB` in the host `.env`, default
  `100`; `cache_gc.sh` converts to bytes internally.
- **Eviction runs in the worker**, at the top of `process_job` — the one
  serialized actor on the host that touches these trees. Files listed in
  the job about to run are excluded from eviction. `CACHE_MAX_GB` is
  therefore a soft cap: one job downloading a 100-video playlist can
  overshoot it by that playlist's audio before the next gc runs. Accepted;
  calling gc between items would evict the batch's own inputs.
- **`run.sh` skips transcription for a content-addressed file** whose
  `.txt` already exists — i.e. media items only. A URL's output stem is the
  video *title*, and two videos in one playlist can share a title, so a
  title-keyed skip would hand back the wrong transcript and poison the
  cache. URLs keep going through the existing duration check.
- **`output/url-cache.json` → `output/transcript-cache.json`.** It now
  holds both URL keys and `<sha1><ext>` media keys. `worker.sh` does the
  `mv` itself at startup, if and only if the old name exists and the new one
  does not — the bot container and the host worker restart independently, so
  the rename needs an owner that runs before either side reads the file.

## Module layout

The bot keeps its self-contained `bot/` tree and imports no repo code.
New logic lands in a framework-agnostic core so a future HTTP adapter
calls the same functions the Telethon handlers do.

```
bot/src/
  core/
    content.py      # sha1 of a file → on-disk name; dedup check; transcript-cache read
    youtube.py      # is_playlist_url / classify; canonical watch URL; id parsing
    debounce.py     # per-key async timer-accumulator for the "batch of N" message
  batch.py          # unchanged in spirit; + MediaItem.title
  spool.py          # + write_expand_job / await_expand_result
  bot.py            # thin Telethon adapter: event → core call → reply
```

Host side (edits to whisper-mlx files; contract with the bot stays the
spool + shared dirs + the cache JSON):

```
whisper-mlx/
  worker.sh         # + *.expand.json handling; + cache_gc call; media-item transcript-cache write; cache rename
  run.sh            # + skip transcription when a media item's .txt exists
  cache_gc.sh       # new: FIFO trim of media/ + input/ to CACHE_MAX_GB
  worker_selfcheck.sh  # + a cache_gc self-check case
```

### `core/content.py`

- `content_name(path: Path) -> str` — `f"{sha1(path)}{path.suffix}"`.
- `store(tmp: Path, media_dir: Path) -> tuple[str, bool]` — compute the
  name; if `media_dir/name` exists, unlink `tmp` and return
  `(name, False)` (dedup hit); else `os.replace` and return
  `(name, True)`.
- `load_transcript_cache(output_dir: Path) -> dict[str, str]` — read
  `transcript-cache.json`, `{}` on any error. (Moved from `bot.py`'s
  `_load_url_cache`, renamed.)

Pure over paths, no Telethon, no `Config`.

### `core/youtube.py`

- `classify(url: str) -> Literal["video", "playlist", "other"]`.
  - `youtu.be/<id>` or `youtube.com/watch?v=<id>` with no usable
    `list` → `video`.
  - `youtube.com/playlist?list=<id>` or `watch?v=…&list=<id>`, where
    `list` does not start with `RD` → `playlist`.
  - `list=RD…` is a radio/mix: generated and effectively endless, so it is
    never expanded. With a `v=` id it is a `video`, without one `other`.
  - else `other`.
- `video_url(video_id: str) -> str` — canonical `watch?v=` form.
- `playlist_id(url: str) -> str | None`.

### `core/debounce.py`

- `Accumulator` keyed by `sender_id`. `add(key, item)` appends and
  (re)arms a `delay`-second timer (default 2.0). On fire, invokes a
  callback with `(key, list[item])` and clears that key. One in-flight
  timer per key; `asyncio`-based, cancellable.
- Used so a burst of forwards (album or loose) yields a single
  `batch of N videos detected` + `Added [i/N] …` message. `N == 1` still
  goes through it (one-line result).

### `spool.py` additions

- `write_expand_job(jobs_dir, job_id, url)` → `jobs/<id>.expand.json` =
  `{"url": url}`.
- `await_expand_result(jobs_dir, job_id, poll, timeout)` → reads
  `jobs/<id>.result.json` and returns it. Shape:
  `{"videos": [{"id": str, "title": str}, …], "error": str | null}`.
  The worker is serialized behind whatever it is transcribing, so this one
  takes a timeout (unlike the existing waits) and the handler answers
  `expanding playlist…` before it starts waiting.

## Data flow

### Playlist

```
paste playlist URL
  → bot: youtube.classify(url) == "playlist"
  → bot: reply "expanding playlist…"; write_expand_job; await_expand_result
  → worker: yt-dlp --flat-playlist
            --cookies-from-browser $YOUTUBE_BROWSER
            --no-warnings --print "%(id)s\t%(title)s" <url>
            → jobs/<id>.result.json {"videos": [...]}   (full playlist)
            (empty stdout / non-zero → {"videos": [], "error": "..."} )
  → bot: total = len(videos); videos = videos[:PLAYLIST_MAX]
  → bot: for each video: batch.add_media(youtube.video_url(id), title=title)
  → bot: reply "batch of {added} videos detected"
         + ("cut from {total} to {PLAYLIST_MAX} (PLAYLIST_MAX)" if truncated)
         + "Added [i/added] {title}" per line
  → /go: each watch?v=<id> runs through the worker's existing URL branch
         unchanged; one combined summary over the batch
```

`_resolve_cached` already keys on `MediaItem.filename`, so a repeated
playlist or an individual repeated video is a cache hit — no re-download,
no re-transcription.

### Forwarded media

```
forward file(s)
  → bot on_media: download to a temp path
  → core.content.store(tmp, media_dir) → (name, is_new)
  → batch.add_media(name)   (batch-level dedup already collapses a repeat)
  → accumulator.add(sender_id, item)
  → 2s after the last forward: one "batch of N videos detected" + list
```

`N` counts the items actually in the batch, not the forwards received: both
`content.store` and `add_media` collapse a repeat, so three files of which
two are identical announce a batch of 2 and list `[1]` and `[2]`.

### Cache eviction

```
worker process_job (any job kind), before work:
  → cache_gc.sh:
      budget = CACHE_MAX_GB * 1024^3
      files = media/* + input/*  (not output/)
      keep  = filenames referenced by jobs/*.job.json
      total = sum of sizes
      for f in files sorted by mtime ascending, skipping keep:
        if total <= budget: stop
        total -= size(f); rm f
```

### Transcript cache write (worker)

On a successful media item (not only a URL), `worker.sh` records
`<name> → <stem>/<stem>.txt` in `transcript-cache.json`, same helper as
the URL path uses today.

## Error handling

| Situation | Behaviour |
|---|---|
| `yt-dlp` expand fails (private, bad cookies, network) | `result.json` `{"videos": [], "error": "..."}`; bot replies the error, adds nothing |
| Expand waits behind a long transcription | Bot already replied `expanding playlist…`; on timeout it says so and adds nothing — the job file is left for the worker to drain |
| Playlist longer than `PLAYLIST_MAX` | First `PLAYLIST_MAX` added; message notes e.g. `cut from 137 to 100 (PLAYLIST_MAX)` |
| Playlist entry is unavailable at `/go` time | Same as any URL item today: worker sets `error`, `transcript_path` null, continues; `/go` retries just that item |
| `mix`/`radio` `list=RD…` link | Never expanded; with a `v=` id it is added as a single video, otherwise ignored |
| Two playlist entries share a title | Each is still its own `watch?v=` item and its own cache key; only `input/` and `output/` stems collide, which the existing duration check already re-downloads through |
| Media evicted before it was ever transcribed | Re-forward needed; documented tolerance, same class as "batch lost on restart" |
| Media evicted after transcription | `/go` still works — transcript cache fills `transcript_path`, no worker call |
| `cache_gc` cannot get under budget (a job's own files exceed it) | Logged; job proceeds; disk may fill. Accepted — 100 GB of audio-only mp3, even at 100 videos a batch, is not reachable in practice |
| Two forwards race the same content | `content.store` is last-writer-wins on an identical file; both resolve to the same name, harmless |

## Testing

**Unit (`bot/tests/`, spool + `yt-dlp` mocked):**

- `content.py`: identical bytes → identical name; second `store` of the
  same content returns `is_new=False` and does not rewrite the file;
  different content → different name.
- `youtube.py`: `watch?v=`, `youtu.be/`, `/playlist?list=PL…`,
  `watch?v=…&list=PL…`, `watch?v=…&list=RD…`, `/playlist?list=RD…`,
  non-YouTube → correct class.
- `debounce.py`: three `add`s inside the window → one callback with all
  three; an `add` after the window → a second callback; `N=1` path.
- expand: from a mocked `result.json` the bot adds N `MediaItem`s with
  titles, in order; a list longer than `PLAYLIST_MAX` adds exactly
  `PLAYLIST_MAX` and carries the truncation note; `error` set → nothing
  added, error surfaced.
- announced `N` equals the batch size, not the forward count, when the
  burst contains a duplicate.
- `_resolve_cached` fills `transcript_path` from a `<sha1><ext>` key, not
  only a URL key.
- existing batch/queue/merge tests still pass (MediaItem gains an
  optional field only).

**Shell (`worker_selfcheck.sh`):**

- `cache_gc`: seed a temp `media/` + `input/` over budget with staggered
  mtimes, run, assert the oldest are gone, the newest and everything in
  `output/` remain, and a file named in a fixture `job.json` is kept even
  if old. A tree one byte over budget loses one file, not all of them.
- an `*.expand.json` fixture with a mocked `yt-dlp` → assert the
  `result.json` `videos` shape.

**Manual E2E:** paste a real 5-video public playlist, `/go`, confirm one
summary; re-paste it, confirm "cached"; forward the same file twice,
confirm one download and `media/` holds one copy.

## Out of scope (for now)

- The FastAPI layer itself — this change only draws the boundaries.
- Persisting batches across a bot restart — unchanged tolerance.
- Download concurrency limits for a large forward burst — Telethon
  dispatches handlers concurrently today; not made worse here, not fixed
  here.
- LRU / access-time bump on cache hit — FIFO was chosen deliberately, and
  with transcripts exempt from eviction the worst an early eviction costs is
  a re-download, never a re-transcription.
- A hard cache cap — gc runs between jobs, not between a job's own
  downloads.
- Per-video summaries — still only the combined summary.
- A per-user playlist cap — `PLAYLIST_MAX` is one host-wide value.
