import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import 'core/camera_stream.dart';
import 'core/live_feed.dart';
import 'core/session.dart';
import 'core/voice.dart';
import 'screens/home_shell.dart';
import 'screens/login_screen.dart';

/// The one seed colour both themes are built from, so light and dark can never quietly drift
/// apart into two hand-tuned palettes — see [_buildTheme].
const _seedColor = Color(0xFF2563EB);

/// Builds a theme for [brightness] from the same seed and the same component overrides, so
/// "light" and "dark" are one definition evaluated twice rather than two `ThemeData` literals a
/// future edit could update in only one place. Keeping this here, instead of `Theme.of(context)`
/// overrides scattered through individual screens, is what makes "does this look right in light
/// mode" a question with one answer instead of one per screen.
ThemeData _buildTheme(Brightness brightness) {
  return ThemeData(
    colorScheme: ColorScheme.fromSeed(seedColor: _seedColor, brightness: brightness),
    useMaterial3: true,
  );
}

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  // This is a one-handed field tool, checked in a bottom nav bar — portrait-only avoids
  // redesigning every screen for a landscape layout nobody asked for.
  await SystemChrome.setPreferredOrientations([
    DeviceOrientation.portraitUp,
    DeviceOrientation.portraitDown,
  ]);
  runApp(const FieldPilotWorkerApp());
}

class FieldPilotWorkerApp extends StatelessWidget {
  const FieldPilotWorkerApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => Session()..restore()),
        ChangeNotifierProvider(create: (_) => Voice()..init()),
        ChangeNotifierProvider(create: (_) => LiveFeed()),
        // Provided app-wide, above `_SignedIn`, rather than owned by `CameraTab`'s `State`: the
        // camera now starts the moment a session exists (see `_SignedIn` below), which means
        // something above every tab has to be the one holding it, not the tab that used to be the
        // only place a worker could switch it on.
        ChangeNotifierProvider(create: (_) => CameraStreamer()),
      ],
      child: MaterialApp(
        title: 'FieldPilot Worker',
        debugShowCheckedModeBanner: false,
        theme: _buildTheme(Brightness.light),
        darkTheme: _buildTheme(Brightness.dark),
        // Explicit, though it is also `MaterialApp`'s own default: a worker's phone is set up
        // however they like it, and a safety tool that is unreadable because it silently forced
        // light mode on a phone set to dark (or vice versa) is not a hypothetical worth risking.
        themeMode: ThemeMode.system,
        home: const _RootRouter(),
      ),
    );
  }
}

/// Routes on the session's own state rather than a named-route table — with just two states
/// (signed in / not) a `Consumer` is clearer than wiring up `onGenerateRoute`.
class _RootRouter extends StatelessWidget {
  const _RootRouter();

  @override
  Widget build(BuildContext context) {
    return Consumer<Session>(
      builder: (context, session, _) {
        if (session.loading) {
          return const Scaffold(body: Center(child: CircularProgressIndicator()));
        }
        return session.isSignedIn ? const _SignedIn() : const LoginScreen();
      },
    );
  }
}

/// Owns the live socket, the speech wiring and the camera for the whole signed-in session.
///
/// This lives above [HomeShell] rather than inside a tab on purpose: a hazard must be spoken —
/// and now, a feed must be streaming — whichever tab the worker happens to have open, or none,
/// with the phone in their pocket, which is the actual intended posture. Wiring either into a tab
/// would mean it only runs while a worker is staring at that specific tab.
///
/// [WidgetsBindingObserver] is what makes the camera safe to leave running unattended: Android
/// can revoke a backgrounded app's camera access at any time, so [_camera] is torn down on the
/// way to the background and reopened on the way back, rather than left open for the OS to yank
/// out from under a live [CameraController] — the classic crash this pattern exists to avoid.
class _SignedIn extends StatefulWidget {
  const _SignedIn();

  @override
  State<_SignedIn> createState() => _SignedInState();
}

