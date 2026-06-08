"""Tests for the ingestion layer — Step 52.

Covers the Document model, the ingestion service (routing + event emission),
the upload view, and the email-to-bill webhook. The headline behaviour the
execution plan asks us to verify — *upload a bill → it lands in the D01 AP
inbox* — is exercised end-to-end in both the service and the view tests.
"""

import shutil
import tempfile

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounting.models import (
    BusEvent,
    Currency,
    Membership,
    Tenant,
)
from agents.events import clear_subscriptions
from ingest.ingestion import IngestError, receive_document, receive_email
from ingest.models import Document


User = get_user_model()

# Run the event bus inline so emit() dispatches handlers without a Q2 worker.
_SYNC_Q = {'name': 'ealedgers', 'orm': 'default', 'sync': True, 'workers': 1}

_MEDIA = tempfile.mkdtemp(prefix='ingest-test-media-')


def _pdf(name='facture.pdf'):
    """A tiny in-memory PDF upload."""
    return SimpleUploadedFile(name, b'%PDF-1.4 fake bill bytes', content_type='application/pdf')


@override_settings(MEDIA_ROOT=_MEDIA)
class IngestScaffoldTests(TestCase):
    def test_app_is_installed(self):
        self.assertTrue(apps.is_installed('ingest'))

    def test_document_model_registered(self):
        self.assertTrue(apps.get_model('ingest', 'Document'))


@override_settings(MEDIA_ROOT=_MEDIA, Q_CLUSTER=_SYNC_Q)
class DocumentModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(
            code='XAF_doc', name='CFA Franc (doc tests)', decimal_places=0)
        cls.tenant = Tenant.objects.create(
            slug='doc-co', name='Doc Co', currency=cls.xaf)

    def setUp(self):
        clear_subscriptions()

    def test_compute_sha256_is_stable_and_restores_pointer(self):
        f = _pdf()
        first = Document.compute_sha256(f)
        # Pointer restored → a second hash matches and the file is still saveable.
        second = Document.compute_sha256(f)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertEqual(f.read(), b'%PDF-1.4 fake bill bytes')

    def test_inbox_for_returns_only_routed_docs_for_that_dept(self):
        routed = receive_document(self.tenant, file=_pdf(), doc_type='vendor_bill')
        # A different department's document must not leak into D01's inbox.
        receive_document(self.tenant, file=_pdf('inv.pdf'), doc_type='customer_invoice')

        inbox = Document.objects.inbox_for(self.tenant, 'D01')
        self.assertEqual(list(inbox), [routed])

    def test_inbox_is_tenant_scoped(self):
        other = Tenant.objects.create(slug='doc-other', name='Other', currency=self.xaf)
        receive_document(self.tenant, file=_pdf(), doc_type='vendor_bill')
        self.assertEqual(Document.objects.inbox_for(other, 'D01').count(), 0)


