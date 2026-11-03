"""Full circle in chat: join after OTP, bank details, release, notify."""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from agent.loop import get_or_create_conversation
from agent.registry import CONSEQUENTIAL_TOOLS, execute
from escrow.models import EscrowContract, SettlementVault
from escrow.services import complete_counterparty_join, issue_otp


def make_contract(code='LKTRfc001', status='awaiting_counterparty',
                  creator='seller@x.com', creator_role='seller',
                  counterparty='buyer@x.com'):
    return EscrowContract.objects.create(
        code=code, title='Laptop', description='D', amount=Decimal('50000'),
        service_fee=Decimal('1500'), fee_payer='buyer',
        creator_email=creator, creator_role=creator_role,
        counterparty_email=counterparty, status=status,
        gateway_reference=f'REF_{code}')


class JoinServiceTest(TestCase):
    def test_join_moves_to_funding_and_flags(self):
        contract = make_contract()
        joined, error = complete_counterparty_join(contract.code, 'buyer@x.com')
        self.assertIsNotNone(joined, error)
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_funding')
        self.assertTrue(contract.buyer_confirmed)

    def test_wrong_email_refused(self):
        contract = make_contract()
        joined, error = complete_counterparty_join(contract.code, 'stranger@x.com')
        self.assertIsNone(joined)
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_counterparty')

    def test_wrong_status_refused(self):
        contract = make_contract(status='in_transit')
        joined, _ = complete_counterparty_join(contract.code, 'buyer@x.com')
        self.assertIsNone(joined)

    def test_unknown_code_refused(self):
        joined, _ = complete_counterparty_join('LKTRnope1', 'buyer@x.com')
        self.assertIsNone(joined)

    def test_join_tool_is_consequential(self):
        self.assertIn('request_counterparty_join', CONSEQUENTIAL_TOOLS)
        self.assertIn('request_seller_bank_details', CONSEQUENTIAL_TOOLS)
        self.assertNotIn('get_bank_list', CONSEQUENTIAL_TOOLS)

    def test_join_tool_executes_for_counterparty(self):
        contract = make_contract(code='LKTRfc002')
        result = execute('request_counterparty_join',
                         {'escrow_reference': contract.code},
                         actor_email='buyer@x.com')
        self.assertTrue(result['ok'], result)
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_funding')

    def test_creator_cannot_join_own_escrow(self):
        contract = make_contract(code='LKTRfc003')
        result = execute('request_counterparty_join',
                         {'escrow_reference': contract.code},
                         actor_email='seller@x.com')
        self.assertFalse(result['ok'])


class ChatJoinAfterOtpTest(TestCase):
    def setUp(self):
        patcher = patch('escrow.services.send_otp_via_termii', return_value=True)
        self.mock_send = patcher.start()
        self.addCleanup(patcher.stop)

    def test_pasted_code_completes_join(self):
        contract = make_contract(code='LKTRfc010')
        self.client.get('/assistant/')
        from agent.models import AIConversation
        convo = AIConversation.objects.get(channel='web')
        convo.pending_email = 'buyer@x.com'
        convo.pending_context = {'join_escrow_reference': contract.code}
        convo.save(update_fields=['pending_email', 'pending_context'])
        issue_otp('buyer@x.com', 'reauth')
        code = self.mock_send.call_args[0][1]
        with patch('agent.views.run_turn') as mock_turn:
            response = self.client.post('/assistant/send/', {'message': code})
            mock_turn.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'joined')
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_funding')
        self.assertTrue(contract.buyer_confirmed)
        convo.refresh_from_db()
        self.assertEqual(convo.owner_email, 'buyer@x.com')
        self.assertEqual(convo.pending_context, {})

    def test_verification_tool_stores_join_context(self):
        convo = get_or_create_conversation('web', 'fc-verify')
        result = execute(
            'request_verification_code',
            {'email': 'buyer@x.com', 'escrow_reference': 'LKTRfc010'},
            actor_email=None, conversation=convo)
        self.assertTrue(result['ok'], result)
        convo.refresh_from_db()
        self.assertEqual(convo.pending_email, 'buyer@x.com')
        self.assertEqual(
            convo.pending_context.get('join_escrow_reference'), 'LKTRfc010')

    def test_join_scanned_from_history_without_context(self):
        contract = make_contract(code='LKTRfc011')
        self.client.get('/assistant/')
        from agent.models import AIConversation
        convo = AIConversation.objects.get(channel='web')
        convo.messages.create(
            role='user', content=f'I want to join {contract.code}')
        convo.pending_email = 'buyer@x.com'
        convo.save(update_fields=['pending_email'])
        issue_otp('buyer@x.com', 'reauth')
        code = self.mock_send.call_args[0][1]
        response = self.client.post('/assistant/send/', {'message': code})
        self.assertContains(response, 'joined')
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_funding')


