"""Tests for the D01 AP department — Steps 51 and 53–56.

Coverage:
  - Specialist hierarchy (subclasses, specialist_type, implemented behaviours)
  - APExtractor.run() — Step 53 LLM extraction logic (mocked Anthropic client)
  - AnthropicBillExtractor unit tests (content block builder, response parser)
  - APClassifier.run() — Step 54 SYSCOHADA account classification (mocked client)
  - AnthropicLineClassifier unit tests (account loading, response parser, merge)
  - APProposer.run() — Step 55 candidate-JE builder (balanced entry, VAT,
    issue surfacing, ok=False hard failures) + money/parse helper units
  - APReviewer.run() — Step 56 adversarial citation check (approve/reject,
    anti-hallucination guard, structural pre-checks, ok=False infra failures)
  - APManager pipeline wiring (order, stop_on_failure, run-to-first-failure)
  - APDepartment class attributes (dept_code, dept_name, capabilities_needed)
  - APDepartment.handle() subscription gate (DepartmentDisabledError when disabled)
  - APDepartment.handle() returns a Proposal with correct fields when enabled
  - APDepartment.handle() chain_id threading
  - APDepartment.execute() — Step 58 ERP execution (CAP.05/CAP.03 via saga or
    local GL), execute_queue_item() approval bridge, ConnectorExecutionError
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
    ConnectorExecutionError,
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

    def test_classifier_run_returns_ok_true_for_empty_input(self):
        """APClassifier.run() no longer raises — empty lines returns ok=True."""
        result = APClassifier(_FakeTenant()).run({})
        self.assertTrue(result.ok)

    def test_reviewer_run_rejects_when_no_proposed_je(self):
        """APReviewer.run() no longer raises — missing entry is a rejection."""
        result = APReviewer(_FakeTenant()).run({})
        self.assertTrue(result.ok)
        self.assertFalse(result.output["approved"])


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

    # --- execute (Step 58 — implemented) ---

    def test_execute_raises_value_error_without_proposer_output(self):
        """A proposal whose pipeline halted before the proposer cannot be
        executed — execute() raises ValueError (no longer NotImplementedError)."""
        dept = self._dept()
        proposal = dept.handle({})  # no file → extractor fails → no proposer output
        with self.assertRaises(ValueError):
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


# ---------------------------------------------------------------------------
# Helpers for classifier tests
# ---------------------------------------------------------------------------

def _make_classify_mock_client(classified_lines: list):
    """Return a mock Anthropic client returning ``classified_lines`` as tool output."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "classify_lines"
    tool_block.input = {"classified_lines": classified_lines}

    mock_response = MagicMock()
    mock_response.stop_reason = "tool_use"
    mock_response.content = [tool_block]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    return mock_client


_SAMPLE_LINES = [
    {"description": "Telephone charges", "quantity": 1, "unit_price": 50000, "amount": 50000},
    {"description": "Office supplies", "quantity": 10, "unit_price": 2000, "amount": 20000},
]

_SAMPLE_CLASSIFIED = [
    {
        "line_index": 0,
        "suggested_account": "628100",
        "suggested_account_name": "Telephone charges",
        "confidence": "high",
        "reasoning": "Direct match to telecom account.",
    },
    {
        "line_index": 1,
        "suggested_account": "604100",
        "suggested_account_name": "Consumables",
        "confidence": "medium",
        "reasoning": "Office supplies map to consumables.",
    },
]


# ---------------------------------------------------------------------------
# APClassifier.run() — Step 54 integration-style tests (injected mock client)
# ---------------------------------------------------------------------------

