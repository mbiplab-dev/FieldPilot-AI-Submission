import 'package:camera/camera.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/camera_stream.dart';
import '../core/session.dart';
import '../widgets/common.dart';

/// The worker's camera, streamed to the server for analysis and to the site manager to watch.
///
/// Streaming is explicitly opt-in and clearly indicated while running. A camera that started
/// itself, or ran without saying so, would be a surveillance device rather than a safety tool —
/// the worker holding the phone should always know when their feed is being watched.
class CameraTab extends StatefulWidget {
  const CameraTab({super.key});

  @override
  State<CameraTab> createState() => _CameraTabState();
}

class _CameraTabState extends State<CameraTab> {
  final _streamer = CameraStreamer();

  @override
  void dispose() {
    _streamer.dispose();
    super.dispose();
  }

  Future<void> _toggle() async {
    if (_streamer.streaming) {
      await _streamer.stop();
      return;
    }
    final session = context.read<Session>();
    final workerId = session.user?.workerId;
    if (workerId == null || workerId.isEmpty) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text(
          'This account has no worker id, so a feed could not be labelled. Ask your site manager.',
        ),
      ));
      return;
    }
    await _streamer.start(
      edgeUrl: session.edgeUrl,
      workerId: workerId,
      zone: session.currentZoneId,
      displayName: session.user?.displayName,
    );
  }

  @override
  Widget build(BuildContext context) {
    return ChangeNotifierProvider.value(
      value: _streamer,
      child: Consumer<CameraStreamer>(
        builder: (context, streamer, _) => Scaffold(
          appBar: AppBar(
            title: const Text('Site camera'),
            actions: const [VoiceAction(), AccountAction(), SizedBox(width: 4)],
          ),
          body: Column(
            children: [
              Expanded(child: _Preview(streamer: streamer)),
              _Controls(streamer: streamer, onToggle: _toggle),
            ],
          ),
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
                'Start the feed to let the site manager see what you see. '
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
    final colour = connected ? const Color(0xFFDC2626) : const Color(0xFF64748B);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.55),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(width: 8, height: 8, decoration: BoxDecoration(color: colour, shape: BoxShape.circle)),
          const SizedBox(width: 6),
          Text(
            connected ? 'LIVE — manager can see this' : 'Connecting…',
            style: const TextStyle(color: Colors.white, fontSize: 11, fontWeight: FontWeight.w700),
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
              ),
              icon: Icon(on ? Icons.stop_rounded : Icons.videocam_rounded),
              label: Text(
                on ? 'STOP STREAMING' : 'START CAMERA FEED',
                style: const TextStyle(fontWeight: FontWeight.w700, letterSpacing: 0.4),
              ),
            ),
          ),
          const SizedBox(height: 8),
          Text(
            on
                ? 'Analysis runs on the server. Detected hazards are spoken to you and raised with your manager.'
                : 'Your camera is off. Nothing is sent until you start it.',
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