class BankDetailsTest(TestCase):
    def setUp(self):
        self.contract = make_contract(
            code='LKTRfc020', status='awaiting_dispatch',
            creator='buyer@x.com', creator_role='buyer',
            counterparty='seller@x.com')

    def test_seller_saves_verified_details(self):
        result = execute(
            'request_seller_bank_details',
            {'escrow_reference': self.contract.code,
             'paypal_email': 'seller@example.com'},
            actor_email='seller@x.com')
        self.assertTrue(result['ok'], result)
        vault = SettlementVault.objects.get(contract=self.contract)
        self.assertEqual(vault.paypal_email, 'seller@example.com')
        self.assertEqual(vault.bank_name, 'PayPal')
        self.assertEqual(result['data']['paypal_email'], 'seller@example.com')
        self.assertEqual(result['data']['masked'], 'se***@example.com')

    def test_buyer_cannot_save_seller_details(self):
        result = execute(
            'request_seller_bank_details',
            {'escrow_reference': self.contract.code,
             'paypal_email': 'seller@example.com'},
            actor_email='buyer@x.com')
        self.assertFalse(result['ok'])
        self.assertFalse(SettlementVault.objects.exists())

    def test_unknown_bank_and_bad_number_refused(self):
        self.assertFalse(execute(
            'request_seller_bank_details',
            {'escrow_reference': self.contract.code,
             'paypal_email': 'not-an-email'},
            actor_email='seller@x.com')['ok'])
        self.assertFalse(execute(
            'request_seller_bank_details',
            {'escrow_reference': self.contract.code,
             'paypal_email': ''},
            actor_email='seller@x.com')['ok'])

    def test_failed_verification_saves_nothing(self):
        result = execute(
            'request_seller_bank_details',
            {'escrow_reference': self.contract.code,
             'paypal_email': 'bad-email'},
            actor_email='seller@x.com')
        self.assertFalse(result['ok'])
        self.assertFalse(SettlementVault.objects.exists())

    def test_existing_vault_not_overwritten(self):
        SettlementVault.objects.create(
            contract=self.contract, paypal_email='seller@example.com',
            bank_name='PayPal', account_name='seller@example.com')
        result = execute(
            'request_seller_bank_details',
            {'escrow_reference': self.contract.code,
             'paypal_email': 'other@example.com'},
            actor_email='seller@x.com')
        self.assertTrue(result['ok'])
        self.assertTrue(result['data'].get('already_saved'))
        self.assertEqual(
            SettlementVault.objects.get(contract=self.contract).paypal_email,
            'seller@example.com')

    def test_bank_list_tool(self):
        result = execute('get_bank_list', {}, actor_email=None)
        self.assertTrue(result['ok'])
        self.assertEqual(result['data']['banks'], [])


class ReleaseBankGateTest(TestCase):
    def test_missing_vault_notifies_seller_and_blocks(self):
        contract = make_contract(
            code='LKTRfc030', status='in_inspection',
            creator='seller@x.com', creator_role='seller',
            counterparty='buyer@x.com')
        with patch('agent.registry.notify_event') as mock_notify:
            result = execute('request_release_approval',
                             {'escrow_reference': contract.code},
                             actor_email='buyer@x.com')
        self.assertFalse(result['ok'])
        self.assertIn('paypal', result['error'].lower())
        self.assertIn('payout email', result['error'].lower())
        mock_notify.assert_called_once_with(
            'bank_details_needed', contract, actor='buyer@x.com')
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'in_inspection')

    def test_receipt_sets_inspection_deadline(self):
        contract = make_contract(
            code='LKTRfc031', status='in_transit',
            creator='seller@x.com', creator_role='seller',
            counterparty='buyer@x.com')
        contract.inspection_days = 3
        contract.save(update_fields=['inspection_days'])
        before = timezone.now()
        result = execute('request_delivery_confirmation',
                         {'escrow_reference': contract.code, 'stage': 'receipt'},
                         actor_email='buyer@x.com')
        self.assertTrue(result['ok'], result)
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'in_inspection')
        self.assertIsNotNone(contract.inspection_ends)
        self.assertGreater(contract.inspection_ends, before)


