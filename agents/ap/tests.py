"""Tests for the D01 AP department — Steps 51 and 53.

Coverage:
  - Specialist hierarchy (subclasses, specialist_type, stubs raise NotImplementedError)
  - APExtractor.run() — Step 53 LLM extraction logic (mocked Anthropic client)
  - AnthropicBillExtractor unit tests (content block builder, response parser)
  - APManager pipeline wiring (order, stop_on_failure, run-to-first-failure)
  - APDepartment class attributes (dept_code, dept_name, capabilities_needed)
  - APDepartment.handle() subscription gate (DepartmentDisabledError when disabled)
  - APDepartment.handle() returns a Proposal with correct fields when enabled
  - APDepartment.handle() chain_id threading
  - APDepartment.execute() stub raises NotImplementedError
  - APDepartment.can_auto_approve() default False
"""

import os
import tempfile
from unittest.mock import MagicMock

from django.test import TestCase

from agents.ap.department import (
    APClassifier,
    APDepartment,
    APExtractor,
    APManager,
    APProposer,
    APReviewer,
)
from agents.department import (
    BaseDepartment,
    DepartmentDisabledError,
    DepartmentManager,
    DepartmentSpecialist,
    Proposal,
)


# ---------------------------------------------------------------------------
# Minimal tenant stub (no DB access needed for pure-unit tests)
# ---------------------------------------------------------------------------

class _FakeTenant:
    """Lightweight stand-in for a real Tenant model instance."""

    id = 9901
    plan = "starter"
    agent_enabled = True

    def __repr__(self):
        return "<FakeTenant#9901>"


# ---------------------------------------------------------------------------
# Specialist hierarchy tests
# ---------------------------------------------------------------------------

class APSpecialistHierarchyTests(TestCase):
    """Each AP specialist must be a proper DepartmentSpecialist subclass."""

    # --- subclass checks ---

    def test_extractor_is_specialist(self):
        self.assertTrue(issubclass(APExtractor, DepartmentSpecialist))

    def test_classifier_is_specialist(self):
        self.assertTrue(issubclass(APClassifier, DepartmentSpecialist))

    def test_reviewer_is_specialist(self):
        self.assertTrue(issubclass(APReviewer, DepartmentSpecialist))

    def test_proposer_is_specialist(self):
        self.assertTrue(issubclass(APProposer, DepartmentSpecialist))

    # --- specialist_type checks ---

    def test_extractor_specialist_type(self):
        self.assertEqual(APExtractor(_FakeTenant()).specialist_type, "extractor")

    def test_classifier_specialist_type(self):
        self.assertEqual(APClassifier(_FakeTenant()).specialist_type, "classifier")

    def test_reviewer_specialist_type(self):
        self.assertEqual(APReviewer(_FakeTenant()).specialist_type, "reviewer")

    def test_proposer_specialist_type(self):
        self.assertEqual(APProposer(_FakeTenant()).specialist_type, "proposer")

    # --- extractor returns ok=False on empty input (Step 53 implemented) ---

    def test_extractor_run_returns_ok_false_without_file_path(self):
        """APExtractor.run() no longer raises — returns SpecialistResult(ok=False)."""
        result = APExtractor(_FakeTenant()).run({})
        self.assertFalse(result.ok)

    def test_classifier_run_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            APClassifier(_FakeTenant()).run({})

    def test_reviewer_run_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            APReviewer(_FakeTenant()).run({})

    def test_proposer_run_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            APProposer(_FakeTenant()).run({})


# ---------------------------------------------------------------------------
# APManager pipeline wiring tests (pure-unit, no DB)
# ---------------------------------------------------------------------------

