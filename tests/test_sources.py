import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import sources


class ParallelResolveTests(unittest.TestCase):
    def test_returns_fast_source_without_waiting_for_slow_source(self):
        slow_started = threading.Event()
        release_slow = threading.Event()

        def slow(_query):
            slow_started.set()
            release_slow.wait(2)
            return {"source": "slow"}

        def fast(_query):
            slow_started.wait(1)
            return fast_result

        fast_result = {"source": "fast"}
        providers = [sources.Source("slow", slow), sources.Source("fast", fast)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                with patch.object(sources, "SOURCES", providers), \
                        patch.object(sources, "DEADLINE", 3), \
                        patch.object(sources, "_RESOLVER_POOL", pool):
                    result = sources.resolve("a song")

                self.assertTrue(slow_started.is_set())
                self.assertIs(result, fast_result)
            finally:
                release_slow.set()


class YoutubeFirstResolveTests(unittest.TestCase):
    def test_youtube_runs_before_alternate_sources(self):
        calls = []

        def youtube(_query):
            calls.append("youtube")
            return {"source": "youtube"}

        def soundcloud(_query):
            calls.append("soundcloud")
            return {"source": "soundcloud"}

        providers = [
            sources.Source("youtube", youtube),
            sources.Source("soundcloud", soundcloud),
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(sources, "SOURCES", providers), \
                    patch.object(sources, "DEADLINE", 3), \
                    patch.object(sources, "YOUTUBE_FIRST", True), \
                    patch.object(sources, "_RESOLVER_POOL", pool):
                result = sources.resolve("a song")

        self.assertEqual(result["source"], "youtube")
        self.assertEqual(calls, ["youtube"])

    def test_alternate_runs_only_after_youtube_fails(self):
        calls = []

        def youtube(_query):
            calls.append("youtube")
            raise sources.NotFound("no youtube")

        def soundcloud(_query):
            calls.append("soundcloud")
            return {"source": "soundcloud"}

        providers = [
            sources.Source("youtube", youtube),
            sources.Source("soundcloud", soundcloud),
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(sources, "SOURCES", providers), \
                    patch.object(sources, "DEADLINE", 3), \
                    patch.object(sources, "YOUTUBE_FIRST", True), \
                    patch.object(sources, "_RESOLVER_POOL", pool):
                result = sources.resolve("a song")

        self.assertEqual(result["source"], "soundcloud")
        self.assertEqual(calls, ["youtube", "soundcloud"])


class AudiusTests(unittest.TestCase):
    @patch("sources._audius_hosts", return_value=["https://first.example", "https://second.example"])
    @patch("sources.get_json")
    @patch("sources._finish", side_effect=lambda info: info)
    def test_search_continues_after_empty_host(self, finish, get_json, _hosts):
        def response(url):
            if "first.example" in url:
                return {"data": []}
            return {"data": [{
                "id": "track-1",
                "title": "Barbaad",
                "user": {"name": "Arijit Singh"},
                "is_streamable": True,
            }]}

        get_json.side_effect = response
        result = sources._audius("barbaad the song")

        self.assertEqual(result["source"], "audius")
        self.assertIn("second.example/v1/tracks/track-1/stream", result["url"])
        self.assertEqual(get_json.call_count, 3)

    @patch("sources._audius_hosts", return_value=["https://audius.example"])
    @patch("sources.get_json", return_value={"data": [
        {"id": "unplayable", "title": "Barbaad", "user": {"name": "Arijit Singh"},
         "is_streamable": True},
        {"id": "playable", "title": "Barbaad", "user": {"name": "Arijit Singh"},
         "is_streamable": True},
    ]})
    @patch("sources._finish", side_effect=[
        RuntimeError("stream unavailable"), {"url": "https://audius.example/tracks/playable/stream"}
    ])
    def test_tries_next_streamable_match(self, finish, _get_json, _hosts):
        result = sources._audius("barbaad arijit singh")

        self.assertIn("/tracks/playable/stream", result["url"])
        self.assertEqual(finish.call_count, 2)


class SourceOrderTests(unittest.TestCase):
    def test_youtube_is_enabled_by_default(self):
        with patch.dict(sources.os.environ, {}, clear=True):
            names = [source.name for source in sources._build()]

        self.assertEqual(names, ["youtube"])


class ProxyPoolTests(unittest.TestCase):
    def test_rotates_and_cools_bad_proxies(self):
        pool = sources.ProxyPool(["http://a:1", "http://b:2"], cooldown=60)

        self.assertEqual(pool.candidates(), ["http://a:1", "http://b:2"])
        pool.mark_bad("http://a:1", "407")
        self.assertEqual(pool.candidates(), ["http://b:2"])
        pool.mark_bad("http://b:2", "407")

        self.assertEqual(pool.candidates(), [None])
        self.assertTrue(pool.all_bad())

    def test_splits_comma_and_newline_separated_list(self):
        raw = "http://a:1, http://b:2\nhttp://c:3;http://a:1"
        self.assertEqual(
            sources._split_proxy_list(raw),
            ["http://a:1", "http://b:2", "http://c:3"],
        )

    def test_label_does_not_expose_credentials(self):
        self.assertEqual(
            sources.ProxyPool.label("http://user:pass@1.2.3.4:8080"),
            "1.2.3.4:8080",
        )


class YoutubePotTests(unittest.TestCase):
    def test_ydl_opts_uses_cookie_compatible_clients_with_http_provider(self):
        opts = sources._ydl_opts()

        self.assertEqual(
            opts["extractor_args"]["youtube"]["player_client"],
            ["tv_embedded", "web_safari", "default"],
        )
        self.assertEqual(
            opts["extractor_args"]["youtubepot-bgutilhttp"]["base_url"],
            ["http://127.0.0.1:4416"],
        )

    def test_ydl_opts_points_to_rust_http_provider(self):
        opts = sources._ydl_opts(clients=["tv"])

        self.assertEqual(
            opts["extractor_args"]["youtubepot-bgutilhttp"]["base_url"],
            ["http://127.0.0.1:4416"],
        )
        self.assertEqual(opts["extractor_args"]["youtube"]["player_client"], ["tv"])

    def test_cookie_bot_check_retries_same_client_without_cookiefile(self):
        def extract(opts, _target):
            if "cookiefile" in opts:
                raise RuntimeError("Sign in to confirm you're not a bot")
            return [{"title": "Song"}]

        with patch.object(sources, "COOKIES", "/tmp/cookies.txt"), \
                patch.object(sources, "_yt_attempts", return_value=[
                    {"cookies": True, "clients": ["tv"]}
                ]), \
                patch.object(sources, "_ydl_entries", side_effect=extract) as ydl_entries, \
                patch.object(sources, "_finish", return_value={"source": "youtube"}) as finish:
            result = sources._yt_extract("https://youtube.com/watch?v=example")

        self.assertEqual(result, {"source": "youtube"})
        first_opts = ydl_entries.call_args_list[0].args[0]
        retry_opts = ydl_entries.call_args_list[1].args[0]
        self.assertEqual(first_opts["cookiefile"], "/tmp/cookies.txt")
        self.assertNotIn("cookiefile", retry_opts)
        self.assertEqual(retry_opts["extractor_args"]["youtube"]["player_client"], ["tv"])
        self.assertEqual(ydl_entries.call_count, 2)
        finish.assert_called_once()


class StreamHeaderTests(unittest.TestCase):
    @patch("sources.playable", return_value=True)
    def test_finish_preserves_individual_ffmpeg_headers(self, playable):
        headers = {
            "User-Agent": "MusicBot/1.0",
            "Accept": "*/*",
            "Referer": "https://soundcloud.com/",
        }
        info = {"url": "https://stream.example/audio", "headers": headers, "source": "soundcloud"}

        result = sources._finish(info)

        playable.assert_called_once_with(info["url"], headers, None)
        self.assertEqual(result["headers"], headers)


class _FakeResponse:
    def __init__(self, status=200, content_type="audio/mpeg", body=b"audio-bytes"):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size=-1):
        return self.body[:size]


class PlayableTests(unittest.TestCase):
    @patch("sources._open", return_value=_FakeResponse())
    def test_accepts_nonempty_audio_response(self, open_stream):
        self.assertTrue(sources.playable("https://stream.example/audio"))
        self.assertEqual(open_stream.call_args.args[1]["Range"], "bytes=0-4095")

    @patch("sources._open", return_value=_FakeResponse(content_type="text/html",
                                                        body=b"<html>blocked</html>"))
    def test_rejects_html_error_page(self, _open_stream):
        self.assertFalse(sources.playable("https://stream.example/audio"))

    @patch("sources._open", return_value=_FakeResponse(content_type="application/json",
                                                        body=b'{"error":"denied"}'))
    def test_rejects_json_error_response(self, _open_stream):
        self.assertFalse(sources.playable("https://stream.example/audio"))

    @patch("sources._open", side_effect=__import__("urllib.error").error.HTTPError(
        "https://stream.example/audio", 416, "Range Not Satisfiable", {}, None))
    def test_rejects_range_not_satisfiable(self, _open_stream):
        self.assertFalse(sources.playable("https://stream.example/audio"))


class CookieParsingTests(unittest.TestCase):
    def test_netscape_export_filters_unrelated_domains(self):
        data = "\n".join([
            "# Netscape HTTP Cookie File",
            ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tabc",
            "consent.youtube.com\tTRUE\t/\tFALSE\t0\tCONSENT\tyes",
            ".google.co.in\tTRUE\t/\tTRUE\t2147483647\tNID\txyz",
            ".example.com\tTRUE\t/\tFALSE\t0\tsession\tsecret",
            "#HttpOnly_.google.com\tTRUE\t/\tTRUE\t2147483647\tHSID\thidden",
        ])

        rows = sources._normalize_cookie_text(data)

        names = {row.split("\t")[5] for row in rows}
        self.assertEqual(names, {"SID", "CONSENT", "NID", "HSID"})
        self.assertFalse(any("example.com" in row for row in rows))
        self.assertTrue(any(row.startswith("#HttpOnly_") for row in rows))

    def test_json_export_is_converted(self):
        data = json.dumps([
            {"domain": ".youtube.com", "name": "SID", "value": "abc", "path": "/",
             "secure": True, "expirationDate": 1_900_000_000, "httpOnly": True},
            {"domain": ".example.com", "name": "session", "value": "secret"},
        ])

        rows = sources._normalize_cookie_text(data)

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith("#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t"))

    def test_raw_cookie_header_is_accepted(self):
        rows = sources._normalize_cookie_text("SID=abc; HSID=def")

        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row.startswith(".youtube.com\tTRUE\t/\tTRUE\t") for row in rows))

    def test_unrelated_export_is_rejected(self):
        with self.assertRaises(ValueError):
            sources._normalize_cookie_text(".example.com\tTRUE\t/\tFALSE\t0\ts\ts")