class APClassifierTests(TestCase):
    """APClassifier.run() with a mocked Anthropic client and real DB accounts."""

    @classmethod
    def setUpTestData(cls):
        """Create a minimal tenant with two expense accounts for classifier tests."""
        from accounting.models import Currency, Tenant, Account

        cls.currency = Currency.objects.create(
            code="XAF_54a", name="CFA Franc (Step 54a)", symbol="XAF", decimal_places=0
        )
        cls.tenant = Tenant.objects.create(
            name="Classifier Test Co", slug="classifier-test-co-54", currency=cls.currency
        )
        # Two expense accounts for the tenant
        Account.objects.create(
            tenant=cls.tenant, code="628100", name="Telephone charges",
            type="expense_direct_cost", deprecated=False,
        )
        Account.objects.create(
            tenant=cls.tenant, code="604100", name="Consumables",
            type="expense", deprecated=False,
        )

    def _make_classifier(self, classified_lines=None):
        client = _make_classify_mock_client(classified_lines or _SAMPLE_CLASSIFIED)
        return APClassifier(self.tenant, classifier_client=client, classifier_model="test-model")

    # --- empty lines → ok=True, classified_lines=[] ---

    def test_run_returns_ok_true_for_empty_lines(self):
        classifier = APClassifier(self.tenant)
        result = classifier.run({"lines": []})
        self.assertTrue(result.ok)

    def test_run_empty_lines_output_is_empty_list(self):
        classifier = APClassifier(self.tenant)
        result = classifier.run({"lines": []})
        self.assertEqual(result.output["classified_lines"], [])

    def test_run_returns_ok_true_when_lines_key_absent(self):
        """Missing 'lines' key treated as empty list → ok=True."""
        classifier = APClassifier(self.tenant)
        result = classifier.run({})
        self.assertTrue(result.ok)

    # --- happy path ---

    def test_run_returns_ok_true_with_mock_client(self):
        result = self._make_classifier().run({"lines": _SAMPLE_LINES})
        self.assertTrue(result.ok)

    def test_run_output_contains_classified_lines(self):
        result = self._make_classifier().run({"lines": _SAMPLE_LINES})
        self.assertIn("classified_lines", result.output)

    def test_run_classified_lines_length_matches_input(self):
        result = self._make_classifier().run({"lines": _SAMPLE_LINES})
        self.assertEqual(len(result.output["classified_lines"]), len(_SAMPLE_LINES))

    def test_run_classified_line_has_suggested_account(self):
        result = self._make_classifier().run({"lines": _SAMPLE_LINES})
        line = result.output["classified_lines"][0]
        self.assertEqual(line["suggested_account"], "628100")

    def test_run_classified_line_has_confidence(self):
        result = self._make_classifier().run({"lines": _SAMPLE_LINES})
        line = result.output["classified_lines"][0]
        self.assertIn(line["confidence"], ("high", "medium", "low"))

    def test_run_classified_line_preserves_original_description(self):
        result = self._make_classifier().run({"lines": _SAMPLE_LINES})
        self.assertEqual(result.output["classified_lines"][0]["description"], "Telephone charges")

    def test_run_specialist_type_is_classifier_on_success(self):
        result = self._make_classifier().run({"lines": _SAMPLE_LINES})
        self.assertEqual(result.specialist_type, "classifier")

    # --- API error → ok=False ---

    def test_run_ok_false_on_api_error(self):
        bad_client = MagicMock()
        bad_client.messages.create.side_effect = RuntimeError("timeout")
        classifier = APClassifier(self.tenant, classifier_client=bad_client)
        result = classifier.run({"lines": _SAMPLE_LINES})
        self.assertFalse(result.ok)
        self.assertIn("timeout", result.error)

    # --- bad API response (no tool_use block) → ok=False ---

    def test_run_ok_false_when_no_tool_use_in_response(self):
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "I cannot classify."
        bad_resp = MagicMock()
        bad_resp.stop_reason = "end_turn"
        bad_resp.content = [text_block]
        bad_client = MagicMock()
        bad_client.messages.create.return_value = bad_resp
        classifier = APClassifier(self.tenant, classifier_client=bad_client)
        result = classifier.run({"lines": _SAMPLE_LINES})
        self.assertFalse(result.ok)

    # --- no accounts for tenant → ok=False ---

    def test_run_ok_false_when_no_accounts_for_tenant(self):
        from accounting.models import Currency, Tenant

        currency = Currency.objects.create(
            code="XAF_54b", name="CFA Franc (Step 54b)", symbol="XAF", decimal_places=0
        )
        empty_tenant = Tenant.objects.create(
            name="Empty Tenant 54", slug="empty-tenant-54", currency=currency
        )
        mock_client = _make_classify_mock_client(_SAMPLE_CLASSIFIED)
        classifier = APClassifier(empty_tenant, classifier_client=mock_client)
        result = classifier.run({"lines": _SAMPLE_LINES})
        self.assertFalse(result.ok)
        self.assertIn("accounts", result.error.lower())


# ---------------------------------------------------------------------------
# AnthropicLineClassifier unit tests — helpers + response parser
# ---------------------------------------------------------------------------

class AnthropicLineClassifierTests(TestCase):
    """Pure-unit tests for AnthropicLineClassifier helpers."""

    # --- _api_client raises ClassifierError without a key ---

    def test_api_client_raises_classifier_error_without_key(self):
        from agents.ap.classifier import AnthropicLineClassifier, ClassifierError
        from django.test import override_settings

        env_bak = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with override_settings(ANTHROPIC_API_KEY=""):
                clf = AnthropicLineClassifier()
                with self.assertRaises(ClassifierError):
                    _ = clf._api_client
        finally:
            if env_bak is not None:
                os.environ["ANTHROPIC_API_KEY"] = env_bak

    # --- _parse_response ---

    def test_parse_response_returns_classified_lines(self):
        from agents.ap.classifier import AnthropicLineClassifier

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "classify_lines"
        tool_block.input = {
            "classified_lines": [
                {"line_index": 0, "suggested_account": "628100",
                 "suggested_account_name": "Telephone charges", "confidence": "high"}
            ]
        }
        resp = MagicMock()
        resp.content = [tool_block]
        result = AnthropicLineClassifier._parse_response(resp)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["suggested_account"], "628100")

    def test_parse_response_raises_classifier_error_on_missing_tool_use(self):
        from agents.ap.classifier import AnthropicLineClassifier, ClassifierError

        text_block = MagicMock()
        text_block.type = "text"
        resp = MagicMock()
        resp.content = [text_block]
        resp.stop_reason = "end_turn"
        with self.assertRaises(ClassifierError):
            AnthropicLineClassifier._parse_response(resp)

    # --- _merge_results ---

    def test_merge_results_preserves_original_fields(self):
        from agents.ap.classifier import AnthropicLineClassifier

        originals = [{"description": "Test", "amount": 1000}]
        classifications = [
            {"line_index": 0, "suggested_account": "628100",
             "suggested_account_name": "Telephone charges", "confidence": "high",
             "reasoning": "Match."}
        ]
        merged = AnthropicLineClassifier._merge_results(originals, classifications)
        self.assertEqual(merged[0]["description"], "Test")
        self.assertEqual(merged[0]["amount"], 1000)
        self.assertEqual(merged[0]["suggested_account"], "628100")

    def test_merge_results_fills_missing_classification_with_low_confidence(self):
        """If Claude misses a line index, the output is still the same length."""
        from agents.ap.classifier import AnthropicLineClassifier

        originals = [{"description": "A"}, {"description": "B"}]
        classifications = [
            {"line_index": 0, "suggested_account": "628100",
             "suggested_account_name": "Telephone", "confidence": "high"}
        ]
        merged = AnthropicLineClassifier._merge_results(originals, classifications)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[1]["confidence"], "low")
        self.assertEqual(merged[1]["suggested_account"], "")


