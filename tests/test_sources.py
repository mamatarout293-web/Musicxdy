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
    def test_ydl_opts_uses_reload_workaround_clients_by_default(self):
        opts = sources._ydl_opts()

        self.assertEqual(
            opts["extractor_args"]["youtube"]["player_client"],
            ["tv", "web_embedded", "default"],
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


if __name__ == "__main__":
    unittest.main()
