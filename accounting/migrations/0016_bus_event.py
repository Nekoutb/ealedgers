# Generated for Step 43 — Event bus BusEvent model

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0015_approval_queue_item'),
    ]

    operations = [
        migrations.CreateModel(
            name='BusEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_type', models.CharField(
                    db_index=True, max_length=64,
                    help_text="Noun.verb event name, e.g. 'bill.posted', 'invoice.sent'.",
                )),
                ('payload', models.JSONField(
                    blank=True, default=dict,
                    help_text='Event-specific data dict (vendor_id, amount, …).',
                )),
                ('chain_id', models.CharField(
                    blank=True, db_index=True, max_length=64,
                    help_text='Causal-chain UUID threading this event to related events.',
                )),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('status', models.CharField(
                    choices=[
                        ('queued',     'Queued — waiting for worker'),
                        ('dispatched', 'Dispatched — all handlers ran'),
                        ('failed',     'Failed — one or more handlers errored'),
                    ],
                    db_index=True, default='queued', max_length=16,
                )),
                ('handler_count', models.IntegerField(
                    default=0,
                    help_text='Number of handlers that were called during dispatch.',
                )),
                ('error', models.TextField(
                    blank=True,
                    help_text='Concatenated errors from any handler that raised.',
                )),
                ('task_id', models.CharField(blank=True, max_length=128)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('dispatched_at', models.DateTimeField(blank=True, null=True)),
                ('tenant', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='bus_events',
                    to='accounting.tenant',
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='busevent',
            index=models.Index(
                fields=['tenant', 'event_type', '-created_at'],
                name='accounting_bus_evt_tenant_type_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='busevent',
            index=models.Index(
                fields=['tenant', 'status', '-created_at'],
                name='accounting_bus_evt_tenant_status_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='busevent',
            index=models.Index(
                fields=['chain_id'],
                name='accounting_bus_evt_chain_id_idx',
            ),
        ),
    ]
