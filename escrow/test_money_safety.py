"""Money safety: amount/currency checks, funding/payout idempotency, uncertain parking, dispute blocking auto-release."""
import datetime
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from escrow.models import (
    EscrowContract,
    PaymentTransaction,
    PayoutRecord,
    SettlementVault,
    WebhookEvent,
)
from escrow.services import (
    auto_release_expired_inspections,
    confirm_funding_payment,
    release_escrow_funds,
)


def make_contract(code, status='awaiting_funding', amount=Decimal('50000'),
                  fee=Decimal('1500')):
    return EscrowContract.objects.create(
        code=code, title='T', description='D', amount=amount,
        service_fee=fee, fee_payer='buyer', creator_email='b@x.com',
        creator_role='buyer', counterparty_email='s@x.com',
        status=status, gateway_reference=f'REF_{code}')


def add_vault(contract):
    return SettlementVault.objects.create(
        contract=contract, paypal_email='seller@example.com',
        bank_name='PayPal', account_name='seller@example.com')


class FundingVerificationTest(TestCase):
    def test_underpayment_rejected(self):
        contract = make_contract('M1')
        self.assertFalse(
            confirm_funding_payment(contract, contract.total_charge_kobo - 100))
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_funding')

    def test_overpayment_rejected(self):
        contract = make_contract('M2')
        self.assertFalse(
            confirm_funding_payment(contract, contract.total_charge_kobo + 100))

    def test_currency_mismatch_rejected(self):
        contract = make_contract('M3')
        self.assertFalse(confirm_funding_payment(
            contract, contract.total_charge_kobo, currency='EUR'))
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_funding')

    def test_matching_currency_accepted(self):
        contract = make_contract('M4')
        self.assertTrue(confirm_funding_payment(
            contract, contract.total_charge_kobo, currency='usd'))

    def test_funding_is_idempotent(self):
        contract = make_contract('M5')
        self.assertTrue(confirm_funding_payment(contract, contract.total_charge_kobo))
        self.assertFalse(confirm_funding_payment(contract, contract.total_charge_kobo))
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_dispatch')
        self.assertEqual(
            PaymentTransaction.objects.filter(
                provider_reference=contract.gateway_reference).count(), 1)

    def test_duplicate_webhook_event_detectable(self):
        WebhookEvent.objects.create(
            provider='paypal', event='PAYMENT.CAPTURE.COMPLETED', event_id='evt-1',
            signature_valid=True, payload={}, processed=True,
            processed_at=timezone.now())
        _, created = WebhookEvent.objects.get_or_create(
            provider='paypal', event='PAYMENT.CAPTURE.COMPLETED', event_id='evt-1',
            defaults={'signature_valid': True, 'payload': {}})
        self.assertFalse(created)


class PayoutSafetyTest(TestCase):
    def test_release_claims_once(self):
        contract = make_contract('P1', status='in_inspection')
        add_vault(contract)
        with patch('escrow.services.get_payment_gateway') as mock_gw:
            mock_gw.return_value.execute_seller_payout.return_value = {
                'status': True,
                'data': {'reference': 'TRF1', 'transfer_code': 'T1'}}
            first = release_escrow_funds(contract)
            second = release_escrow_funds(contract)
        self.assertTrue(first.success)
        self.assertFalse(second.success)
        self.assertEqual(
            PayoutRecord.objects.filter(escrow=contract, status='success').count(), 1)
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'completed')

    def test_gateway_timeout_parks_as_uncertain(self):
        contract = make_contract('P2', status='in_inspection')
        add_vault(contract)
        with patch('escrow.services.get_payment_gateway') as mock_gw:
            mock_gw.return_value.execute_seller_payout.side_effect = Exception('timeout')
            result = release_escrow_funds(contract)
        self.assertFalse(result.success)
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'pending_payout')
        self.assertEqual(
            PayoutRecord.objects.filter(escrow=contract, status='uncertain').count(), 1)

    def test_no_vault_no_payout(self):
        contract = make_contract('P3', status='in_inspection')
        result = release_escrow_funds(contract)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'missing_vault')
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'in_inspection')

    def test_dispute_blocks_auto_release(self):
        disputed = make_contract('P4', status='disputed')
        disputed.inspection_ends = timezone.now() - datetime.timedelta(hours=1)
        disputed.save(update_fields=['inspection_ends'])
        auto_release_expired_inspections()
        disputed.refresh_from_db()
        self.assertEqual(disputed.status, 'disputed')
