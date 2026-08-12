import 'package:flutter/foundation.dart';
import 'package:flutter_tts/flutter_tts.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Spoken hazard alerts on the worker's phone.
///
/// This is the product's headline promise made real: the worker keeps their hands on the tools and
/// *hears* the verdict — "Stop work. Put on your hard hat." — instead of reading it. The phone is
/// the right machine to synthesise on. The backend also has a `alerts/tts.py`, but that renders
/// onto the *server's* speakers, which nobody on a site is standing next to; and on a host without
/// espeak-ng it cannot speak at all. So the backend ships the *sentence* (`data.speech`, written by
/// `fieldpilot/alerts/speech.py` in the second person for this audience) and the device speaks it.
///
/// Synthesising on-device also means speech keeps working with no connectivity and no API key,
/// which matters on a site with dead zones.
///
/// Policy notes, mirroring `frontend/src/lib/speech.ts` so both clients behave the same way:
///   * Voice defaults **on** here, unlike the dashboard. A worker wearing this in their pocket is
///     the intended listener; a manager in a shared office is not.
///   * Announcements dedup by key, because an alert arrives over the socket and again on the next
///     poll refresh.
///   * critical/high interrupt whatever is being spoken — that is the "stop work" case. medium/low
///     are dropped while something is already speaking rather than queued behind it, because a
///     phone reciting a stale backlog while a worker is mid-task is worse than silence.
class Voice extends ChangeNotifier {
  static const _prefKey = 'fieldpilot.voice';

  /// Cap on remembered keys so a long shift cannot grow this without bound.
  static const _seenLimit = 300;

  final FlutterTts _tts = FlutterTts();

  /// Insertion-ordered, so trimming removes the oldest keys first.
  final _spoken = <String>{};

  bool _enabled = true;
  bool _ready = false;
  bool _speaking = false;

  /// The last sentence spoken, surfaced so the UI can show what the worker just heard — useful
  /// when a jackhammer drowns it out, and the only way to tell "spoke" from "silently failed".
  String? lastSpoken;

  bool get enabled => _enabled;
  bool get ready => _ready;
  bool get speaking => _speaking;

  /// Loads the preference and configures the engine. Safe to call once at startup.
  Future<void> init() async {
    final prefs = await SharedPreferences.getInstance();
    _enabled = prefs.getBool(_prefKey) ?? true;

    try {
      await _tts.setLanguage('en-US');
      await _tts.setSpeechRate(
        0.5,
      ); // flutter_tts uses 0..1 on Android; 0.5 is normal pace
      await _tts.setVolume(1.0);
      await _tts.setPitch(1.0);
      // Without this, `speak()` returns before the utterance finishes and `awaitSpeakCompletion`
      // state tracking would be wrong.
      await _tts.awaitSpeakCompletion(true);
      _ready = true;
    } catch (e) {
      // A device with no TTS engine installed must not break the app — alerts are still shown on
      // screen and the UI reports voice as unavailable rather than pretending it works.
      _ready = false;
      debugPrint('TTS unavailable: $e');
    }
    notifyListeners();
  }

  Future<void> setEnabled(bool value) async {
    if (_enabled == value) return;
    _enabled = value;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(_prefKey, value);
    if (!value) await stop();
    notifyListeners();
    if (value) await _say('Voice alerts on.', interrupt: true);
  }

  Future<void> stop() async {
    try {
      await _tts.stop();
    } catch (_) {
      // Nothing to stop, or the engine went away — not worth surfacing.
    }
    _speaking = false;
  }

  /// Speak a sample so the worker can confirm they will actually hear an alert.
  Future<void> speakSample() => _say(
    'Stop work. Rebar spacing is 40 millimetres above spec.',
    interrupt: true,
  );

  /// Speak a worker-invoked assistant response. Unlike ambient medium/low alerts this is never
  /// queued: the worker asked for it and expects the next sentence to be the answer.
  Future<void> speakAssistant(String text) async {
    if (!_enabled || !_ready || text.trim().isEmpty) return;
    await _say(text.trim(), interrupt: true);
  }

  /// Announce an alert once.
  ///
  /// Returns what happened so callers (and the UI) can tell a deliberate drop from a silent
  /// failure. See the class doc for the priority policy.
  Future<VoiceOutcome> announce(
    String key,
    String? sentence,
    String? severity,
  ) async {
    final text = (sentence ?? '').trim();
    // Checked before the dedup key is recorded, so an empty payload does not burn the key and
    // block the real sentence if it arrives later.
    if (text.isEmpty) return VoiceOutcome.dropped;
    if (!_enabled || !_ready) return VoiceOutcome.disabled;
    if (_spoken.contains(key)) return VoiceOutcome.duplicate;

    _spoken.add(key);
    if (_spoken.length > _seenLimit) {
      _spoken.remove(_spoken.first);
    }

    final urgent = severity == 'critical' || severity == 'high';
    if (!urgent && _speaking) return VoiceOutcome.dropped;

    await _say(text, interrupt: urgent);
    return VoiceOutcome.spoken;
  }

  Future<void> _say(String text, {required bool interrupt}) async {
    if (!_ready) return;
    if (interrupt) await stop();
    _speaking = true;
    lastSpoken = text;
    notifyListeners();
    try {
      // Resolves when the utterance completes, because of `awaitSpeakCompletion(true)` above.
      await _tts.speak(text);
    } catch (e) {
      debugPrint('TTS speak failed: $e');
    } finally {
      _speaking = false;
      notifyListeners();
    }
  }

  /// Forget what has been announced — used when a different worker signs in on this phone, so the
  /// new user is not silently denied an alert already spoken to their predecessor.
  void reset() {
    _spoken.clear();
    lastSpoken = null;
  }

  @override
  void dispose() {
    _tts.stop();
    super.dispose();
  }
}

enum VoiceOutcome { spoken, duplicate, dropped, disabled }
