import 'package:fieldpilot_worker/core/speech_bridge.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('SpeechBridge.parseTranscript', () {
    test('accepts the FieldPilot wake phrase and extracts the command', () {
      final result = SpeechBridge.parseTranscript(
        'Hey Field Pilot, identify this valve',
      );

      expect(result.transcript, 'Hey Field Pilot, identify this valve');
      expect(result.command, 'identify this valve');
    });

    test('matches wake phrase case-insensitively', () {
      final result = SpeechBridge.parseTranscript(
        'HEY FIELDPILOT measure this',
      );

      expect(result.command, 'measure this');
    });

    test('rejects speech that does not start the assistant', () {
      expect(
        () => SpeechBridge.parseTranscript('measure this'),
        throwsA(isA<SpeechException>()),
      );
    });
  });
}
