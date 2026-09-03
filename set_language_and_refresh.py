#!/usr/bin/env python3
"""Set each movie's preferred metadata language, then force a full metadata
refresh with image redownload for every movie in the library.

Language (L1) resolution, in priority order:
  1. TMDb original_language  - Emby does not expose the original title language
     (OriginalLanguage is always empty on this server), so we look it up from
     TMDb using the movie's Tmdb ProviderId. Requires tmdb_api_key in config.
  2. First audio track language - fallback when no TMDb id/key or the lookup
     yields nothing.

Target language (L2) rule - the metadata download language is NOT L1 directly:
  - if L1 is French or Italian -> L2 = L1 (metadata stays in the original language)
  - otherwise                  -> L2 = English
So L2 is always one of {fr, it, en}.

Pass 1 - language: set/force PreferredMetadataLanguage = L2 on every movie so
all metadata downloads use L2.

Pass 2 - refresh: force a full metadata refresh with image redownload so the
metadata is re-fetched in L1.

Safety features:
  - --dry-run  previews what would change without writing anything.
  - An explicit confirmation prompt before any destructive run (skip with --yes).
  - Paginated fetching so every movie is seen, never truncated.
  - Per-item error isolation: one bad movie can't abort the whole run.
  - Retry with backoff on transient network/HTTP errors (incl. TMDb 429).
  - Language codes are validated as real ISO 639-1 before being applied.
  - --max limits the run to a subset (safe way to test first).
  - --delay paces requests so neither Emby nor TMDb is hammered.
  - --no-tmdb disables the external TMDb lookups (audio fallback only).

Logging:
  - Every run writes a timestamped trace logfile to --log-dir (default: logs/).
    The file captures DEBUG detail (per-movie before/after, decisions, failures)
    so every change can be audited; the console stays concise.
  - TMDb lookups are cached in --tmdb-cache so edits/re-runs never re-query
    the external API for the same movie unnecessarily.
"""

import argparse
import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests

CONFIG_PATH = Path(__file__).parent / "config.json"

# Languages whose metadata is kept in the original language (L2 = L1).
# Everything else falls back to English.
PREFERRED_LANGUAGES = {"fr", "it"}

lg = logging.getLogger("set_language")

# Emby reports audio-track languages as 3-letter ISO 639-2 codes, while metadata
# languages are 2-letter ISO 639-1 codes. Map the common 3-letter codes used as
# a fallback source (both ISO 639-2/T and /B variants).
ISO639_2_TO_1 = {
    "ara": "ar", "ben": "bn", "bul": "bg", "cat": "ca", "ces": "cs",
    "chi": "zh", "cze": "cs", "dan": "da", "deu": "de", "ell": "el",
    "eng": "en", "est": "et", "eus": "eu", "fas": "fa", "fin": "fi",
    "fra": "fr", "fre": "fr", "gle": "ga", "glg": "gl", "gre": "el",
    "heb": "he", "hin": "hi", "hrv": "hr", "hun": "hu", "hye": "hy",
    "ind": "id", "isl": "is", "ita": "it", "jpn": "ja", "kat": "ka",
    "kaz": "kk", "kor": "ko", "lav": "lv", "lit": "lt", "may": "ms",
    "msa": "ms", "nld": "nl", "nob": "nb", "nor": "no", "pan": "pa",
    "pol": "pl", "por": "pt", "ron": "ro", "rum": "ro", "rus": "ru",
    "slk": "sk", "slo": "sk", "slv": "sl", "spa": "es", "sqi": "sq",
    "srp": "sr", "swe": "sv", "tgl": "tl", "tha": "th", "tur": "tr",
    "ukr": "uk", "urd": "ur", "vie": "vi", "wel": "cy", "yid": "yi",
    "zho": "zh",
    # ISO 639-2/B bibliographic aliases and additional 3-letter codes seen in
    # Emby audio streams.
    "alb": "sq", "arm": "hy", "bur": "my", "dut": "nl", "fil": "tl",
    "geo": "ka", "ger": "de", "ice": "is", "kan": "kn", "mac": "mk",
    "mal": "ml", "mao": "mi", "mar": "mr", "per": "fa", "rom": "ro",
    "tam": "ta", "tel": "te",
}

