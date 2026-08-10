import 'package:fieldpilot_worker/core/models.dart';
import 'package:flutter_test/flutter_test.dart';

/// `DirectMessage.fromJson` is the boundary between the backend's `Message` shape
/// (`fieldpilot/backend/app.py::_message_out`) and the Messages tab — a field renamed or
/// mistyped here would not throw, it would just silently render the wrong sender or drop a voice
/// note, so the mapping is pinned exactly rather than trusted by inspection.
void main() {
  group('DirectMessage.fromJson', () {
    test('parses a text-only message from the site manager', () {
      final m = DirectMessage.fromJson({
        'message_id': 'msg-1',
        'worker_id': 'w-1',
        'sender_role': 'site_manager',
        'sender_id': 'mgr-1',
        'sender_name': 'Priya Singh',
        'text': 'How is scaffolding on level 3?',
        'audio_url': null,
        'audio_seconds': null,
        'read_at': null,
        'created_at': 1786000000.0,
      });

      expect(m.messageId, 'msg-1');
      expect(m.workerId, 'w-1');
      expect(m.senderRole, 'site_manager');
      expect(m.senderName, 'Priya Singh');
      expect(m.text, 'How is scaffolding on level 3?');
      expect(m.fromWorker, isFalse);
      expect(m.hasAudio, isFalse);
      expect(m.audioSeconds, isNull);
      expect(m.readAt, isNull);
      expect(m.createdAt, 1786000000.0);
    });

    test('parses a voice note from the worker, with an empty text field', () {
      final m = DirectMessage.fromJson({
        'message_id': 'msg-2',
        'worker_id': 'w-1',
        'sender_role': 'worker',
        'sender_id': 'w-1',
        'sender_name': 'Ravi Kumar',
        'text': '',
        'audio_url': '/uploads/voice-abc.m4a',
        'audio_seconds': 12.5,
        'read_at': 1786000100.0,
        'created_at': 1786000050.0,
      });

      expect(m.fromWorker, isTrue);
      expect(m.hasAudio, isTrue);
      expect(m.audioUrl, '/uploads/voice-abc.m4a');
      expect(m.audioSeconds, 12.5);
      expect(m.readAt, 1786000100.0);
    });

    test('an integer created_at is accepted as well as a double', () {
      // JSON numbers with no fractional part arrive as int, same case live_feed_test.dart pins
      // for `LiveFrame.ts`.
      final m = DirectMessage.fromJson({
        'message_id': 'msg-3',
        'worker_id': 'w-1',
        'sender_role': 'worker',
        'sender_id': 'w-1',
        'sender_name': 'Ravi Kumar',
        'text': 'On it.',
        'created_at': 1786000000,
      });
      expect(m.createdAt, 1786000000.0);
    });

    test('missing optional fields degrade instead of throwing', () {
      final m = DirectMessage.fromJson({
        'message_id': 'msg-4',
        'worker_id': 'w-1',
        'sender_role': 'site_manager',
        'sender_id': 'mgr-1',
      });

      expect(m.senderName, '');
      expect(m.text, '');
      expect(m.audioUrl, isNull);
      expect(m.hasAudio, isFalse);
      expect(m.createdAt, 0);
    });

    test('an empty string audio_url is not treated as "has audio"', () {
      // The backend never sends this, but a client that trusted a bare null-check on `audio_url`
      // instead of `hasAudio` would render a broken play button for it.
      final m = DirectMessage.fromJson({
        'message_id': 'msg-5',
        'worker_id': 'w-1',
        'sender_role': 'worker',
        'sender_id': 'w-1',
        'audio_url': '',
      });
      expect(m.hasAudio, isFalse);
    });
  });
}
