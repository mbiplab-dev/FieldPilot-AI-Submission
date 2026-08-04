import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/image_pick.dart';
import '../core/models.dart';
import '../core/session.dart';
import '../widgets/common.dart';

/// Ask about something on site, with a photo. The answer comes from two places, shown
/// distinctly: an immediate LLM answer grounded in the site's own documents when it can be,
/// and the site manager's reply once they see it — the human answer is the authoritative one.
class AskTab extends StatefulWidget {
  const AskTab({super.key});

  @override
  State<AskTab> createState() => _AskTabState();
}

class _AskTabState extends State<AskTab> {
  final _textController = TextEditingController();
  Uint8List? _imageBytes;
  String? _imageName;
  bool _sending = false;
  String? _error;
  late Future<List<WorkerQuestion>> _future;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _textController.dispose();
    super.dispose();
  }

  void _load() {
    // Captured once, synchronously — see the identical comment in alerts_tab.dart.
    final session = context.read<Session>();
    _future = session.api.myQuestions().catchError((e) {
      if (e is ApiException && e.isAuthFailure) session.forceSignOut();
      throw e;
    });
  }

  Future<void> _refresh() async {
    setState(_load);
    await _future;
  }

  Future<void> _attachPhoto(bool fromCamera) async {
    final picked = await pickPhoto(fromCamera: fromCamera);
    if (picked == null) return;
    setState(() {
      _imageBytes = picked.bytes;
      _imageName = picked.filename;
    });
  }

  Future<void> _send() async {
    final text = _textController.text.trim();
    if (text.isEmpty) {
      setState(() => _error = 'Type your question first.');
      return;
    }
    setState(() {
      _sending = true;
      _error = null;
    });
    try {
      await context.read<Session>().api.ask(text: text, imageBytes: _imageBytes);
      _textController.clear();
      setState(() {
        _imageBytes = null;
        _imageName = null;
      });
      await _refresh();
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('Question sent')));
      }
    } catch (e) {
      setState(() => _error = friendlyError(e));
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Ask a question'),
        actions: const [AccountAction(), SizedBox(width: 4)],
      ),
      body: Column(
        children: [
          Expanded(
            child: RefreshIndicator(
              onRefresh: _refresh,
              child: FutureBuilder<List<WorkerQuestion>>(
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
                  final questions = snapshot.data ?? const [];
                  if (questions.isEmpty) {
                    return ListView(children: const [
                      SizedBox(height: 60),
                      EmptyState(
                        icon: Icons.forum_outlined,
                        title: 'No questions yet',
                        subtitle: 'Ask about anything you\'re unsure of — take a photo of it.',
                      ),
                    ]);
                  }
                  return ListView.separated(
                    padding: const EdgeInsets.all(12),
                    itemCount: questions.length,
                    separatorBuilder: (_, _) => const SizedBox(height: 8),
                    itemBuilder: (context, i) => _QuestionCard(question: questions[i]),
                  );
                },
              ),
            ),
          ),
          _Composer(
            textController: _textController,
            imageBytes: _imageBytes,
            imageName: _imageName,
            sending: _sending,
            error: _error,
            onAttachCamera: () => _attachPhoto(true),
            onAttachGallery: () => _attachPhoto(false),
            onRemoveImage: () => setState(() {
              _imageBytes = null;
              _imageName = null;
            }),
            onSend: _send,
          ),
        ],
      ),
    );
  }
}

class _Composer extends StatelessWidget {
  final TextEditingController textController;
  final Uint8List? imageBytes;
  final String? imageName;
  final bool sending;
  final String? error;
  final VoidCallback onAttachCamera;
  final VoidCallback onAttachGallery;
  final VoidCallback onRemoveImage;
  final VoidCallback onSend;

