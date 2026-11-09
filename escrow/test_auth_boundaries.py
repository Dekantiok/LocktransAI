"""Auth boundaries: OTP lifecycle, email normalization, per-sender code binding, verified-is-not-authorized."""
import datetime
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from agent.registry import execute
from escrow.models import AuthOTP, EscrowContract
from escrow.services import consume_otp, issue_otp


class OtpLifecycleTest(TestCase):
    def setUp(self):
        patcher = patch('escrow.services.send_otp_via_termii', return_value=True)
        self.mock_send = patcher.start()
        self.addCleanup(patcher.stop)

    def code(self):
        return self.mock_send.call_args[0][1]

    def test_expired_code_is_rejected(self):
        issue_otp('exp@x.com', 'create')
        rec = AuthOTP.objects.filter(email='exp@x.com', is_used=False).first()
        rec.expires_at = timezone.now() - datetime.timedelta(seconds=1)
        rec.save(update_fields=['expires_at'])
        result = consume_otp('exp@x.com', self.code(), 'create')
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, 'expired')

    def test_five_wrong_tries_lock_the_right_code(self):
        issue_otp('exh@x.com', 'create')
        good = self.code()
        for _ in range(5):
            consume_otp('exh@x.com', '000000', 'create')
        self.assertFalse(consume_otp('exh@x.com', good, 'create').ok)
        self.assertEqual(
            AuthOTP.objects.filter(email='exh@x.com').first().attempts_left, 0)

    def test_code_is_wrong_intent_rejected(self):
        issue_otp('intent@x.com', 'create')
        good = self.code()
        self.assertFalse(consume_otp('intent@x.com', good, 'join').ok)
        self.assertFalse(consume_otp('intent@x.com', good, 'reauth').ok)
        self.assertTrue(consume_otp('intent@x.com', good, 'create').ok)

    def test_delivery_failure_leaves_no_live_code(self):
        with patch('escrow.services.send_otp_via_termii', return_value=False):
            self.assertIsNone(issue_otp('fail@x.com', 'create'))
        self.assertEqual(
            AuthOTP.objects.filter(email='fail@x.com', is_used=False).count(), 0)

    def test_email_matching_is_case_insensitive(self):
        issue_otp('Case@X.com', 'reauth')
        self.assertTrue(consume_otp('case@x.com', self.code(), 'reauth').ok)


class WhatsAppBindingTest(TestCase):
    def test_code_is_bound_to_requesting_sender(self):
        from whatsapp.flow import handle_text_message
        from whatsapp.models import WhatsAppLink
        with patch('whatsapp.adapter.send_text_message', return_value=True), \
             patch('escrow.services.send_otp_via_termii', return_value=True) as mock_otp:
            handle_text_message('2348000000A1', 'victim@x.com')
            code = mock_otp.call_args[0][1]
            handle_text_message('2348000000B2', code)
            self.assertEqual(
                WhatsAppLink.objects.get(wa_id='2348000000B2').verified_email, '')
            handle_text_message('2348000000A1', code)
            self.assertEqual(
                WhatsAppLink.objects.get(wa_id='2348000000A1').verified_email,
                'victim@x.com')

    def test_verified_does_not_mean_authorized(self):
        EscrowContract.objects.create(
            code='AUTH001', title='T', description='D', amount=Decimal('50000'),
            service_fee=Decimal('1500'), fee_payer='buyer',
            creator_email='owner@x.com', creator_role='buyer',
            counterparty_email='partner@x.com', status='awaiting_funding',
            gateway_reference='REF_AUTH001')
        self.assertFalse(execute(
            'get_authorized_escrow_status', {'escrow_reference': 'AUTH001'},
            actor_email='victim@x.com')['ok'])
