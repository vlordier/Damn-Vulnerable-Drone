from __future__ import annotations

import unittest

from summarize_antoine_sequence import exactly_two_publications


class SequenceSummaryTests(unittest.TestCase):
    def test_publication_count_requires_exact_integer_two(self) -> None:
        self.assertTrue(exactly_two_publications({"publishedMessages": 2}))
        for value in (2.0, True, "2", None):
            with self.subTest(value=value):
                self.assertFalse(exactly_two_publications({"publishedMessages": value}))


if __name__ == "__main__":
    unittest.main()
