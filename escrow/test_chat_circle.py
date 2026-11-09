"""Full circle in chat: verify, join, unverified drafts forgotten, on-demand listing."""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from agent.loop import get_or_create_conversation, run_turn
from agent.registry import CONSEQUENTIAL_TOOLS, execute
from escrow.models import AuthOTP, EscrowContract, EscrowDraft
from escrow.services import issue_otp
from escrow.utils import generate_escrow_code, normalize_email


def make_contract(code='LKTRaB1xK', buyer='buyer@x.com', seller='seller@x.com',
                  status='awaiting_funding'):
    return EscrowContract.objects.create(
        code=code, title='T', description='D', amount=Decimal('50000'),
        service_fee=Decimal('1500'), fee_payer='buyer',
        creator_email=buyer, creator_role='buyer',
        counterparty_email=seller, status=status,
        gateway_reference=f'REF_{code}')


class TrustCodeTest(TestCase):
    def test_format_and_uniqueness(self):
        import re
        for _ in range(5):
            code = generate_escrow_code()
            self.assertTrue(re.fullmatch(r'LKTR[A-Za-z0-9]{5}', code), code)
        from escrow.services import get_unique_contract_code
        seen = {get_unique_contract_code() for _ in range(20)}
        self.assertEqual(len(seen), 20)
        self.assertTrue(all(s.startswith('LKTR') for s in seen))


class VerificationToolTest(TestCase):
    def setUp(self):
        patcher = patch('escrow.services.send_otp_via_termii', return_value=True)
        self.mock_send = patcher.start()
        self.addCleanup(patcher.stop)

    def test_sends_code_and_parks_pending(self):
        from agent.models import AIConversation
        convo = get_or_create_conversation('web', 'verify-1')
        result = execute('request_verification_code', {'email': 'joiner@x.com'},
                         actor_email=None, conversation=convo)
        self.assertTrue(result['ok'])
        convo.refresh_from_db()
        self.assertEqual(convo.pending_email, 'joiner@x.com')
        self.assertTrue(AuthOTP.objects.filter(
            email='joiner@x.com', intent='reauth', is_used=False).exists())

    def test_invalid_email_refused(self):
        convo = get_or_create_conversation('web', 'verify-2')
        self.assertFalse(execute(
            'request_verification_code', {'email': 'not-an-email'},
            actor_email=None, conversation=convo)['ok'])

    def test_already_verified_short_circuits(self):
        convo = get_or_create_conversation('web', 'verify-3')
        convo.owner_email = 'joiner@x.com'
        convo.save(update_fields=['owner_email'])
        result = execute('request_verification_code', {'email': 'joiner@x.com'},
                         actor_email='joiner@x.com', conversation=convo)
        self.assertTrue(result['ok'])
        self.assertTrue(result['data'].get('already_verified'))
        self.assertFalse(AuthOTP.objects.filter(email='joiner@x.com').exists())

    def test_tool_is_not_consequential(self):
        self.assertNotIn('request_verification_code', CONSEQUENTIAL_TOOLS)

    def test_run_turn_stores_pending(self):
        convo = get_or_create_conversation('web', 'verify-4')
        call = {'id': 'v1', 'name': 'request_verification_code',
                'arguments': {'email': 'run@x.com'}}
        with patch('agent.loop.chat', return_value={
                'text': '', 'tool_calls': [call], 'usage': {}}):
            run_turn(convo, 'my email is run@x.com')
        convo.refresh_from_db()
        self.assertEqual(convo.pending_email, 'run@x.com')