class APManagerTests(TestCase):
    """APManager must assemble the correct pipeline in the correct order."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        tenant = _FakeTenant()
        # Build a department shell without hitting the DB (no is_enabled() call here)
        dept = APDepartment.__new__(APDepartment)
        dept.tenant = tenant
        dept._connector = None
        dept._manager = APManager(dept)
        cls.manager = APManager(dept)

    def test_manager_is_department_manager(self):
        self.assertIsInstance(self.manager, DepartmentManager)

    def test_pipeline_has_four_specialists(self):
        self.assertEqual(len(self.manager.specialists), 4)

    def test_pipeline_order_extractor_first(self):
        self.assertIsInstance(self.manager.specialists[0], APExtractor)

    def test_pipeline_order_classifier_second(self):
        self.assertIsInstance(self.manager.specialists[1], APClassifier)

    def test_pipeline_order_proposer_third(self):
        self.assertIsInstance(self.manager.specialists[2], APProposer)

    def test_pipeline_order_reviewer_last(self):
        self.assertIsInstance(self.manager.specialists[3], APReviewer)

    def test_stop_on_failure_is_true(self):
        self.assertTrue(self.manager.stop_on_failure)

    def test_run_halts_after_first_failure(self):
        """Pipeline halts at APExtractor (file not found); only 1 result returned."""
        results = self.manager.run({"file_path": "no/such/bill.pdf"})
        # stop_on_failure=True means we get exactly 1 result (the failed extractor)
        self.assertEqual(len(results), 1)

    def test_run_first_result_is_failed(self):
        results = self.manager.run({})
        self.assertFalse(results[0].ok)

    def test_run_first_result_mentions_missing_file(self):
        """Extractor returns ok=False because input_data has no file_path."""
        results = self.manager.run({})
        self.assertIn("file_path", results[0].error)

    def test_run_first_result_specialist_type_is_extractor(self):
        results = self.manager.run({})
        self.assertEqual(results[0].specialist_type, "extractor")


# ---------------------------------------------------------------------------
# APDepartment class-attribute tests (pure-unit, no DB)
# ---------------------------------------------------------------------------

class APDepartmentHierarchyTests(TestCase):
    """Static checks: ABCs, class attributes, manager type."""

    def test_is_base_department(self):
        self.assertTrue(issubclass(APDepartment, BaseDepartment))

    def test_dept_code_is_d01(self):
        self.assertEqual(APDepartment.dept_code, "D01")

    def test_dept_name(self):
        self.assertEqual(APDepartment.dept_name, "AP — Accounts Payable")

    def test_capabilities_includes_cap03(self):
        self.assertIn("CAP.03", APDepartment.capabilities_needed)

    def test_capabilities_includes_cap05(self):
        self.assertIn("CAP.05", APDepartment.capabilities_needed)

    def test_manager_attribute_is_ap_manager(self):
        dept = APDepartment(_FakeTenant())
        self.assertIsInstance(dept.manager, APManager)

    def test_manager_department_back_reference(self):
        dept = APDepartment(_FakeTenant())
        self.assertIs(dept.manager.department, dept)


# ---------------------------------------------------------------------------
# APDepartment.handle() — uses DB for TenantDepartmentSubscription
# ---------------------------------------------------------------------------

class APDepartmentHandleTests(TestCase):
    """handle() subscription gate and Proposal construction."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        from accounting.models import Currency, Tenant, TenantDepartmentSubscription

        User = get_user_model()
        cls.currency = Currency.objects.create(
            code="XAF_51a",
            name="CFA Franc (Step 51a)",
            symbol="XAF",
            decimal_places=0,
        )
        cls.tenant = Tenant.objects.create(
            name="AP Scaffold Co",
            slug="ap-scaffold-co",
            currency=cls.currency,
        )
        cls.user = User.objects.create_user("ap51_user", password="pw51")
        # Active D01 subscription for the primary test tenant
        TenantDepartmentSubscription.objects.create(
            tenant=cls.tenant,
            department="D01",
            active=True,
        )

    def _dept(self) -> APDepartment:
        return APDepartment(self.tenant)

    # --- gate: subscription absent ---

    def test_handle_raises_when_not_subscribed(self):
        from accounting.models import Currency, Tenant

        currency = Currency.objects.create(
            code="XAF_51b", name="CFA Franc (Step 51b)", symbol="XAF", decimal_places=0
        )
        other = Tenant.objects.create(
            name="No Sub Co", slug="no-sub-co-51", currency=currency
        )
        with self.assertRaises(DepartmentDisabledError):
            APDepartment(other).handle({"document_path": "/tmp/x.pdf"})

    # --- gate: inactive subscription ---

    def test_handle_raises_when_subscription_inactive(self):
        from accounting.models import Currency, Tenant, TenantDepartmentSubscription

        currency = Currency.objects.create(
            code="XAF_51c", name="CFA Franc (Step 51c)", symbol="XAF", decimal_places=0
        )
        inactive_tenant = Tenant.objects.create(
            name="Inactive Sub Co", slug="inactive-sub-51", currency=currency
        )
        TenantDepartmentSubscription.objects.create(
            tenant=inactive_tenant, department="D01", active=False
        )
        with self.assertRaises(DepartmentDisabledError):
            APDepartment(inactive_tenant).handle({})

    # --- successful handle path ---

    def test_handle_returns_proposal(self):
        proposal = self._dept().handle({})
        self.assertIsInstance(proposal, Proposal)

    def test_handle_proposal_dept_code_is_d01(self):
        proposal = self._dept().handle({})
        self.assertEqual(proposal.dept_code, "D01")

    def test_handle_proposal_tenant_id(self):
        proposal = self._dept().handle({})
        self.assertEqual(proposal.tenant_id, self.tenant.id)

    def test_handle_proposal_all_ok_false_while_stubs(self):
        """Proposal.all_ok is False: extractor gets no file_path, returns ok=False."""
        proposal = self._dept().handle({})
        self.assertFalse(proposal.all_ok)

    def test_handle_threads_chain_id(self):
        proposal = self._dept().handle({"chain_id": "test-chain-51"})
        self.assertEqual(proposal.chain_id, "test-chain-51")

    def test_handle_generates_chain_id_when_absent(self):
        proposal = self._dept().handle({})
        self.assertTrue(proposal.chain_id)  # non-empty

    # --- execute stub ---

    def test_execute_raises_not_implemented(self):
        dept = self._dept()
        proposal = dept.handle({})
        with self.assertRaises(NotImplementedError):
            dept.execute(proposal)

    # --- can_auto_approve default ---

    def test_can_auto_approve_is_false_by_default(self):
        dept = self._dept()
        proposal = dept.handle({})
        self.assertFalse(dept.can_auto_approve(proposal))


