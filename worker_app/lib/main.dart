import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import 'core/session.dart';
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
    return ChangeNotifierProvider(
      create: (_) => Session()..restore(),
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
        return session.isSignedIn ? const HomeShell() : const LoginScreen();
      },
    );
  }
}