class ChatConsumeCodeTest(TestCase):
    def setUp(self):
        patcher = patch('escrow.services.send_otp_via_termii', return_value=True)
        self.mock_send = patcher.start()
        self.addCleanup(patcher.stop)

    def test_pasted_code_verifies_without_model(self):
        self.client.get('/assistant/')
        from agent.models import AIConversation
        convo = AIConversation.objects.get(channel='web')
        convo.pending_email = 'joiner@x.com'
        convo.save(update_fields=['pending_email'])
        issue_otp('joiner@x.com', 'reauth')
        code = self.mock_send.call_args[0][1]
        with patch('agent.views.run_turn') as mock_turn:
            response = self.client.post('/assistant/send/', {'message': code})
            mock_turn.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Verified')
        convo.refresh_from_db()
        self.assertEqual(convo.owner_email, 'joiner@x.com')
        self.assertIsNone(convo.pending_email)

    def test_wrong_code_stays_unverified(self):
        self.client.get('/assistant/')
        from agent.models import AIConversation
        convo = AIConversation.objects.get(channel='web')
        convo.pending_email = 'joiner@x.com'
        convo.save(update_fields=['pending_email'])
        issue_otp('joiner@x.com', 'reauth')
        with patch('agent.views.run_turn') as mock_turn:
            self.client.post('/assistant/send/', {'message': '000000'})
            mock_turn.assert_not_called()
        convo.refresh_from_db()
        self.assertIsNone(convo.owner_email)
        self.assertEqual(convo.pending_email, 'joiner@x.com')


class DraftAmnesiaTest(TestCase):
    def _convo(self):
        self.client.get('/assistant/')
        from agent.models import AIConversation
        return AIConversation.objects.get(channel='web')

    def test_reload_abandons_anonymous_drafts(self):
        convo = self._convo()
        session_key = self.client.session.session_key
        draft = EscrowDraft.objects.create(
            owner_email='', owner_session=session_key,
            title='Old deal', amount=Decimal('50000'))
        self.client.get('/assistant/')
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'abandoned')

    def test_reload_abandons_even_verified_owner_drafts(self):
        convo = self._convo()
        session = self.client.session
        session['chat_verified_email'] = 'me@x.com'
        session.save()
        self.client.get('/assistant/')
        convo.refresh_from_db()
        draft = EscrowDraft.objects.create(
            owner_email='me@x.com',
            owner_session=self.client.session.session_key,
            title='Mine', amount=Decimal('50000'))
        self.client.get('/assistant/')
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'abandoned')

    def test_abandoned_draft_cannot_be_used(self):
        convo = self._convo()
        session_key = self.client.session.session_key
        draft = EscrowDraft.objects.create(
            owner_email='', owner_session=session_key,
            title='Old', amount=Decimal('50000'))
        self.client.get('/assistant/')
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'abandoned')
        self.assertFalse(execute(
            'update_escrow_draft', {'draft_id': draft.id, 'title': 'New'},
            actor_email=None, session_key=session_key)['ok'])


class ListMyEscrowsTest(TestCase):
    def test_verified_lists_theirs(self):
        make_contract(code='LKTRlist1')
        make_contract(code='LKTRlist2', status='completed')
        result = execute('list_my_escrows', {}, actor_email='buyer@x.com')
        self.assertTrue(result['ok'])
        refs = [e['reference'] for e in result['data']['escrows']]
        self.assertIn('LKTRlist1', refs)
        self.assertNotIn('LKTRlist2', refs)

    def test_closed_opt_in(self):
        make_contract(code='LKTRlist3', status='completed')
        result = execute('list_my_escrows', {'include_closed': True},
                         actor_email='buyer@x.com')
        self.assertIn('LKTRlist3',
                      [e['reference'] for e in result['data']['escrows']])

    def test_unverified_refused(self):
        make_contract(code='LKTRlist4')
        self.assertFalse(execute(
            'list_my_escrows', {}, actor_email=None)['ok'])

    def test_stranger_sees_nothing(self):
        make_contract(code='LKTRlist5')
        result = execute('list_my_escrows', {}, actor_email='other@x.com')
        self.assertTrue(result['ok'])
        self.assertEqual(result['data']['escrows'], [])

    def test_tool_is_not_consequential(self):
        self.assertNotIn('list_my_escrows', CONSEQUENTIAL_TOOLS)
