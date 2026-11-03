"""Regression tests for the post-analysis fix pass."""
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from escrow.forms import ContractCreationForm
from escrow.models import AuthOTP, EscrowContract, PayoutRecord, RefundRecord, SettlementVault
from escrow.services import initiate_refund, settle_pending_payout


def make_contract(code='FIX01', status='pending_payout', amount=Decimal('50000')):
    return EscrowContract.objects.create(
        code=code, title='T', description='D', amount=amount,
        service_fee=Decimal('1500'), fee_payer='buyer',
        creator_email='b@x.com', creator_role='buyer',
        counterparty_email='s@x.com', status=status,
        gateway_reference=f'REF_{code}')


def add_vault(contract):
    return SettlementVault.objects.create(
        contract=contract, paypal_email='seller@example.com',
        bank_name='PayPal', account_name='seller@example.com')


class AtomicTransitionTest(TestCase):
    def test_stale_instance_loses_cleanly(self):
        from escrow.models import TransitionError
        contract = make_contract(code='FIX02', status='awaiting_counterparty')
        first = EscrowContract.objects.get(code='FIX02')
        second = EscrowContract.objects.get(code='FIX02')
        first.transition_to('awaiting_funding')
        with self.assertRaises(TransitionError):
            second.transition_to('cancelled')
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_funding')


class SettleClaimTest(TestCase):
    def setUp(self):
        self.contract = make_contract(code='FIX03')
        add_vault(self.contract)
        patcher = patch('escrow.services.get_payment_gateway')
        self.mock_gw = patcher.start()
        self.addCleanup(patcher.stop)

    def test_pending_transfer_parks_then_webhook_completes(self):
        self.mock_gw.return_value.execute_seller_payout.return_value = {
            'status': True,
            'data': {'status': 'pending', 'transfer_code': 'T9',
                     'reference': 'R9'}}
        result = settle_pending_payout(self.contract)
        self.assertTrue(result.success)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'pending_payout')
        self.assertEqual(
            PayoutRecord.objects.get(escrow=self.contract).status, 'initiated')

        # A second settle must never send a second transfer.
        again = settle_pending_payout(self.contract)
        self.assertFalse(again.success)
        self.assertEqual(again.reason, 'payout_in_progress')
        self.assertEqual(
            self.mock_gw.return_value.execute_seller_payout.call_count, 1)

        payload = {'event_type': 'PAYMENT.PAYOUTS-ITEM.SUCCEEDED', 'id': 'evt-payout-1',
                   'resource': {'payout_batch_id': 'R9', 'sender_batch_id': 'locktrans-FIX03'}}
        with patch('escrow.views.get_payment_gateway') as mock_wh:
            mock_wh.return_value.verify_incoming_webhook.return_value = payload
            response = self.client.post(
                '/webhooks/paypal/', data=json.dumps(payload),
                content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'completed')
        self.assertEqual(
            PayoutRecord.objects.get(escrow=self.contract).status, 'success')

    def test_failed_transfer_keeps_pending_payout(self):
        PayoutRecord.objects.create(
            escrow=self.contract, amount=Decimal('50000'), currency='USD',
            provider_transfer_code='T8', transfer_reference='R8',
            status='initiated')
        payload = {'event_type': 'PAYMENT.PAYOUTS-ITEM.FAILED', 'id': 'evt-payout-2',
                   'resource': {'payout_batch_id': 'R8', 'sender_batch_id': 'locktrans-FIX03'}}
        with patch('escrow.views.get_payment_gateway') as mock_wh:
            mock_wh.return_value.verify_incoming_webhook.return_value = payload
            response = self.client.post(
                '/webhooks/paypal/', data=json.dumps(payload),
                content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'pending_payout')
        self.assertEqual(
            PayoutRecord.objects.get(escrow=self.contract).status, 'initiated')


