"""Load XAF currency, KISSYWEARS placeholder company, 5 default journals,
and the full SYSCOHADA chart of accounts from a CSV at BASE_DIR.

Idempotent — safe to re-run.
"""

import csv
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from accounting.models import Account, Company, Currency, Journal


# Maps the CSV's human-readable "Type" column (Odoo's account_type label)
# to our Account.TYPES code.
ODOO_TYPE_MAP = {
    'Receivable': 'receivable',
    'Payable': 'payable',
    'Bank and Cash': 'asset_cash',
    'Current Assets': 'asset_current',
    'Non-current Assets': 'asset_non_current',
    'Prepayments': 'asset_prepayments',
    'Fixed Assets': 'asset_fixed',
    'Credit Card': 'liability_credit_card',
    'Current Liabilities': 'liability_current',
    'Non-current Liabilities': 'liability_non_current',
    'Equity': 'equity',
    'Current Year Earnings': 'equity_unaffected',
    'Income': 'income',
    'Other Income': 'income_other',
    'Expenses': 'expense',
    'Depreciation': 'expense_depreciation',
    'Cost of Revenue': 'expense_direct_cost',
    'Other Expenses': 'expense_other',
    'Off-Balance Sheet': 'off_balance_sheet',
}

# Try these encodings in order — Excel COM CSV exports default to system locale (cp1252 on FR Windows)
ENCODINGS = ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1')


class Command(BaseCommand):
    help = 'Seed currencies, company, default journals, and the SYSCOHADA chart of accounts.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--chart',
            default=str(Path(settings.BASE_DIR) / 'chart_of_accounts.csv'),
            help='Path to the chart-of-accounts CSV (default: BASE_DIR/chart_of_accounts.csv)',
        )

    def handle(self, *args, **options):
        self._seed_currencies()
        self._seed_company()
        self._seed_journals()
        self._load_chart(options['chart'])
        self.stdout.write(self.style.SUCCESS('All seed data loaded.'))

    # ----- seed helpers -----------------------------------------------------

    def _seed_currencies(self):
        seeds = [
            ('XAF', 'CFA Franc BEAC', 'FCFA', 0),
            ('EUR', 'Euro', '€', 2),
            ('USD', 'US Dollar', '$', 2),
        ]
        for code, name, symbol, places in seeds:
            Currency.objects.update_or_create(
                code=code,
                defaults={
                    'name': name, 'symbol': symbol,
                    'decimal_places': places, 'active': True,
                },
            )
        self.stdout.write(f'Currencies: {", ".join(c for c, *_ in seeds)} (XAF default).')

    def _seed_company(self):
        xaf = Currency.objects.get(code='XAF')
        company, created = Company.objects.update_or_create(
            name='KISSYWEARS SARL',
            defaults={
                'legal_name': 'KISSYWEARS SARL',
                'currency': xaf,
                'country': 'Cameroon',
                'fiscal_year_start_month': 1,
                'active': True,
            },
        )
        self.stdout.write(f'Company: {company.name} ({"created" if created else "updated"}).')

    def _seed_journals(self):
        seeds = [
            ('VEN', 'Sales Journal', 'sale', 'VEN/'),
            ('ACH', 'Purchases Journal', 'purchase', 'ACH/'),
            ('BNK', 'Bank', 'bank', 'BNK/'),
            ('CAS', 'Cash', 'cash', 'CAS/'),
            ('OD', 'Miscellaneous Operations', 'general', 'OD/'),
        ]
        for code, name, jtype, prefix in seeds:
            Journal.objects.update_or_create(
                code=code,
                defaults={
                    'name': name, 'type': jtype,
                    'sequence_prefix': prefix, 'active': True,
                },
            )
        self.stdout.write(f'Journals: {len(seeds)} default journals seeded.')

    # ----- chart loader -----------------------------------------------------

    def _read_csv_text(self, path):
        for enc in ENCODINGS:
            try:
                with open(path, encoding=enc) as f:
                    return f.read()
            except UnicodeDecodeError:
                continue
        raise SystemExit(f'Could not decode {path} with any of {ENCODINGS}')

    def _load_chart(self, path):
        p = Path(path)
        if not p.exists():
            self.stdout.write(self.style.WARNING(
                f'Chart of accounts not found at {p}. Skipping chart load.'
            ))
            return
        text = self._read_csv_text(p)
        reader = csv.reader(text.splitlines(), delimiter=';')
        try:
            header = next(reader)
        except StopIteration:
            self.stdout.write(self.style.WARNING('CSV is empty. Skipping.'))
            return

        unknown_types = set()
        created = updated = skipped = 0
        for row in reader:
            if len(row) < 4 or not row[0]:
                skipped += 1
                continue
            code = row[0].strip()
            name = row[1].strip()
            type_label = row[2].strip()
            reconcile_str = row[3].strip().upper()
            if not code or not name:
                skipped += 1
                continue
            acct_type = ODOO_TYPE_MAP.get(type_label)
            if not acct_type:
                unknown_types.add(type_label)
                acct_type = 'asset_current'  # safe fallback
            reconcile = reconcile_str in ('VRAI', 'TRUE', '1', 'YES')
            _, was_created = Account.objects.update_or_create(
                code=code,
                defaults={'name': name, 'type': acct_type, 'reconcile': reconcile},
            )
            if was_created:
                created += 1
            else:
                updated += 1

        self.stdout.write(
            f'Chart of accounts: {created} created, {updated} updated, {skipped} skipped.'
        )
        if unknown_types:
            self.stdout.write(self.style.WARNING(
                f'Unknown type labels (fell back to asset_current): {sorted(unknown_types)}'
            ))