class CookieStoreTests(unittest.TestCase):
    def test_cookie_copy_is_throwaway(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("original")
            path = handle.name
        try:
            with sources._cookie_copy(path) as tmp:
                self.assertNotEqual(tmp, path)
                with open(tmp, "w", encoding="utf-8") as writer:
                    writer.write("changed by yt-dlp")
            with open(path, encoding="utf-8") as reader:
                self.assertEqual(reader.read(), "original")
        finally:
            os.unlink(path)

    def test_changed_cookies_file_is_hot_reloaded(self):
        old_cookies = sources.COOKIES
        with tempfile.TemporaryDirectory() as folder:
            cookie_file = os.path.join(folder, "cookies.txt")
            with open(cookie_file, "w", encoding="utf-8") as handle:
                handle.write(".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tone\n")
            with patch.object(sources, "ROOT", folder), \
                    patch.object(sources, "_COOKIE_PATH", os.path.join(folder, "out.txt")), \
                    patch.object(sources, "_COOKIE_STATE", {
                        "signature": None, "count": 0, "file_sig": None,
                        "remote_at": 0.0, "source": "",
                    }), \
                    patch.dict(os.environ, {}, clear=True):
                try:
                    sources.refresh_cookies(force=True)
                    self.assertEqual(sources._COOKIE_STATE["count"], 1)

                    with open(cookie_file, "w", encoding="utf-8") as handle:
                        handle.write(".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tone\n"
                                     ".google.com\tTRUE\t/\tTRUE\t0\tHSID\ttwo\n")
                    stat = os.stat(cookie_file)
                    os.utime(cookie_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

                    sources.refresh_cookies()
                    self.assertEqual(sources._COOKIE_STATE["count"], 2)
                finally:
                    sources.COOKIES = old_cookies


class YoutubeAttemptOrderTests(unittest.TestCase):
    def setUp(self):
        sources._LAST_GOOD = None

    def tearDown(self):
        sources._LAST_GOOD = None

    @patch("sources._browser_cookie_specs", return_value=[])
    def test_cookie_less_high_quality_client_runs_first(self, _browsers):
        with patch.object(sources, "COOKIES", None):
            attempts = sources._yt_attempts()

        self.assertFalse(any(attempt.get("cookies") for attempt in attempts))
        self.assertEqual(attempts[0]["clients"], ["default"])
        self.assertEqual(attempts[-1]["clients"], ["tv_simply", "mweb", "android_vr"])

    @patch("sources._browser_cookie_specs", return_value=[])
    def test_cookie_attempts_only_when_cookies_are_loaded(self, _browsers):
        with patch.object(sources, "COOKIES", "/tmp/cookies.txt"):
            attempts = sources._yt_attempts()

        self.assertTrue(any(attempt.get("cookies") for attempt in attempts))

    def test_cookie_error_retries_same_client_without_cookiefile(self):
        def extract(opts, _target):
            if "cookiefile" in opts:
                raise RuntimeError("The page needs to be reloaded.")
            return [{"title": "Song"}]

        with patch.object(sources, "COOKIES", "/tmp/cookies.txt"), \
                patch.object(sources, "_yt_attempts", return_value=[
                    {"cookies": True, "clients": ["tv"]}
                ]), \
                patch.object(sources, "_ydl_entries", side_effect=extract) as ydl_entries, \
                patch.object(sources, "_finish", return_value={"source": "youtube"}) as finish:
            result = sources._yt_extract("https://youtube.com/watch?v=example")

        self.assertEqual(result, {"source": "youtube"})
        self.assertNotIn("cookiefile", ydl_entries.call_args_list[1].args[0])
        self.assertEqual(ydl_entries.call_count, 2)
        finish.assert_called_once()

    def test_search_retries_with_cookies_when_cookie_less_search_fails(self):
        used_cookies = []

        def entries(opts, _target):
            used_cookies.append("cookiefile" in opts)
            if "cookiefile" not in opts:
                raise RuntimeError("search blocked")
            return [{"id": "x"}]

        with patch.object(sources, "COOKIES", "/tmp/cookies.txt"), \
                patch.object(sources, "_ydl_entries", side_effect=entries):
            result = sources._search_entries("ytsearch1:test")

        self.assertEqual(result, [{"id": "x"}])
        self.assertEqual(used_cookies, [False, True])


class QualityGateTests(unittest.TestCase):
    def test_rejects_muxed_format_without_audio_bitrate(self):
        info = {"url": "https://example/stream", "source": "youtube",
                "vcodec": "avc1.42001E", "abr": None, "asr": None}
        with self.assertRaises(RuntimeError):
            sources._finish(info)

    @patch("sources.playable", return_value=True)
    def test_allows_muxed_format_as_last_resort(self, _playable):
        info = {"url": "https://example/stream", "source": "youtube",
                "vcodec": "avc1.42001E", "abr": None, "asr": None}
        self.assertEqual(sources._finish(info, allow_low_quality=True), info)

    @patch("sources.playable", return_value=True)
    def test_youtube_stream_is_always_probed(self, playable):
        info = {"url": "https://example/stream", "source": "youtube",
                "vcodec": "none", "abr": 138.8, "asr": 48000}
        self.assertEqual(sources._finish(info), info)
        playable.assert_called_once()

    def test_last_good_attempt_moves_to_front(self):
        good = sources.YT_ATTEMPTS[4]
        with patch.object(sources, "COOKIES", None), \
                patch.object(sources, "_browser_cookie_specs", return_value=[]):
            sources._LAST_GOOD = good
            try:
                attempts = sources._yt_attempts()
            finally:
                sources._LAST_GOOD = None
        self.assertEqual(attempts[0], good)


class SearchMatchingTests(unittest.TestCase):
    def test_typo_is_still_relevant(self):
        self.assertTrue(sources._relevant(
            "believr imagine dragns",
            "Imagine Dragons - Believer (Official Music Video)",
            "ImagineDragons",
        ))

    def test_short_typo_is_relevant(self):
        self.assertTrue(sources._relevant(
            "shape of yu", "Ed Sheeran - Shape of You (Official Music Video)"))

    def test_remix_tiktok_edit_is_rejected(self):
        for title in ("Believer (TikTok Remix)", "Believer (Slowed + Reverb)",
                      "Believer (Sped Up)", "Believer (Cover)", "Believer (Live)",
                      "Believer (WhatsApp Status)", "Believer (8D Audio)"):
            self.assertTrue(sources._variant_unwanted("believer", title, ""), title)

    def test_lyrics_rejected_until_relaxed(self):
        title = "Imagine Dragons - Believer (Lyrics)"
        self.assertTrue(sources._variant_unwanted("believer", title, "7clouds"))
        self.assertFalse(sources._variant_unwanted(
            "believer", title, "7clouds", allow_soft=True))

    def test_lyrics_query_still_rejects_remix(self):
        self.assertTrue(sources._variant_unwanted(
            "believer lyrics", "Believer (TikTok Remix)", ""))
        self.assertFalse(sources._variant_unwanted(
            "believer lyrics", "Believer (Lyrics)", ""))
        self.assertFalse(sources._variant_unwanted(
            "believer remix", "Believer (Remix)", ""))

    def test_regional_labels_are_official(self):
        for channel in [
            "SVF Music", "Eskay Movies", "Surinder Films", "RTV Music",
            "Sarthak Music", "Dhoom Music", "Tarang TV", "Suriya Music Official",
            "Humble Music", "Speed Records", "Saregama Punjabi",
            "Fueled by Ramen", "Big Machine Records",
        ]:
            self.assertTrue(sources._is_official("Song", channel, "song"), channel)

    def test_language_hint_is_removed_from_coverage_but_biases_ranking(self):
        entry = {"title": "Jhumka", "channel": "Suriya Music",
                 "duration": 200, "view_count": 1_000_000}
        self.assertTrue(sources._candidate_ok("jhumka odia", entry))
        odia = sources._match_score(
            "jhumka odia", "JHUMKA | SAMBALPURI SONG", "Suriya Music Official",
            227, True, 29_000_000)
        hindi = sources._match_score(
            "jhumka odia", "Jhumka Patola", "Himesh Reshammiya Melodies",
            218, True, 139_000_000)
        self.assertGreater(odia, hindi)

    def test_script_mismatch_for_latin_query_and_native_title(self):
        self.assertTrue(sources._script_mismatch(
            "tumi robe nirobe", "তুমি রবে নীরবে"))
        self.assertTrue(sources._script_mismatch(
            "kannaana kanney", "கண்ணான கண்ணே"))
        self.assertFalse(sources._script_mismatch(
            "tumi robe nirobe", "Tumi Robe Nirobe"))
        self.assertFalse(sources._script_mismatch(
            "arijit singh", "Arijit Singh - তুমি রবে নীরবে"))

    def test_native_variant_words_are_filtered(self):
        self.assertTrue(sources._variant_unwanted(
            "song", "গানটি কভার", ""))
        self.assertTrue(sources._variant_unwanted(
            "song", "ਗੀਤ ਦਾ ਰੀਮਿਕਸ", ""))
        self.assertTrue(sources._variant_unwanted(
            "song", "ଗୀତ କଭର", ""))
        self.assertTrue(sources._variant_unwanted(
            "song", "गाना लिरिक्स", ""))

    def test_thumbnail_candidates_prefer_maxres(self):
        urls = sources._thumb_candidates(
            {"id": "abc123", "webpage_url": "https://www.youtube.com/watch?v=abc123"},
            "youtube")
        self.assertEqual(urls[0], "https://i.ytimg.com/vi/abc123/maxresdefault.jpg")
        self.assertIn("https://i.ytimg.com/vi/abc123/maxres1.jpg", urls)
        self.assertIn("https://i.ytimg.com/vi/abc123/sddefault.jpg", urls)

    def test_thumbnail_candidates_for_other_sources(self):
        urls = sources._thumb_candidates(
            {"id": "1", "thumbnail": "https://example.com/a.jpg"}, "soundcloud")
        self.assertEqual(urls, ["https://example.com/a.jpg"])

    def test_official_upload_is_detected(self):
        self.assertTrue(sources._is_official(
            "Imagine Dragons - Believer (Official Music Video)",
            "ImagineDragons", "believer imagine dragons"))
        self.assertTrue(sources._is_official(
            "Believer", "Imagine Dragons - Topic", "believer"))
        self.assertTrue(sources._is_official(
            "Tum Hi Ho", "T-Series", "tum hi ho", verified=True))
        self.assertTrue(sources._is_official(
            "Tum Hi Ho", "T-Series", "tum hi ho"))
        self.assertTrue(sources._is_official(
            "Kesariya", "Sony Music India", "kesariya"))
        self.assertFalse(sources._is_official(
            "Apna Bana Le", "Mudit-Pancham", "apna bana le",
            verified=True, views=660_000))
        self.assertTrue(sources._is_official(
            "Apna Bana Le", "Mudit-Pancham", "apna bana le",
            verified=True, views=20_000_000))
        self.assertFalse(sources._is_official(
            "Believer (Lyrics)", "7clouds", "believer imagine dragons"))
        self.assertFalse(sources._is_official(
            "Believer", "Imagine Dragons Spain", "believer imagine dragons"))
        # A title that merely claims "official" is not an official channel.
        self.assertFalse(sources._is_official(
            "Tum Hi Ho (Official Video)", "Random Channel", "tum hi ho"))

    def test_popular_official_beats_obscure_same_title(self):
        q = "apna bana le"
        zee = sources._match_score(
            q, "Apna Bana Le - Full Audio | Bhediya", "Zee Music Company",
            273, True, 128_000_000)
        obscure = sources._match_score(
            q, "Apna Bana Le | Official Song Visualiser", "Mudit-Pancham",
            187, True, 660_000)
        self.assertGreater(zee, obscure)

    def test_official_scores_above_lyrics_and_remix(self):
        q = "believer imagine dragons"
        official = sources._match_score(
            q, "Imagine Dragons - Believer (Official Music Video)",
            "ImagineDragons", 217, True)
        lyrics = sources._match_score(
            q, "Imagine Dragons - Believer (Lyrics)", "7clouds", 205, True)
        remix = sources._match_score(
            q, "Believer (TikTok Remix)", "edits", 30, False)
        self.assertGreater(official, lyrics)
        self.assertGreater(official, remix)

    def test_candidate_filter_rejects_short_clips(self):
        entry = {"title": "Believer (Official Audio)", "channel": "ImagineDragons",
                 "duration": 30}
        self.assertFalse(sources._candidate_ok("believer", entry))
        entry["duration"] = 217
        self.assertTrue(sources._candidate_ok("believer", entry))

    def test_candidate_filter_rejects_remix_even_when_relaxed(self):
        entry = {"title": "Believer (TikTok Remix)", "channel": "edits", "duration": 200}
        self.assertFalse(sources._candidate_ok("believer", entry, 0.5, allow_soft=True))

    def test_candidate_filter_rejects_live_streams(self):
        entry = {"title": "Believer", "channel": "ImagineDragons", "duration": 200,
                 "live_status": "is_live"}
        self.assertFalse(sources._candidate_ok("believer", entry))

    def test_candidate_filter_rejects_near_zero_views(self):
        entry = {"title": "Believer", "channel": "ImagineDragons", "duration": 200,
                 "view_count": 5}
        self.assertFalse(sources._candidate_ok("believer", entry))
        entry["view_count"] = 100_000
        self.assertTrue(sources._candidate_ok("believer", entry))

    def test_oembed_channel_enrichment(self):
        entries = [{"id": "abc", "title": "Song"}]
        with patch.object(sources, "_oembed_author", return_value="T-Series"):
            sources._enrich_channels(entries)
        self.assertEqual(entries[0]["channel"], "T-Series")


class YoutubeUnavailableTests(unittest.TestCase):
    def test_permanent_unavailable_is_detected(self):
        self.assertTrue(sources._is_unavailable(RuntimeError("Video unavailable")))
        self.assertFalse(sources._is_unavailable(
            RuntimeError("Sign in to confirm you're not a bot")))

    def test_unavailable_video_does_not_retry_every_client(self):
        with patch.object(sources, "_yt_attempts", return_value=[
            {"clients": ["default"], "cookies": False},
            {"clients": ["tv"], "cookies": False},
        ]), patch.object(sources, "_ydl_entries",
                         side_effect=RuntimeError("Video unavailable")) as ydl:
            with self.assertRaises(RuntimeError):
                sources._yt_extract("https://youtube.com/watch?v=example")
        self.assertEqual(ydl.call_count, 1)


if __name__ == "__main__":
    unittest.main()
