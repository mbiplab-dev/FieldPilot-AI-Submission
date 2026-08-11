import 'package:camera/camera.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/camera_stream.dart';
import '../core/session.dart';
import '../widgets/common.dart';

/// The worker's camera, streamed to the server for analysis and to the site manager to watch.
///
/// The streamer itself is owned above [HomeShell] now (see `_SignedIn` in `main.dart`), not by
/// this tab's `State` — it starts the moment a session exists, not when the worker happens to
/// open this tab, so this widget is just a renderer of whatever [CameraStreamer] the app-wide
/// provider hands it plus a control to stop/resume it.
///
/// Auto-starting does not make this any less the worker's camera to control, though: it stays
/// clearly indicated while running (the red "LIVE" badge in [_LiveBadge]) and stays stoppable at
/// a tap (the button in [_Controls] always reads STOP while live). A camera that started itself
/// and gave the person carrying it no way to see or end that would be a surveillance device
/// wearing a safety app's name; what changed here is the default, not that guarantee.
class CameraTab extends StatelessWidget {
  const CameraTab({super.key});

  Future<void> _toggle(BuildContext context) async {
    final streamer = context.read<CameraStreamer>();
    if (streamer.streaming) {
      await streamer.stop();
      return;
    }
    // Read before the `await` inside `start` — the same rule every other screen in this app
    // follows, because `context` is not safe to touch again after an async gap.
    final session = context.read<Session>();
    await streamer.start(
      edgeUrl: session.edgeUrl,
      workerId: session.user?.workerId ?? '',
      zone: session.currentZoneId,
      displayName: session.user?.displayName,
    );
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<CameraStreamer>(
      builder: (context, streamer, _) => Scaffold(
        appBar: AppBar(
          title: const Text('Site camera'),
          actions: const [VoiceAction(), AccountAction(), SizedBox(width: 4)],
        ),
        body: Column(
          children: [
            Expanded(child: _Preview(streamer: streamer)),
            _Controls(streamer: streamer, onToggle: () => _toggle(context)),
          ],
        ),
      ),
    );
  }
}

class _Preview extends StatelessWidget {
  final CameraStreamer streamer;
  const _Preview({required this.streamer});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    if (streamer.starting) {
      return const Center(child: CircularProgressIndicator());
    }

    final controller = streamer.controller;
    if (!streamer.ready || controller == null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(28),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.videocam_off_outlined, size: 52, color: theme.colorScheme.outline),
              const SizedBox(height: 14),
              Text('Camera off', style: theme.textTheme.titleMedium),
              const SizedBox(height: 6),
              Text(
                'Resume the feed to let the site manager see what you see. '
                'All analysis runs on the server — your phone only sends the picture.',
                textAlign: TextAlign.center,
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
              ),
              if (streamer.error != null) ...[
                const SizedBox(height: 16),
                Text(
                  streamer.error!,
                  textAlign: TextAlign.center,
                  style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.error),
                ),
              ],
            ],
          ),
        ),
      );
    }

    return Stack(
      fit: StackFit.expand,
      children: [
        FittedBox(
          fit: BoxFit.cover,
          child: SizedBox(
            width: controller.value.previewSize?.height ?? 480,
            height: controller.value.previewSize?.width ?? 640,
            child: CameraPreview(controller),
          ),
        ),
        // Unmissable while the feed is being watched.
        Positioned(
          top: 12,
          left: 12,
          child: _LiveBadge(connected: streamer.connected),
        ),
        if (streamer.error != null)
          Positioned(
            left: 12,
            right: 12,
            bottom: 12,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              decoration: BoxDecoration(
                color: theme.colorScheme.errorContainer,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Text(
                streamer.error!,
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onErrorContainer),
              ),
            ),
          ),
      ],
    );
  }
}

class _LiveBadge extends StatelessWidget {
  final bool connected;
  const _LiveBadge({required this.connected});

  @override
  Widget build(BuildContext context) {
    // The "live" red has to stay red regardless of app theme — it is the one indicator this
    // whole feature exists to keep visible (see the note on `CameraTab`), not a decorative accent
    // to retune per mode.
    final colour = connected ? const Color(0xFFDC2626) : const Color(0xFF64748B);
    final theme = Theme.of(context);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
      decoration: BoxDecoration(
        // This scrim floats over the live camera preview, not over app chrome, so a plain
        // "black behind white text" was never actually a light/dark bug on its own — but it also
        // never went through the theme, which made it the odd one out. `inverseSurface`/
        // `onInverseSurface` is Material's own pairing for exactly this — content that must read
        // clearly no matter what is behind or around it (it is what `SnackBar` uses) — so this
        // gets the same guarantee for free instead of a second hand-picked pair of literals.
        color: theme.colorScheme.inverseSurface.withValues(alpha: 0.72),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(width: 8, height: 8, decoration: BoxDecoration(color: colour, shape: BoxShape.circle)),
          const SizedBox(width: 6),
          Text(
            connected ? 'LIVE — manager can see this' : 'Connecting…',
            style: TextStyle(
              color: theme.colorScheme.onInverseSurface,
              fontSize: 11,
              fontWeight: FontWeight.w700,
            ),
          ),
        ],
      ),
    );
  }
}

class _Controls extends StatelessWidget {
  final CameraStreamer streamer;
  final VoidCallback onToggle;
  const _Controls({required this.streamer, required this.onToggle});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final on = streamer.streaming;

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(16, 14, 16, 20),
      decoration: BoxDecoration(
        color: theme.colorScheme.surface,
        border: Border(top: BorderSide(color: theme.colorScheme.outlineVariant)),
      ),
      child: Column(
        children: [
          if (on)
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceEvenly,
              children: [
                _Stat(label: 'Sent', value: '${streamer.framesSent}'),
                _Stat(label: 'Rate', value: '${streamer.fps.toStringAsFixed(1)}/s'),
                _Stat(label: 'People', value: '${streamer.peopleSeen}'),
                _Stat(label: 'Hazards', value: '${streamer.hazardsSeen}'),
              ],
            ),
          if (on) const SizedBox(height: 14),
          SizedBox(
            width: double.infinity,
            height: 52,
            child: FilledButton.icon(
              onPressed: streamer.starting ? null : onToggle,
              style: FilledButton.styleFrom(
                backgroundColor: on ? theme.colorScheme.error : theme.colorScheme.primary,
                // Overriding `backgroundColor` alone leaves the label/icon on the button's
                // default foreground, which is picked to contrast with `primary` — wrong the
                // moment the background switches to `error` instead. Pairing each background
                // with its matching `on*` colour is what actually keeps this readable, in either
                // theme, rather than working by coincidence in just one of them.
                foregroundColor: on ? theme.colorScheme.onError : theme.colorScheme.onPrimary,
              ),
              icon: Icon(on ? Icons.stop_rounded : Icons.videocam_rounded),
              label: Text(
                on ? 'STOP STREAMING' : 'RESUME CAMERA FEED',
                style: const TextStyle(fontWeight: FontWeight.w700, letterSpacing: 0.4),
              ),
            ),
          ),
          const SizedBox(height: 8),
          Text(
            on
                ? 'Analysis runs on the server. Detected hazards are spoken to you and raised with your manager.'
                : 'Your camera is off. Nothing is sent until you resume it.',
            textAlign: TextAlign.center,
            style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onSurfaceVariant),
          ),
        ],
      ),
    );
  }
}

class _Stat extends StatelessWidget {
  final String label;
  final String value;
  const _Stat({required this.label, required this.value});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      children: [
        Text(value, style: theme.textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
        Text(label,
            style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
      ],
    );
  }
}
