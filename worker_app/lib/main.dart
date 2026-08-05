import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import 'core/live_feed.dart';
import 'core/session.dart';
import 'core/voice.dart';
import 'screens/home_shell.dart';
import 'screens/login_screen.dart';

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
      ],
      child: MaterialApp(
        title: 'FieldPilot Worker',
        debugShowCheckedModeBanner: false,
        theme: ThemeData(
          colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFF2563EB)),
          useMaterial3: true,
        ),
        darkTheme: ThemeData(
          colorScheme: ColorScheme.fromSeed(
            seedColor: const Color(0xFF2563EB), brightness: Brightness.dark),
          useMaterial3: true,
        ),
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

/// Owns the live socket and the speech wiring for the whole signed-in session.
///
/// This lives above [HomeShell] rather than inside a tab on purpose: a hazard must be spoken
/// whichever tab the worker happens to have open — or none, with the phone in their pocket, which
/// is the actual intended posture. Wiring speech into the Alerts tab would mean an alert is only
/// heard by a worker already staring at the alerts list.
class _SignedIn extends StatefulWidget {
  const _SignedIn();

  @override
  State<_SignedIn> createState() => _SignedInState();
}

class _SignedInState extends State<_SignedIn> {
  Session? _session;
  LiveFeed? _feed;
  Voice? _voice;

  @override
  void initState() {
    super.initState();
    // Deferred to the first frame: `context.read` is not legal during initState, and the socket
    // needs the session the router above has already confirmed is signed in.
    WidgetsBinding.instance.addPostFrameCallback((_) => _wire());
  }

  void _wire() {
    if (!mounted) return;
    final session = context.read<Session>();
    final voice = context.read<Voice>();
    final feed = context.read<LiveFeed>();
    _session = session;
    _feed = feed;
    _voice = voice;

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

    // Listening rather than re-running this from `build` keeps socket management out of the render
    // path: the socket is re-scoped when the session actually changes, not on every rebuild.
    session.addListener(_syncSocket);
    _syncSocket();
  }

  /// Re-points the socket when the worker checks into a different zone, so zone-scoped advisories
  /// follow them around the site. `connect` is idempotent when nothing changed.
  void _syncSocket() {
    final session = _session;
    final feed = _feed;
    if (session == null || feed == null || !session.isSignedIn) return;
    feed.connect(
      baseUrl: session.serverUrl,
      workerId: session.user?.workerId,
      zone: session.currentZoneId,
    );
  }

  @override
  void dispose() {
    _session?.removeListener(_syncSocket);
    // Signing out tears the socket down and stops any sentence mid-flight. The dedup memory is
    // cleared too: the next worker to use this phone must not be silently denied an alert that was
    // already spoken to their predecessor.
    _feed?.onFrame = null;
    _feed?.disconnect();
    _voice?.stop();
    _voice?.reset();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => const HomeShell();
}
