import unittest

from seek import parse_seek_position


class SeekPositionTests(unittest.TestCase):
    def test_parses_seconds_and_clock_formats(self):
        self.assertEqual(parse_seek_position("90"), 90)
        self.assertEqual(parse_seek_position("1:30"), 90)
        self.assertEqual(parse_seek_position("1:02:03"), 3723)

    def test_rejects_invalid_positions(self):
        for value in ("", "-1", "1:60", "1:2:60", "1:2:3:4", "abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_seek_position(value)


if __name__ == "__main__":
    unittest.main()
