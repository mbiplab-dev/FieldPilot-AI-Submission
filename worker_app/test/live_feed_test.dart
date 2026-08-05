import 'dart:convert';

import 'package:fieldpilot_worker/core/live_feed.dart';
import 'package:flutter_test/flutter_test.dart';

/// Frame parsing is the boundary between the network and the speech path, so a malformed frame
/// must degrade to `null` rather than throw somewhere inside a socket callback where nothing is
/// listening to catch it.
void main() {
  String frame(Map<String, dynamic> m) => jsonEncode(m);

  group('LiveFrame.tryParse', () {
    test('parses a primary alert frame and exposes the spoken sentence', () {
      final f = LiveFrame.tryParse(frame({
        'topic': 'alert',
        'zone': 'zone-a',
        'ts': 1785859147.5,
        'data': {
          'alert_id': 'al-1',
          'severity': 'high',
          'speech': 'Stop work. Put on your hard hat.',
          'message': 'no helmet',
        },
      }));

      expect(f, isNotNull);
      expect(f!.topic, 'alert');
      expect(f.zone, 'zone-a');
      expect(f.ts, 1785859147.5);
      expect(f.alertId, 'al-1');
      expect(f.severity, 'high');
      expect(f.speech, 'Stop work. Put on your hard hat.');
      expect(f.message, 'no helmet');
    });

    test('parses an advisory frame', () {
      final f = LiveFrame.tryParse(frame({
        'topic': 'advisory',
        'zone': 'zone-a',
        'ts': 1.0,
        'data': {
          'severity': 'low',
          'speech': 'Warning. A possible fall was detected for a worker in your zone.',
        },
      }));

      expect(f!.topic, 'advisory');
      expect(f.speech, contains('in your zone'));
      // Advisories carry no alert_id of their own subject's alert in every case — must not throw.
      expect(f.alertId, isNull);
    });

    test('drops keep-alive pongs, which are not site activity', () {
      expect(LiveFrame.tryParse(frame({'topic': 'pong', 'ts': 1.0, 'data': {}})), isNull);
    });

    test('returns null rather than throwing on unparseable input', () {
      for (final raw in <String>[
        'not json at all',
        '',
        '[]', // valid JSON, wrong shape
        '"a string"',
        '123',
        '{"no_topic": true}',
        '{"topic": 42}', // topic must be a string
      ]) {
        expect(LiveFrame.tryParse(raw), isNull, reason: 'input: $raw');
      }
    });

    test('a frame with a missing or wrongly typed data block still parses', () {
      final noData = LiveFrame.tryParse(frame({'topic': 'alert', 'ts': 1.0}));
      expect(noData, isNotNull);
      expect(noData!.data, isEmpty);
      expect(noData.speech, isNull);
      expect(noData.severity, isNull);

      final badData = LiveFrame.tryParse('{"topic":"alert","ts":1.0,"data":"oops"}');
      expect(badData, isNotNull);
      expect(badData!.data, isEmpty);
    });

    test('a missing timestamp degrades to zero instead of throwing', () {
      final f = LiveFrame.tryParse(frame({'topic': 'alert', 'data': {}}));
      expect(f!.ts, 0);
    });

    test('an integer timestamp is accepted as well as a double', () {
      // JSON numbers arrive as int when they have no fractional part.
      final f = LiveFrame.tryParse('{"topic":"alert","ts":1785859147,"data":{}}');
      expect(f!.ts, 1785859147.0);
    });
  });
}