# ---------------------------------------------------------------------------
# APProposer.run() — Step 55 candidate journal-entry builder
# ---------------------------------------------------------------------------

def _proposer_context(**overrides):
    """A realistic merged extractor+classifier context for the proposer."""
    ctx = {
        "vendor_name": "MTN Cameroon",
        "vendor_vat": "M021500001234X",
        "invoice_date": "2026-03-15",
        "invoice_number": "FAC-2026-0042",
        "currency": "XAF",
        "subtotal": 70000,
        "tax_amount": 13475,
        "total": 83475,
        "classified_lines": [
            {"description": "Telephone charges", "amount": 50000,
             "suggested_account": "628100", "suggested_account_name": "Telephone charges",
             "confidence": "high"},
            {"description": "Office supplies", "amount": 20000,
             "suggested_account": "604100", "suggested_account_name": "Consumables",
             "confidence": "medium"},
        ],
    }
    ctx.update(overrides)
    return ctx


class APProposerTests(TestCase):
    """APProposer.run() builds a balanced SYSCOHADA vendor-bill JE from context."""

    @classmethod
    def setUpTestData(cls):
        from accounting.models import Currency, Tenant, Account, Journal

        cls.currency = Currency.objects.create(
            code="XAF_55", name="CFA Franc (Step 55)", symbol="XAF", decimal_places=0
        )
        cls.tenant = Tenant.objects.create(
            name="Proposer Test Co", slug="proposer-test-co-55", currency=cls.currency
        )
        # Expense accounts (classifier targets)
        Account.objects.create(tenant=cls.tenant, code="628100",
                               name="Telephone charges", type="expense_direct_cost")
        Account.objects.create(tenant=cls.tenant, code="604100",
                               name="Consumables", type="expense")
        # Supplier + VAT + suspense
        Account.objects.create(tenant=cls.tenant, code="401100",
                               name="Suppliers", type="payable", reconcile=True)
        Account.objects.create(tenant=cls.tenant, code="445200",
                               name="VAT recoverable on purchases", type="liability_current")
        # Purchase journal
        Journal.objects.create(tenant=cls.tenant, name="Purchases", code="ACH",
                               type="purchase")

    def _run(self, context=None):
        return APProposer(self.tenant).run(context if context is not None else _proposer_context())

    # --- happy path ---------------------------------------------------------

    def test_run_ok_true(self):
        self.assertTrue(self._run().ok)

    def test_proposed_je_present(self):
        out = self._run().output
        self.assertIn("proposed_je", out)
        self.assertIn("lines", out["proposed_je"])

    def test_entry_is_balanced(self):
        out = self._run().output
        self.assertTrue(out["balanced"])
        self.assertEqual(out["debit_total"], out["credit_total"])

    def test_expense_debits_and_vat_and_supplier_credit(self):
        lines = self._run().output["proposed_je"]["lines"]
        # 2 expense debits + 1 VAT debit + 1 supplier credit
        self.assertEqual(len(lines), 4)
        accounts = [ln["account"] for ln in lines]
        self.assertEqual(accounts, ["628100", "604100", "445200", "401100"])

    def test_debit_total_equals_sum_of_lines_plus_vat(self):
        out = self._run().output
        # 50000 + 20000 expenses + 13475 VAT = 83475
        self.assertEqual(out["debit_total"], "83475.00")

    def test_supplier_credit_is_total_ttc(self):
        lines = self._run().output["proposed_je"]["lines"]
        supplier = lines[-1]
        self.assertEqual(supplier["account"], "401100")
        self.assertEqual(supplier["credit"], "83475.00")
        self.assertEqual(supplier["debit"], "0.00")
        self.assertEqual(supplier["partner_vat"], "M021500001234X")

    def test_total_matches_extracted(self):
        out = self._run().output
        self.assertTrue(out["total_matches"])
        self.assertFalse(out["needs_review"])
        self.assertEqual(out["issues"], [])

    def test_je_header_fields(self):
        je = self._run().output["proposed_je"]
        self.assertEqual(je["date"], "2026-03-15")
        self.assertEqual(je["ref"], "FAC-2026-0042")
        self.assertEqual(je["journal_code"], "ACH")

    # --- VAT handling -------------------------------------------------------

    def test_no_tax_means_no_vat_line(self):
        ctx = _proposer_context(tax_amount=0, total=70000)
        lines = self._run(ctx).output["proposed_je"]["lines"]
        accounts = [ln["account"] for ln in lines]
        self.assertNotIn("445200", accounts)
        self.assertEqual(len(lines), 3)  # 2 expenses + supplier

    # --- discrepancy surfacing ---------------------------------------------

    def test_total_mismatch_flags_issue_but_stays_balanced(self):
        # Stated total (99999) does not equal expenses+VAT (83475)
        ctx = _proposer_context(total=99999)
        out = self._run(ctx).output
        self.assertTrue(out["balanced"])          # entry still balances internally
        self.assertFalse(out["total_matches"])
        self.assertTrue(out["needs_review"])
        self.assertTrue(any("differs" in m for m in out["issues"]))

    def test_low_confidence_line_sets_needs_review(self):
        ctx = _proposer_context()
        ctx["classified_lines"][1]["confidence"] = "low"
        out = self._run(ctx).output
        self.assertTrue(out["needs_review"])

    def test_unknown_account_line_is_skipped_with_issue(self):
        ctx = _proposer_context()
        ctx["classified_lines"][1]["suggested_account"] = "999999"  # not in chart
        out = self._run(ctx).output
        accounts = [ln["account"] for ln in out["proposed_je"]["lines"]]
        self.assertNotIn("999999", accounts)
        self.assertTrue(any("not in the tenant" in m for m in out["issues"]))
        self.assertTrue(out["needs_review"])

    def test_line_missing_account_is_skipped(self):
        ctx = _proposer_context()
        ctx["classified_lines"][1]["suggested_account"] = ""
        out = self._run(ctx).output
        self.assertTrue(any("no account suggested" in m for m in out["issues"]))

    # --- hard failures → ok=False (never raises) ---------------------------

    def test_no_lines_returns_ok_false(self):
        ctx = _proposer_context(classified_lines=[])
        result = self._run(ctx)
        self.assertFalse(result.ok)
        self.assertIn("no classified line items", result.error)

    def test_all_lines_unusable_returns_ok_false(self):
        ctx = _proposer_context()
        for ln in ctx["classified_lines"]:
            ln["suggested_account"] = ""
        result = self._run(ctx)
        self.assertFalse(result.ok)

    def test_no_supplier_account_returns_ok_false(self):
        from accounting.models import Currency, Tenant, Account, Journal
        cur = Currency.objects.create(code="XAF_55b", name="x", symbol="X", decimal_places=0)
        bare = Tenant.objects.create(name="Bare", slug="bare-55b", currency=cur)
        Account.objects.create(tenant=bare, code="628100", name="Tel", type="expense")
        result = APProposer(bare).run(_proposer_context())
        self.assertFalse(result.ok)
        self.assertIn("supplier", result.error.lower())


