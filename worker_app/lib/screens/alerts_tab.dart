import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/live_feed.dart';
import '../core/models.dart';
import '../core/session.dart';
import '../widgets/common.dart';

/// "My alerts" — everything hazard detection has raised against this worker, newest first.
/// Scoped server-side by `/me/alerts`; there is no client-side filtering to get wrong.
class AlertsTab extends StatefulWidget {
  const AlertsTab({super.key});

  @override
  State<AlertsTab> createState() => _AlertsTabState();
}

class _AlertsTabState extends State<AlertsTab> {
  late Future<List<Alert>> _future;
  LiveFeed? _feed;
  double _lastHandledTs = 0;

  @override
  void initState() {
    super.initState();
    _load();
    // Deferred: `context.read` is not legal during initState.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final feed = context.read<LiveFeed>();
      _feed = feed;
      feed.addListener(_onLiveFrame);
    });
  }

  /// A pushed alert about *this* worker means `/me/alerts` has changed — refetch rather than trying
  /// to splice the socket payload into the list, so the server stays the single source of truth.
  void _onLiveFrame() {
    final last = _feed?.last;
    if (last == null || last.topic != 'alert') return;
    if (last.ts <= _lastHandledTs) return;
    _lastHandledTs = last.ts;
    if (mounted) setState(_load);
  }

  @override
  void dispose() {
    _feed?.removeListener(_onLiveFrame);
    super.dispose();
  }

  void _load() {
    // Captured once, synchronously, so the `catchError` closure — which runs later, after the
    // widget may have been disposed — never re-reads `context`.
    final session = context.read<Session>();
    _future = session.api.myAlerts().catchError((e) {
      if (e is ApiException && e.isAuthFailure) session.forceSignOut();
      throw e;
    });
  }

  Future<void> _refresh() async {
    setState(_load);
    await _future;
  }

  @override
  Widget build(BuildContext context) {
    final user = context.watch<Session>().user;
    // Advisories are pushed, not stored against this worker, so `/me/alerts` will never contain
    // them. Without this the worker would *hear* "a worker is missing a hard hat in your zone" and
    // find nothing on screen explaining it.
    final advisories = context
        .watch<LiveFeed>()
        .frames
        .where((f) => f.topic == 'advisory')
        .take(3)
        .toList();

    return Scaffold(
      appBar: AppBar(
        title: const Text('My alerts'),
        actions: [
          const LiveDot(),
          const SizedBox(width: 6),
          const VoiceAction(),
          IconButton(icon: const Icon(Icons.refresh), onPressed: _refresh),
          const AccountAction(),
          const SizedBox(width: 4),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _refresh,
        child: FutureBuilder<List<Alert>>(
          future: _future,
          builder: (context, snapshot) {
            if (snapshot.connectionState == ConnectionState.waiting) {
              return const Center(child: CircularProgressIndicator());
            }
            if (snapshot.hasError) {
              return ListView(children: [
                const SizedBox(height: 80),
                ErrorBanner(message: friendlyError(snapshot.error!), onRetry: _refresh),
              ]);
            }
            final alerts = snapshot.data ?? const [];

            if (alerts.isEmpty && advisories.isEmpty) {
              return ListView(children: [
                const SizedBox(height: 80),
                EmptyState(
                  icon: Icons.verified_outlined,
                  title: 'No alerts',
                  subtitle: user != null
                      ? 'Nothing has been raised against ${user.displayName} yet.'
                      : null,
                ),
              ]);
            }

            return ListView(
              padding: const EdgeInsets.all(12),
              children: [
                if (advisories.isNotEmpty) ...[
                  _SectionLabel(
                    icon: Icons.hearing_rounded,
                    label: 'Heard in your zone',
                  ),
                  const SizedBox(height: 8),
                  ...advisories.map((f) => Padding(
                        padding: const EdgeInsets.only(bottom: 8),
                        child: _AdvisoryCard(frame: f),
                      )),
                  const SizedBox(height: 12),
                  if (alerts.isNotEmpty)
                    const _SectionLabel(
                      icon: Icons.notifications_active_outlined,
                      label: 'Raised against you',
                    ),
                  const SizedBox(height: 8),
                ],
                ...alerts.map((a) => Padding(
                      padding: const EdgeInsets.only(bottom: 8),
                      child: _AlertCard(alert: a),
                    )),
              ],
            );
          },
        ),
      ),
    );
  }
}