# ---------------------------------------------------------------------------
# Helpers shared by extractor tests
# ---------------------------------------------------------------------------

def _make_mock_anthropic_client(extraction: dict):
    """Return a mock Anthropic client that returns ``extraction`` as tool_use input."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "extract_bill"
    tool_block.input = extraction

    mock_response = MagicMock()
    mock_response.stop_reason = "tool_use"
    mock_response.content = [tool_block]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    return mock_client


_SAMPLE_EXTRACTION = {
    "vendor_name": "Orange Cameroon SA",
    "vendor_vat": "M012345678",
    "invoice_date": "2024-01-15",
    "due_date": "2024-02-15",
    "invoice_number": "FAC-2024-001",
    "currency": "XAF",
    "subtotal": 500000,
    "tax_amount": 96250,
    "total": 596250,
    "lines": [
        {
            "description": "Abonnement internet Pro",
            "quantity": 1,
            "unit_price": 500000,
            "amount": 500000,
        }
    ],
}


# ---------------------------------------------------------------------------
# APExtractor.run() — Step 53 integration-style tests (injected mock client)
# ---------------------------------------------------------------------------

class APExtractorTests(TestCase):
    """APExtractor.run() with a mocked Anthropic client and real temp files."""

    # --- missing file_path ---

    def test_run_ok_false_when_no_file_path(self):
        result = APExtractor(_FakeTenant()).run({})
        self.assertFalse(result.ok)

    def test_run_error_mentions_file_path_key(self):
        result = APExtractor(_FakeTenant()).run({})
        self.assertIn("file_path", result.error)

    def test_run_specialist_type_is_extractor_on_empty_input(self):
        result = APExtractor(_FakeTenant()).run({})
        self.assertEqual(result.specialist_type, "extractor")

    # --- file not found ---

    def test_run_ok_false_when_file_not_found(self):
        result = APExtractor(_FakeTenant()).run({"file_path": "no/such/bill.pdf"})
        self.assertFalse(result.ok)

    def test_run_error_mentions_not_found_for_missing_file(self):
        result = APExtractor(_FakeTenant()).run({"file_path": "no/such/bill.pdf"})
        self.assertIn("not found", result.error.lower())

    # --- document_path alias ---

    def test_run_accepts_document_path_alias(self):
        """document_path is an alias for file_path; missing file ≠ missing key."""
        result = APExtractor(_FakeTenant()).run({"document_path": "no/such/bill.pdf"})
        # Fails with "not found" rather than "file_path" key error
        self.assertFalse(result.ok)
        self.assertNotIn("No file_path", result.error)

    # --- happy path with mock client ---

    def _run_with_mock(self, extra_input=None):
        """Create a temp PDF, run APExtractor with a mock client, clean up."""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 fake content for test")
            tmp_path = f.name
        try:
            client = _make_mock_anthropic_client(_SAMPLE_EXTRACTION)
            extractor = APExtractor(_FakeTenant(), extractor_client=client)
            input_data = {"file_path": tmp_path, **(extra_input or {})}
            result = extractor.run(input_data)
        finally:
            os.unlink(tmp_path)
        return result

    def test_run_returns_ok_true_with_mock_client(self):
        self.assertTrue(self._run_with_mock().ok)

    def test_run_output_contains_vendor_name(self):
        result = self._run_with_mock()
        self.assertEqual(result.output["vendor_name"], "Orange Cameroon SA")

    def test_run_output_contains_total(self):
        result = self._run_with_mock()
        self.assertEqual(result.output["total"], 596250)

    def test_run_output_contains_lines(self):
        result = self._run_with_mock()
        self.assertIsInstance(result.output["lines"], list)
        self.assertEqual(len(result.output["lines"]), 1)

    def test_run_output_specialist_type_is_extractor_on_success(self):
        self.assertEqual(self._run_with_mock().specialist_type, "extractor")

    # --- API error → ok=False (no crash) ---

    def test_run_ok_false_on_api_error(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 fake")
            tmp_path = f.name
        try:
            bad_client = MagicMock()
            bad_client.messages.create.side_effect = RuntimeError("network error")
            extractor = APExtractor(_FakeTenant(), extractor_client=bad_client)
            result = extractor.run({"file_path": tmp_path})
        finally:
            os.unlink(tmp_path)
        self.assertFalse(result.ok)
        self.assertIn("network error", result.error)

    # --- bad API response (no tool_use block) → ok=False ---

    def test_run_ok_false_when_no_tool_use_in_response(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 fake")
            tmp_path = f.name
        try:
            # Response with a text block instead of a tool_use block
            text_block = MagicMock()
            text_block.type = "text"
            text_block.text = "I cannot extract this."
            mock_resp = MagicMock()
            mock_resp.stop_reason = "end_turn"
            mock_resp.content = [text_block]
            bad_client = MagicMock()
            bad_client.messages.create.return_value = mock_resp

            extractor = APExtractor(_FakeTenant(), extractor_client=bad_client)
            result = extractor.run({"file_path": tmp_path})
        finally:
            os.unlink(tmp_path)
        self.assertFalse(result.ok)


# ---------------------------------------------------------------------------
# AnthropicBillExtractor unit tests — content block builder + response parser
# ---------------------------------------------------------------------------

class AnthropicBillExtractorTests(TestCase):
    """Pure-unit tests for AnthropicBillExtractor helpers."""

    def _ext(self):
        from agents.ap.extractor import AnthropicBillExtractor
        return AnthropicBillExtractor()

    # --- _api_client raises ExtractorError without a key ---

    def test_api_client_raises_extractor_error_without_key(self):
        from agents.ap.extractor import AnthropicBillExtractor, ExtractorError
        from django.test import override_settings

        env_bak = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with override_settings(ANTHROPIC_API_KEY=""):
                ext = AnthropicBillExtractor()  # no injected client
                with self.assertRaises(ExtractorError):
                    _ = ext._api_client
        finally:
            if env_bak is not None:
                os.environ["ANTHROPIC_API_KEY"] = env_bak

    # --- _build_content_block ---

    def test_pdf_content_block_type_is_document(self):
        from agents.ap.extractor import AnthropicBillExtractor
        block = AnthropicBillExtractor._build_content_block(b"data", "application/pdf")
        self.assertEqual(block["type"], "document")

    def test_pdf_content_block_media_type(self):
        from agents.ap.extractor import AnthropicBillExtractor
        block = AnthropicBillExtractor._build_content_block(b"data", "application/pdf")
        self.assertEqual(block["source"]["media_type"], "application/pdf")

    def test_jpeg_content_block_type_is_image(self):
        from agents.ap.extractor import AnthropicBillExtractor
        block = AnthropicBillExtractor._build_content_block(b"data", "image/jpeg")
        self.assertEqual(block["type"], "image")

    def test_png_content_block_media_type(self):
        from agents.ap.extractor import AnthropicBillExtractor
        block = AnthropicBillExtractor._build_content_block(b"data", "image/png")
        self.assertEqual(block["source"]["media_type"], "image/png")

    # --- _parse_response ---

    def test_parse_response_returns_input_dict(self):
        from agents.ap.extractor import AnthropicBillExtractor
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "extract_bill"
        tool_block.input = {"vendor_name": "ACME", "total": 1000, "lines": []}
        resp = MagicMock()
        resp.content = [tool_block]
        result = AnthropicBillExtractor._parse_response(resp)
        self.assertEqual(result["vendor_name"], "ACME")

    def test_parse_response_raises_extractor_error_on_missing_tool_use(self):
        from agents.ap.extractor import AnthropicBillExtractor, ExtractorError
        text_block = MagicMock()
        text_block.type = "text"
        resp = MagicMock()
        resp.content = [text_block]
        resp.stop_reason = "end_turn"
        with self.assertRaises(ExtractorError):
            AnthropicBillExtractor._parse_response(resp)
