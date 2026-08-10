import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/live_feed.dart';
import '../core/session.dart';
import 'alerts_tab.dart';
import 'ask_tab.dart';
import 'camera_tab.dart';
import 'messages_tab.dart';
import 'report_tab.dart';
import 'zone_tab.dart';

/// The signed-in worker's six pages behind a bottom navigation bar.
///
/// Six is the practical limit for a Material `NavigationBar` before labels start truncating on a
/// narrow phone — this is the point where a seventh tab should become a menu item instead, not
/// another destination.
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
  static const _messagesIndex = 3;

  int _index = 0;
  int _unread = 0;
  LiveFeed? _feed;
  double _lastHandledTs = 0;

  static const _pages = [
    AlertsTab(),
    CameraTab(),
    ZoneTab(),
    MessagesTab(),
    AskTab(),
    ReportTab(),
  ];

  @override
  void initState() {
    super.initState();
    // Deferred: `context.read` is not legal during initState.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final feed = context.read<LiveFeed>();
      _feed = feed;
      feed.addListener(_onLiveFrame);
      _loadUnread();
    });
  }

  @override
  void dispose() {
    _feed?.removeListener(_onLiveFrame);
    super.dispose();
  }

  Future<void> _loadUnread() async {
    final session = context.read<Session>();
    try {
      final n = await session.api.unreadMessages();
      if (mounted) setState(() => _unread = n);
    } catch (_) {
      // A stale badge is a cosmetic problem, not one worth an error banner over the whole shell.
    }
  }

  void _onLiveFrame() {
    final last = _feed?.last;
    if (last == null || last.topic != 'message') return;
    if (last.ts <= _lastHandledTs) return;
    _lastHandledTs = last.ts;
    _loadUnread();
  }

  void _select(int i) {
    setState(() {
      _index = i;
      // Optimistic: opening the tab marks the thread read server-side (see
      // `MessagesTab._markThreadRead`), so the badge would only be lit again by the next
      // `_loadUnread` — clearing it here avoids a flash of a stale count in between.
      if (i == _messagesIndex) _unread = 0;
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: IndexedStack(index: _index, children: _pages),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: _select,
        destinations: [
          const NavigationDestination(icon: Icon(Icons.notifications_outlined),
              selectedIcon: Icon(Icons.notifications), label: 'Alerts'),
          const NavigationDestination(icon: Icon(Icons.videocam_outlined),
              selectedIcon: Icon(Icons.videocam), label: 'Camera'),
          const NavigationDestination(icon: Icon(Icons.map_outlined),
              selectedIcon: Icon(Icons.map), label: 'Zone'),
          NavigationDestination(
            icon: _MessagesIcon(unread: _unread, icon: Icons.chat_bubble_outline),
            selectedIcon: _MessagesIcon(unread: _unread, icon: Icons.chat_bubble),
            label: 'Messages',
          ),
          const NavigationDestination(icon: Icon(Icons.forum_outlined),
              selectedIcon: Icon(Icons.forum), label: 'Ask'),
          const NavigationDestination(icon: Icon(Icons.campaign_outlined),
              selectedIcon: Icon(Icons.campaign), label: 'Report'),
        ],
      ),
    );
  }
}

class _MessagesIcon extends StatelessWidget {
  final int unread;
  final IconData icon;
  const _MessagesIcon({required this.unread, required this.icon});

  @override
  Widget build(BuildContext context) {
    final child = Icon(icon);
    if (unread <= 0) return child;
    return Badge(label: Text(unread > 9 ? '9+' : '$unread'), child: child);
  }
}
