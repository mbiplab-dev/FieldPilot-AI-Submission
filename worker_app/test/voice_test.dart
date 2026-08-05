import 'package:fieldpilot_worker/core/voice.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// The announce policy decides whether a worker actually hears a hazard, so it is tested against a
/// mocked TTS engine rather than trusted to read correctly.
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  const channel = MethodChannel('flutter_tts');

  /// Every utterance handed to the platform, in order.
  late List<String> spoken;
  late List<String> calls;

  setUp(() {
    spoken = [];
    calls = [];
    SharedPreferences.setMockInitialValues({});
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
      calls.add(call.method);
      if (call.method == 'speak') {
        spoken.add(call.arguments as String);
      }
      return 1; // flutter_tts treats 1 as success
    });
  });

  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, null);
  });

  Future<Voice> ready() async {
    final v = Voice();
    await v.init();
    expect(v.ready, isTrue, reason: 'the mocked engine should report ready');
    spoken.clear();
    return v;
  }

  test('voice defaults on, because the worker is the intended listener', () async {
    final v = await ready();
    expect(v.enabled, isTrue);
  });

  test('a stored preference of off is honoured', () async {
    SharedPreferences.setMockInitialValues({'fieldpilot.voice': false});
    final v = Voice();
    await v.init();
    expect(v.enabled, isFalse);
  });

  test('speaks a fresh alert and records what was said', () async {
    final v = await ready();
    final outcome = await v.announce('alert:al-1', 'Stop work. Put on your hard hat.', 'high');

    expect(outcome, VoiceOutcome.spoken);
    expect(spoken, ['Stop work. Put on your hard hat.']);
    expect(v.lastSpoken, 'Stop work. Put on your hard hat.');
  });

  test('the same alert is never announced twice', () async {
    final v = await ready();
    await v.announce('alert:al-1', 'Stop work.', 'high');
    spoken.clear();

    final again = await v.announce('alert:al-1', 'Stop work.', 'high');
    expect(again, VoiceOutcome.duplicate);
    expect(spoken, isEmpty);
  });

  test('an alert and a later advisory about it are separate announcements', () async {
    final v = await ready();
    expect(await v.announce('alert:al-1', 'Stop work.', 'high'), VoiceOutcome.spoken);
    expect(
      await v.announce('advisory:al-1', 'Heads up. In your zone.', 'low'),
      VoiceOutcome.spoken,
    );
  });

  test('an empty sentence is dropped without burning the dedup key', () async {
    final v = await ready();
    expect(await v.announce('alert:al-2', '   ', 'high'), VoiceOutcome.dropped);
    expect(await v.announce('alert:al-2', null, 'high'), VoiceOutcome.dropped);
    expect(spoken, isEmpty);

    // The real sentence arriving later must still be spoken.
    expect(
      await v.announce('alert:al-2', 'Stop work. Fire detected.', 'critical'),
      VoiceOutcome.spoken,
    );
    expect(spoken, ['Stop work. Fire detected.']);
  });

  test('nothing is spoken while voice is off', () async {
    final v = await ready();
    await v.setEnabled(false);
    spoken.clear();

    expect(await v.announce('alert:al-3', 'Stop work.', 'critical'), VoiceOutcome.disabled);
    expect(spoken, isEmpty);
  });

  test('turning voice on confirms itself out loud', () async {
    final v = await ready();
    await v.setEnabled(false);
    spoken.clear();
    await v.setEnabled(true);
    // Otherwise the worker has no way to know the toggle did anything.
    expect(spoken, ['Voice alerts on.']);
  });

  test('turning voice off stops whatever is mid-sentence', () async {
    final v = await ready();
    calls.clear();
    await v.setEnabled(false);
    expect(calls, contains('stop'));
  });

  test('an urgent alert interrupts, a routine one does not', () async {
    final v = await ready();
    calls.clear();
    await v.announce('alert:c1', 'Stop work. Fire detected.', 'critical');
    expect(calls.where((c) => c == 'stop'), isNotEmpty,
        reason: 'critical must cancel whatever is being said');

    calls.clear();
    await v.announce('alert:m1', 'Note. Minor thing.', 'medium');
    expect(calls.where((c) => c == 'stop'), isEmpty,
        reason: 'a routine alert must not cut off the current sentence');
  });

  test('after reset a previously announced alert can be spoken again', () async {
    final v = await ready();
    await v.announce('alert:al-1', 'Stop work.', 'high');
    expect(await v.announce('alert:al-1', 'Stop work.', 'high'), VoiceOutcome.duplicate);

    // A different worker signing in on this phone must not inherit the memory.
    v.reset();
    expect(await v.announce('alert:al-1', 'Stop work.', 'high'), VoiceOutcome.spoken);
    expect(v.lastSpoken, isNotNull);
  });

  test('a device with no speech engine reports unavailable instead of pretending', () async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
      throw PlatformException(code: 'no_engine', message: 'no TTS engine');
    });

    final v = Voice();
    await v.init();

    expect(v.ready, isFalse);
    // Alerts are still shown on screen; the UI shows voice as unavailable.
    expect(await v.announce('alert:al-1', 'Stop work.', 'critical'), VoiceOutcome.disabled);
  });

  test('the sample line is the one from the product pitch', () async {
    final v = await ready();
    await v.speakSample();
    expect(spoken.single, 'Stop work. Rebar spacing is 40 millimetres above spec.');
  });
}
