import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:just_audio/just_audio.dart';
import 'package:path_provider/path_provider.dart';
import 'package:provider/provider.dart';
import 'package:record/record.dart';

import '../core/live_feed.dart';
import '../core/models.dart';
import '../core/session.dart';
import '../widgets/common.dart';

/// Direct thread with the site manager: text and voice notes, either direction.
///
/// The manager's half of this already exists on the web dashboard (`frontend/src/app/messages`),
/// which settled the wire contract this screen has to match: `POST /me/messages` takes `text` and
/// an optional `audio` file, and a message with neither is rejected server-side, so this screen
/// never needs to invent that validation itself.
class MessagesTab extends StatefulWidget {
  const MessagesTab({super.key});

  @override
  State<MessagesTab> createState() => _MessagesTabState();
}

class _MessagesTabState extends State<MessagesTab> {
  /// Matches the web dashboard's own cap (`frontend/src/components/VoiceRecorder.tsx`) — same
  /// backend, same reason: a voice note is a quick aside, not a briefing, and an unbounded
  /// recording is one workaround away from someone using this as a dictaphone.
  static const _maxRecordSeconds = 120;

  final _textController = TextEditingController();
  final _scrollController = ScrollController();
  final _recorder = AudioRecorder();
  final _player = AudioPlayer();

  late Future<List<DirectMessage>> _future;
  LiveFeed? _feed;
  double _lastHandledTs = 0;

  bool _sendingText = false;
  bool _recording = false;
  int _recordSeconds = 0;
  Timer? _recordTicker;
  String? _error;

  String? _playingMessageId;
  Duration _playPosition = Duration.zero;
  Duration? _playDuration;
  StreamSubscription<PlayerState>? _playerStateSub;
  StreamSubscription<Duration>? _positionSub;

