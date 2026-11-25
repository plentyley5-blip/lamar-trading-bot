import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from handlers import geopolitical_context as geo


class GeopoliticalContextTests(unittest.TestCase):
    def test_currency_query_only_supports_mapped_currencies(self):
        self.assertIn("United States", geo.build_gdelt_query("USD"))
        self.assertIn("sanctions", geo.build_gdelt_query("USD"))
        self.assertIsNone(geo.build_gdelt_query("XYZ"))

    def test_trusted_domain_matching_is_exact_or_subdomain_only(self):
        self.assertTrue(geo._domain_is_trusted("reuters.com"))
        self.assertTrue(geo._domain_is_trusted("www.reuters.com"))
        self.assertFalse(geo._domain_is_trusted("reuters.com.attacker.invalid"))
        self.assertFalse(geo._domain_is_trusted("notreuters.com"))

    def test_article_host_must_match_declared_domain(self):
        good = {"url": "https://www.reuters.com/world/story", "domain": "reuters.com"}
        spoofed = {"url": "https://reuters.com.attacker.invalid/story", "domain": "reuters.com"}
        self.assertEqual(geo._article_domain(good), "reuters.com")
        self.assertEqual(geo._article_domain(spoofed), "")

    def test_only_recent_allowlisted_articles_are_retained(self):
        now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(minutes=30)).strftime("%Y%m%dT%H%M%SZ")
        old = (now - timedelta(days=4)).strftime("%Y%m%dT%H%M%SZ")
        rows = [
            {"title": "Recent Reuters report", "url": "https://www.reuters.com/world/a",
             "domain": "reuters.com", "seendate": recent},
            {"title": "Old Reuters report", "url": "https://www.reuters.com/world/b",
             "domain": "reuters.com", "seendate": old},
            {"title": "Untrusted report", "url": "https://example.invalid/story",
             "domain": "example.invalid", "seendate": recent},
            {"title": "Spoofed publisher", "url": "https://reuters.com.evil.invalid/story",
             "domain": "reuters.com", "seendate": recent},
        ]
        with patch.object(geo, "_now", return_value=now):
            result = geo._normalise_articles(rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "reuters.com")

    def test_independent_source_count_uses_distinct_allowlisted_domains(self):
        now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        rows = [
            {"title": "A", "url": "https://www.reuters.com/a", "domain": "reuters.com", "seendate": timestamp},
            {"title": "B", "url": "https://apnews.com/b", "domain": "apnews.com", "seendate": timestamp},
            {"title": "C", "url": "https://www.reuters.com/c", "domain": "reuters.com", "seendate": timestamp},
        ]
        with patch.object(geo, "_now", return_value=now):
            cleaned = geo._normalise_articles(rows)
        domains = {x["source"] for x in cleaned}
        self.assertEqual(len(domains), 2)


if __name__ == "__main__":
    unittest.main()