class RefundTest(TestCase):
    @override_settings(DEMO_MODE=True)
    def test_refund_initiated_once(self):
        contract = make_contract(code='FIX04', status='cancelled')
        first = initiate_refund(contract)
        second = initiate_refund(contract)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.status, 'initiated')
        self.assertEqual(
            RefundRecord.objects.filter(escrow=contract).count(), 1)


class FeeValidationTest(TestCase):
    def test_seller_cannot_pay_fee_larger_than_amount(self):
        # USD tiers start at $1.50, MIN_ESCROW_AMOUNT is $5, so no valid
        # amount fails the seller-fee check. Below-minimum amounts still fail.
        form = ContractCreationForm({
            'title': 'T', 'description': 'D', 'amount': '1',
            'fee_payer': 'seller', 'creator_email': 'a@x.com',
            'creator_role': 'buyer', 'counterparty_email': 'b@x.com',
            'inspection_days': 3})
        self.assertFalse(form.is_valid())

    def test_buyer_fee_on_small_amount_ok(self):
        form = ContractCreationForm({
            'title': 'T', 'description': 'D', 'amount': '1000',
            'fee_payer': 'buyer', 'creator_email': 'a@x.com',
            'creator_role': 'buyer', 'counterparty_email': 'b@x.com',
            'inspection_days': 3})
        self.assertTrue(form.is_valid(), form.errors)


class FailClosedTest(TestCase):
    def test_whatsapp_signature_rejected_without_secret_in_prod(self):
        from whatsapp.adapter import verify_signature
        with override_settings(WHATSAPP_APP_SECRET='', DEBUG=False):
            self.assertFalse(verify_signature(b'{}', 'sha256=x'))
        with override_settings(WHATSAPP_APP_SECRET='', DEBUG=True):
            self.assertTrue(verify_signature(b'{}', 'sha256=x'))

    def test_paypal_email_rejects_invalid_without_lookup_in_prod(self):
        from escrow.bank_verification import verify_paypal_email
        with override_settings(DEBUG=False, DEMO_MODE=False):
            result = verify_paypal_email('not-an-email')
        self.assertFalse(result['status'])


class HealthCheckTest(TestCase):
    def test_healthz_ok(self):
        response = self.client.get('/healthz')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'ok': True})


class OtpRaceTest(TestCase):
    def test_wrong_code_burns_exactly_one_attempt(self):
        from escrow.services import issue_otp, consume_otp
        with patch('escrow.services.send_otp_via_termii', return_value=True):
            issue_otp('race@x.com', 'reauth')
        for _ in range(2):
            result = consume_otp('race@x.com', '000000', 'reauth')
            self.assertFalse(result.ok)
        record = AuthOTP.objects.get(email='race@x.com', intent='reauth')
        self.assertEqual(record.attempts_left, 3)

    def test_expired_code_does_not_burn_attempts(self):
        from datetime import timedelta
        from django.utils import timezone
        from escrow.services import issue_otp, consume_otp
        with patch('escrow.services.send_otp_via_termii', return_value=True) as mock_send:
            issue_otp('race2@x.com', 'reauth')
            real_code = mock_send.call_args[0][1]
        record = AuthOTP.objects.get(email='race2@x.com', intent='reauth')
        record.expires_at = timezone.now() - timedelta(seconds=1)
        record.save(update_fields=['expires_at'])
        result = consume_otp('race2@x.com', real_code, 'reauth')
        self.assertEqual(result.outcome, 'expired')
        record.refresh_from_db()
        self.assertEqual(record.attempts_left, 5)

    def test_second_consumer_loses_race(self):
        from escrow.services import issue_otp, consume_otp
        with patch('escrow.services.send_otp_via_termii', return_value=True) as mock_send:
            issue_otp('race3@x.com', 'reauth')
            real_code = mock_send.call_args[0][1]
        # Two sessions verify the same code "at once": first wins,
        # second finds nothing live rather than reusing the code.
        self.assertTrue(consume_otp('race3@x.com', real_code, 'reauth').ok)
        retry = consume_otp('race3@x.com', real_code, 'reauth')
        self.assertFalse(retry.ok)