@override_settings(MEDIA_ROOT=_MEDIA, Q_CLUSTER=_SYNC_Q)
class ReceiveDocumentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(
            code='XAF_rcv', name='CFA Franc (rcv tests)', decimal_places=0)
        cls.tenant = Tenant.objects.create(
            slug='rcv-co', name='Rcv Co', currency=cls.xaf)

    def setUp(self):
        clear_subscriptions()

    def test_vendor_bill_lands_in_d01_inbox(self):
        doc = receive_document(self.tenant, file=_pdf(), doc_type='vendor_bill')
        self.assertEqual(doc.routed_dept, 'D01')
        self.assertEqual(doc.status, 'routed')
        self.assertTrue(doc.is_routed)
        self.assertIsNotNone(doc.routed_at)

    def test_file_metadata_is_captured(self):
        doc = receive_document(self.tenant, file=_pdf('bill.pdf'), doc_type='vendor_bill')
        self.assertEqual(doc.original_filename, 'bill.pdf')
        self.assertEqual(doc.content_type, 'application/pdf')
        self.assertGreater(doc.size_bytes, 0)
        self.assertEqual(len(doc.sha256), 64)
        self.assertTrue(doc.file.name)

    def test_unknown_doc_type_is_unrouted(self):
        doc = receive_document(self.tenant, file=_pdf(), doc_type='other')
        self.assertEqual(doc.routed_dept, '')
        self.assertEqual(doc.status, 'unrouted')
        self.assertFalse(doc.is_routed)

    def test_customer_invoice_routes_to_d02(self):
        doc = receive_document(self.tenant, file=_pdf(), doc_type='customer_invoice')
        self.assertEqual(doc.routed_dept, 'D02')

    def test_requires_file_or_text(self):
        with self.assertRaises(IngestError):
            receive_document(self.tenant, doc_type='vendor_bill')

    def test_text_only_document_is_allowed(self):
        doc = receive_document(
            self.tenant, raw_text='Bill body', doc_type='vendor_bill', source='email')
        self.assertEqual(doc.routed_dept, 'D01')
        self.assertFalse(doc.file)

    def test_emits_document_received_bus_event(self):
        doc = receive_document(self.tenant, file=_pdf(), doc_type='vendor_bill')
        evt = BusEvent.objects.filter(
            tenant=self.tenant, event_type='document.received').first()
        self.assertIsNotNone(evt)
        self.assertEqual(evt.chain_id, doc.chain_id)
        self.assertEqual(evt.payload['document_id'], doc.pk)
        self.assertEqual(evt.payload['doc_type'], 'vendor_bill')

    def test_emit_event_can_be_suppressed(self):
        receive_document(
            self.tenant, file=_pdf(), doc_type='vendor_bill', emit_event=False)
        self.assertFalse(BusEvent.objects.filter(
            tenant=self.tenant, event_type='document.received').exists())

    def test_full_chain_produces_d01_work_queued(self):
        """With the dispatcher registered, document.received → D01.work.queued,
        threaded on the same chain_id (the audit-chain guarantee)."""
        from agents.dispatcher import register_dispatcher
        register_dispatcher()

        doc = receive_document(self.tenant, file=_pdf(), doc_type='vendor_bill')

        queued = BusEvent.objects.filter(
            tenant=self.tenant, event_type='D01.work.queued').first()
        self.assertIsNotNone(queued)
        self.assertEqual(queued.chain_id, doc.chain_id)


@override_settings(MEDIA_ROOT=_MEDIA, Q_CLUSTER=_SYNC_Q)
class ReceiveEmailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(
            code='XAF_eml', name='CFA Franc (email tests)', decimal_places=0)
        cls.tenant = Tenant.objects.create(
            slug='eml-co', name='Eml Co', currency=cls.xaf)

    def setUp(self):
        clear_subscriptions()

    def test_attachments_become_vendor_bills(self):
        from django.utils.datastructures import MultiValueDict
        files = MultiValueDict({'attachment-1': [_pdf('a.pdf')], 'attachment-2': [_pdf('b.pdf')]})
        payload = {'sender': 'vendor@acme.test', 'subject': 'Invoice 42'}

        docs = receive_email(self.tenant, payload, files)

        self.assertEqual(len(docs), 2)
        for d in docs:
            self.assertEqual(d.source, 'email')
            self.assertEqual(d.doc_type, 'vendor_bill')
            self.assertEqual(d.routed_dept, 'D01')
            self.assertEqual(d.sender_email, 'vendor@acme.test')
        # One email = one causal chain.
        self.assertEqual(len({d.chain_id for d in docs}), 1)

    def test_body_only_email_still_lands(self):
        payload = {'sender': 'v@x.test', 'subject': 'Bill', 'body-plain': 'Amount due 100000'}
        docs = receive_email(self.tenant, payload, {})
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].routed_dept, 'D01')
        self.assertIn('100000', docs[0].raw_text)

    def test_disallowed_attachments_are_skipped(self):
        from django.utils.datastructures import MultiValueDict
        exe = SimpleUploadedFile('malware.exe', b'MZ', content_type='application/octet-stream')
        files = MultiValueDict({'attachment-1': [exe]})
        payload = {'sender': 'v@x.test', 'subject': 'x', 'body-plain': ''}
        docs = receive_email(self.tenant, payload, files)
        self.assertEqual(docs, [])


