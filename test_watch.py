"""Offline tests for watch.py's page parsing. Run: python3 -m unittest -v"""

import unittest
from unittest import mock

import watch

URL = "https://www.amazon.in/dp/B0DSKL9MQ8"


def respond(body: str):
    """Patch urlopen so fetch_once receives `body` without touching the network."""
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = body.encode()
    return mock.patch("urllib.request.urlopen", return_value=resp)


class FetchClassification(unittest.TestCase):
    def test_normal_product_page_is_returned(self):
        with respond('<span id="productTitle">Galaxy</span>'):
            self.assertIn("Galaxy", watch.fetch_once(URL))

    def test_captcha_is_blocked_not_layout_change(self):
        with respond("<form>Type the characters you see: captcha</form>"):
            with self.assertRaises(watch.Blocked) as ctx:
                watch.fetch_once(URL)
        self.assertNotIsInstance(ctx.exception, watch.LayoutChanged)

    def test_bot_check_challenge_is_blocked_not_layout_change(self):
        body = "<meta http-equiv=\"refresh\" content=\"5; URL='/dp/x?bm-verify=AAQ'\">"
        with respond(body):
            with self.assertRaises(watch.Blocked) as ctx:
                watch.fetch_once(URL)
        self.assertNotIsInstance(ctx.exception, watch.LayoutChanged)

    def test_unrecognised_page_is_layout_change(self):
        with respond("<html><h1 class='new-title'>Galaxy</h1></html>"):
            with self.assertRaises(watch.LayoutChanged):
                watch.fetch_once(URL)

    def test_layout_change_is_not_retried(self):
        with mock.patch.object(watch, "fetch_once",
                               side_effect=watch.LayoutChanged("x")) as f:
            with self.assertRaises(watch.LayoutChanged):
                watch.fetch(URL)
        self.assertEqual(f.call_count, 1)


class VariantMatching(unittest.TestCase):
    VARIANTS = {
        "B0DSKL9MQ8": ["12GB + 256GB", "No cost EMI", "Titanium Whitesilver"],
        "B0DSBTKP5Q": ["12GB + 512GB", "No cost EMI", "Titanium Silverblue"],
    }

    def test_no_match_when_colour_only_exists_in_other_size(self):
        self.assertIsNone(watch.find_variant(self.VARIANTS, ["512GB", "Whitesilver"]))

    def test_match_ignores_case_and_spacing(self):
        self.assertEqual(watch.find_variant(self.VARIANTS, ["512 gb", "silver blue"]),
                         "B0DSBTKP5Q")

    def test_missing_variant_table_is_layout_change(self):
        with self.assertRaises(watch.LayoutChanged):
            watch.parse_variants('<span id="productTitle">x</span>')


if __name__ == "__main__":
    unittest.main()
