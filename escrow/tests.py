import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.timezone import now

from .models import (
    AuditEvent,
    AuthOTP,
    EscrowContract,
    Identity,
    PaymentTransaction,
    PayoutRecord,
    SettlementVault,
    TransitionError,
    VerifiedContact,
    WebhookEvent,
)
from .services import (
    calculate_service_fee,
    confirm_funding_payment,
    consume_otp,
    contracts_for_email,
    issue_otp,
    release_escrow_funds,
    settle_pending_payout,
)
from .utils import generate_otp, generate_secure_code, normalize_email

User = get_user_model()

BUYER = 'buyer@test.com'
SELLER = 'seller@test.com'


def make_contract(**overrides):
    code = overrides.pop('code', 'TESTCODE')
    fields = {
        'title': 'Test Item',
        'description': 'A test transaction',
        'amount': Decimal('10000.00'),
        'service_fee': Decimal('1500.00'),
        'fee_payer': 'buyer',
        'creator_email': BUYER,
        'creator_role': 'buyer',
        'counterparty_email': SELLER,
        'status': 'awaiting_funding',
        'gateway_reference': f'REF_{code}',
    }
    fields.update(overrides)
    return EscrowContract.objects.create(code=code, **fields)


def sent_codes(mock_sender):
    """Plaintexts from the mocked delivery; the DB stores only hashes."""
    return [call[0][1] for call in mock_sender.call_args_list]


def sent_code(mock_sender):
    return sent_codes(mock_sender)[-1]


def verify(client, email, code=None):
    """Bind email like OTP confirmation (one role per device)."""
    session = client.session
    session['my_escrow_verified'] = email
    session.pop('verified_emails', None)
    if code is not None:
        scoped = dict(session.get('verified_contracts', {}))
        scoped[code] = email
        session['verified_contracts'] = scoped
    session.save()


# Payouts go to PayPal email; no bank resolution stubbed.


class ServiceFeeTest(TestCase):
    def test_fee_tiers(self):
        self.assertEqual(calculate_service_fee(10), Decimal('1.50'))
        self.assertEqual(calculate_service_fee(50), Decimal('1.50'))
        self.assertEqual(calculate_service_fee(75), Decimal('2.00'))
        self.assertEqual(calculate_service_fee(200), Decimal('2.50'))
        self.assertEqual(calculate_service_fee(800), Decimal('5.00'))
        self.assertEqual(calculate_service_fee(5000), Decimal('10.00'))


class UtilsTest(TestCase):
    def test_code_generation(self):
        code = generate_secure_code(9)
        self.assertEqual(len(code), 9)
        self.assertTrue(code.isalnum())

    def test_otp_generation(self):
        otp = generate_otp(6)
        self.assertEqual(len(otp), 6)
        self.assertTrue(otp.isdigit())

    def test_normalize_email(self):
        self.assertEqual(normalize_email('  Buyer@Test.COM '), 'buyer@test.com')
        self.assertEqual(normalize_email(None), '')


class StateMachineTest(TestCase):
    def test_legal_transition_applies(self):
        contract = make_contract(code='SM1', status='in_transit')
        contract.transition_to('in_inspection', seller_confirmed=True)
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'in_inspection')
        self.assertTrue(contract.seller_confirmed)

    def test_illegal_transition_is_refused(self):
        contract = make_contract(code='SM2', status='awaiting_funding')
        with self.assertRaises(TransitionError):
            contract.transition_to('completed')
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_funding')

    def test_terminal_statuses_have_no_exits(self):
        for index, status in enumerate(['completed', 'cancelled']):
            contract = make_contract(code=f'SM3{index}', status=status)
            self.assertTrue(contract.is_terminal)
            with self.assertRaises(TransitionError):
                contract.transition_to('pending_payout')

    def test_role_helpers(self):
        contract = make_contract(code='SM4')
        self.assertEqual(contract.buyer_email, BUYER)
        self.assertEqual(contract.seller_email, SELLER)
        flipped = make_contract(code='SM5', creator_role='seller')
        self.assertEqual(flipped.buyer_email, SELLER)
        self.assertEqual(flipped.seller_email, BUYER)

    def test_seller_receives_deducts_fee_only_when_seller_pays(self):
        buyer_pays = make_contract(code='SM6', fee_payer='buyer')
        self.assertEqual(buyer_pays.seller_receives, Decimal('10000.00'))
        seller_pays = make_contract(code='SM7', fee_payer='seller')
        self.assertEqual(seller_pays.seller_receives, Decimal('8500.00'))

    def test_emails_are_lowercased_on_save(self):
        contract = make_contract(
            code='SM8', creator_email='Buyer@Test.COM', counterparty_email='Seller@Test.com'
        )
        contract.refresh_from_db()
        self.assertEqual(contract.creator_email, 'buyer@test.com')
        self.assertEqual(contract.counterparty_email, 'seller@test.com')