@override_settings(MEDIA_ROOT=_MEDIA, Q_CLUSTER=_SYNC_Q)
class DocumentUploadViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(
            code='XAF_upv', name='CFA Franc (upload view)', decimal_places=0)
        cls.tenant = Tenant.objects.create(
            slug='upv-co', name='Upv Co', currency=cls.xaf)
        cls.user = User.objects.create_user('uploader', 'u@x.test', 'pw')
        Membership.objects.create(user=cls.user, tenant=cls.tenant, role='owner')

    def setUp(self):
        clear_subscriptions()

    def test_login_required(self):
        resp = self.client.get(reverse('ingest:document_upload'))
        self.assertEqual(resp.status_code, 302)

    def test_get_renders_form(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse('ingest:document_upload'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Document Ingestion')

    def test_upload_routes_bill_to_inbox(self):
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse('ingest:document_upload'),
            {'doc_type': 'vendor_bill', 'file': _pdf()},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        inbox = Document.objects.inbox_for(self.tenant, 'D01')
        self.assertEqual(inbox.count(), 1)
        self.assertContains(resp, 'routed to')

    def test_upload_rejects_bad_file_type(self):
        self.client.force_login(self.user)
        bad = SimpleUploadedFile('notes.txt', b'hello', content_type='text/plain')
        resp = self.client.post(
            reverse('ingest:document_upload'),
            {'doc_type': 'vendor_bill', 'file': bad},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Document.objects.for_tenant(self.tenant).count(), 0)


@override_settings(MEDIA_ROOT=_MEDIA, Q_CLUSTER=_SYNC_Q, INGEST_WEBHOOK_TOKEN='')
class EmailWebhookViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(
            code='XAF_whk', name='CFA Franc (webhook)', decimal_places=0)
        cls.tenant = Tenant.objects.create(
            slug='acme', name='Acme', currency=cls.xaf)

    def setUp(self):
        clear_subscriptions()

    def test_webhook_resolves_tenant_from_recipient_and_creates_doc(self):
        resp = self.client.post(reverse('ingest:email_webhook'), {
            'recipient': 'bills+acme@inbound.ealedgers.com',
            'sender': 'vendor@v.test',
            'subject': 'Invoice',
            'file': _pdf(),
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['tenant'], 'acme')
        self.assertEqual(data['created'], 1)
        self.assertEqual(Document.objects.inbox_for(self.tenant, 'D01').count(), 1)

    def test_webhook_resolves_via_plain_local_part(self):
        resp = self.client.post(reverse('ingest:email_webhook'), {
            'recipient': 'acme@inbound.ealedgers.com',
            'body-plain': 'A bill',
        })
        self.assertEqual(resp.json()['tenant'], 'acme')

    def test_webhook_unknown_tenant_returns_422(self):
        resp = self.client.post(reverse('ingest:email_webhook'), {
            'recipient': 'nobody@inbound.ealedgers.com',
            'body-plain': 'x',
        })
        self.assertEqual(resp.status_code, 422)

    def test_webhook_get_not_allowed(self):
        resp = self.client.get(reverse('ingest:email_webhook'))
        self.assertEqual(resp.status_code, 405)

    @override_settings(INGEST_WEBHOOK_TOKEN='s3cret')
    def test_webhook_rejects_bad_token(self):
        resp = self.client.post(reverse('ingest:email_webhook'), {
            'recipient': 'acme@inbound.ealedgers.com',
            'body-plain': 'x',
        })
        self.assertEqual(resp.status_code, 403)

    @override_settings(INGEST_WEBHOOK_TOKEN='s3cret')
    def test_webhook_accepts_good_token_header(self):
        resp = self.client.post(
            reverse('ingest:email_webhook'),
            {'recipient': 'acme@inbound.ealedgers.com', 'body-plain': 'x'},
            HTTP_X_INGEST_TOKEN='s3cret',
        )
        self.assertEqual(resp.status_code, 200)


def tearDownModule():
    shutil.rmtree(_MEDIA, ignore_errors=True)