  const _Composer({
    required this.textController,
    required this.imageBytes,
    required this.imageName,
    required this.sending,
    required this.error,
    required this.onAttachCamera,
    required this.onAttachGallery,
    required this.onRemoveImage,
    required this.onSend,
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
            if (imageBytes != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Stack(
                  children: [
                    ClipRRect(
                      borderRadius: BorderRadius.circular(8),
                      child: Image.memory(imageBytes!, height: 84, width: 84, fit: BoxFit.cover),
                    ),
                    Positioned(
                      top: -8,
                      right: -8,
                      child: IconButton(
                        icon: const Icon(Icons.cancel, size: 20),
                        onPressed: onRemoveImage,
                      ),
                    ),
                  ],
                ),
              ),
            if (error != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 6),
                child: Text(error!, style: TextStyle(color: theme.colorScheme.error, fontSize: 12)),
              ),
            Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                IconButton(
                  icon: const Icon(Icons.photo_camera_outlined),
                  tooltip: 'Take a photo',
                  onPressed: onAttachCamera,
                ),
                IconButton(
                  icon: const Icon(Icons.image_outlined),
                  tooltip: 'Choose a photo',
                  onPressed: onAttachGallery,
                ),
                Expanded(
                  child: TextField(
                    controller: textController,
                    minLines: 1,
                    maxLines: 4,
                    textCapitalization: TextCapitalization.sentences,
                    decoration: const InputDecoration(
                      hintText: 'Ask about what you\'re looking at…',
                      border: OutlineInputBorder(),
                      contentPadding: EdgeInsets.symmetric(horizontal: 12, vertical: 10),
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
                  onPressed: sending ? null : onSend,
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _QuestionCard extends StatelessWidget {
  final WorkerQuestion question;
  const _QuestionCard({required this.question});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      margin: EdgeInsets.zero,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (question.imageUrl != null) ...[
                  ClipRRect(
                    borderRadius: BorderRadius.circular(8),
                    child: Image.network(
                      context.read<Session>().api.mediaUrl(question.imageUrl!),
                      width: 52,
                      height: 52,
                      fit: BoxFit.cover,
                      errorBuilder: (_, _, _) => Container(
                          width: 52, height: 52, color: theme.colorScheme.surfaceContainerHighest),
                    ),
                  ),
                  const SizedBox(width: 10),
                ],
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(question.text, style: theme.textTheme.bodyMedium?.copyWith(fontWeight: FontWeight.w600)),
                      const SizedBox(height: 2),
                      Text(timeAgo(question.createdAt),
                          style: theme.textTheme.bodySmall
                              ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
                    ],
                  ),
                ),
              ],
            ),
            if (question.llmAnswer != null) ...[
              const SizedBox(height: 10),
              _AnswerBubble(
                icon: Icons.smart_toy_outlined,
                label: 'Automated answer',
                text: question.llmAnswer!,
                warn: question.llmGrounded == false,
                warnText: 'Not confirmed against site documents',
              ),
            ],
            if (question.hasManagerReply) ...[
              const SizedBox(height: 8),
              _AnswerBubble(
                icon: Icons.verified_user_outlined,
                label: 'Site manager',
                text: question.managerReply!,
                warn: false,
                emphasize: true,
              ),
            ] else if (question.status == 'pending') ...[
              const SizedBox(height: 8),
              Row(
                children: [
                  const SizedBox(
                      width: 12, height: 12, child: CircularProgressIndicator(strokeWidth: 1.6)),
                  const SizedBox(width: 6),
                  Text('Waiting for the site manager',
                      style: theme.textTheme.bodySmall
                          ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _AnswerBubble extends StatelessWidget {
  final IconData icon;
  final String label;
  final String text;
  final bool warn;
  final String? warnText;
  final bool emphasize;

  const _AnswerBubble({
    required this.icon,
    required this.label,
    required this.text,
    required this.warn,
    this.warnText,
    this.emphasize = false,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final bg = emphasize
        ? theme.colorScheme.primaryContainer
        : theme.colorScheme.surfaceContainerHighest;
    final fg = emphasize ? theme.colorScheme.onPrimaryContainer : theme.colorScheme.onSurface;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(10)),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(icon, size: 14, color: fg),
              const SizedBox(width: 5),
              Text(label, style: TextStyle(fontSize: 11, fontWeight: FontWeight.w700, color: fg)),
              if (warn) ...[
                const SizedBox(width: 6),
                const Icon(Icons.warning_amber_rounded, size: 13, color: Colors.orange),
              ],
            ],
          ),
          const SizedBox(height: 4),
          Text(text, style: theme.textTheme.bodySmall?.copyWith(color: fg)),
          if (warn && warnText != null) ...[
            const SizedBox(height: 4),
            Text(warnText!, style: const TextStyle(fontSize: 11, color: Colors.orange)),
          ],
        ],
      ),
    );
  }
}