class OtpTest(TestCase):
    @override_settings(OTP_ISSUE_COOLDOWN_SECONDS=0)
    @patch('escrow.services.send_otp_via_termii', return_value=True)
    def test_issue_burns_previous_codes(self, _mock):
        first = issue_otp(BUYER, 'create')
        second = issue_otp(BUYER, 'create')
        first.refresh_from_db()
        self.assertTrue(first.is_used)
        self.assertFalse(second.is_used)
        self.assertEqual(AuthOTP.objects.filter(email=BUYER, is_used=False).count(), 1)

    @patch('escrow.services.send_otp_via_termii', return_value=True)
    def test_issue_cooldown_blocks_resend(self, _mock):
        self.assertIsNotNone(issue_otp(BUYER, 'create'))
        self.assertIsNone(issue_otp(BUYER, 'create'))
        self.assertEqual(AuthOTP.objects.count(), 1)

    @patch('escrow.services.send_otp_via_termii', return_value=False)
    def test_failed_delivery_does_not_leave_a_live_code(self, _mock):
        self.assertIsNone(issue_otp(BUYER, 'create'))
        self.assertEqual(AuthOTP.objects.filter(is_used=False).count(), 0)

    @patch('escrow.services.send_otp_via_termii', return_value=True)
    def test_wrong_code_decrements_attempts(self, _mock):
        record = issue_otp(BUYER, 'create')
        for expected in (4, 3):
            result = consume_otp(BUYER, '000000', 'create')
            self.assertFalse(result.ok)
            record.refresh_from_db()
            self.assertEqual(record.attempts_left, expected)

    @patch('escrow.services.send_otp_via_termii', return_value=True)
    def test_brute_force_is_eventually_exhausted(self, mock_send):
        record = issue_otp(BUYER, 'create')
        for _ in range(6):
            consume_otp(BUYER, '000000', 'create')
        record.refresh_from_db()
        self.assertEqual(record.attempts_left, 0)
        result = consume_otp(BUYER, sent_code(mock_send), 'create')
        self.assertEqual(result.outcome, 'exhausted')

    @patch('escrow.services.send_otp_via_termii', return_value=True)
    def test_correct_code_succeeds_once(self, mock_send):
        record = issue_otp(BUYER, 'create')
        code = sent_code(mock_send)
        self.assertTrue(consume_otp(BUYER, code, 'create').ok)
        self.assertFalse(consume_otp(BUYER, code, 'create').ok)

    @patch('escrow.services.send_otp_via_termii', return_value=True)
    def test_expired_code_is_rejected(self, mock_send):
        record = issue_otp(BUYER, 'create')
        record.expires_at = now() - datetime.timedelta(minutes=1)
        record.save()
        result = consume_otp(BUYER, sent_code(mock_send), 'create')
        self.assertEqual(result.outcome, 'expired')

    @patch('escrow.services.send_otp_via_termii', return_value=True)
    def test_intent_is_scoped(self, mock_send):
        record = issue_otp(BUYER, 'create')
        self.assertFalse(consume_otp(BUYER, sent_code(mock_send), 'join').ok)

    @patch('escrow.services.send_otp_via_termii', return_value=True)
    def test_stored_code_is_a_hash_not_the_code(self, mock_send):
        record = issue_otp(BUYER, 'create')
        record.refresh_from_db()
        code = sent_code(mock_send)
        self.assertNotEqual(record.code_hash, code)
        self.assertTrue(record.check_code(code))
        self.assertTrue(consume_otp(BUYER, code, 'create').ok)

    @patch('escrow.services.send_otp_via_termii', return_value=True)
    def test_stored_hash_is_not_accepted_as_a_code(self, mock_send):
        record = issue_otp(BUYER, 'create')
        record.refresh_from_db()
        self.assertFalse(consume_otp(BUYER, record.code_hash, 'create').ok)


@override_settings(
    DEBUG=False, TERMII_API_KEY='live_test_key',
    TERMII_EMAIL_CONFIG_ID='live_test_config',
)
@override_settings(DEMO_MODE=False)
class TermiiPayloadTest(TestCase):
    """Regression: the emailed code must be the code the backend accepts (payload once omitted `code`)."""

    def _post(self, response_json):
        with patch('escrow.notifications.requests.post') as mock_post:
            mock_post.return_value.json.return_value = response_json
            from .notifications import send_otp_via_termii
            result = send_otp_via_termii('buyer@test.com', '482913')
        return mock_post, result

    def test_sends_the_stored_code(self):
        mock_post, result = self._post({'code': 'ok'})
        self.assertTrue(result)
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs['json']['code'], '482913')
        self.assertEqual(kwargs['json']['email_address'], 'buyer@test.com')
        self.assertEqual(kwargs['json']['email_configuration_id'], 'live_test_config')

    def test_rejection_returns_false(self):
        _, result = self._post({'code': 'error', 'message': 'Insufficient balance'})
        self.assertFalse(result)

    def test_unset_credentials_short_circuit(self):
        with override_settings(TERMII_API_KEY='dummy_key'):
            with patch('escrow.notifications.requests.post') as mock_post:
                from .notifications import send_otp_via_termii
                self.assertFalse(send_otp_via_termii('buyer@test.com', '482913'))
                mock_post.assert_not_called()


class ContractCreationTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.payload = {
            'title': 'Test Item', 'description': 'Test desc', 'amount': '50000.00',
            'fee_payer': 'buyer', 'creator_email': 'Buyer@Test.com', 'creator_role': 'buyer',
            'counterparty_email': 'Seller@Test.com', 'inspection_days': 3,
        }

    @patch('escrow.services.send_otp_via_termii', return_value=True)
    def test_create_then_verify(self, mock_send):
        response = self.client.post(reverse('create_contract'), self.payload)
        contract = EscrowContract.objects.get()
        self.assertRedirects(
            response,
            reverse('verify_otp', kwargs={'code': contract.code}),
            target_status_code=200,
        )
        self.assertEqual(contract.status, 'pending_verification')
        self.assertEqual(contract.service_fee, Decimal('10.00'))
        self.assertEqual(contract.creator_email, 'buyer@test.com')
        self.assertEqual(contract.counterparty_email, 'seller@test.com')

        self.client.post(
            reverse('verify_otp', kwargs={'code': contract.code}),
            {'otp': sent_code(mock_send)},
        )
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_counterparty')
        self.assertEqual(
            self.client.session['verified_contracts'][contract.code], 'buyer@test.com'
        )

    def test_service_fee_is_server_computed(self):
        with patch('escrow.services.send_otp_via_termii', return_value=True):
            payload = {**self.payload, 'amount': '750000.00', 'service_fee': '1.00'}
            self.client.post(reverse('create_contract'), payload)
        self.assertEqual(EscrowContract.objects.get().service_fee, Decimal('10.00'))

    def test_rejects_amount_below_minimum(self):
        response = self.client.post(
            reverse('create_contract'), {**self.payload, 'amount': '1.00'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(EscrowContract.objects.count(), 0)

    def test_rejects_out_of_range_inspection_window(self):
        response = self.client.post(
            reverse('create_contract'), {**self.payload, 'inspection_days': '365'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(EscrowContract.objects.count(), 0)

    def test_rejects_self_dealing_case_insensitively(self):
        response = self.client.post(
            reverse('create_contract'), {**self.payload, 'counterparty_email': 'BUYER@TEST.COM'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'cannot use the same email address')
        self.assertEqual(EscrowContract.objects.count(), 0)


class JoinEscrowTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.contract = make_contract(code='JOIN1', status='awaiting_counterparty')

    @patch('escrow.services.send_otp_via_termii', return_value=True)
    def test_join_and_verify(self, mock_send):
        response = self.client.post(
            reverse('join_escrow_page'), {'code': 'JOIN1', 'email': 'Seller@Test.com'}
        )
        self.assertEqual(response.status_code, 302)
        self.client.post(
            reverse('counterparty_verify_otp', kwargs={'code': 'JOIN1'}),
            {'otp': sent_code(mock_send)},
        )
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'awaiting_funding')
        self.assertEqual(
            self.client.session['verified_contracts']['JOIN1'], 'seller@test.com'
        )

    def test_wrong_email_is_rejected(self):
        response = self.client.post(
            reverse('join_escrow_page'), {'code': 'JOIN1', 'email': 'intruder@test.com'}
        )
        self.assertContains(response, 'could not be verified')
        self.assertEqual(AuthOTP.objects.count(), 0)

    def test_unknown_code_is_rejected(self):
        response = self.client.post(
            reverse('join_escrow_page'), {'code': 'NOPE', 'email': SELLER}
        )
        self.assertContains(response, 'could not be verified')

    @patch('escrow.services.send_otp_via_termii', return_value=True)
    def test_cannot_join_twice(self, _mock):
        self.contract.status = 'awaiting_funding'
        self.contract.save()
        response = self.client.post(
            reverse('join_escrow_page'), {'code': 'JOIN1', 'email': SELLER}
        )
        self.assertContains(response, 'not currently accepting')

    @patch('escrow.services.send_otp_via_termii', return_value=True)
    def test_same_device_cannot_verify_both_roles(self, mock_send):
        """Buyer and seller are different people: one browser holds one role."""
        creator_contract = make_contract(
            code='DUAL1', status='pending_verification',
            creator_email=BUYER, creator_role='buyer', counterparty_email=SELLER,
        )
        issue_otp(BUYER, 'create')
        self.client.post(
            reverse('verify_otp', kwargs={'code': 'DUAL1'}),
            {'otp': sent_codes(mock_send)[0]},
        )
        creator_contract.refresh_from_db()
        self.assertEqual(creator_contract.status, 'awaiting_counterparty')

        join_otp = issue_otp(SELLER, 'join')
        response = self.client.post(
            reverse('counterparty_verify_otp', kwargs={'code': 'DUAL1'}),
            {'otp': sent_codes(mock_send)[1]},
        )
        self.assertContains(response, 'must verify on their own device')
        creator_contract.refresh_from_db()
        # Second role refused: status unchanged, seller OTP left unconsumed.
        self.assertEqual(creator_contract.status, 'awaiting_counterparty')
        join_otp.refresh_from_db()
        self.assertFalse(join_otp.is_used)


class AuthorizationTest(TestCase):
    """Every money-moving and lifecycle view must reject a non-party."""

    def setUp(self):
        self.client = Client()
        self.contract = make_contract(code='AUTH1', status='in_inspection')
        self.contract.inspection_ends = now() + datetime.timedelta(days=3)
        self.contract.save()
        SettlementVault.objects.create(
            contract=self.contract, paypal_email='seller-paypal@example.com',
            bank_name='PayPal', account_name='seller-paypal@example.com',
        )

    def test_detail_is_locked_from_non_parties(self):
        response = self.client.get(reverse('contract_detail', kwargs={'code': 'AUTH1'}))
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, '10,000.00', status_code=403)
        self.assertNotContains(response, SELLER, status_code=403)
        self.assertNotContains(response, 'seller-paypal@example.com', status_code=403)

    def test_detail_is_visible_to_parties(self):
        verify(self.client, BUYER)
        response = self.client.get(reverse('contract_detail', kwargs={'code': 'AUTH1'}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '10,000.00')

    def test_detail_masks_vault_for_buyer(self):
        verify(self.client, BUYER)
        response = self.client.get(reverse('contract_detail', kwargs={'code': 'AUTH1'}))
        self.assertContains(response, 'se***@example.com')

    def test_detail_shows_full_vault_for_seller(self):
        verify(self.client, SELLER)
        response = self.client.get(reverse('contract_detail', kwargs={'code': 'AUTH1'}))
        self.assertContains(response, 'seller-paypal@example.com')

    def test_seller_cannot_release_funds(self):
        verify(self.client, SELLER)
        response = self.client.post(
            reverse('buyer_release_funds', kwargs={'code': 'AUTH1'})
        )
        self.assertEqual(response.status_code, 403)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'in_inspection')

    def test_anonymous_cannot_dispute(self):
        response = self.client.post(
            reverse('buyer_raise_dispute', kwargs={'code': 'AUTH1'})
        )
        self.assertEqual(response.status_code, 403)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'in_inspection')

    def test_buyer_cannot_add_payout_details(self):
        verify(self.client, BUYER)
        response = self.client.post(reverse('seller_add_vault', kwargs={'code': 'AUTH1'}))
        self.assertEqual(response.status_code, 403)

    def test_mutating_views_reject_get(self):
        verify(self.client, BUYER)
        for name in ('buyer_release_funds', 'buyer_raise_dispute',
                     'buyer_confirm_receipt', 'seller_confirm_dispatch'):
            response = self.client.get(reverse(name, kwargs={'code': 'AUTH1'}))
            self.assertEqual(response.status_code, 405, name)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'in_inspection')

    def test_payment_requires_verified_buyer(self):
        make_contract(code='AUTH2', status='awaiting_funding')
        verify(self.client, SELLER)
        response = self.client.post(reverse('initiate_payment', kwargs={'code': 'AUTH2'}))
        self.assertEqual(response.status_code, 403)


class WebhookTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.contract = make_contract(code='WH1', status='awaiting_funding',
                                      amount=Decimal('10000.00'))
        self.url = reverse('paypal_webhook')

    def capture_event(self, event_type='PAYMENT.CAPTURE.COMPLETED',
                      amount_kobo=None, reference=None, event_id='evt-1'):
        if amount_kobo is None:
            amount_kobo = self.contract.total_charge_kobo
        return {
            'event_type': event_type,
            'id': event_id,
            'resource': {
                'id': 'cap-1',
                'amount': {
                    'value': f"{amount_kobo / 100:.2f}",
                    'currency_code': 'USD',
                },
                'supplementary_data': {
                    'related_ids': {
                        'order_id': reference or self.contract.gateway_reference,
                    },
                },
            },
        }

    def post(self, mock_gateway, event_type='PAYMENT.CAPTURE.COMPLETED', amount_kobo=None):
        mock_gateway.return_value.verify_incoming_webhook.return_value = (
            self.capture_event(event_type=event_type, amount_kobo=amount_kobo)
        )
        return self.client.post(
            self.url, data=b'{}', content_type='application/json',
        )

    @patch('escrow.views.get_payment_gateway')
    def test_charge_success_advances_to_awaiting_dispatch(self, mock_gateway):
        response = self.post(mock_gateway)
        self.assertEqual(response.status_code, 200)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'awaiting_dispatch')

    @patch('escrow.views.get_payment_gateway')
    def test_callback_without_gateway_confirmation_leaves_state(self, mock_gateway):
        """Only a PayPal verification of our stored reference may advance the contract."""
        mock_gateway.return_value.verify_transaction.return_value = {
            'status': True,
            'data': {'status': 'failed', 'reference': self.contract.gateway_reference,
                     'amount': 1150000},
        }
        response = self.client.get(
            reverse('payment_callback', kwargs={'code': 'WH1'})
        )
        self.assertEqual(response.status_code, 302)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'awaiting_funding')

    @patch('escrow.views.get_payment_gateway')
    def test_replay_does_not_re_advance(self, mock_gateway):
        self.post(mock_gateway)
        self.contract.refresh_from_db()
        self.contract.transition_to('in_transit')
        response = self.post(mock_gateway)
        self.assertEqual(response.status_code, 200)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'in_transit')

    @patch('escrow.views.get_payment_gateway')
    def test_amount_mismatch_is_rejected(self, mock_gateway):
        response = self.post(mock_gateway, amount_kobo=1)
        self.assertEqual(response.status_code, 400)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'awaiting_funding')

    @patch('escrow.views.get_payment_gateway')
    def test_bad_signature_is_rejected(self, mock_gateway):
        mock_gateway.return_value.verify_incoming_webhook.return_value = False
        response = self.client.post(
            self.url, data=b'{}', content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'awaiting_funding')

    @patch('escrow.views.get_payment_gateway')
    def test_unknown_reference_is_reported(self, mock_gateway):
        mock_gateway.return_value.verify_incoming_webhook.return_value = (
            self.capture_event(reference='NOPE')
        )
        response = self.client.post(
            self.url, data=b'{}', content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)

    @patch('escrow.views.get_payment_gateway')
    def test_charge_failed_is_ignored(self, mock_gateway):
        response = self.post(mock_gateway, event_type='PAYMENT.CAPTURE.DENIED')
        self.assertEqual(response.status_code, 200)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'awaiting_funding')

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 400)


