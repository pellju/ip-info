import tempfile
import unittest
from datetime import date
from pathlib import Path

from importer import asn_rows, block_rows, date_from_filename, parse_asn


class ImportParsingTests(unittest.TestCase):
    def test_dates_support_two_and_four_digit_years(self):
        self.assertEqual(date_from_filename(Path("table-13-06-26.txt")), date(2026, 6, 13))
        self.assertEqual(date_from_filename(Path("asns-13-06-2026.csv")), date(2026, 6, 13))

    def test_asn_forms(self):
        self.assertEqual(parse_asn("AS64500"), 64500)
        self.assertEqual(parse_asn("64500"), 64500)

    def test_blocks_are_canonicalized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "table-01-01-2026.txt"
            path.write_text("192.0.2.9/24 64500\n2001:db8::/32 AS64501\n")
            self.assertEqual(list(block_rows(path)), [("192.0.2.0/24", 64500), ("2001:db8::/32", 64501)])

    def test_quoted_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "asns-01-01-2026.csv"
            path.write_text('asn,name,class,cc\nAS10,"Example, Inc.",Content,us\n')
            self.assertEqual(list(asn_rows(path)), [(10, "Example, Inc.", "Content", "US")])


if __name__ == "__main__":
    unittest.main()