# Full ISO 639-1 two-letter set. Only real languages are ever applied, so a
# rogue/undefined code can never be written to the server.
KNOWN_LANGUAGES = frozenset({
    "aa", "ab", "ae", "af", "ak", "am", "an", "ar", "as", "av", "ay", "az",
    "ba", "be", "bg", "bh", "bi", "bm", "bn", "bo", "br", "bs", "ca", "ce",
    "ch", "co", "cr", "cs", "cu", "cv", "cy", "da", "de", "dv", "dz", "ee",
    "el", "en", "eo", "es", "et", "eu", "fa", "ff", "fi", "fj", "fo", "fr",
    "fy", "ga", "gd", "gl", "gn", "gu", "gv", "ha", "he", "hi", "ho", "hr",
    "ht", "hu", "hy", "hz", "ia", "id", "ie", "ig", "ii", "ik", "io", "is",
    "it", "iu", "ja", "jv", "ka", "kg", "ki", "kj", "kk", "kl", "km", "kn",
    "ko", "kr", "ks", "ku", "kv", "kw", "ky", "la", "lb", "lg", "li", "ln",
    "lo", "lt", "lu", "lv", "mg", "mh", "mi", "mk", "ml", "mn", "mr", "ms",
    "mt", "my", "na", "nb", "nd", "ne", "ng", "nl", "nn", "no", "nr", "nv",
    "ny", "oc", "oj", "om", "or", "os", "pa", "pi", "pl", "ps", "pt", "qu",
    "rm", "rn", "ro", "ru", "rw", "sa", "sc", "sd", "se", "sg", "si", "sk",
    "sl", "sm", "sn", "so", "sq", "sr", "ss", "st", "su", "sv", "sw", "ta",
    "te", "tg", "th", "ti", "tk", "tl", "tn", "to", "tr", "ts", "tt", "tw",
    "ty", "ug", "uk", "ur", "uz", "ve", "vi", "vo", "wa", "wo", "xh", "yi",
    "yo", "za", "zh", "zu",
})


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def emby_get(config, endpoint, params=None):
    url = f"{config['emby_url']}/emby{endpoint}"
    headers = {"X-Emby-Token": config["api_key"]}
    resp = requests.get(url, headers=headers, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def emby_post(config, endpoint, json_body=None):
    url = f"{config['emby_url']}/emby{endpoint}"
    headers = {"X-Emby-Token": config["api_key"]}
    resp = requests.post(url, headers=headers, json=json_body, timeout=120)
    resp.raise_for_status()
    return resp


def call_with_retry(fn, attempts=3, base_backoff=1.0):
    """Run fn, retrying transient failures with growing backoff."""
    last = None
    for attempt in range(attempts):
        try:
            return fn()
        except requests.RequestException as e:
            last = e
            lg.warning("transient error (attempt %d/%d): %s",
                       attempt + 1, attempts, e)
            time.sleep(base_backoff * (attempt + 1))
    raise last


# ---------------------------------------------------------------------------
# Language resolution
# ---------------------------------------------------------------------------

def normalize_language(code):
    """Normalize a language code to a 2-letter ISO 639-1 code, or None.

    Emby data sometimes wraps a valid code in stray quotes (e.g. "'ita'") or
    carries other junk characters, so only alphanumerics are kept before match.
    """
    if not code:
        return None
    code = "".join(ch for ch in code if ch.isalnum()).lower()
    return code if len(code) == 2 else ISO639_2_TO_1.get(code)


def resolve_language(code):
    """Normalize and require a real 2-letter code; else None (reject junk)."""
    code = normalize_language(code)
    return code if code in KNOWN_LANGUAGES else None


def audio_language(item):
    """First audio-track language, else (None, None)."""
    for stream in item.get("MediaStreams", []):
        if stream.get("Type") == "Audio":
            lang = resolve_language(stream.get("Language"))
            if lang:
                return lang, "audio"
    return None, None


def load_tmdb_cache(path):
    if path and Path(path).exists():
        try:
            return json.loads(Path(path).read_text())
        except Exception:
            lg.warning("could not read tmdb cache at %s; starting fresh", path)
    return {}


def save_tmdb_cache(cache, path):
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(cache, indent=2))