class CallbackVerifyTest(TestCase):
    """Callback verifies our stored reference so a missing webhook can't strand a paid escrow."""

    def setUp(self):
        self.client = Client()
        self.contract = make_contract(code='CB1', status='awaiting_funding',
                                      amount=Decimal('10000.00'))
        self.url = reverse('payment_callback', kwargs={'code': 'CB1'})

    def success_payload(self, amount=1150000):
        return {
            'status': True,
            'data': {'status': 'success', 'amount': amount,
                     'reference': self.contract.gateway_reference},
        }

    @patch('escrow.views.get_payment_gateway')
    def test_verified_payment_advances(self, mock_gateway):
        mock_gateway.return_value.verify_transaction.return_value = self.success_payload()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'awaiting_dispatch')
        mock_gateway.return_value.verify_transaction.assert_called_once_with(
            self.contract.gateway_reference
        )

    @patch('escrow.views.get_payment_gateway')
    def test_amount_mismatch_does_not_advance(self, mock_gateway):
        mock_gateway.return_value.verify_transaction.return_value = self.success_payload(
            amount=1
        )
        self.client.get(self.url)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'awaiting_funding')

    @patch('escrow.views.get_payment_gateway')
    def test_wrong_reference_does_not_advance(self, mock_gateway):
        payload = self.success_payload()
        payload['data']['reference'] = 'someone-elses-reference'
        mock_gateway.return_value.verify_transaction.return_value = payload
        self.client.get(self.url)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'awaiting_funding')

    @patch('escrow.views.get_payment_gateway')
    def test_failed_transaction_does_not_advance(self, mock_gateway):
        payload = self.success_payload()
        payload['data']['status'] = 'failed'
        mock_gateway.return_value.verify_transaction.return_value = payload
        self.client.get(self.url)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'awaiting_funding')

    @patch('escrow.views.get_payment_gateway')
    def test_lookup_error_does_not_advance(self, mock_gateway):
        mock_gateway.return_value.verify_transaction.side_effect = TimeoutError('boom')
        self.client.get(self.url)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'awaiting_funding')

    @patch('escrow.views.get_payment_gateway')
    def test_revisit_after_advance_verifies_nothing(self, mock_gateway):
        mock_gateway.return_value.verify_transaction.return_value = self.success_payload()
        self.client.get(self.url)
        self.client.get(self.url)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'awaiting_dispatch')
        mock_gateway.return_value.verify_transaction.assert_called_once()


