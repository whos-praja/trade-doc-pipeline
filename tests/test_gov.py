"""End-to-end tests for the consent-gated ID pipeline. No API key required.

The vision call is the only non-deterministic part, so it is stubbed; everything
that matters for privacy -- the consent gate, minimisation, masking, the
deterministic decision, retention and erasure -- is exercised for real.

Run:  python -m tests.test_gov
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# Keys must exist before the modules that read them are imported. These are
# throwaway test keys generated per-run; never reuse a key from a test anywhere real.
os.environ.setdefault("CONSENT_SIGNING_KEY", "test-consent-signing-key")
os.environ.setdefault("BLIND_INDEX_PEPPER", "test-blind-index-pepper")
if not os.environ.get("FIELD_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["FIELD_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.gov import consent as consent_mod           # noqa: E402
from app.gov import redaction, verify                # noqa: E402
from app.gov.consent import ConsentError             # noqa: E402
from app.gov.schema import DocType, PURPOSES, get_purpose  # noqa: E402

# A SYNTHETIC test number: the sequential digits 2345678901 plus the correct Verhoeff
# check digit. Structurally valid so the checksum tests are meaningful; not issued to
# anyone. Never put a real person's ID number in a test file.
VALID_AADHAAR = "234567890124"


class TestVerhoeff(unittest.TestCase):
    def test_valid_number_passes(self):
        self.assertTrue(verify.verhoeff_ok(VALID_AADHAAR))

    def test_single_digit_typo_is_caught(self):
        """Verhoeff catches every single-digit error -- the OCR failure mode."""
        for i in range(len(VALID_AADHAAR)):
            for d in "0123456789":
                if d == VALID_AADHAAR[i]:
                    continue
                typo = VALID_AADHAAR[:i] + d + VALID_AADHAAR[i + 1:]
                self.assertFalse(verify.verhoeff_ok(typo), f"missed typo {typo}")

    def test_adjacent_transposition_is_caught(self):
        for i in range(len(VALID_AADHAAR) - 1):
            a, b = VALID_AADHAAR[i], VALID_AADHAAR[i + 1]
            if a == b:
                continue
            swapped = VALID_AADHAAR[:i] + b + a + VALID_AADHAAR[i + 2:]
            self.assertFalse(verify.verhoeff_ok(swapped), f"missed swap {swapped}")


class TestMrz(unittest.TestCase):
    def test_icao_published_examples(self):
        # From ICAO Doc 9303 Part 3, the canonical worked examples.
        self.assertEqual(verify.mrz_check_digit("L898902C<"), 3)
        self.assertEqual(verify.mrz_check_digit("740812"), 2)
        self.assertEqual(verify.mrz_check_digit("120415"), 9)

    def test_field_ok(self):
        self.assertTrue(verify.mrz_field_ok("L898902C<", "3"))
        self.assertFalse(verify.mrz_field_ok("L898902C<", "4"))


class TestIdStructure(unittest.TestCase):
    def test_aadhaar_rules(self):
        self.assertEqual(verify.check_id_number(DocType.AADHAAR, VALID_AADHAAR)[0], verify.VALID)
        # Leading 0/1 are never issued.
        self.assertEqual(verify.check_id_number(DocType.AADHAAR, "012345678901")[0], verify.INVALID)
        # Wrong length.
        self.assertEqual(verify.check_id_number(DocType.AADHAAR, "12345")[0], verify.INVALID)
        # Formatting must not matter.
        spaced = "2345 6789 0124"
        self.assertEqual(verify.check_id_number(DocType.AADHAAR, spaced)[0], verify.VALID)

    def test_pan_rules(self):
        self.assertEqual(verify.check_id_number(DocType.PAN, "ABCPE1234F")[0], verify.VALID)
        self.assertEqual(verify.check_id_number(DocType.PAN, "ABCDE1234")[0], verify.INVALID)
        # 'Z' is not a valid holder-type letter in position 4.
        self.assertEqual(verify.check_id_number(DocType.PAN, "ABCZE1234F")[0], verify.INVALID)

    def test_other_documents(self):
        self.assertEqual(verify.check_id_number(DocType.PASSPORT, "A1234567")[0], verify.VALID)
        # Q, X and Z are not used as the leading letter.
        for bad in ("Q1234567", "X1234567", "Z1234567"):
            self.assertEqual(verify.check_id_number(DocType.PASSPORT, bad)[0], verify.INVALID)
        # Wrong length is rejected.
        self.assertEqual(verify.check_id_number(DocType.PASSPORT, "A123456")[0], verify.INVALID)
        self.assertEqual(
            verify.check_id_number(DocType.DRIVING_LICENCE, "MH1220110012345")[0], verify.VALID)
        self.assertEqual(verify.check_id_number(DocType.VOTER_ID, "ABC1234567")[0], verify.VALID)


class TestDates(unittest.TestCase):
    def test_future_dob_rejected(self):
        future = (date.today() + timedelta(days=365)).strftime("%d/%m/%Y")
        self.assertEqual(verify.check_date("date_of_birth", future)[0], verify.INVALID)

    def test_expired_document_rejected(self):
        self.assertEqual(verify.check_date("expiry_date", "01/01/2020")[0], verify.INVALID)

    def test_age_derivation(self):
        today = date(2026, 8, 30)
        self.assertEqual(verify.age_on("30/08/2008", today=today), 18)
        self.assertEqual(verify.age_on("31/08/2008", today=today), 17)  # birthday tomorrow


class TestConsent(unittest.TestCase):
    def test_grant_derives_fields_from_purpose(self):
        rec = consent_mod.grant("subj-1", "age_verification", [DocType.AADHAAR])
        self.assertEqual(rec.fields, ["date_of_birth", "full_name"])
        self.assertNotIn("id_number", rec.fields)

    def test_caller_cannot_exceed_purpose_retention(self):
        with self.assertRaises(ConsentError):
            consent_mod.grant("subj-1", "age_verification", [DocType.AADHAAR],
                              retention_days=9999)

    def test_tampering_is_detected(self):
        rec = consent_mod.grant("subj-1", "age_verification", [DocType.AADHAAR])
        self.assertTrue(rec.signature_ok())
        rec.fields = rec.fields + ["id_number"]        # widen the grant after the fact
        self.assertFalse(rec.signature_ok())
        with self.assertRaises(ConsentError) as ctx:
            consent_mod.authorise(rec, DocType.AADHAAR)
        self.assertIn("signature", str(ctx.exception))

    def test_expired_consent_refused(self):
        rec = consent_mod.grant("subj-1", "age_verification", [DocType.AADHAAR],
                                validity_days=1)
        rec.expires_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
            timespec="seconds")
        rec.sign()
        with self.assertRaises(ConsentError) as ctx:
            consent_mod.authorise(rec, DocType.AADHAAR)
        self.assertIn("expired", str(ctx.exception))

    def test_withdrawn_consent_refused(self):
        rec = consent_mod.withdraw(consent_mod.grant("s", "age_verification", [DocType.AADHAAR]))
        with self.assertRaises(ConsentError) as ctx:
            consent_mod.authorise(rec, DocType.AADHAAR)
        self.assertIn("withdrawn", str(ctx.exception))

    def test_wrong_document_type_refused(self):
        rec = consent_mod.grant("s", "identity_verification", [DocType.AADHAAR])
        with self.assertRaises(ConsentError):
            consent_mod.authorise(rec, DocType.PASSPORT)

    def test_authorise_returns_only_available_fields(self):
        # kyc_onboarding wants an address, but a PAN card has none.
        rec = consent_mod.grant("s", "kyc_onboarding", [DocType.PAN])
        permitted = consent_mod.authorise(rec, DocType.PAN)
        self.assertNotIn("address", permitted)
        self.assertIn("full_name", permitted)


class TestRedaction(unittest.TestCase):
    def test_aadhaar_shows_only_last_four(self):
        masked = redaction.mask_id(DocType.AADHAAR, "2345 6789 0124")
        self.assertEqual(masked, "XXXX XXXX 0124")
        self.assertNotIn("2345", masked)

    def test_blind_index_is_format_insensitive_and_keyed(self):
        a = redaction.blind_index("2345 6789 0124")
        b = redaction.blind_index("234567890124")
        self.assertEqual(a, b)
        self.assertNotIn("234567890124", a)

    def test_aadhaar_full_storage_is_refused_even_when_requested(self):
        out = redaction.protect("id_number", VALID_AADHAAR, DocType.AADHAAR, store_full=True)
        self.assertEqual(out["full_storage"], "refused_by_policy")
        self.assertNotIn("ciphertext", out)
        self.assertNotIn(VALID_AADHAAR, json.dumps(out))


class TestPipelineDecision(unittest.TestCase):
    def setUp(self):
        from app.gov import pipeline
        self.pipeline = pipeline

    def _checks(self, statuses: dict) -> dict:
        fields = {n: {"status": s, "reason": s, "confidence": 0.9} for n, s in statuses.items()}
        counts = {verify.VALID: 0, verify.INVALID: 0, verify.UNVERIFIED: 0}
        for r in fields.values():
            counts[r["status"]] += 1
        return {"fields": fields, "summary": {
            "doc_type": "aadhaar", "valid": counts[verify.VALID],
            "invalid": counts[verify.INVALID], "unverified": counts[verify.UNVERIFIED],
            "total": len(fields), "has_hard_failure": counts[verify.INVALID] > 0}}

    OK_SIGNALS = {"document_type_seen": "aadhaar", "visible_tampering": False,
                  "appears_to_be_screen_or_photocopy": False, "image_quality": "good"}

    def test_all_valid_is_accepted(self):
        outcome, _ = self.pipeline.decide(
            self._checks({"id_number": verify.VALID}), self.OK_SIGNALS)
        self.assertEqual(outcome, self.pipeline.ACCEPTED)

    def test_hard_failure_is_rejected(self):
        outcome, why = self.pipeline.decide(
            self._checks({"id_number": verify.INVALID}), self.OK_SIGNALS)
        self.assertEqual(outcome, self.pipeline.REJECTED)
        self.assertIn("id_number", why)

    def test_unverified_never_auto_accepts(self):
        outcome, _ = self.pipeline.decide(
            self._checks({"id_number": verify.VALID, "full_name": verify.UNVERIFIED}),
            self.OK_SIGNALS)
        self.assertEqual(outcome, self.pipeline.REVIEW)

    def test_tampering_forces_review(self):
        signals = {**self.OK_SIGNALS, "visible_tampering": True}
        outcome, _ = self.pipeline.decide(self._checks({"id_number": verify.VALID}), signals)
        self.assertEqual(outcome, self.pipeline.REVIEW)

    def test_wrong_document_type_forces_review(self):
        signals = {**self.OK_SIGNALS, "document_type_seen": "pan"}
        outcome, _ = self.pipeline.decide(self._checks({"id_number": verify.VALID}), signals)
        self.assertEqual(outcome, self.pipeline.REVIEW)

    def test_age_verification_discards_the_date_of_birth(self):
        extracted = {"full_name": {"value": "A Person", "confidence": 0.97},
                     "date_of_birth": {"value": "30/08/2000", "confidence": 0.95}}
        out = self.pipeline.minimise(extracted, DocType.AADHAAR, "age_verification")
        self.assertTrue(out["date_of_birth"]["derived_only"])
        self.assertTrue(out["date_of_birth"]["is_over_18"])
        self.assertNotIn("2000", json.dumps(out["date_of_birth"]))


class TestStorageLifecycle(unittest.TestCase):
    """Full flow against a temporary database, with the vision call stubbed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch("app.gov.storage.DB_PATH", Path(self.tmp.name) / "gov.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        from app.gov import pipeline, storage
        self.pipeline, self.storage = pipeline, storage

    def _fake_extract(self, fields):
        def _inner(path, doc_type, permitted):
            self.assertEqual(sorted(permitted), sorted(fields))
            return {
                "fields": {n: {"value": v, "confidence": 0.95} for n, v in fields.items()},
                "signals": {"document_type_seen": doc_type.value, "visible_tampering": False,
                            "appears_to_be_screen_or_photocopy": False, "image_quality": "good"},
            }
        return _inner

    def test_end_to_end_then_withdrawal_erases(self):
        rec = consent_mod.grant("subject-42", "identity_verification", [DocType.AADHAAR])
        self.storage.save_consent(rec)

        doc = Path(self.tmp.name) / "card.png"
        doc.write_bytes(b"not-a-real-image")

        fields = {"full_name": "A Person", "date_of_birth": "30/08/1990",
                  "gender": "FEMALE", "id_number": VALID_AADHAAR}
        with mock.patch("app.gov.extractor.extract", self._fake_extract(fields)):
            out = self.pipeline.process(str(doc), rec.consent_id, DocType.AADHAAR,
                                        store_full=True)

        self.assertEqual(out["outcome"], self.pipeline.REVIEW,
                         "name/gender have no deterministic check, so REVIEW is correct")
        # The raw Aadhaar must not appear anywhere in what was stored.
        row = self.storage.get_verification(out["verification_id"])
        self.assertNotIn(VALID_AADHAAR, row["fields_json"])
        self.assertIn("XXXX", row["fields_json"])

        # Withdrawal erases the data and is itself audited.
        result = self.pipeline.withdraw_and_erase(rec.consent_id)
        self.assertEqual(result["rows_erased"], 1)
        self.assertIsNone(self.storage.get_verification(out["verification_id"]))
        actions = [e["action"] for e in self.storage.access_log_for(rec.consent_id)]
        for expected in ("CONSENT_GRANTED", "AUTHORISED", "EXTRACT",
                         "VERIFICATION_STORED", "CONSENT_WITHDRAWN", "ERASE"):
            self.assertIn(expected, actions)

    def test_no_consent_means_the_file_is_never_read(self):
        missing = Path(self.tmp.name) / "never-opened.png"   # deliberately absent
        with self.assertRaises(ConsentError):
            self.pipeline.process(str(missing), "cns_does_not_exist", DocType.AADHAAR)

    def test_retention_purge_erases_expired_rows(self):
        rec = consent_mod.grant("subject-9", "age_verification", [DocType.AADHAAR],
                                retention_days=1)
        self.storage.save_consent(rec)
        doc = Path(self.tmp.name) / "card.png"
        doc.write_bytes(b"x")

        fields = {"full_name": "A Person", "date_of_birth": "30/08/1990"}
        with mock.patch("app.gov.extractor.extract", self._fake_extract(fields)):
            out = self.pipeline.process(str(doc), rec.consent_id, DocType.AADHAAR)

        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec="seconds")
        self.assertEqual(len(self.storage.due_for_erasure(as_of=future)), 1)
        self.assertEqual(self.storage.purge_expired(as_of=future), 1)
        self.assertIsNone(self.storage.get_verification(out["verification_id"]))

    def test_subject_export_covers_consents_and_log(self):
        rec = consent_mod.grant("subject-7", "age_verification", [DocType.PAN])
        self.storage.save_consent(rec)
        export = self.storage.subject_export("subject-7")
        self.assertEqual(len(export["consents"]), 1)
        self.assertTrue(export["access_log"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