def _tmdb_fetch(config, tmdb_id, args):
    """Look up a movie's original_language on TMDb. Returns code or None."""
    key = config.get("tmdb_api_key")
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}"
    for attempt in range(4):
        if args.delay:
            time.sleep(args.delay)
        try:
            resp = requests.get(url, params={"api_key": key, "language": "en"},
                                timeout=30)
        except requests.RequestException as e:
            lg.warning("tmdb id %s: network error: %s", tmdb_id, e)
            time.sleep(0.5 * (attempt + 1))
            continue
        if resp.status_code in (401, 403):
            lg.error("TMDb authentication failed (HTTP %d): check tmdb_api_key "
                     "in config.json; disabling TMDb lookups for this run",
                     resp.status_code)
            args.tmdb_disabled = True
            return None
        if resp.status_code == 404:
            lg.debug("tmdb id %s: not found", tmdb_id)
            return None
        if resp.status_code == 429:
            lg.warning("tmdb id %s: rate limited, backing off", tmdb_id)
            time.sleep(2.0 * (attempt + 1))
            continue
        if resp.status_code >= 500:
            lg.warning("tmdb id %s: server error %d", tmdb_id, resp.status_code)
            time.sleep(0.5 * (attempt + 1))
            continue
        resp.raise_for_status()
        data = resp.json()
        lang = resolve_language(data.get("original_language"))
        lg.debug("tmdb id %s: original_language=%r -> %s",
                 tmdb_id, data.get("original_language"), lang)
        return lang
    return None


def tmdb_original_language(config, tmdb_id, cache, args):
    """Original language from TMDb, using the persistent cache when possible."""
    if getattr(args, "tmdb_disabled", False):
        return None
    if not config.get("tmdb_api_key") or not tmdb_id:
        return None
    tmdb_id = str(tmdb_id)
    if not tmdb_id.isdigit():
        lg.warning("ignoring non-numeric Tmdb id %r", tmdb_id)
        return None
    if tmdb_id in cache:
        return cache[tmdb_id]
    lang = _tmdb_fetch(config, tmdb_id, args)
    cache[tmdb_id] = lang
    return lang


def resolve_movie_language(config, movie, cache, args):
    """L1 = TMDb original_language, else first audio track.

    Returns (lang, source) where source is 'tmdb' or 'audio', or (None, None).
    """
    tmdb_id = (movie.get("ProviderIds") or {}).get("Tmdb")
    if not args.no_tmdb and config.get("tmdb_api_key") and tmdb_id:
        lang = tmdb_original_language(config, tmdb_id, cache, args)
        if lang:
            return lang, "tmdb"
    return audio_language(movie)


def metadata_language(l1):
    """Apply the L2 rule: L1 in {fr,it} stays, anything else -> English."""
    if l1 in PREFERRED_LANGUAGES:
        return l1
    return "en"


# ---------------------------------------------------------------------------
# Emby operations
# ---------------------------------------------------------------------------

def get_all_movies(config, batch=200):
    """Page through /Items so no movie is ever missed due to a size cap."""
    movies = []
    start = 0
    while True:
        items = call_with_retry(lambda: emby_get(config, "/Items", params={
            "IncludeItemTypes": "Movie",
            "Recursive": "true",
            "Fields": "ProviderIds,MediaStreams",
            "Limit": batch,
            "StartIndex": start,
        }))
        batch_items = items.get("Items", [])
        movies.extend(batch_items)
        lg.debug("fetched movie page start=%d count=%d total=%d",
                 start, len(batch_items), len(movies))
        if len(batch_items) < batch:
            break
        start += batch
    return movies


def set_preferred_language(config, user_id, item_id, detected_lang, args):
    """Force PreferredMetadataLanguage = L2 on one movie. Returns (changed, lang)."""
    if args.delay:
        time.sleep(args.delay)
    full = call_with_retry(
        lambda: emby_get(config, f"/Users/{user_id}/Items/{item_id}"))
    lang = detected_lang
    if lang is None:  # listing had no language; fall back to full-item audio, mapped to L2
        l1 = audio_language(full)[0]
        lang = metadata_language(l1) if l1 else None
    if lang is None:
        lg.debug("item %s: no usable language found on full item", item_id)
        return False, None
    old = full.get("PreferredMetadataLanguage")
    if old == lang:
        lg.debug("item %s: preferred language already %s", item_id, lang)
        return False, lang
    full["PreferredMetadataLanguage"] = lang
    if args.delay:
        time.sleep(args.delay)
    call_with_retry(lambda: emby_post(config, f"/Items/{item_id}", json_body=full))
    lg.debug("item %s: preferred language %s -> %s", item_id, old, lang)
    return True, lang


