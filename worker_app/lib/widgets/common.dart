import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/live_feed.dart';
import '../core/models.dart';
import '../core/session.dart';
import '../core/voice.dart';

/// The signed-in worker's account menu, meant for an `AppBar.actions` list on every tab — see
/// the note on [HomeShell] for why this lives per-tab rather than on a shared shell drawer.
class AccountAction extends StatelessWidget {
  const AccountAction({super.key});

  @override
  Widget build(BuildContext context) {
    final user = context.watch<Session>().user;
    return PopupMenuButton<String>(
      icon: CircleAvatar(
        radius: 15,
        child: Text(
          (user?.displayName.trim().isNotEmpty == true ? user!.displayName[0] : '?').toUpperCase(),
          style: const TextStyle(fontSize: 13),
        ),
      ),
      itemBuilder: (context) => [
        PopupMenuItem<String>(
          enabled: false,
          child: Text(user?.displayName ?? '', style: const TextStyle(fontWeight: FontWeight.w600)),
        ),
        const PopupMenuDivider(),
        const PopupMenuItem<String>(
          value: 'logout',
          child: Row(children: [Icon(Icons.logout, size: 18), SizedBox(width: 8), Text('Sign out')]),
        ),
      ],
      onSelected: (value) {
        if (value == 'logout') context.read<Session>().logout();
      },
    );
  }
}

/// Voice on/off, in every tab's `AppBar`.
///
/// Prominent rather than buried in a settings page on purpose: whether the worker will *hear* the
/// next hazard is a safety-relevant fact they should be able to check at a glance. Long-press
/// speaks a sample, which is the only way to tell "voice is on" from "voice is on but this phone's
/// media volume is muted".
class VoiceAction extends StatelessWidget {
  const VoiceAction({super.key});

  @override
  Widget build(BuildContext context) {
    final voice = context.watch<Voice>();

    if (!voice.ready) {
      return IconButton(
        icon: const Icon(Icons.voice_over_off_outlined),
        // Reported honestly instead of showing an "on" toggle that cannot speak.
        tooltip: 'No speech engine on this device — alerts are shown but not spoken',
        onPressed: () => ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text('This device has no text-to-speech engine, so alerts cannot be spoken.'),
          ),
        ),
      );
    }

    // `InkWell` rather than `IconButton`, which has no long-press affordance.
    return Tooltip(
      message: voice.enabled
          ? 'Spoken alerts on — long-press to hear a sample'
          : 'Spoken alerts off — tap to turn on',
      child: InkWell(
        onTap: () => voice.setEnabled(!voice.enabled),
        onLongPress: voice.enabled ? voice.speakSample : null,
        customBorder: const CircleBorder(),
        child: Padding(
          padding: const EdgeInsets.all(10),
          child: Icon(voice.enabled ? Icons.volume_up_rounded : Icons.volume_off_rounded),
        ),
      ),
    );
  }
}

/// Live-connection indicator for an `AppBar`.
///
/// Live push is an optimisation, not a requirement — every screen still polls — so this reports
/// the socket's state without dressing a disconnection up as an error.
class LiveDot extends StatelessWidget {
  const LiveDot({super.key});

  @override
  Widget build(BuildContext context) {
    final connected = context.watch<LiveFeed>().connected;
    final color = connected ? const Color(0xFF16A34A) : const Color(0xFF94A3B8);
    return Tooltip(
      message: connected
          ? 'Live — hazards arrive within seconds'
          : 'Not live — still refreshing on a timer',
      child: Row(
        children: [
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(color: color, shape: BoxShape.circle),
          ),
          const SizedBox(width: 5),
          Text(
            connected ? 'Live' : 'Offline',
            style: TextStyle(fontSize: 11, color: color, fontWeight: FontWeight.w600),
          ),
        ],
      ),
    );
  }
}

const _severityColors = {
  'low': Color(0xFF64748B),
  'medium': Color(0xFFD97706),
  'high': Color(0xFFDC2626),
  'critical': Color(0xFF991B1B),
};

Color severityColor(String severity) => _severityColors[severity] ?? _severityColors['medium']!;

class SeverityChip extends StatelessWidget {
  final String severity;
  const SeverityChip({super.key, required this.severity});

  @override
  Widget build(BuildContext context) {
    final color = severityColor(severity);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.14),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: color.withValues(alpha: 0.4)),
      ),
      child: Text(
        severity.toUpperCase(),
        style: TextStyle(color: color, fontWeight: FontWeight.w700, fontSize: 11),
      ),
    );
  }
}

class HazardLevelChip extends StatelessWidget {
  final String level;
  const HazardLevelChip({super.key, required this.level});

  @override
  Widget build(BuildContext context) => SeverityChip(
        severity: level == 'high' ? 'high' : (level == 'low' ? 'low' : 'medium'),
      );
}

/// A full-bleed empty state, used whenever a list has nothing to show — never a blank screen.
class EmptyState extends StatelessWidget {
  final IconData icon;
  final String title;
  final String? subtitle;
  const EmptyState({super.key, required this.icon, required this.title, this.subtitle});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 48, color: theme.colorScheme.outline),
            const SizedBox(height: 12),
            Text(title, style: theme.textTheme.titleMedium, textAlign: TextAlign.center),
            if (subtitle != null) ...[
              const SizedBox(height: 6),
              Text(subtitle!,
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
                  textAlign: TextAlign.center),
            ],
          ],
        ),
      ),
    );
  }
}

/// A dead-backend / network-failure banner with a retry action — every screen's failure path
/// renders this instead of an unhandled exception or a blank screen.
class ErrorBanner extends StatelessWidget {
  final String message;
  final VoidCallback? onRetry;
  const ErrorBanner({super.key, required this.message, this.onRetry});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.cloud_off_rounded, size: 40, color: theme.colorScheme.error),
            const SizedBox(height: 12),
            Text(message, textAlign: TextAlign.center, style: theme.textTheme.bodyMedium),
            if (onRetry != null) ...[
              const SizedBox(height: 16),
              FilledButton.tonal(onPressed: onRetry, child: const Text('Retry')),
            ],
          ],
        ),
      ),
    );
  }
}

String friendlyError(Object error) {
  if (error is ApiException) return error.message;
  return 'Could not reach the server. Check your connection and the server address in Settings.';
}

String timeAgo(double epochSeconds) {
  if (epochSeconds <= 0) return '';
  final dt = DateTime.fromMillisecondsSinceEpoch((epochSeconds * 1000).round());
  final diff = DateTime.now().difference(dt);
  if (diff.inSeconds < 60) return '${diff.inSeconds}s ago';
  if (diff.inMinutes < 60) return '${diff.inMinutes}m ago';
  if (diff.inHours < 24) return '${diff.inHours}h ago';
  return '${diff.inDays}d ago';
}