class _SignedInState extends State<_SignedIn> with WidgetsBindingObserver {
  Session? _session;
  LiveFeed? _feed;
  Voice? _voice;
  CameraStreamer? _camera;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    // Deferred to the first frame: `context.read` is not legal during initState, and the socket
    // needs the session the router above has already confirmed is signed in.
    WidgetsBinding.instance.addPostFrameCallback((_) => _wire());
  }

  void _wire() {
    if (!mounted) return;
    final session = context.read<Session>();
    final voice = context.read<Voice>();
    final feed = context.read<LiveFeed>();
    final camera = context.read<CameraStreamer>();
    _session = session;
    _feed = feed;
    _voice = voice;
    _camera = camera;

    feed.onFrame = (frame) {
      // Only `alert` and `advisory` carry a spoken sentence, and the backend authors it for this
      // audience (second person for the worker's own hazard). The app deliberately does not
      // compose its own wording — two phrasings of one alert would drift apart.
      if (frame.topic != 'alert' && frame.topic != 'advisory') return;
      voice.announce(
        '${frame.topic}:${frame.alertId ?? frame.ts}',
        frame.speech,
        frame.severity,
      );
    };

    // Listening rather than re-running this from `build` keeps socket/camera management out of
    // the render path: both are re-scoped when the session actually changes, not on every rebuild.
    session.addListener(_syncSocket);
    session.addListener(_syncCamera);
    _syncSocket();
    _syncCamera();
  }

  /// Re-points the socket when the worker checks into a different zone, so zone-scoped advisories
  /// follow them around the site. `connect` is idempotent when nothing changed.
  void _syncSocket() {
    final session = _session;
    final feed = _feed;
    if (session == null || feed == null || !session.isSignedIn) return;
    feed.connect(
      baseUrl: session.serverUrl,
      token: session.token,
      workerId: session.user?.workerId,
      zone: session.currentZoneId,
    );
  }

  /// Starts the camera the instant a session exists — this is the whole point of C1: the worker
  /// should never have to open the Camera tab and press a button for the feed to begin. `start` is
  /// a no-op once already streaming, so calling this from every session change (not just sign-in)
  /// costs nothing.
  void _syncCamera() {
    final session = _session;
    final camera = _camera;
    if (session == null || camera == null || !session.isSignedIn) return;
    camera.start(
      edgeUrl: session.edgeUrl,
      workerId: session.user?.workerId ?? '',
      zone: session.currentZoneId,
      displayName: session.user?.displayName,
    );
  }

  /// Pauses the camera on the way to the background and, if the worker had not deliberately
  /// stopped it themselves, reopens it on the way back. See the class doc for why this cannot be
  /// skipped: `paused`/`inactive`/`hidden` are all treated as "not in front of the worker right
  /// now" rather than trying to special-case exactly which of them Android will revoke access on.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    final camera = _camera;
    final session = _session;
    if (camera == null || session == null) return;
    if (state == AppLifecycleState.resumed) {
      camera.resumeFromBackground(
        edgeUrl: session.edgeUrl,
        workerId: session.user?.workerId ?? '',
        zone: session.currentZoneId,
        displayName: session.user?.displayName,
      );
    } else {
      camera.pauseForBackground();
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _session?.removeListener(_syncSocket);
    _session?.removeListener(_syncCamera);
    // Signing out tears the socket down and stops any sentence mid-flight. The dedup memory is
    // cleared too: the next worker to use this phone must not be silently denied an alert that was
    // already spoken to their predecessor.
    _feed?.onFrame = null;
    _feed?.disconnect();
    _voice?.stop();
    _voice?.reset();
    // Stop and release the camera on sign-out — it must not keep streaming a feed labelled with a
    // worker id who is no longer signed in. `reset()` on top clears the frame/hazard counters so
    // the next worker to sign in on this phone does not inherit their predecessor's numbers; see
    // the reasoning on `CameraStreamer.reset`.
    _camera?.stop();
    _camera?.reset();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => const HomeShell();
}