def refresh_movie(config, item_id, args):
    """Force a full metadata refresh with image redownload (async)."""
    if args.delay:
        time.sleep(args.delay)
    call_with_retry(lambda: emby_post(config, f"/Items/{item_id}/Refresh", json_body={
        "MetadataRefreshMode": "FullRefresh",
        "ImageRefreshMode": "FullRefresh",
        "ReplaceAllMetadata": args.replace_metadata,
        "ReplaceAllImages": True,
        "EnableRemoteContentProbe": True,
    }))


# ---------------------------------------------------------------------------
# Incremental (--new-only) state
# ---------------------------------------------------------------------------

def load_state(path):
    """Load the processed-movies state file (movie id -> applied L2 or 'nodata')."""
    if path and Path(path).exists():
        try:
            return json.loads(Path(path).read_text())
        except Exception:
            lg.warning("could not read state file %s; starting fresh", path)
    return {}


def save_state(state, path):
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(state, indent=2))


def target_marker(l2):
    """Marker stored in state: 'nodata' when no language, else the L2 code."""
    return "nodata" if l2 is None else l2


def select_new_movies(config, movies, cache, state, args):
    """For --new-only: films not yet recorded as processed at the current target.

    Uses only the cheap bulk listing + the warm TMDb cache (no per-movie
    full-item fetch, no repeated network calls) to compute each movie's target
    L2 and compare it against what was recorded as already applied.
    """
    candidates = []
    already = 0
    for movie in movies:
        l1, _src = resolve_movie_language(config, movie, cache, args)
        l2 = metadata_language(l1) if l1 else None
        if state.get(movie["Id"]) == target_marker(l2):
            already += 1
        else:
            candidates.append(movie)
    lg.info("new-only: %d already processed (skipped), %d net-new to handle",
            already, len(candidates))
    return candidates


def preview(config, movies, cache, args):
    l1_sources = Counter()
    l2_counts = Counter()
    samples = []
    for movie in movies:
        l1, src = resolve_movie_language(config, movie, cache, args)
        if l1 is None:
            l1_sources["undetected"] += 1
            continue
        l1_sources[src] += 1
        l2 = metadata_language(l1)
        l2_counts[l2] += 1
        if len(samples) < 15:
            samples.append((movie.get("Name", "?"), l1, l2))
    lg.info("PREVIEW (dry run, nothing was written):")
    lg.info("  total movies:          %d", len(movies))
    lg.info("  L1 from TMDb original: %d", l1_sources.get("tmdb", 0))
    lg.info("  L1 from audio track:   %d", l1_sources.get("audio", 0))
    lg.info("  L1 undetected:         %d", l1_sources.get("undetected", 0))
    lg.info("  L2 target = fr:        %d", l2_counts.get("fr", 0))
    lg.info("  L2 target = it:        %d", l2_counts.get("it", 0))
    lg.info("  L2 target = en:        %d", l2_counts.get("en", 0))
    for name, l1, l2 in samples:
        lg.info("  sample  %s: L1=%s -> L2=%s", name, l1, l2)


