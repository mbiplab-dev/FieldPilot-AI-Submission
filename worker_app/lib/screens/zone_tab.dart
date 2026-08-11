import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/models.dart';
import '../core/session.dart';
import '../widgets/common.dart';

/// Zone check-in / check-out. A worker is in at most one zone at a time — moving to a new one
/// closes the previous check-in automatically, which the backend reports and this screen
/// surfaces rather than hiding.
class ZoneTab extends StatefulWidget {
  const ZoneTab({super.key});

  @override
  State<ZoneTab> createState() => _ZoneTabState();
}

class _ZoneTabState extends State<ZoneTab> {
  ZoneOccupancy? _current;
  List<ZoneInfo> _zones = const [];
  bool _loading = true;
  String? _error;
  String? _busyZoneId;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    // Captured once, before the `await` — `Session` is a stable object handed to us by
    // Provider, so calling methods on it later needs no `context`. Reading `context` again
    // *after* an async gap would risk "looking up a deactivated widget's ancestor" if the
    // user has navigated away in the meantime.
    final session = context.read<Session>();
    try {
      final results = await Future.wait([session.api.myZone(), session.api.zones()]);
      final current = results[0] as ZoneOccupancy?;
      // Publish the zone to the session even if this widget is gone: the live socket is scoped by
      // it, so a check-in that updated the server but not the session would leave the worker
      // hearing another zone's advisories.
      session.setCurrentZone(current?.zoneId);
      if (!mounted) return;
      setState(() {
        _current = current;
        _zones = results[1] as List<ZoneInfo>;
        _loading = false;
      });
    } catch (e) {
      if (e is ApiException && e.isAuthFailure) {
        await session.forceSignOut();
        return;
      }
      if (!mounted) return;
      setState(() {
        _error = friendlyError(e);
        _loading = false;
      });
    }
  }

  Future<void> _enter(ZoneInfo zone) async {
    setState(() => _busyZoneId = zone.zoneId);
    final api = context.read<Session>().api;
    try {
      final (_, closedZoneId) = await api.enterZone(zone.zoneId);
      if (!mounted) return;
      await _load();
      if (!mounted) return;
      final message = closedZoneId != null
          ? 'Checked into ${zone.name} (left $closedZoneId)'
          : 'Checked into ${zone.name}';
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(friendlyError(e))));
    } finally {
      if (mounted) setState(() => _busyZoneId = null);
    }
  }

  Future<void> _leave() async {
    final current = _current;
    if (current == null) return;
    setState(() => _busyZoneId = current.zoneId);
    final api = context.read<Session>().api;
    try {
      await api.leaveZone(current.zoneId);
      await _load();
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('Left ${current.zoneName ?? current.zoneId}')));
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(friendlyError(e))));
    } finally {
      if (mounted) setState(() => _busyZoneId = null);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Zone check-in'),
        actions: const [VoiceAction(), AccountAction(), SizedBox(width: 4)],
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: _buildBody(),
      ),
    );
  }

  Widget _buildBody() {
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_error != null) {
      return ListView(children: [
        const SizedBox(height: 80),
        ErrorBanner(message: _error!, onRetry: _load),
      ]);
    }
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        _CurrentZoneCard(current: _current, busy: _busyZoneId == _current?.zoneId, onLeave: _leave),
        const SizedBox(height: 20),
        Text('All zones', style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        if (_zones.isEmpty)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 24),
            child: EmptyState(icon: Icons.map_outlined, title: 'No zones configured'),
          )
        else
          ..._zones.map((z) => Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: _ZoneRow(
                  zone: z,
                  isCurrent: _current?.zoneId == z.zoneId,
                  busy: _busyZoneId == z.zoneId,
                  onEnter: () => _enter(z),
                ),
              )),
      ],
    );
  }
}

class _CurrentZoneCard extends StatelessWidget {
  final ZoneOccupancy? current;
  final bool busy;
  final VoidCallback onLeave;
  const _CurrentZoneCard({required this.current, required this.busy, required this.onLeave});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    if (current == null) {
      return Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: theme.colorScheme.surfaceContainerHighest,
          borderRadius: BorderRadius.circular(14),
        ),
        child: Row(
          children: [
            Icon(Icons.location_off_outlined, color: theme.colorScheme.onSurfaceVariant),
            const SizedBox(width: 12),
            Expanded(
              child: Text('Not checked into a zone', style: theme.textTheme.bodyMedium),
            ),
          ],
        ),
      );
    }
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: theme.colorScheme.primaryContainer,
        borderRadius: BorderRadius.circular(14),
      ),
      child: Row(
        children: [
          Icon(Icons.my_location, color: theme.colorScheme.onPrimaryContainer),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Currently in',
                    style: theme.textTheme.bodySmall
                        ?.copyWith(color: theme.colorScheme.onPrimaryContainer)),
                Text(current!.zoneName ?? current!.zoneId,
                    style: theme.textTheme.titleMedium?.copyWith(
                        color: theme.colorScheme.onPrimaryContainer, fontWeight: FontWeight.w700)),
                Text('since ${timeAgo(current!.enteredAt)}',
                    style: theme.textTheme.bodySmall
                        ?.copyWith(color: theme.colorScheme.onPrimaryContainer)),
              ],
            ),
          ),
          FilledButton.tonal(
            onPressed: busy ? null : onLeave,
            child: busy
                ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('Leave'),
          ),
        ],
      ),
    );
  }
}

class _ZoneRow extends StatelessWidget {
  final ZoneInfo zone;
  final bool isCurrent;
  final bool busy;
  final VoidCallback onEnter;
  const _ZoneRow({
    required this.zone,
    required this.isCurrent,
    required this.busy,
    required this.onEnter,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    // Reuse the severity palette's "critical" red rather than the raw Material red this used to
    // hardcode: that literal was fine on this card's light-mode white background but far too dark
    // to read on the same card once it goes near-black in dark mode. `severityColor` already
    // carries a shade tuned for each surface.
    final dangerColor = severityColor(context, 'critical');
    return Card(
      margin: EdgeInsets.zero,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      child: ListTile(
        leading: CircleAvatar(
          backgroundColor: (zone.danger ? dangerColor : theme.colorScheme.primary).withValues(alpha: 0.12),
          child: Icon(
            zone.danger ? Icons.warning_amber_rounded : Icons.place_outlined,
            color: zone.danger ? dangerColor : theme.colorScheme.primary,
          ),
        ),
        title: Text(zone.name),
        subtitle: Row(
          children: [
            HazardLevelChip(level: zone.hazardLevel),
            if (zone.danger) ...[
              const SizedBox(width: 6),
              Text('Danger zone', style: TextStyle(fontSize: 11, color: dangerColor)),
            ],
          ],
        ),
        trailing: isCurrent
            ? const Chip(label: Text('Here'), visualDensity: VisualDensity.compact)
            : FilledButton(
                onPressed: busy ? null : onEnter,
                child: busy
                    ? const SizedBox(
                        width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                    : const Text('Enter'),
              ),
      ),
    );
  }
}