class WebhookSignatureTest(TestCase):
    def test_missing_headers_are_rejected(self):
        from .gateways.paypal import PayPalGateway

        gateway = PayPalGateway()
        self.assertFalse(gateway.verify_incoming_webhook({}, b'{}'))

    @override_settings(PAYPAL_WEBHOOK_ID='wh-test')
    def test_malformed_json_is_rejected(self):
        from .gateways.paypal import PayPalGateway

        gateway = PayPalGateway()
        headers = {
            'PAYPAL-TRANSMISSION-ID': 'tid',
            'PAYPAL-TRANSMISSION-TIME': '2024-01-01T00:00:00Z',
            'PAYPAL-CERT-URL': 'https://example.com/cert',
            'PAYPAL-AUTH-ALGO': 'SHA256withRSA',
            'PAYPAL-TRANSMISSION-SIG': 'sig',
        }
        self.assertFalse(gateway.verify_incoming_webhook(headers, b'not json'))

    @override_settings(PAYPAL_WEBHOOK_ID='wh-test')
    def test_failed_verification_is_rejected(self):
        from unittest.mock import Mock

        from .gateways.paypal import PayPalGateway

        gateway = PayPalGateway()
        headers = {
            'PAYPAL-TRANSMISSION-ID': 'tid',
            'PAYPAL-TRANSMISSION-TIME': '2024-01-01T00:00:00Z',
            'PAYPAL-CERT-URL': 'https://example.com/cert',
            'PAYPAL-AUTH-ALGO': 'SHA256withRSA',
            'PAYPAL-TRANSMISSION-SIG': 'sig',
        }
        body = b'{"id": "evt-1"}'
        with patch.object(PayPalGateway, '_access_token', return_value='tok'), \
                patch('escrow.gateways.paypal.requests.post') as mock_post:
            mock_post.return_value.json.return_value = {'verification_status': 'FAILURE'}
            self.assertFalse(gateway.verify_incoming_webhook(headers, body))

    @override_settings(PAYPAL_WEBHOOK_ID='wh-test')
    def test_successful_verification_returns_event(self):
        from .gateways.paypal import PayPalGateway

        gateway = PayPalGateway()
        headers = {
            'PAYPAL-TRANSMISSION-ID': 'tid',
            'PAYPAL-TRANSMISSION-TIME': '2024-01-01T00:00:00Z',
            'PAYPAL-CERT-URL': 'https://example.com/cert',
            'PAYPAL-AUTH-ALGO': 'SHA256withRSA',
            'PAYPAL-TRANSMISSION-SIG': 'sig',
        }
        body = b'{"id": "evt-1", "event_type": "PAYMENT.CAPTURE.COMPLETED"}'
        with patch.object(PayPalGateway, '_access_token', return_value='tok'), \
                patch('escrow.gateways.paypal.requests.post') as mock_post:
            mock_post.return_value.json.return_value = {'verification_status': 'SUCCESS'}
            event = gateway.verify_incoming_webhook(headers, body)
        self.assertEqual(event['id'], 'evt-1')


class PayoutTest(TestCase):
    def setUp(self):
        self.contract = make_contract(code='PAY1', status='in_inspection')
        self.contract.inspection_ends = now() + datetime.timedelta(days=3)
        self.contract.save()
        self.vault = SettlementVault.objects.create(
            contract=self.contract, bank_name='Test Bank', bank_code='058',
            account_number='1234567890', account_name='Test Seller',
        )

    def test_release_marks_completed(self):
        with patch('escrow.services.get_payment_gateway') as mock_gateway:
            mock_gateway.return_value.execute_seller_payout.return_value = {'status': True}
            result = release_escrow_funds(self.contract)
        self.assertTrue(result.success)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'completed')
        self.assertTrue(self.contract.buyer_confirmed)

    def test_release_deducts_fee_when_seller_pays(self):
        self.contract.fee_payer = 'seller'
        self.contract.save()
        with patch('escrow.services.get_payment_gateway') as mock_gateway:
            gateway = mock_gateway.return_value
            gateway.execute_seller_payout.return_value = {'status': True}
            release_escrow_funds(self.contract)
        self.assertEqual(
            gateway.execute_seller_payout.call_args.args[1], Decimal('8500.00')
        )

    def test_release_is_idempotent(self):
        """A second release must not issue a second transfer."""
        with patch('escrow.services.get_payment_gateway') as mock_gateway:
            gateway = mock_gateway.return_value
            gateway.execute_seller_payout.return_value = {'status': True}
            release_escrow_funds(self.contract)
            second = release_escrow_funds(self.contract)
        self.assertFalse(second.success)
        self.assertEqual(gateway.execute_seller_payout.call_count, 1)

    def test_concurrent_release_pays_only_once(self):
        """Two stale in_inspection reads must not both win; one transfer max."""
        request_a = EscrowContract.objects.get(pk=self.contract.pk)
        request_b = EscrowContract.objects.get(pk=self.contract.pk)
        with patch('escrow.services.get_payment_gateway') as mock_gateway:
            gateway = mock_gateway.return_value
            gateway.execute_seller_payout.return_value = {'status': True}
            first = release_escrow_funds(request_a)
            second = release_escrow_funds(request_b)
        self.assertTrue(first.success)
        self.assertFalse(second.success)
        self.assertEqual(second.reason, 'not_in_inspection')
        self.assertEqual(gateway.execute_seller_payout.call_count, 1)

    def test_failed_transfer_leaves_pending_payout(self):
        with patch('escrow.services.get_payment_gateway') as mock_gateway:
            mock_gateway.return_value.execute_seller_payout.return_value = {
                'status': False, 'message': 'Insufficient balance'
            }
            result = release_escrow_funds(self.contract)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'transfer_failed')
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'pending_payout')
        self.assertIn('did not go through', result.message)

    def test_gateway_exception_does_not_crash(self):
        with patch('escrow.services.get_payment_gateway') as mock_gateway:
            mock_gateway.return_value.execute_seller_payout.side_effect = TimeoutError('boom')
            result = release_escrow_funds(self.contract)
        self.assertFalse(result.success)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'pending_payout')

    def test_missing_vault_is_reported(self):
        self.vault.delete()
        result = release_escrow_funds(self.contract)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'missing_vault')
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'in_inspection')

    def test_release_view_reports_outcome(self):
        self.client = Client()
        verify(self.client, BUYER)
        with patch('escrow.services.get_payment_gateway') as mock_gateway:
            mock_gateway.return_value.execute_seller_payout.return_value = {'status': True}
            response = self.client.post(
                reverse('buyer_release_funds', kwargs={'code': 'PAY1'}), follow=True
            )
        self.assertContains(response, 'Funds released')

    def test_release_view_surfaces_failure(self):
        self.client = Client()
        verify(self.client, BUYER)
        with patch('escrow.services.get_payment_gateway') as mock_gateway:
            mock_gateway.return_value.execute_seller_payout.return_value = {'status': False}
            response = self.client.post(
                reverse('buyer_release_funds', kwargs={'code': 'PAY1'}), follow=True
            )
        self.assertContains(response, 'did not go through')


class DisputeTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.contract = make_contract(code='DIS1', status='in_inspection')
        self.contract.inspection_ends = now() + datetime.timedelta(days=3)
        self.contract.save()
        verify(self.client, BUYER)

    def test_buyer_can_dispute(self):
        self.client.post(reverse('buyer_raise_dispute', kwargs={'code': 'DIS1'}))
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'disputed')

    def test_dispute_cannot_be_raised_twice(self):
        self.client.post(reverse('buyer_raise_dispute', kwargs={'code': 'DIS1'}))
        self.client.post(reverse('buyer_raise_dispute', kwargs={'code': 'DIS1'}))
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'disputed')

    def test_dispute_blocks_payout(self):
        self.client.post(reverse('buyer_raise_dispute', kwargs={'code': 'DIS1'}))
        with patch('escrow.services.get_payment_gateway') as mock_gateway:
            self.client.post(reverse('buyer_release_funds', kwargs={'code': 'DIS1'}))
            self.assertEqual(mock_gateway.return_value.execute_seller_payout.call_count, 0)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'disputed')

    def test_dispute_can_be_resolved_to_payout(self):
        self.client.post(reverse('buyer_raise_dispute', kwargs={'code': 'DIS1'}))
        SettlementVault.objects.create(
            contract=self.contract, bank_name='Test Bank', bank_code='058',
            account_number='1234567890', account_name='Test Seller',
        )
        self.contract.refresh_from_db()
        self.contract.transition_to('pending_payout')
        with patch('escrow.services.get_payment_gateway') as mock_gateway:
            mock_gateway.return_value.execute_seller_payout.return_value = {'status': True}
            result = settle_pending_payout(self.contract)
        self.assertTrue(result.success)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'completed')

    def test_settling_twice_pays_once(self):
        self.client.post(reverse('buyer_raise_dispute', kwargs={'code': 'DIS1'}))
        SettlementVault.objects.create(
            contract=self.contract, bank_name='Test Bank', bank_code='058',
            account_number='1234567890', account_name='Test Seller',
        )
        self.contract.refresh_from_db()
        self.contract.transition_to('pending_payout')
        with patch('escrow.services.get_payment_gateway') as mock_gateway:
            gateway = mock_gateway.return_value
            gateway.execute_seller_payout.return_value = {'status': True}
            settle_pending_payout(self.contract)
            second = settle_pending_payout(self.contract)
        self.assertFalse(second.success)
        self.assertEqual(gateway.execute_seller_payout.call_count, 1)

    def test_dispute_can_be_cancelled(self):
        self.client.post(reverse('buyer_raise_dispute', kwargs={'code': 'DIS1'}))
        self.contract.refresh_from_db()
        self.contract.transition_to('cancelled')
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'cancelled')


class LifecycleTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.contract = make_contract(code='LIFE1', status='awaiting_dispatch')
        verify(self.client, SELLER)

    def test_seller_dispatches(self):
        self.client.post(reverse('seller_confirm_dispatch', kwargs={'code': 'LIFE1'}))
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'in_transit')
        self.assertTrue(self.contract.seller_confirmed)

    def test_dispatch_is_not_repeatable(self):
        self.client.post(reverse('seller_confirm_dispatch', kwargs={'code': 'LIFE1'}))
        self.client.post(reverse('seller_confirm_dispatch', kwargs={'code': 'LIFE1'}))
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'in_transit')

    def test_buyer_receipt_starts_inspection_window(self):
        self.contract.transition_to('in_transit')
        verify(self.client, BUYER)
        self.client.post(reverse('buyer_confirm_receipt', kwargs={'code': 'LIFE1'}))
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'in_inspection')
        self.assertIsNotNone(self.contract.inspection_ends)
        self.assertGreater(self.contract.inspection_ends, now())

    def test_settlement_vault_is_seller_only_and_one_per_contract(self):
        self.client.post(reverse('seller_add_vault', kwargs={'code': 'LIFE1'}), {
            'destination': 'other',
            'paypal_email': 'seller@example.com',
        })
        self.assertEqual(SettlementVault.objects.count(), 1)
        self.vault = SettlementVault.objects.get()
        self.assertEqual(self.vault.paypal_email, 'seller@example.com')
        self.assertEqual(self.vault.bank_name, 'PayPal')

        response = self.client.post(reverse('seller_add_vault', kwargs={'code': 'LIFE1'}), {
            'destination': 'other',
            'paypal_email': 'seller@example.com',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SettlementVault.objects.count(), 1)

    def test_vault_one_tap_uses_verified_email(self):
        self.client.post(reverse('seller_add_vault', kwargs={'code': 'LIFE1'}), {
            'destination': 'verified',
        })
        self.assertEqual(SettlementVault.objects.count(), 1)
        self.assertEqual(SettlementVault.objects.get().paypal_email, SELLER)

    def test_vault_rejects_invalid_paypal_email(self):
        response = self.client.post(reverse('seller_add_vault', kwargs={'code': 'LIFE1'}), {
            'destination': 'other',
            'paypal_email': 'not-an-email',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Enter a valid email address.')
        self.assertEqual(SettlementVault.objects.count(), 0)


class AutoReleaseTest(TestCase):
    def make(self, code, status, ends_delta):
        contract = make_contract(code=code, status=status)
        contract.inspection_ends = now() + ends_delta
        contract.save()
        SettlementVault.objects.create(
            contract=contract, bank_name='Test Bank', bank_code='058',
            account_number='1234567890', account_name='Test Seller',
        )
        return contract

    def test_releases_only_expired_inspections(self):
        expired = self.make('AR1', 'in_inspection', -datetime.timedelta(days=1))
        pending = self.make('AR2', 'in_inspection', datetime.timedelta(days=1))
        # in_transit with a past deadline must never be picked up (fixture guards the query).
        transit = self.make('AR3', 'in_transit', -datetime.timedelta(days=5))

        with patch('escrow.services.get_payment_gateway') as mock_gateway:
            mock_gateway.return_value.execute_seller_payout.return_value = {'status': True}
            call_command('auto_release_escrow')

        expired.refresh_from_db()
        pending.refresh_from_db()
        transit.refresh_from_db()
        self.assertEqual(expired.status, 'completed')
        self.assertEqual(pending.status, 'in_inspection')
        self.assertEqual(transit.status, 'in_transit')
        self.assertEqual(mock_gateway.return_value.execute_seller_payout.call_count, 1)

    def test_dry_run_pays_nobody(self):
        self.make('AR4', 'in_inspection', -datetime.timedelta(days=1))
        with patch('escrow.services.get_payment_gateway') as mock_gateway:
            call_command('auto_release_escrow', '--dry-run')
            self.assertEqual(mock_gateway.return_value.execute_seller_payout.call_count, 0)
        self.assertEqual(
            EscrowContract.objects.get(code='AR4').status, 'in_inspection'
        )

    def test_missing_vault_is_skipped_not_crashed(self):
        contract = self.make('AR5', 'in_inspection', -datetime.timedelta(days=1))
        SettlementVault.objects.filter(contract=contract).delete()
        call_command('auto_release_escrow')
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'in_inspection')


class MyEscrowTest(TestCase):
    def setUp(self):
        self.client = Client()

    @patch('escrow.services.send_otp_via_termii', return_value=True)
    def test_reauth_flow_lists_escrows(self, mock_send):
        make_contract(code='ME1', status='in_inspection')
        self.client.post(reverse('my_escrow'), {'email': 'Buyer@Test.com'})
        response = self.client.post(
            reverse('my_escrow_verify_otp'), {'otp': sent_code(mock_send)}
        )
        self.assertRedirects(response, reverse('my_escrow_list'))
        listing = self.client.get(reverse('my_escrow_list'))
        self.assertContains(listing, 'ME1')

    def test_unknown_email_is_told_no_escrows(self):
        response = self.client.post(reverse('my_escrow'), {'email': 'nobody@test.com'})
        self.assertContains(response, 'No active escrows')

    def test_list_requires_verification(self):
        response = self.client.get(reverse('my_escrow_list'))
        self.assertRedirects(response, reverse('my_escrow'))

    def test_completed_escrows_are_hidden(self):
        make_contract(code='ME2', status='completed')
        make_contract(code='ME3', status='cancelled')
        self.assertEqual(contracts_for_email(BUYER).count(), 0)
        self.assertEqual(contracts_for_email(BUYER, include_closed=True).count(), 2)


class PollingTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.contract = make_contract(code='POLL1', status='in_inspection')
        self.contract.inspection_ends = now() + datetime.timedelta(days=3)
        self.contract.save()
        verify(self.client, BUYER)

    def test_htmx_request_gets_the_panel_partial(self):
        response = self.client.get(
            reverse('contract_detail', kwargs={'code': 'POLL1'}),
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'escrow/_contract_panel.html')
        self.assertNotIn(b'<html', response.content)

    def test_standard_request_gets_the_full_page(self):
        response = self.client.get(reverse('contract_detail', kwargs={'code': 'POLL1'}))
        self.assertTemplateUsed(response, 'escrow/contract_detail.html')
        self.assertIn(b'<html', response.content)

    def test_panel_carries_the_poll_attributes(self):
        response = self.client.get(
            reverse('contract_detail', kwargs={'code': 'POLL1'}),
            HTTP_HX_REQUEST='true',
        )
        self.assertContains(response, 'hx-get="/escrow/POLL1/"')
        self.assertContains(response, 'hx-trigger="load delay:1s, every 15s')

    def test_terminal_escrow_does_not_advertise_live_polling(self):
        self.contract.transition_to('pending_payout')
        self.contract.transition_to('completed')
        response = self.client.get(reverse('contract_detail', kwargs={'code': 'POLL1'}))
        self.assertNotContains(response, 'Live')
        self.assertContains(response, 'Escrow Complete')


class SmokeTest(TestCase):
    """Guards against templates or URLs that no longer resolve."""

    def setUp(self):
        self.client = Client()
        self.contract = make_contract(code='SMOKE1', status='in_inspection')
        self.contract.inspection_ends = now() + datetime.timedelta(days=3)
        self.contract.save()
        SettlementVault.objects.create(
            contract=self.contract, bank_name='Zenith Bank', bank_code='057',
            account_number='1234567890', account_name='Seller Name',
        )
        session = self.client.session
        session['verified_contracts'] = {'SMOKE1': BUYER}
        session['my_escrow_verified'] = BUYER
        session.save()

    def test_every_page_renders(self):
        paths = [
            reverse('home'),
            reverse('create_contract'),
            reverse('join_escrow_page'),
            reverse('my_escrow'),
            reverse('my_escrow_list'),
            reverse('contract_detail', kwargs={'code': 'SMOKE1'}),
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_vault_form_renders_when_no_vault_exists(self):
        make_contract(code='SMOKE2', status='awaiting_dispatch')
        verify(self.client, SELLER, code='SMOKE2')
        response = self.client.get(reverse('seller_add_vault', kwargs={'code': 'SMOKE2'}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'paypal_email')

    def test_vault_page_redirects_once_a_vault_exists(self):
        verify(self.client, SELLER, code='SMOKE1')
        response = self.client.get(reverse('seller_add_vault', kwargs={'code': 'SMOKE1'}))
        self.assertRedirects(
            response, reverse('contract_detail', kwargs={'code': 'SMOKE1'})
        )

    def test_admin_pages_render(self):
        admin_user = User.objects.create_superuser('ops', 'ops@test.com', 'pw-for-tests')
        self.client.force_login(admin_user)
        for path in ['/admin/', '/admin/escrow/escrowcontract/']:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_every_app_url_reverses(self):
        """Each escrow route must be reachable by name from config.urls."""
        from config import urls as project_urls

        entries = []
        for p in project_urls.urlpatterns:
            if getattr(p, 'app_name', None) == 'admin':
                continue
            if hasattr(p, 'url_patterns'):
                # Qualify included routes with the namespace, as reverse() expects.
                namespace = getattr(p, 'namespace', None)
                for sub in p.url_patterns:
                    name = f"{namespace}:{sub.name}" if namespace else sub.name
                    entries.append((name, sub.pattern.regex.groupindex))
                continue
            if (p.name or '').startswith('admin'):
                continue
            entries.append((p.name, p.pattern.regex.groupindex))
        self.assertGreater(len(entries), 5, 'expected the escrow routes to be registered')
        for name, groupindex in entries:
            with self.subTest(name=name):
                kwargs = {key: 'TESTCODE' for key in groupindex}
                self.assertTrue(reverse(name, kwargs=kwargs or None))

    def test_no_references_to_removed_templates(self):
        # This file names the removed templates on purpose, so it is excluded.
        source = ''
        for path in list(Path('escrow').rglob('*.py')) + list(Path('templates').rglob('*.html')):
            if path.name == Path(__file__).name:
                continue
            source += path.read_text()
        for dead in ('payment_callback.html', 'payment_error.html', 'reauth_verify_otp.html'):
            with self.subTest(dead=dead):
                self.assertNotIn(dead, source)

    def test_removed_css_classes_are_gone(self):
        base = Path('templates/base.html').read_text()
        self.assertNotIn('bg-black-custom', base)
        self.assertNotIn('rgba(220,38,38,0.08)', base)

    def test_no_stale_accent_colour_in_templates(self):
        for path in Path('templates').rglob('*.html'):
            with self.subTest(path=str(path)):
                self.assertNotIn('4f8ef7', path.read_text())


class FinancialRecordsTest(TestCase):
    def test_funding_creates_payment_record_and_audit(self):
        contract = make_contract(code='REC1')
        self.assertTrue(confirm_funding_payment(contract, 1150000))
        payment = PaymentTransaction.objects.get(
            provider_reference=contract.gateway_reference
        )
        self.assertEqual(payment.status, 'success')
        self.assertEqual(payment.amount_kobo, 1150000)
        self.assertEqual(payment.escrow, contract)
        self.assertTrue(
            AuditEvent.objects.filter(
                action='funding.confirmed', escrow=contract
            ).exists()
        )

    def test_second_confirmation_does_not_duplicate_rows(self):
        contract = make_contract(code='REC2')
        self.assertTrue(confirm_funding_payment(contract, 1150000))
        self.assertFalse(confirm_funding_payment(contract, 1150000))
        self.assertEqual(
            PaymentTransaction.objects.filter(
                provider_reference=contract.gateway_reference
            ).count(), 1,
        )
        self.assertEqual(
            AuditEvent.objects.filter(
                action='funding.confirmed', escrow=contract
            ).count(), 1,
        )

    def test_settle_creates_payout_record_and_audit(self):
        contract = make_contract(code='REC3', status='pending_payout')
        SettlementVault.objects.create(
            contract=contract, bank_name='Test Bank', bank_code='058',
            account_number='1234567890', account_name='Test Seller',
        )
        with patch('escrow.services.get_payment_gateway') as mock_gateway:
            mock_gateway.return_value.execute_seller_payout.return_value = {
                'status': True, 'message': 'Transfer has been queued',
                'data': {
                    'reference': 'locktrans-REC3-1000000',
                    'transfer_code': 'TRF_test123',
                    'recipient': 'RCP_test456',
                },
            }
            result = settle_pending_payout(contract)
        self.assertTrue(result.success)
        record = PayoutRecord.objects.get(escrow=contract)
        self.assertEqual(record.status, 'success')
        self.assertEqual(record.transfer_reference, 'locktrans-REC3-1000000')
        self.assertEqual(record.provider_transfer_code, 'TRF_test123')
        self.assertTrue(
            AuditEvent.objects.filter(
                action='payout.completed', escrow=contract
            ).exists()
        )

    def test_uncertain_payout_is_recorded_not_lost(self):
        contract = make_contract(code='REC4', status='pending_payout')
        SettlementVault.objects.create(
            contract=contract, bank_name='Test Bank', bank_code='058',
            account_number='1234567890', account_name='Test Seller',
        )
        with patch('escrow.services.get_payment_gateway') as mock_gateway:
            mock_gateway.return_value.execute_seller_payout.side_effect = (
                TimeoutError('gateway timed out')
            )
            result = settle_pending_payout(contract)
        self.assertFalse(result.success)
        record = PayoutRecord.objects.get(escrow=contract)
        self.assertEqual(record.status, 'uncertain')
        self.assertTrue(
            AuditEvent.objects.filter(
                action='payout.failed', escrow=contract
            ).exists()
        )
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'pending_payout')


class WebhookDedupTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.contract = make_contract(code='DEDUP1', status='awaiting_funding')
        self.url = reverse('paypal_webhook')

    def post(self, mock_gateway):
        amount_kobo = self.contract.total_charge_kobo
        mock_gateway.return_value.verify_incoming_webhook.return_value = {
            'event_type': 'PAYMENT.CAPTURE.COMPLETED',
            'id': 'evt-dedup-1',
            'resource': {
                'id': 'cap-dedup-1',
                'amount': {
                    'value': f"{amount_kobo / 100:.2f}",
                    'currency_code': 'USD',
                },
                'supplementary_data': {
                    'related_ids': {'order_id': self.contract.gateway_reference},
                },
            },
        }
        return self.client.post(
            self.url, data=b'{}', content_type='application/json',
        )

    @patch('escrow.views.get_payment_gateway')
    def test_replayed_event_id_is_ignored(self, mock_gateway):
        first = self.post(mock_gateway)
        self.assertEqual(first.status_code, 200)
        second = self.post(mock_gateway)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.content, b'Duplicate event')
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, 'awaiting_dispatch')
        self.assertEqual(
            AuditEvent.objects.filter(
                action='funding.confirmed', escrow=self.contract
            ).count(), 1,
        )

    @patch('escrow.views.get_payment_gateway')
    def test_rejected_signature_is_logged(self, mock_gateway):
        mock_gateway.return_value.verify_incoming_webhook.return_value = False
        response = self.client.post(
            self.url, data=b'{}', content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(
            WebhookEvent.objects.filter(
                provider='paypal', signature_valid=False
            ).exists()
        )


class IdentityTest(TestCase):
    def test_identity_with_verified_contact(self):
        identity = Identity.objects.create(email='Buyer@Test.com')
        self.assertEqual(identity.email, 'buyer@test.com')
        VerifiedContact.objects.create(
            identity=identity, channel='email', value='buyer@test.com',
        )
        self.assertEqual(identity.contacts.count(), 1)

    def test_duplicate_contact_is_rejected(self):
        from django.db import IntegrityError
        identity = Identity.objects.create(email='buyer@test.com')
        VerifiedContact.objects.create(
            identity=identity, channel='email', value='buyer@test.com',
        )
        with self.assertRaises(IntegrityError):
            VerifiedContact.objects.create(
                identity=identity, channel='email', value='buyer@test.com',
            )