def confirm(question):
    answer = input(f"{question} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Set per-movie metadata language (TMDb original, else audio) "
                    "and force a metadata refresh.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would change without writing anything.")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip the confirmation prompt (dangerous).")
    parser.add_argument("--language-only", action="store_true",
                        help="Only set/force the metadata language (no refresh).")
    parser.add_argument("--refresh-only", action="store_true",
                        help="Only force the metadata refresh (no language change).")
    parser.add_argument("--replace-metadata", action="store_true", default=True,
                        help="Overwrite existing provider metadata on refresh "
                             "(DANGEROUS: discards hand-edited fields).")
    parser.add_argument("--no-replace-metadata", dest="replace_metadata",
                        action="store_false",
                        help="Refresh metadata without replacing existing provider data.")
    parser.add_argument("--max", type=int, default=None, metavar="N",
                        help="Only process the first N movies (safe for testing).")
    parser.add_argument("--delay", type=float, default=0.15, metavar="SECONDS",
                        help="Pause between API calls to pace the server and TMDb.")
    parser.add_argument("--log-dir", type=str, default="logs", metavar="DIR",
                        help="Directory for per-run trace logfiles (default: logs).")
    parser.add_argument("--no-log", action="store_true",
                        help="Disable writing a trace logfile.")
    parser.add_argument("--no-tmdb", action="store_true",
                        help="Skip external TMDb lookups (audio fallback only).")
    parser.add_argument("--tmdb-cache", type=str,
                        default=None, metavar="PATH",
                        help="JSON cache of TMDb original languages "
                             "(default: <log-dir>/tmdb_original_language_cache.json).")
    parser.add_argument("--new-only", action="store_true",
                        help="Only process net-new films not already recorded as "
                             "processed; skip already-processed ones and only "
                             "refresh the newly handled films.")
    parser.add_argument("--state-file", type=str,
                        default=None, metavar="PATH",
                        help="JSON state of processed movies "
                             "(default: <log-dir>/processed_movies.json).")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    config = load_config()

    if args.tmdb_cache is None:
        args.tmdb_cache = str(Path(args.log_dir) / "tmdb_original_language_cache.json") \
            if not args.no_log else None
    if args.state_file is None:
        args.state_file = str(Path(args.log_dir) / "processed_movies.json") \
            if not args.no_log else None

    if args.no_log:
        lg.setLevel(logging.INFO)
        lg.propagate = False
        sh = logging.StreamHandler()
        sh.setLevel(logging.INFO)
        fmtr = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
        sh.setFormatter(fmtr)
        lg.addHandler(sh)
        log_path = None
    else:
        log_path = setup_logging(args.log_dir)

    lg.info("=== set_language_and_refresh run started ===")

    required = ("emby_url", "api_key")
    missing = [k for k in required if not config.get(k)]
    if missing:
        lg.error("config.json is missing required key(s): %s", ", ".join(missing))
        sys.exit(1)

    # Redact the key so it never lands in the trace log.
    key = config["api_key"]
    lg.info("server: %s | api_key: %s...%s | tmdb_key_present: %s",
            config["emby_url"], key[:4], key[-4:] if len(key) > 8 else "xxxx",
            bool(config.get("tmdb_api_key")))
    lg.info("args: %s", vars(args))

    if args.language_only and args.refresh_only:
        lg.error("Cannot use both --language-only and --refresh-only.")
        sys.exit(1)

    cache = load_tmdb_cache(args.tmdb_cache)
    if not args.no_tmdb and not config.get("tmdb_api_key"):
        lg.warning("no tmdb_api_key in config.json; falling back to audio-track "
                   "language only. Add tmdb_api_key to use TMDb original_language.")
    elif not args.no_tmdb:
        lg.info("TMDb original-language lookup enabled (cache: %s)", args.tmdb_cache)

    print(f"Connecting to Emby at {config['emby_url']}...")
    try:
        call_with_retry(lambda: emby_get(config, "/System/Info"))
    except requests.RequestException as e:
        lg.error("Error connecting to Emby: %s", e)
        sys.exit(1)
    lg.info("connected OK")

    users = call_with_retry(lambda: emby_get(config, "/Users"))
    user_id = users[0]["Id"]
    lg.debug("using user %s", user_id)

    state = load_state(args.state_file)
    movies = get_all_movies(config)
    if not movies:
        lg.warning("No movies found in library.")
        print("No movies found in library.")
        return
    lg.info("found %d movies in library", len(movies))
    print(f"Found {len(movies)} movies in library.\n")

    # Determine the set to actually work on (all, or just net-new for --new-only).
    if args.new_only:
        work = select_new_movies(config, movies, cache, state, args)
    else:
        work = list(movies)
    if args.max:
        work = work[:args.max]
        lg.info("limited run to first %d movies (--max)", args.max)
    lg.info("processing %d movies", len(work))
    print(f"Processing {len(work)} movies.\n")

    if args.dry_run:
        preview(config, work, cache, args)
        save_tmdb_cache(cache, args.tmdb_cache)
        lg.info("=== dry run complete (no changes made) ===")
        return

    # Confirmation: gated but default-on unless --yes.
    if not args.yes:
        lg.info("confirmation prompt shown")
        print("About to run against this Emby server:")
        print(f"  URL:    {config['emby_url']}")
        print(f"  Movies: {len(movies) if not args.new_only else f'{len(work)} (net-new, of {len(movies)} total)'}")
        print(f"  Language pass:    {'yes' if not args.refresh_only else 'no'}")
        print(f"  Refresh pass:     {'yes' if not args.language_only else 'no'}")
        if not args.language_only:
            print(f"  Replace metadata: {'yes (WARNING: overwrites hand-edited fields)' if args.replace_metadata else 'no'}")
            print("  Replace images:   yes (redownloads posters/backdrops)")
        print()
        if not confirm("This performs a destructive operation. Continue?"):
            lg.info("run aborted by user at confirmation; no changes made")
            print("Aborted. Nothing was changed.")
            sys.exit(1)
        lg.info("user confirmed execution")
        print()

    # --- Pass 1: set/force metadata language ---
    processed = {}
    failed_ids = set()
    if not args.refresh_only:
        lg.info("=== pass 1: setting preferred metadata language ===")
        set_lang = already = no_lang = failed = 0
        for movie in work:
            name = movie.get("Name", "?")
            item_id = movie["Id"]
            try:
                l1, src = resolve_movie_language(config, movie, cache, args)
                l2 = metadata_language(l1) if l1 else None
                changed, lang = set_preferred_language(
                    config, user_id, item_id, l2, args)
                processed[item_id] = target_marker(lang)
                if lang is None:
                    no_lang += 1
                    lg.info("%s: no language detected (L1), skipped", name)
                elif changed:
                    set_lang += 1
                    lg.info("%s: L1=%s (%s) -> L2=%s: set", name, l1, src, lang)
                else:
                    already += 1
                    lg.info("%s: L1=%s (%s) -> L2=%s: already set",
                            name, l1, src, lang)
            except requests.RequestException as e:
                failed += 1
                failed_ids.add(item_id)
                lg.error("%s (id %s): ERROR %s", name, item_id, e)
        save_tmdb_cache(cache, args.tmdb_cache)
        # Persist what was handled so future --new-only runs skip these.
        for item_id, marker in processed.items():
            state[item_id] = marker
        save_state(state, args.state_file)
        lg.info("pass 1 summary: set %d, already set %d, "
                "no language %d, failed %d", set_lang, already, no_lang, failed)
        print(f"\nLanguage pass: set {set_lang}, already set {already}, "
              f"no language {no_lang}, failed {failed}.\n")

    # --- Pass 2: force metadata refresh + image redownload ---
    if not args.language_only:
        lg.info("=== pass 2: forcing metadata refresh with image redownload ===")
        # Refresh only the movies being handled (net-new in --new-only), never
        # the already-processed rest of the library. Skip any that errored in
        # the language pass so we don't touch their metadata/state blindly.
        refresh_set = [m for m in work if m["Id"] not in failed_ids]
        failed = 0
        for movie in refresh_set:
            name = movie.get("Name", "?")
            item_id = movie["Id"]
            try:
                refresh_movie(config, item_id, args)
                lg.info("%s (id %s): refresh scheduled "
                        "(replace_metadata=%s, replace_images=True)",
                        name, item_id, args.replace_metadata)
            except requests.RequestException as e:
                failed += 1
                lg.error("%s (id %s): ERROR %s", name, item_id, e)
        lg.info("pass 2 summary: scheduled %d, failed %d",
                len(refresh_set) - failed, failed)
        print(f"\nRefresh pass: scheduled {len(refresh_set) - failed}, failed {failed}.")
        print("Emby processes the scheduled refreshes in the background.")

    lg.info("=== run complete ===")
    if log_path:
        print(f"\nTrace log: {log_path}")
    print("\nDone.")


def setup_logging(log_dir):
    """Configure console (INFO) + per-run trace file (DEBUG). Returns the path."""
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = Path(log_dir) / f"set_language_and_refresh_{timestamp}.log"

    lg.setLevel(logging.DEBUG)
    lg.propagate = False

    fmtr = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmtr)
    lg.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmtr)
    lg.addHandler(sh)

    return log_path


if __name__ == "__main__":
    main()