@override_settings(WHATSAPP_ACCESS_TOKEN='tok', WHATSAPP_PHONE_NUMBER_ID='123')
class CreatorNotifiedTest(TestCase):
    def test_join_notifies_creator_not_joiner(self):
        from whatsapp.models import WhatsAppLink
        WhatsAppLink.objects.create(wa_id='234SELLER', verified_email='seller@x.com')
        WhatsAppLink.objects.create(wa_id='234BUYER', verified_email='buyer@x.com')
        contract = make_contract(code='LKTRfc040')
        with patch('whatsapp.adapter.send_text_message',
                   return_value=True) as mock_send:
            complete_counterparty_join(contract.code, 'buyer@x.com')
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(mock_send.call_args[0][0], '234SELLER')
        self.assertIn('joined', mock_send.call_args[0][1])

class ResumeEitherPartyTest(TestCase):
    def setUp(self):
        patcher = patch('escrow.services.send_otp_via_termii', return_value=True)
        self.mock_send = patcher.start()
        self.addCleanup(patcher.stop)

    def test_creator_resumes_own_escrow(self):
        from escrow.services import resume_escrow_after_verification
        contract = make_contract(code='LKTRfc050', status='awaiting_funding')
        resumed, kind, error = resume_escrow_after_verification(
            contract.code, 'seller@x.com')
        self.assertIsNotNone(resumed, error)
        self.assertEqual(kind, 'resumed')
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_funding')

    def test_joined_counterparty_resumes_not_rejoins(self):
        from escrow.services import resume_escrow_after_verification
        contract = make_contract(code='LKTRfc051', status='awaiting_funding')
        resumed, kind, _ = resume_escrow_after_verification(
            contract.code, 'buyer@x.com')
        self.assertEqual(kind, 'resumed')

    def test_counterparty_still_joins_via_resume(self):
        from escrow.services import resume_escrow_after_verification
        contract = make_contract(code='LKTRfc052')
        resumed, kind, _ = resume_escrow_after_verification(
            contract.code, 'buyer@x.com')
        self.assertEqual(kind, 'joined')
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_funding')

    def test_non_party_refused(self):
        from escrow.services import resume_escrow_after_verification
        contract = make_contract(code='LKTRfc053', status='awaiting_funding')
        resumed, kind, error = resume_escrow_after_verification(
            contract.code, 'stranger@x.com')
        self.assertIsNone(resumed)
        self.assertIn('not linked', error)

    def test_creator_continue_in_chat_welcomes_back(self):
        contract = make_contract(code='LKTRfc054', status='awaiting_funding')
        self.client.get('/assistant/')
        from agent.models import AIConversation
        convo = AIConversation.objects.get(channel='web')
        convo.messages.create(
            role='user', content=f'continue {contract.code}')
        convo.pending_email = 'seller@x.com'
        convo.pending_context = {'join_escrow_reference': contract.code}
        convo.save(update_fields=['pending_email', 'pending_context'])
        issue_otp('seller@x.com', 'reauth')
        code = self.mock_send.call_args[0][1]
        response = self.client.post('/assistant/send/', {'message': code})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'welcome back')
        self.assertNotContains(response, "can't continue")
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_funding')

    def test_receipt_asks_seller_for_bank_details(self):
        contract = make_contract(
            code='LKTRfc055', status='in_transit',
            creator='seller@x.com', creator_role='seller',
            counterparty='buyer@x.com')
        with patch('agent.registry.notify_event') as mock_notify:
            result = execute('request_delivery_confirmation',
                             {'escrow_reference': contract.code,
                              'stage': 'receipt'},
                             actor_email='buyer@x.com')
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['data']['seller_bank_saved'])
        events = [c.args[0] for c in mock_notify.call_args_list]
        self.assertIn('receipt_confirmed', events)
        self.assertIn('bank_details_needed', events)

    def test_status_reports_bank_saved_flag(self):
        contract = make_contract(code='LKTRfc056', status='in_inspection')
        result = execute('get_authorized_escrow_status',
                         {'escrow_reference': contract.code},
                         actor_email='buyer@x.com')
        self.assertTrue(result['ok'])
        self.assertFalse(result['data']['seller_bank_saved'])
        SettlementVault.objects.create(
            contract=contract, paypal_email='seller@example.com',
            bank_name='PayPal', account_name='seller@example.com')
        result = execute('get_authorized_escrow_status',
                         {'escrow_reference': contract.code},
                         actor_email='seller@x.com')
        self.assertTrue(result['data']['seller_bank_saved'])

    def test_next_step_asks_seller_for_bank_in_inspection(self):
        from escrow.services import escrow_next_step
        contract = make_contract(code='LKTRfc057', status='in_inspection')
        step = escrow_next_step(contract, 'seller@x.com')
        self.assertIn('paypal', step.lower())

    def test_bank_details_accept_numeric_json_args(self):
        # account_number is accepted as an alias for paypal_email, so a
        # model emitting the legacy key still saves, without a generic crash.
        contract = make_contract(
            code='LKTRfc060', status='in_inspection',
            creator='buyer@x.com', creator_role='buyer',
            counterparty='seller@x.com')
        result = execute(
            'request_seller_bank_details',
            {'escrow_reference': contract.code,
             'account_number': 'seller@example.com'},
            actor_email='seller@x.com')
        self.assertTrue(result['ok'], result)
        vault = SettlementVault.objects.get(contract=contract)
        self.assertEqual(vault.paypal_email, 'seller@example.com')
        self.assertTrue(vault.bank_name.startswith('PayPal'))
        self.assertEqual(vault.account_name, 'seller@example.com')

