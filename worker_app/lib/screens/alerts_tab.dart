import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

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

  @override
  void initState() {
    super.initState();
    _load();
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
    return Scaffold(
      appBar: AppBar(
        title: const Text('My alerts'),
        actions: [
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
            if (alerts.isEmpty) {
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
            return ListView.separated(
              padding: const EdgeInsets.all(12),
              itemCount: alerts.length,
              separatorBuilder: (_, _) => const SizedBox(height: 8),
              itemBuilder: (context, i) => _AlertCard(alert: alerts[i]),
            );
          },
        ),
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
    final color = severityColor(alert.severity);
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
