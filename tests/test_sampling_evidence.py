
from __future__ import annotations

import gzip
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.sampling import iter_bulk  # noqa: E402


class RegistrySourceRowTests(unittest.TestCase):
    def test_source_evidence_includes_csv_header_and_original_row(self):
        csv_text = (
            "organisasjonsnummer;navn;organisasjonsform.kode;"
            "antallAnsatte;konkurs;underAvvikling;"
            "forretningsadresse.kommune;"
            "forretningsadresse.kommunenummer;"
            "naeringskode1.kode;naeringskode1.beskrivelse;"
            "hjemmeside;sisteInnsendteAarsregnskap\n"
            "123456789;Synthetic Example AS;AS;3;false;false;"
            "Oslo;0301;62.010;Computer programming;;2025\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.csv.gz"
            with gzip.open(
                path, "wt", encoding="utf-8", newline=""
            ) as handle:
                handle.write(csv_text)

            records = list(iter_bulk(path))

        self.assertEqual(len(records), 1)
        record = records[0]
        source_span = record["raw"]["__signalpost_source_row_text__"]

        self.assertIn("organisasjonsnummer;navn;", source_span)
        self.assertIn("123456789;Synthetic Example AS;AS;3;", source_span)
        self.assertEqual(record["organisation_number"], "123456789")


if __name__ == "__main__":
    unittest.main()
