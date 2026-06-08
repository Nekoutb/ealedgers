"""Ingestion forms — Step 52."""

from __future__ import annotations

from django import forms

from ingest.ingestion import IngestError, validate_upload
from ingest.models import DOC_TYPE_CHOICES


# The doc types a human would manually upload (the routable subset). We drop
# 'other' from the front of the list but keep it as the catch-all fallback.
_UPLOAD_DOC_TYPES = [c for c in DOC_TYPE_CHOICES if c[0] != 'other'] + [
    c for c in DOC_TYPE_CHOICES if c[0] == 'other'
]


class DocumentUploadForm(forms.Form):
    """Manual PDF/image upload of a single source document."""

    file = forms.FileField(
        label='Document (PDF or image)',
        widget=forms.ClearableFileInput(attrs={
            'accept': '.pdf,.jpg,.jpeg,.png,.tif,.tiff,.heic,.webp',
        }),
    )
    doc_type = forms.ChoiceField(
        label='Document type',
        choices=_UPLOAD_DOC_TYPES,
        initial='vendor_bill',
    )

    def clean_file(self):
        file = self.cleaned_data['file']
        try:
            validate_upload(file)
        except IngestError as exc:
            raise forms.ValidationError(str(exc))
        return file