  @override
  void initState() {
    super.initState();
    _load();
    _playerStateSub = _player.playerStateStream.listen((state) {
      if (state.processingState == ProcessingState.completed) {
        _player.stop();
        if (mounted) setState(() => _playingMessageId = null);
      }
    });
    _positionSub = _player.positionStream.listen((p) {
      if (mounted) setState(() => _playPosition = p);
    });
    // Deferred: `context.read` is not legal during initState, and marking the thread read is a
    // side effect this screen should only trigger once it is actually the one on screen.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final feed = context.read<LiveFeed>();
      _feed = feed;
      feed.addListener(_onLiveFrame);
      _markThreadRead();
    });
  }

  @override
  void dispose() {
    _feed?.removeListener(_onLiveFrame);
    _recordTicker?.cancel();
    _playerStateSub?.cancel();
    _positionSub?.cancel();
    _textController.dispose();
    _scrollController.dispose();
    _recorder.dispose();
    _player.dispose();
    super.dispose();
  }

  void _load() {
    // Captured once, synchronously — see the identical comment in alerts_tab.dart.
    final session = context.read<Session>();
    _future = session.api.myMessages().then((messages) {
      _scrollToBottomSoon();
      return messages;
    }).catchError((e) {
      if (e is ApiException && e.isAuthFailure) session.forceSignOut();
      throw e;
    });
  }

  Future<void> _refresh() async {
    setState(_load);
    await _future;
  }

  /// A pushed `message` frame means the thread changed — either side. Refetching rather than
  /// splicing the payload in keeps the server the single source of truth for read state and
  /// ordering, the same choice `alerts_tab.dart` makes for pushed alerts.
  void _onLiveFrame() {
    final last = _feed?.last;
    if (last == null || last.topic != 'message') return;
    if (last.ts <= _lastHandledTs) return;
    _lastHandledTs = last.ts;
    if (mounted) setState(_load);
    _markThreadRead();
  }

  Future<void> _markThreadRead() async {
    final workerId = context.read<Session>().user?.workerId;
    if (workerId == null) return;
    try {
      await context.read<Session>().api.markMessagesRead(workerId);
    } catch (_) {
      // Best-effort: an unread badge staying lit one refresh longer is not worth an error banner
      // over a screen the worker can already read fine.
    }
  }

  void _scrollToBottomSoon() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scrollController.hasClients) return;
      _scrollController.jumpTo(_scrollController.position.maxScrollExtent);
    });
  }

  Future<void> _sendText() async {
    final text = _textController.text.trim();
    if (text.isEmpty) return;
    final session = context.read<Session>(); // before any await
    setState(() {
      _sendingText = true;
      _error = null;
    });
    try {
      await session.api.sendMessage(text: text);
      _textController.clear();
      await _refresh();
    } catch (e) {
      if (e is ApiException && e.isAuthFailure) {
        session.forceSignOut();
      } else if (mounted) {
        setState(() => _error = friendlyError(e));
      }
    } finally {
      if (mounted) setState(() => _sendingText = false);
    }
  }

  Future<void> _startRecording() async {
    if (_recording) return;
    setState(() => _error = null);
    final has = await _recorder.hasPermission();
    if (!has) {
      // Never fail silently: a worker holding the mic button and hearing nothing happen would
      // reasonably assume the feature is broken rather than that Android just denied it.
      if (mounted) {
        setState(() => _error =
            'Microphone access is off for FieldPilot Worker. Turn it on in the phone\'s Settings to send voice notes.');
      }
      return;
    }
    final dir = await getTemporaryDirectory();
    final path = '${dir.path}/voice-${DateTime.now().microsecondsSinceEpoch}.m4a';
    try {
      // Default encoder is AAC-LC in an MP4 container (`.m4a`), which is on the backend's
      // allowlist — see the class doc.
      await _recorder.start(const RecordConfig(), path: path);
    } catch (e) {
      if (mounted) setState(() => _error = friendlyError(e));
      return;
    }
    setState(() {
      _recording = true;
      _recordSeconds = 0;
    });
    _recordTicker = Timer.periodic(const Duration(seconds: 1), (_) {
      if (!mounted) return;
      setState(() => _recordSeconds++);
      if (_recordSeconds >= _maxRecordSeconds) _stopRecording(send: true);
    });
  }

  Future<void> _stopRecording({required bool send}) async {
    if (!_recording) return;
    final session = context.read<Session>(); // before any await
    _recordTicker?.cancel();
    _recordTicker = null;
    final seconds = _recordSeconds;
    setState(() {
      _recording = false;
      _recordSeconds = 0;
    });

    final path = await _recorder.stop();
    if (path == null) return;
    final file = File(path);
    // A hold shorter than a second is almost always a mis-tap, not an aborted sentence — sending
    // it would leave the manager an empty-sounding voice note with nothing to act on.
    if (!send || seconds < 1) {
      unawaited(file.delete().catchError((_) => file));
      return;
    }
    try {
      final bytes = await file.readAsBytes();
      await session.api.sendMessage(audioBytes: bytes, filename: 'voice.m4a');
      await _refresh();
    } catch (e) {
      if (e is ApiException && e.isAuthFailure) {
        session.forceSignOut();
      } else if (mounted) {
        setState(() => _error = friendlyError(e));
      }
    } finally {
      unawaited(file.delete().catchError((_) => file));
    }
  }

  Future<void> _togglePlay(DirectMessage message) async {
    final session = context.read<Session>();
    final url = message.audioUrl;
    if (url == null) return;
    if (_playingMessageId == message.messageId) {
      await _player.pause();
      setState(() => _playingMessageId = null);
      return;
    }
    try {
      await _player.setUrl(session.api.mediaUrl(url));
      setState(() {
        _playingMessageId = message.messageId;
        _playDuration = _player.duration;
      });
      await _player.play();
    } catch (e) {
      if (mounted) setState(() => _error = friendlyError(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Messages'),
        actions: const [VoiceAction(), AccountAction(), SizedBox(width: 4)],
      ),
      body: Column(
        children: [
          Expanded(
            child: RefreshIndicator(
              onRefresh: _refresh,
              child: FutureBuilder<List<DirectMessage>>(
                future: _future,
                builder: (context, snapshot) {
                  if (snapshot.connectionState == ConnectionState.waiting) {
                    return const Center(child: CircularProgressIndicator());
                  }
                  if (snapshot.hasError) {
                    return ListView(children: [
                      const SizedBox(height: 60),
                      ErrorBanner(message: friendlyError(snapshot.error!), onRetry: _refresh),
                    ]);
                  }
                  final messages = snapshot.data ?? const [];
                  if (messages.isEmpty) {
                    return ListView(children: const [
                      SizedBox(height: 60),
                      EmptyState(
                        icon: Icons.forum_outlined,
                        title: 'No messages yet',
                        subtitle: 'Send the site manager a text or a voice note.',
                      ),
                    ]);
                  }
                  return ListView.builder(
                    controller: _scrollController,
                    padding: const EdgeInsets.all(12),
                    itemCount: messages.length,
                    itemBuilder: (context, i) {
                      final message = messages[i];
                      return Padding(
                        padding: const EdgeInsets.only(bottom: 8),
                        child: _MessageBubble(
                          message: message,
                          playing: _playingMessageId == message.messageId,
                          position: _playingMessageId == message.messageId ? _playPosition : null,
                          duration: _playingMessageId == message.messageId ? _playDuration : null,
                          onTogglePlay: message.hasAudio ? () => _togglePlay(message) : null,
                        ),
                      );
                    },
                  );
                },
              ),
            ),
          ),
          _Composer(
            textController: _textController,
            sending: _sendingText,
            recording: _recording,
            recordSeconds: _recordSeconds,
            error: _error,
            onSendText: _sendText,
            onStartRecording: _startRecording,
            onStopRecording: () => _stopRecording(send: true),
            onCancelRecording: () => _stopRecording(send: false),
          ),
        ],
      ),
    );
  }
}

