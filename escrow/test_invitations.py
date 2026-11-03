"""Counterparty invitation emails (spec sections 8.6, 11).

Sent once when an escrow first reaches awaiting_counterparty, from every
creation path. Best-effort: mail failure never breaks verification, and
the audit trail guards against reminder spam.
"""
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings

from escrow.invitations import send_counterparty_invitation
from escrow.models import AuditEvent, EscrowContract


def make_pending(code='INV01'):
    return EscrowContract.objects.create(
        code=code, title='Laptop', description='D', amount=Decimal('50000'),
        service_fee=Decimal('1500'), fee_payer='buyer',
        creator_email='creator@x.com', creator_role='buyer',
        counterparty_email='partner@x.com', status='pending_verification',
        gateway_reference=f'REF_{code}')


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class InvitationTest(TestCase):
    def setUp(self):
        self.send_patcher = patch('escrow.services.send_otp_via_termii',
                                  return_value=True)
        self.mock_otp = self.send_patcher.start()
        self.addCleanup(self.send_patcher.stop)

    def test_manual_verify_sends_invite(self):
        from escrow.services import issue_otp
        contract = make_pending()
        issue_otp('creator@x.com', 'create')
        code = self.mock_otp.call_args[0][1]
        response = self.client.post(f'/verify/{contract.code}/', {'otp': code})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ['partner@x.com'])
        self.assertIn(contract.code, email.body)
        self.assertIn('/join/', email.body)
        contract.refresh_from_db()
        self.assertEqual(contract.status, 'awaiting_counterparty')

    def test_no_duplicate_invites(self):
        contract = make_pending()
        self.assertTrue(send_counterparty_invitation(contract, base_url='http://t'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertFalse(send_counterparty_invitation(contract, base_url='http://t'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            AuditEvent.objects.filter(
                escrow=contract, action='invitation.sent').count(), 1)

    def test_mail_failure_does_not_break_flow(self):
        contract = make_pending()
        with patch('escrow.invitations.send_mail',
                   side_effect=Exception('smtp down')):
            self.assertFalse(send_counterparty_invitation(contract, base_url='http://t'))
        self.assertEqual(len(mail.outbox), 0)

    def test_agent_resume_sends_invite(self):
        from escrow.models import EscrowDraft
        from agent.loop import get_or_create_conversation, propose_action
        self.client.get('/assistant/')
        from agent.models import AIConversation
        convo = AIConversation.objects.get(channel='web')
        session_key = self.client.session.session_key
        draft = EscrowDraft.objects.create(
            owner_email='', owner_session=session_key,
            title='T', description='D', amount=Decimal('50000'),
            fee_payer='buyer', creator_role='buyer',
            counterparty_email='partner@x.com', inspection_days=3)
        propose_action(convo, 'request_escrow_creation', {'draft_id': draft.id})
        convo.pending_action['verification_email'] = 'creator@x.com'
        convo.pending_action['draft_id'] = draft.id
        convo.save(update_fields=['pending_action'])
        draft.owner_email = 'creator@x.com'
        draft.save(update_fields=['owner_email'])
        from escrow.services import issue_otp
        issue_otp('creator@x.com', 'create')
        code = self.mock_otp.call_args[0][1]
        response = self.client.post('/assistant/send/', {'message': code})
        self.assertEqual(response.status_code, 200)
        invites = [m for m in mail.outbox if m.to == ['partner@x.com']]
        self.assertEqual(len(invites), 1)
        self.assertIn('/join/', invites[0].body)

    def test_join_page_prefills_code(self):
        response = self.client.get('/join/?code=kSLI1rjlN')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="kSLI1rjlN"')