class SyscohadaBillProposerUnitTests(TestCase):
    """Unit tests for the pure money/parse helpers in agents.ap.proposer."""

    def test_to_decimal_parses_numbers_and_strings(self):
        from agents.ap.proposer import _to_decimal
        from decimal import Decimal
        self.assertEqual(_to_decimal(1000), Decimal("1000"))
        self.assertEqual(_to_decimal("2500.50"), Decimal("2500.50"))

    def test_to_decimal_returns_none_for_blank_or_bad(self):
        from agents.ap.proposer import _to_decimal
        self.assertIsNone(_to_decimal(None))
        self.assertIsNone(_to_decimal(""))
        self.assertIsNone(_to_decimal("not-a-number"))

    def test_money_renders_two_decimals(self):
        from agents.ap.proposer import _money
        from decimal import Decimal
        self.assertEqual(_money(Decimal("83475")), "83475.00")


# ---------------------------------------------------------------------------
# APReviewer.run() — Step 56 adversarial citation check
# ---------------------------------------------------------------------------

def _make_review_mock_client(verdict="approve", citations=None, review_notes="Looks compliant."):
    """Return a mock Anthropic client whose response is a review_journal_entry tool_use."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "review_journal_entry"
    tool_block.input = {
        "verdict": verdict,
        "citations": citations if citations is not None else [],
        "review_notes": review_notes,
    }
    mock_response = MagicMock()
    mock_response.content = [tool_block]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    return mock_client


def _reviewer_context(**overrides):
    """A realistic merged pipeline context as it reaches the reviewer."""
    ctx = {
        "vendor_name": "MTN Cameroon",
        "vendor_vat": "M021500001234X",
        "currency": "XAF",
        "total": 83475,
        "tax_amount": 13475,
        "classified_lines": [
            {"description": "Telephone charges", "amount": 50000,
             "suggested_account": "628100", "confidence": "high"},
        ],
        "issues": [],
        "proposed_je": {
            "date": "2026-03-15",
            "ref": "FAC-2026-0042",
            "journal_code": "ACH",
            "lines": [
                {"account": "628100", "label": "Telephone charges",
                 "debit": "70000.00", "credit": "0.00", "partner_vat": ""},
                {"account": "445200", "label": "TVA récupérable",
                 "debit": "13475.00", "credit": "0.00", "partner_vat": ""},
                {"account": "401100", "label": "MTN Cameroon",
                 "debit": "0.00", "credit": "83475.00",
                 "partner_vat": "M021500001234X"},
            ],
        },
    }
    ctx.update(overrides)
    return ctx


class _FakeRuleObj:
    """Bare object carrying the source_text attr _format_rules reads."""

    def __init__(self, source_text):
        self.source_text = source_text


def _fake_retrieved():
    """Deterministic stand-in for knowledge.retrieval.retrieve() results."""
    return [
        {
            "kind": "rule",
            "slug": "test-cgi-telephone-charges",
            "title": "Telephone charges deductible",
            "framework": "CGI-2025",
            "score": 9,
            "source_ref": "CGI 2025 art. 7-A",
            "object": _FakeRuleObj(
                "Telephone charges are deductible when incurred for business."),
        },
        {
            "kind": "rule",
            "slug": "test-cgi-tva-recoverable",
            "title": "TVA recoverable on vendor bill purchases",
            "framework": "CGI-2025",
            "score": 7,
            "source_ref": "CGI 2025 art. 132",
            "object": _FakeRuleObj(
                "TVA on purchases is recoverable when a compliant invoice exists."),
        },
    ]


class APReviewerTests(TestCase):
    """APReviewer.run() with a mocked Anthropic client and patched retrieval.

    Retrieval is patched (rather than seeding Rule rows) so ranking against
    the ~100 framework rules the data migrations load into the test DB can
    never make these tests flaky.
    """

    @classmethod
    def setUpTestData(cls):
        from accounting.models import Currency, Tenant

        cls.currency = Currency.objects.create(
            code="XAF_56", name="CFA Franc (Step 56)", symbol="XAF", decimal_places=0
        )
        cls.tenant = Tenant.objects.create(
            name="Reviewer Test Co", slug="reviewer-test-co-56", currency=cls.currency
        )

    def _run(self, context=None, *, verdict="approve", citations=None, notes="ok",
             retrieved=None):
        from unittest.mock import patch
        client = _make_review_mock_client(verdict=verdict, citations=citations, review_notes=notes)
        reviewer = APReviewer(self.tenant, reviewer_client=client, reviewer_model="test-model")
        with patch(
            "agents.ap.reviewer._retrieve_rules",
            return_value=_fake_retrieved() if retrieved is None else retrieved,
        ):
            result = reviewer.run(context if context is not None else _reviewer_context())
        return result, client

    # --- approve path --------------------------------------------------------

    def test_approve_verdict_with_passing_citations(self):
        result, _ = self._run(
            citations=[{"rule_slug": "test-cgi-telephone-charges", "verdict": "pass",
                        "notes": "Business telecom expense."}],
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.output["approved"])

    def test_output_has_required_keys(self):
        result, _ = self._run()
        for key in ("approved", "citations", "review_notes", "structural_issues"):
            self.assertIn(key, result.output)

    def test_citation_source_ref_comes_from_database(self):
        """source_ref is re-read from the Rule row, not taken from the model."""
        result, _ = self._run(
            citations=[{"rule_slug": "test-cgi-telephone-charges", "verdict": "pass",
                        "notes": "x"}],
        )
        cite = result.output["citations"][0]
        self.assertEqual(cite["source_ref"], "CGI 2025 art. 7-A")

    # --- reject paths ---------------------------------------------------------

    def test_reject_verdict_is_ok_true_approved_false(self):
        result, _ = self._run(verdict="reject", notes="Violates deductibility cap.")
        self.assertTrue(result.ok)
        self.assertFalse(result.output["approved"])
        self.assertIn("Violates", result.output["review_notes"])

    def test_fail_citation_overrides_approve_verdict(self):
        """Even if the model says approve, a 'fail' citation forces rejection."""
        result, _ = self._run(
            verdict="approve",
            citations=[{"rule_slug": "test-cgi-tva-recoverable", "verdict": "fail",
                        "notes": "No compliant invoice."}],
        )
        self.assertFalse(result.output["approved"])

    # --- anti-hallucination guard ---------------------------------------------

    def test_unknown_rule_slug_citation_is_dropped(self):
        result, _ = self._run(
            citations=[
                {"rule_slug": "invented-rule-slug", "verdict": "fail", "notes": "fake"},
                {"rule_slug": "test-cgi-telephone-charges", "verdict": "pass", "notes": "real"},
            ],
        )
        slugs = [c["rule_slug"] for c in result.output["citations"]]
        self.assertEqual(slugs, ["test-cgi-telephone-charges"])
        self.assertIn("anti-hallucination", result.output["review_notes"])

    def test_dropped_fail_citation_does_not_block_approval(self):
        """A hallucinated 'fail' must not reject the entry once discarded."""
        result, _ = self._run(
            citations=[{"rule_slug": "invented-rule-slug", "verdict": "fail", "notes": "fake"}],
        )
        self.assertTrue(result.output["approved"])

    # --- structural rejection (no API call) ------------------------------------

    def test_unbalanced_entry_rejected_without_api_call(self):
        ctx = _reviewer_context()
        ctx["proposed_je"]["lines"][0]["debit"] = "999999.00"
        result, client = self._run(ctx)
        self.assertTrue(result.ok)
        self.assertFalse(result.output["approved"])
        self.assertTrue(result.output["structural_issues"])
        client.messages.create.assert_not_called()

    def test_missing_proposed_je_rejected(self):
        result, client = self._run({"vendor_name": "X"})
        self.assertFalse(result.output["approved"])
        client.messages.create.assert_not_called()

    # --- no relevant rules → conservative reject -------------------------------

    def test_no_retrieved_rules_means_could_not_verify(self):
        result, client = self._run(retrieved=[])
        self.assertTrue(result.ok)
        self.assertFalse(result.output["approved"])
        self.assertIn("Manual review required", result.output["review_notes"])
        client.messages.create.assert_not_called()

    # --- reviewer infrastructure failures → ok=False ----------------------------

    def test_api_error_returns_ok_false(self):
        from unittest.mock import patch
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("connection reset")
        reviewer = APReviewer(self.tenant, reviewer_client=client, reviewer_model="test-model")
        with patch("agents.ap.reviewer._retrieve_rules", return_value=_fake_retrieved()):
            result = reviewer.run(_reviewer_context())
        self.assertFalse(result.ok)
        self.assertIn("Review failed", result.error)

    def test_no_tool_use_block_returns_ok_false(self):
        from unittest.mock import patch
        text_block = MagicMock()
        text_block.type = "text"
        response = MagicMock()
        response.content = [text_block]
        response.stop_reason = "end_turn"
        client = MagicMock()
        client.messages.create.return_value = response
        reviewer = APReviewer(self.tenant, reviewer_client=client, reviewer_model="test-model")
        with patch("agents.ap.reviewer._retrieve_rules", return_value=_fake_retrieved()):
            result = reviewer.run(_reviewer_context())
        self.assertFalse(result.ok)

    # --- retrieval integration (real retrieve() against migration-loaded rules)

    def test_real_retrieval_returns_results_for_bill_query(self):
        """Integration: the real knowledge base yields candidate rules for a
        typical vendor-bill query (the data migrations load ~100 rules)."""
        from agents.ap.reviewer import _retrieve_rules
        retrieved = _retrieve_rules(_reviewer_context(), self.tenant)
        self.assertTrue(retrieved)
        self.assertTrue(all("slug" in r and "source_ref" in r for r in retrieved))


class ReviewerStructuralChecksTests(TestCase):
    """Unit tests for agents.ap.reviewer.structural_issues (pure, no DB)."""

    def _je(self, lines, date="2026-03-15"):
        return {"date": date, "ref": "X", "journal_code": "ACH", "lines": lines}

    def test_sound_entry_has_no_issues(self):
        je = self._je([
            {"account": "628100", "debit": "100.00", "credit": "0.00"},
            {"account": "401100", "debit": "0.00", "credit": "100.00"},
        ])
        from agents.ap.reviewer import structural_issues
        self.assertEqual(structural_issues(je), [])

    def test_empty_lines_flagged(self):
        from agents.ap.reviewer import structural_issues
        self.assertEqual(structural_issues(self._je([])), ["The proposed entry has no lines."])

    def test_unbalanced_flagged(self):
        from agents.ap.reviewer import structural_issues
        issues = structural_issues(self._je([
            {"account": "628100", "debit": "100.00", "credit": "0.00"},
            {"account": "401100", "debit": "0.00", "credit": "90.00"},
        ]))
        self.assertTrue(any("unbalanced" in i for i in issues))

    def test_negative_amount_flagged(self):
        from agents.ap.reviewer import structural_issues
        issues = structural_issues(self._je([
            {"account": "628100", "debit": "-100.00", "credit": "0.00"},
            {"account": "401100", "debit": "0.00", "credit": "-100.00"},
        ]))
        self.assertTrue(any("negative" in i for i in issues))

    def test_double_sided_line_flagged(self):
        from agents.ap.reviewer import structural_issues
        issues = structural_issues(self._je([
            {"account": "628100", "debit": "100.00", "credit": "100.00"},
        ]))
        self.assertTrue(any("both a debit and a credit" in i for i in issues))

    def test_missing_account_flagged(self):
        from agents.ap.reviewer import structural_issues
        issues = structural_issues(self._je([
            {"account": "", "debit": "100.00", "credit": "0.00"},
            {"account": "401100", "debit": "0.00", "credit": "100.00"},
        ]))
        self.assertTrue(any("no account code" in i for i in issues))

    def test_zero_total_flagged(self):
        from agents.ap.reviewer import structural_issues
        issues = structural_issues(self._je([
            {"account": "628100", "debit": "0.00", "credit": "0.00"},
            {"account": "401100", "debit": "0.00", "credit": "0.00"},
        ]))
        self.assertTrue(any("zero" in i for i in issues))

    def test_missing_date_flagged(self):
        from agents.ap.reviewer import structural_issues
        issues = structural_issues(self._je([
            {"account": "628100", "debit": "100.00", "credit": "0.00"},
            {"account": "401100", "debit": "0.00", "credit": "100.00"},
        ], date=""))
        self.assertTrue(any("no date" in i for i in issues))


# ---------------------------------------------------------------------------
# APDepartment.execute() — Step 58 ERP execution (CAP.05 + CAP.03)
# ---------------------------------------------------------------------------

class _FakeConnector:
    """Minimal IConnector-shaped fake for execute() tests."""

    vendor = "fake"

    def __init__(self, caps=("CAP.03",), result=None, raise_exc=None):
        self._caps = frozenset(caps)
        self._result = result or {"external_id": 555, "state": "posted"}
        self._raise = raise_exc
        self.calls = []

    def supports(self, code):
        return code in self._caps

    def post_journal_entry(self, **kw):
        self.calls.append(("CAP.03", kw))
        if self._raise:
            raise self._raise
        return dict(self._result)

    def execute(self, code, **kw):
        self.calls.append((code, kw))
        if self._raise:
            raise self._raise
        return dict(self._result)


def _je_lines():
    return [
        {"account": "628100", "label": "Telephone", "debit": "70000.00",
         "credit": "0.00", "partner_vat": ""},
        {"account": "445200", "label": "TVA récupérable", "debit": "13475.00",
         "credit": "0.00", "partner_vat": ""},
        {"account": "401100", "label": "MTN Cameroon", "debit": "0.00",
         "credit": "83475.00", "partner_vat": "M0215X"},
    ]


def _proposal_with_je(tenant_id, lines="default", journal="ACH"):
    from agents.department import Proposal, SpecialistResult
    je = {"date": "2026-03-15", "ref": "FAC-0042", "journal_code": journal,
          "lines": _je_lines() if lines == "default" else lines}
    return Proposal(
        dept_code="D01", tenant_id=tenant_id, action="Post vendor bill",
        inputs={}, chain_id="chain-58",
        specialist_results=[SpecialistResult("proposer", {"proposed_je": je}, True)],
    )


class APExecuteTests(TestCase):
    """APDepartment.execute() posts an approved proposal to the connector."""

    @classmethod
    def setUpTestData(cls):
        from accounting.models import Currency, Tenant
        cls.currency = Currency.objects.create(
            code="XAF_58", name="CFA (58)", symbol="XAF", decimal_places=0)
        cls.tenant = Tenant.objects.create(
            name="Exec Co", slug="exec-co-58", currency=cls.currency)

    # --- CAP.03 direct (standalone, no ERPConnection) -----------------------

    def test_execute_posts_via_cap03(self):
        fake = _FakeConnector(caps=("CAP.03",))
        dept = APDepartment(self.tenant, connector=fake)
        result = dept.execute(_proposal_with_je(self.tenant.id))
        self.assertTrue(result["posted"])
        self.assertEqual(result["capability"], "CAP.03")
        self.assertEqual(result["external_id"], 555)
        self.assertIsNone(result["operation_id"])  # no saga (standalone)
        # the connector received mapped lines (account_code, post=True)
        cap, kw = fake.calls[0]
        self.assertEqual(cap, "CAP.03")
        self.assertTrue(kw["post"])
        self.assertEqual(kw["lines"][0]["account_code"], "628100")
        self.assertEqual(kw["lines"][0]["name"], "Telephone")
        self.assertEqual(kw["journal_code"], "ACH")

    def test_execute_prefers_cap05_when_supported(self):
        fake = _FakeConnector(caps=("CAP.03", "CAP.05"), result={"external_id": 9, "state": "posted"})
        dept = APDepartment(self.tenant, connector=fake)
        result = dept.execute(_proposal_with_je(self.tenant.id))
        self.assertEqual(result["capability"], "CAP.05")
        self.assertEqual(fake.calls[0][0], "CAP.05")

    def test_execute_raises_without_proposed_je(self):
        from agents.department import Proposal, SpecialistResult
        proposal = Proposal(
            dept_code="D01", tenant_id=self.tenant.id, action="x", inputs={},
            specialist_results=[SpecialistResult("extractor", {}, False, "boom")],
        )
        dept = APDepartment(self.tenant, connector=_FakeConnector())
        with self.assertRaises(ValueError):
            dept.execute(proposal)

    def test_execute_raises_with_empty_lines(self):
        dept = APDepartment(self.tenant, connector=_FakeConnector())
        with self.assertRaises(ValueError):
            dept.execute(_proposal_with_je(self.tenant.id, lines=[]))

    # --- saga path (real ERPConnection present) -----------------------------

    def test_execute_uses_saga_when_connection_exists(self):
        from accounting.models import ERPConnection, ERPOperation
        ERPConnection.objects.create(
            tenant=self.tenant, name="Test Odoo", vendor="odoo", is_active=True)
        fake = _FakeConnector(caps=("CAP.03",), result={"external_id": 77, "state": "posted"})
        dept = APDepartment(self.tenant, connector=fake)
        result = dept.execute(_proposal_with_je(self.tenant.id))
        self.assertEqual(result["external_id"], 77)
        self.assertIsNotNone(result["operation_id"])
        op = ERPOperation.objects.get(pk=result["operation_id"])
        self.assertEqual(op.status, "success")
        self.assertEqual(op.external_ids, {"external_id": 77})

    def test_execute_raises_connector_error_on_saga_failure(self):
        from accounting.models import ERPConnection, ERPOperation
        ERPConnection.objects.create(
            tenant=self.tenant, name="Test Odoo", vendor="odoo", is_active=True)
        fake = _FakeConnector(raise_exc=RuntimeError("odoo down"))
        dept = APDepartment(self.tenant, connector=fake)
        with self.assertRaises(ConnectorExecutionError):
            dept.execute(_proposal_with_je(self.tenant.id))
        self.assertEqual(ERPOperation.objects.filter(status="failed").count(), 1)

    def test_manual_connection_is_treated_as_standalone(self):
        from accounting.models import ERPConnection
        ERPConnection.objects.create(
            tenant=self.tenant, name="None", vendor="manual", is_active=True)
        fake = _FakeConnector()
        result = APDepartment(self.tenant, connector=fake).execute(
            _proposal_with_je(self.tenant.id))
        self.assertIsNone(result["operation_id"])  # manual => no saga


class APExecuteLocalGLTests(TestCase):
    """execute() against the real LocalGLConnector posts to the local ledger."""

    @classmethod
    def setUpTestData(cls):
        from accounting.models import Currency, Tenant, Account, Journal
        cls.currency = Currency.objects.create(
            code="XAF_58L", name="CFA (58L)", symbol="XAF", decimal_places=0)
        cls.tenant = Tenant.objects.create(
            name="Local Exec Co", slug="local-exec-58", currency=cls.currency)
        for code, name, typ in [
            ("628100", "Telephone charges", "expense_direct_cost"),
            ("445200", "VAT recoverable", "liability_current"),
            ("401100", "Suppliers", "payable"),
        ]:
            Account.objects.create(tenant=cls.tenant, code=code, name=name, type=typ)
        Journal.objects.create(tenant=cls.tenant, name="Purchases", code="ACH",
                               type="purchase")

    def test_execute_posts_balanced_entry_to_local_gl(self):
        from accounting.models import JournalEntry, JournalEntryLine
        # No connector injected → connector_for_tenant => LocalGLConnector
        dept = APDepartment(self.tenant)
        result = dept.execute(_proposal_with_je(self.tenant.id))
        self.assertTrue(result["posted"])
        self.assertEqual(result["capability"], "CAP.03")
        entry = JournalEntry.objects.get(pk=result["external_id"])
        self.assertEqual(entry.state, "posted")
        self.assertEqual(entry.tenant_id, self.tenant.id)
        lines = JournalEntryLine.objects.filter(entry=entry)
        self.assertEqual(lines.count(), 3)
        total_debit = sum(l.debit for l in lines)
        total_credit = sum(l.credit for l in lines)
        self.assertEqual(total_debit, total_credit)


class APExecuteQueueItemTests(TestCase):
    """execute_queue_item() bridges an approved ApprovalQueueItem to execute()."""

    @classmethod
    def setUpTestData(cls):
        from accounting.models import Currency, Tenant, ApprovalQueueItem
        cls.currency = Currency.objects.create(
            code="XAF_58Q", name="CFA (58Q)", symbol="XAF", decimal_places=0)
        cls.tenant = Tenant.objects.create(
            name="Queue Exec Co", slug="queue-exec-58", currency=cls.currency)

    def _item(self, status="approved", results=None):
        from accounting.models import ApprovalQueueItem
        return ApprovalQueueItem.objects.create(
            tenant=self.tenant, dept_code="D01", action="Post vendor bill",
            inputs={}, chain_id="chain-58q", status=status,
            specialist_results=results if results is not None else [
                {"specialist_type": "proposer", "ok": True, "error": "",
                 "output": {"proposed_je": {"date": "2026-03-15", "ref": "F1",
                            "journal_code": "ACH", "lines": _je_lines()}}},
            ],
        )

    def test_execute_queue_item_marks_executed(self):
        item = self._item()
        fake = _FakeConnector(result={"external_id": 321, "state": "posted"})
        result = APDepartment(self.tenant, connector=fake).execute_queue_item(item)
        item.refresh_from_db()
        self.assertEqual(item.status, "executed")
        self.assertEqual(item.metadata["execution"]["external_id"], 321)
        self.assertEqual(result["external_id"], 321)

    def test_execute_queue_item_rejects_unapproved(self):
        item = self._item(status="pending")
        with self.assertRaises(ValueError):
            APDepartment(self.tenant, connector=_FakeConnector()).execute_queue_item(item)
        item.refresh_from_db()
        self.assertEqual(item.status, "pending")

    def test_execute_queue_item_marks_failed_on_error(self):
        from accounting.models import ERPConnection
        ERPConnection.objects.create(
            tenant=self.tenant, name="Odoo", vendor="odoo", is_active=True)
        item = self._item()
        fake = _FakeConnector(raise_exc=RuntimeError("boom"))
        with self.assertRaises(ConnectorExecutionError):
            APDepartment(self.tenant, connector=fake).execute_queue_item(item)
        item.refresh_from_db()
        self.assertEqual(item.status, "execution_failed")
        self.assertIn("boom", item.review_note)