class PayPalEmailVerificationTest(TestCase):
    def test_valid_paypal_email_accepted_without_network(self):
        from escrow.bank_verification import verify_paypal_email
        with override_settings(DEBUG=True, DEMO_MODE=False):
            result = verify_paypal_email('seller@example.com')
        self.assertTrue(result['status'])
        self.assertEqual(result['account_name'], 'seller@example.com')

    def test_invalid_paypal_email_rejected_without_network(self):
        from escrow.bank_verification import verify_paypal_email
        with override_settings(DEBUG=True, DEMO_MODE=False):
            self.assertFalse(verify_paypal_email('not-an-email')['status'])
            self.assertFalse(verify_paypal_email('')['status'])

class WebchatPushTest(TestCase):
    def _web_convo(self, email):
        from agent.loop import get_or_create_conversation
        convo = get_or_create_conversation('web', f'push-{email}')
        convo.owner_email = email
        convo.save(update_fields=['owner_email'])
        return convo

    def test_bank_needed_lands_in_seller_webchat(self):
        from escrow.lifecycle import notify_event
        seller_convo = self._web_convo('seller@x.com')
        contract = make_contract(code='LKTRpush1', status='in_inspection')
        sent = notify_event('bank_details_needed', contract, actor='buyer@x.com')
        self.assertGreaterEqual(sent, 1)
        contents = [m.content for m in seller_convo.messages.all()]
        self.assertTrue(any('PayPal payout email' in c for c in contents), contents)

    def test_actor_own_chat_not_duplicated(self):
        from escrow.lifecycle import notify_event
        buyer_convo = self._web_convo('buyer@x.com')
        seller_convo = self._web_convo('seller@x.com')
        contract = make_contract(code='LKTRpush2', status='in_inspection')
        notify_event('receipt_confirmed', contract, actor='buyer@x.com')
        self.assertEqual(buyer_convo.messages.count(), 0)
        self.assertEqual(seller_convo.messages.count(), 1)

    def test_no_webchat_is_silent(self):
        from escrow.lifecycle import notify_event
        contract = make_contract(code='LKTRpush3', status='in_inspection')
        self.assertEqual(
            notify_event('bank_details_needed', contract, actor='buyer@x.com'), 0)

    def test_poll_returns_new_then_204(self):
        self.client.get('/assistant/')
        from agent.models import AIConversation
        convo = AIConversation.objects.get(channel='web')
        convo.messages.create(role='assistant', content='Seller shipped it!')
        response = self.client.get('/assistant/poll/?since=0')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Seller shipped it!')
        last_id = convo.messages.order_by('-id').first().id
        response = self.client.get(f'/assistant/poll/?since={last_id}')
        self.assertEqual(response.status_code, 204)
        convo.refresh_from_db()
        self.assertEqual(convo.messages.count(), 1)