class _Composer extends StatelessWidget {
  final TextEditingController textController;
  final bool sending;
  final bool recording;
  final int recordSeconds;
  final String? error;
  final VoidCallback onSendText;
  final VoidCallback onStartRecording;
  final VoidCallback onStopRecording;
  final VoidCallback onCancelRecording;

  const _Composer({
    required this.textController,
    required this.sending,
    required this.recording,
    required this.recordSeconds,
    required this.error,
    required this.onSendText,
    required this.onStartRecording,
    required this.onStopRecording,
    required this.onCancelRecording,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return SafeArea(
      top: false,
      child: Container(
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
        decoration: BoxDecoration(
          color: theme.colorScheme.surface,
          border: Border(top: BorderSide(color: theme.colorScheme.outlineVariant)),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (error != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 6),
                child: Text(error!, style: TextStyle(color: theme.colorScheme.error, fontSize: 12)),
              ),
            if (recording)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Row(
                  children: [
                    Icon(Icons.fiber_manual_record, size: 14, color: theme.colorScheme.error),
                    const SizedBox(width: 6),
                    Text('Recording… ${recordSeconds}s',
                        style: theme.textTheme.bodyMedium
                            ?.copyWith(color: theme.colorScheme.error, fontWeight: FontWeight.w600)),
                    const Spacer(),
                    TextButton(onPressed: onCancelRecording, child: const Text('Cancel')),
                  ],
                ),
              ),
            Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Expanded(
                  child: TextField(
                    controller: textController,
                    enabled: !recording,
                    minLines: 1,
                    maxLines: 4,
                    textCapitalization: TextCapitalization.sentences,
                    decoration: const InputDecoration(
                      hintText: 'Message the site manager…',
                      border: OutlineInputBorder(),
                      contentPadding: EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                    ),
                  ),
                ),
                const SizedBox(width: 6),
                // Hold to record, release to send — a tap alone does nothing, since
                // `onLongPressStart` only fires once the hold is recognised.
                GestureDetector(
                  onLongPressStart: (_) => onStartRecording(),
                  onLongPressEnd: (_) => onStopRecording(),
                  child: CircleAvatar(
                    radius: 22,
                    backgroundColor:
                        recording ? theme.colorScheme.error : theme.colorScheme.primaryContainer,
                    child: Icon(
                      Icons.mic,
                      color: recording ? Colors.white : theme.colorScheme.onPrimaryContainer,
                    ),
                  ),
                ),
                const SizedBox(width: 6),
                IconButton.filled(
                  icon: sending
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                      : const Icon(Icons.send),
                  onPressed: sending || recording ? null : onSendText,
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _MessageBubble extends StatelessWidget {
  final DirectMessage message;
  final bool playing;
  final Duration? position;
  final Duration? duration;
  final VoidCallback? onTogglePlay;

  const _MessageBubble({
    required this.message,
    required this.playing,
    required this.position,
    required this.duration,
    required this.onTogglePlay,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final fromWorker = message.fromWorker;
    final bg = fromWorker ? theme.colorScheme.primaryContainer : theme.colorScheme.surfaceContainerHighest;
    final fg = fromWorker ? theme.colorScheme.onPrimaryContainer : theme.colorScheme.onSurface;

    return Row(
      mainAxisAlignment: fromWorker ? MainAxisAlignment.end : MainAxisAlignment.start,
      children: [
        ConstrainedBox(
          constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.78),
          child: Container(
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(12)),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                // The manager's name identifies who sent it; the worker's own bubble needs no
                // label — it is unambiguously "me" by being on the right.
                if (!fromWorker)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 3),
                    child: Text(message.senderName,
                        style: theme.textTheme.labelSmall?.copyWith(fontWeight: FontWeight.w700, color: fg)),
                  ),
                if (message.hasAudio)
                  _AudioRow(
                    fg: fg,
                    playing: playing,
                    position: position,
                    duration: duration ?? _durationOf(message.audioSeconds),
                    onToggle: onTogglePlay,
                  ),
                if (message.text.isNotEmpty)
                  Padding(
                    padding: EdgeInsets.only(top: message.hasAudio ? 6 : 0),
                    child: Text(message.text, style: theme.textTheme.bodyMedium?.copyWith(color: fg)),
                  ),
                const SizedBox(height: 3),
                Text(timeAgo(message.createdAt),
                    style: theme.textTheme.bodySmall?.copyWith(color: fg.withValues(alpha: 0.65))),
              ],
            ),
          ),
        ),
      ],
    );
  }

