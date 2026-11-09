import io
import math
import struct
import wave
from unittest.mock import patch

from django.test import TestCase, override_settings

from voice.transcriber import (
    VoiceConfigError,
    VoiceTranscriptionError,
    convert_to_wav,
    transcribe_audio,
    transcribe_wav,
)


def make_wav(seconds=0.5, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        frames = b''.join(
            struct.pack('<h', int(10000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(seconds * rate)))
        fh.writeframes(frames)
    return buf.getvalue()


class ConvertTest(TestCase):
    def test_valid_wav_passes_through(self):
        out = convert_to_wav(make_wav())
        self.assertTrue(out.startswith(b'RIFF'))

    def test_garbage_raises_friendly_error(self):
        with self.assertRaises(VoiceTranscriptionError):
            convert_to_wav(b'this is not audio at all' * 100)

    def test_empty_raises(self):
        with self.assertRaises(VoiceTranscriptionError):
            convert_to_wav(b'')

    @override_settings(VOICE_MAX_BYTES=10)
    def test_oversize_rejected(self):
        with self.assertRaises(VoiceTranscriptionError):
            convert_to_wav(make_wav())


class TranscribeApiTest(TestCase):
    @override_settings(MODEL_API_KEY='')
    def test_missing_key_is_config_error(self):
        with self.assertRaises(VoiceConfigError):
            transcribe_wav(make_wav())

    @override_settings(MODEL_API_KEY='k')
    def test_success_returns_payload(self):
        from requests import Response
        resp = Response()
        resp.status_code = 200
        resp._content = b'{"transcript": "buy a laptop", "turns": []}'
        with patch('voice.transcriber.requests.post', return_value=resp):
            payload = transcribe_wav(make_wav())
        self.assertEqual(payload['transcript'], 'buy a laptop')

    @override_settings(MODEL_API_KEY='k')
    def test_401_is_config_error(self):
        from requests import Response
        resp = Response()
        resp.status_code = 401
        resp._content = b'unauthorized'
        with patch('voice.transcriber.requests.post', return_value=resp):
            with self.assertRaises(VoiceConfigError):
                transcribe_wav(make_wav())

    @override_settings(MODEL_API_KEY='k')
    def test_429_and_500_are_retryable_errors(self):
        from requests import Response
        for code in (429, 500):
            resp = Response()
            resp.status_code = code
            resp._content = b'busy'
            with patch('voice.transcriber.requests.post', return_value=resp):
                with self.assertRaises(VoiceTranscriptionError):
                    transcribe_wav(make_wav())

    @override_settings(MODEL_API_KEY='k')
    def test_empty_transcript_raises(self):
        from requests import Response
        resp = Response()
        resp.status_code = 200
        resp._content = b'{"transcript": "   "}'
        with patch('voice.transcriber.requests.post', return_value=resp):
            with self.assertRaises(VoiceTranscriptionError):
                transcribe_audio(make_wav())


class ChatVoiceViewTest(TestCase):
    def setUp(self):
        self.send_patcher = patch('escrow.services.send_otp_via_termii',
                                  return_value=True)
        self.mock_send = self.send_patcher.start()
        self.addCleanup(self.send_patcher.stop)

    def post_voice(self, content=b'RIFFfake', name='note.webm'):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.client.post(
            '/assistant/voice/',
            {'audio': SimpleUploadedFile(name, content,
                                         content_type='audio/webm')})

    def test_voice_note_runs_a_turn(self):
        from agent.models import AIConversation
        with patch('agent.views.transcribe_audio',
                   return_value='fee on 50000?') as mock_tr, \
             patch('agent.views.run_turn') as mock_turn:
            from agent.models import ConversationMessage
            mock_turn.side_effect = lambda c, t, **kw: (
                ConversationMessage.objects.create(
                    conversation_id=c.id, role='user', content=t),
                ConversationMessage.objects.create(
                    conversation_id=c.id, role='assistant', content='Fee ok'),
                'Fee ok')[2]
            response = self.post_voice()
        self.assertEqual(response.status_code, 200)
        mock_tr.assert_called_once()
        self.assertEqual(mock_turn.call_args[0][1], 'fee on 50000?')
        convo = AIConversation.objects.get(channel='web')
        self.assertTrue(convo.messages.filter(
            role='user', content='fee on 50000?').exists())

    def test_transcription_failure_shows_message(self):
        with patch('agent.views.transcribe_audio',
                   side_effect=VoiceTranscriptionError('nope')):
            response = self.post_voice()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'nope')

    def test_missing_key_degrades(self):
        with patch('agent.views.transcribe_audio',
                   side_effect=VoiceConfigError('not configured')):
            response = self.post_voice()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'not configured')

    def test_field_transcribe_returns_json(self):
        with patch('agent.views.transcribe_audio',
                   return_value='laptop for Adaeze'):
            response = self.post_voice()
        # Field endpoint is separate from chat_voice.
        from django.core.files.uploadedfile import SimpleUploadedFile
        with patch('agent.views.transcribe_audio',
                   return_value='laptop for Adaeze'):
            response = self.client.post(
                '/assistant/transcribe/',
                {'audio': SimpleUploadedFile('n.webm', b'x',
                                             content_type='audio/webm')})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {'ok': True, 'transcript': 'laptop for Adaeze'})

    def test_field_transcribe_throttles(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        with patch('agent.views.transcribe_audio', return_value='hi'):
            last = None
            for _ in range(22):
                last = self.client.post(
                    '/assistant/transcribe/',
                    {'audio': SimpleUploadedFile('n.webm', b'x',
                                                 content_type='audio/webm')})
            self.assertEqual(last.status_code, 429)


class WhatsAppVoiceTest(TestCase):
    def setUp(self):
        self.send_patcher = patch('whatsapp.adapter.send_text_message',
                                  return_value=True)
        self.mock_send = self.send_patcher.start()
        self.addCleanup(self.send_patcher.stop)
        self.otp_patcher = patch('escrow.services.send_otp_via_termii',
                                 return_value=True)
        self.otp_patcher.start()
        self.addCleanup(self.otp_patcher.stop)

    def test_voice_note_flows_into_agent(self):
        from whatsapp.flow import handle_voice_message
        with patch('whatsapp.flow.transcribe_audio',
                   return_value='hello there') as mock_tr, \
             patch('whatsapp.flow.run_turn', return_value='hi back') as mock_turn:
            reply = handle_voice_message('2348000000077', b'ogg-bytes')
        self.assertEqual(reply, 'hi back')
        mock_tr.assert_called_once_with(b'ogg-bytes')
        mock_turn.assert_called_once()
        self.assertIn('hello there', mock_turn.call_args[0][1])

    def test_transcription_failure_replies_gracefully(self):
        from whatsapp.flow import handle_voice_message
        with patch('whatsapp.flow.transcribe_audio',
                   side_effect=VoiceTranscriptionError('unclear')):
            reply = handle_voice_message('2348000000078', b'ogg-bytes')
        self.assertEqual(reply, 'unclear')

    def test_missing_audio_replies_gracefully(self):
        from whatsapp.flow import handle_voice_message
        reply = handle_voice_message('2348000000079', None)
        self.assertIn('voice note', reply)

    def test_audio_webhook_downloads_and_processes(self):
        import hashlib
        import hmac
        import json as _json

        from django.test import override_settings
        payload = {
            'object': 'whatsapp_business_account',
            'entry': [{'changes': [{'value': {
                'messaging_product': 'whatsapp',
                'messages': [{
                    'from': '2348000000099', 'id': 'wamid.voice1',
                    'timestamp': '1', 'type': 'audio',
                    'audio': {'id': 'media-1', 'mime_type': 'audio/ogg'},
                }],
            }}]}],
        }
        body = _json.dumps(payload).encode()
        sig = 'sha256=' + hmac.new(
            b'test-secret', body, hashlib.sha256).hexdigest()
        with override_settings(WHATSAPP_APP_SECRET='test-secret',
                               WHATSAPP_ACCESS_TOKEN='tok',
                               WHATSAPP_PHONE_NUMBER_ID='123'), \
             patch('whatsapp.adapter.download_media',
                   return_value=b'ogg') as mock_dl, \
             patch('whatsapp.flow.transcribe_audio',
                   return_value='create escrow') as mock_tr, \
             patch('whatsapp.flow.run_turn', return_value='ok'):
            response = self.client.post(
                '/webhooks/whatsapp/', data=body,
                content_type='application/json',
                HTTP_X_HUB_SIGNATURE_256=sig)
        self.assertEqual(response.status_code, 200)
        mock_dl.assert_called_once_with('media-1')
        mock_tr.assert_called_once_with(b'ogg')

    def test_non_dict_payload_raises_friendly_error(self):
        from unittest.mock import MagicMock
        from voice.transcriber import transcribe_audio
        fake_wav = make_wav()
        with patch('voice.transcriber.convert_to_wav', return_value=fake_wav), \
             patch('voice.transcriber.transcribe_wav', return_value=['not', 'a', 'dict']):
            with self.assertRaises(VoiceTranscriptionError):
                transcribe_audio(b'x' * 100)
