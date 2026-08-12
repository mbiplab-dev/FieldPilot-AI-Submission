import 'package:flutter/services.dart';

/// One-shot Android speech recognition armed by the worker.
///
/// This deliberately does not leave the microphone running in the background. The worker taps
/// the large beacon, says "Hey FieldPilot" and a command in one utterance, and Android returns the
/// transcript. A production always-on wake word should use a dedicated low-power KWS model.
class SpeechBridge {
  static const _channel = MethodChannel('fieldpilot/speech');
  static const wakePhrase = 'hey fieldpilot';

  Future<bool> available() async =>
      await _channel.invokeMethod<bool>('available') ?? false;

  Future<SpeechCommand> listen() async {
    final raw = await _channel.invokeMapMethod<String, dynamic>('listenOnce');
    final transcript = (raw?['transcript'] as String? ?? '').trim();
    return parseTranscript(transcript);
  }

  /// Validate the wake phrase and return only the command that follows it.
  ///
  /// Kept separate from the Android channel so wake-word behavior is unit-testable and identical
  /// whether Android's on-device or system speech recognizer produced the transcript.
  static SpeechCommand parseTranscript(String rawTranscript) {
    final transcript = rawTranscript.trim();
    if (transcript.isEmpty) {
      throw const SpeechException('No speech was understood.');
    }

    final wake = RegExp(
      r'hey\s+field\s*pilot',
      caseSensitive: false,
    ).firstMatch(transcript);
    if (wake == null) {
      throw SpeechException(
        'Wake phrase not heard. Start with “Hey FieldPilot”. Heard: $transcript',
      );
    }
    final command = transcript
        .substring(wake.end)
        .replaceFirst(RegExp(r'^[\s,.:;-]+'), '')
        .trim();
    if (command.isEmpty) {
      throw const SpeechException(
        'Wake phrase heard, but no command followed it.',
      );
    }
    return SpeechCommand(transcript: transcript, command: command);
  }

  Future<void> cancel() => _channel.invokeMethod<void>('cancel');
}

class SpeechCommand {
  final String transcript;
  final String command;
  const SpeechCommand({required this.transcript, required this.command});
}

class SpeechException implements Exception {
  final String message;
  const SpeechException(this.message);
  @override
  String toString() => message;
}
