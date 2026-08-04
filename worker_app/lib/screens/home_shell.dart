import 'package:flutter/material.dart';

import 'alerts_tab.dart';
import 'ask_tab.dart';
import 'report_tab.dart';
import 'zone_tab.dart';

/// The signed-in worker's four pages behind a bottom navigation bar, per the requested layout.
///
/// Each tab is its own `Scaffold` with its own `AppBar` (a legal and common Flutter pattern —
/// `Navigator`-free tabs each want their own title and actions). That does mean a drawer or
/// action placed on a *shell*-level `Scaffold` would be unreachable from inside a tab, since
/// `Scaffold.of(context)` resolves to the nearest one; the account/sign-out action therefore
/// lives on each tab's own `AppBar` via [AccountAction] rather than on a shell drawer.
class HomeShell extends StatefulWidget {
  const HomeShell({super.key});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _index = 0;

  static const _pages = [AlertsTab(), ZoneTab(), AskTab(), ReportTab()];

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: IndexedStack(index: _index, children: _pages),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (i) => setState(() => _index = i),
        destinations: const [
          NavigationDestination(icon: Icon(Icons.notifications_outlined),
              selectedIcon: Icon(Icons.notifications), label: 'Alerts'),
          NavigationDestination(icon: Icon(Icons.map_outlined),
              selectedIcon: Icon(Icons.map), label: 'Zone'),
          NavigationDestination(icon: Icon(Icons.forum_outlined),
              selectedIcon: Icon(Icons.forum), label: 'Ask'),
          NavigationDestination(icon: Icon(Icons.campaign_outlined),
              selectedIcon: Icon(Icons.campaign), label: 'Report'),
        ],
      ),
    );
  }
}