  static Duration? _durationOf(double? seconds) =>
      seconds == null ? null : Duration(milliseconds: (seconds * 1000).round());
}

class _AudioRow extends StatelessWidget {
  final Color fg;
  final bool playing;
  final Duration? position;
  final Duration? duration;
  final VoidCallback? onToggle;

  const _AudioRow({
    required this.fg,
    required this.playing,
    required this.position,
    required this.duration,
    required this.onToggle,
  });

  @override
  Widget build(BuildContext context) {
    final shown = playing ? position : duration;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        IconButton(
          icon: Icon(playing ? Icons.pause_circle_filled : Icons.play_circle_fill, color: fg),
          iconSize: 30,
          padding: EdgeInsets.zero,
          constraints: const BoxConstraints(),
          onPressed: onToggle,
        ),
        const SizedBox(width: 6),
        Icon(Icons.graphic_eq, size: 16, color: fg.withValues(alpha: 0.7)),
        if (shown != null) ...[
          const SizedBox(width: 6),
          Text(_format(shown), style: TextStyle(color: fg, fontSize: 12)),
        ],
      ],
    );
  }

  String _format(Duration d) {
    final m = d.inMinutes;
    final s = d.inSeconds % 60;
    return '$m:${s.toString().padLeft(2, '0')}';
  }
}
