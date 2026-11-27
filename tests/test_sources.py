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

    def test_allows_muxed_format_as_last_resort(self):
        info = {"url": "https://example/stream", "source": "youtube",
                "vcodec": "avc1.42001E", "abr": None, "asr": None}
        self.assertEqual(sources._finish(info, allow_low_quality=True), info)

    @patch("sources.playable", side_effect=AssertionError("must not probe YouTube"))
    def test_youtube_stream_is_not_probed_by_default(self, _playable):
        info = {"url": "https://example/stream", "source": "youtube",
                "vcodec": "none", "abr": 138.8, "asr": 48000}
        self.assertEqual(sources._finish(info), info)

    @patch("sources.playable", return_value=True)
    def test_youtube_stream_is_probed_when_enabled(self, playable):
        info = {"url": "https://example/stream", "source": "youtube",
                "vcodec": "none", "abr": 138.8, "asr": 48000}
        with patch.object(sources, "VERIFY_YOUTUBE", True):
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


if __name__ == "__main__":
    unittest.main()