class _SectionLabel extends StatelessWidget {
  final IconData icon;
  final String label;
  const _SectionLabel({required this.icon, required this.label});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Row(
      children: [
        Icon(icon, size: 15, color: theme.colorScheme.onSurfaceVariant),
        const SizedBox(width: 6),
        Text(
          label.toUpperCase(),
          style: theme.textTheme.labelSmall?.copyWith(
            color: theme.colorScheme.onSurfaceVariant,
            fontWeight: FontWeight.w700,
            letterSpacing: 0.6,
          ),
        ),
      ],
    );
  }
}

/// A zone advisory about a colleague's hazard — shows the exact sentence that was spoken, so a
/// worker who missed it over site noise can read it back.
class _AdvisoryCard extends StatelessWidget {
  final LiveFrame frame;
  const _AdvisoryCard({required this.frame});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final text = frame.speech ?? frame.message ?? 'Advisory in your zone.';
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: theme.colorScheme.secondaryContainer.withValues(alpha: 0.5),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: theme.colorScheme.outlineVariant),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.campaign_outlined, size: 18, color: theme.colorScheme.onSecondaryContainer),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(text, style: theme.textTheme.bodyMedium),
                const SizedBox(height: 3),
                Text(
                  timeAgo(frame.ts),
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _AlertCard extends StatelessWidget {
  final Alert alert;
  const _AlertCard({required this.alert});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final color = severityColor(context, alert.severity);
    return Card(
      margin: EdgeInsets.zero,
      clipBehavior: Clip.antiAlias,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(color: color.withValues(alpha: 0.25)),
      ),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (alert.imageUrl != null) ...[
              ClipRRect(
                borderRadius: BorderRadius.circular(8),
                child: Image.network(
                  context.read<Session>().api.mediaUrl(alert.imageUrl!),
                  width: 64,
                  height: 64,
                  fit: BoxFit.cover,
                  errorBuilder: (_, _, _) => Container(
                    width: 64,
                    height: 64,
                    color: theme.colorScheme.surfaceContainerHighest,
                    child: const Icon(Icons.image_not_supported_outlined, size: 20),
                  ),
                ),
              ),
              const SizedBox(width: 12),
            ],
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      SeverityChip(severity: alert.severity),
                      const SizedBox(width: 6),
                      _StateChip(state: alert.state),
                      const Spacer(),
                      Text(timeAgo(alert.lastSeen),
                          style: theme.textTheme.bodySmall
                              ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
                    ],
                  ),
                  const SizedBox(height: 6),
                  Text(
                    alert.message?.isNotEmpty == true
                        ? alert.message!
                        : alert.eventType.replaceAll('_', ' '),
                    style: theme.textTheme.bodyMedium?.copyWith(fontWeight: FontWeight.w600),
                  ),
                  if (alert.zone != null) ...[
                    const SizedBox(height: 4),
                    Row(
                      children: [
                        Icon(Icons.place_outlined, size: 14, color: theme.colorScheme.outline),
                        const SizedBox(width: 2),
                        Text(alert.zone!, style: theme.textTheme.bodySmall),
                        if (alert.hitCount > 1) ...[
                          const SizedBox(width: 8),
                          Text('×${alert.hitCount}',
                              style: theme.textTheme.bodySmall
                                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
                        ],
                      ],
                    ),
                  ],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _StateChip extends StatelessWidget {
  final String state;
  const _StateChip({required this.state});

  @override
  Widget build(BuildContext context) {
    final resolved = state == 'RESOLVED' || state == 'SUPPRESSED';
    final theme = Theme.of(context);
    final color = resolved ? theme.colorScheme.outline : theme.colorScheme.primary;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Text(state, style: TextStyle(color: color, fontSize: 10, fontWeight: FontWeight.w600)),
    );
  }
}
