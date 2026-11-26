import unittest
from unittest.mock import patch

import sources


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
    def test_public_sources_precede_youtube_and_piped_is_not_registered(self):
        with patch.dict(sources.os.environ, {}, clear=True):
            names = [source.name for source in sources._build()]

        self.assertEqual(names, ["soundcloud", "audius", "youtube"])


if __name__ == "__main__":
    unittest.main()
