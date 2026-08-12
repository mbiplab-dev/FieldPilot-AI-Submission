import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/image_pick.dart';
import '../core/models.dart';
import '../core/session.dart';
import '../core/speech_bridge.dart';
import '../core/voice.dart';
import '../widgets/common.dart';
import 'ask_tab.dart';
import 'measurement_screen.dart';

const _concrete = Color(0xFF16232B);
const _hardhat = Color(0xFFF2B705);
const _steel = Color(0xFF66808E);

class AssistantTab extends StatefulWidget {
  const AssistantTab({super.key});
  @override
  State<AssistantTab> createState() => _AssistantTabState();
}

class _AssistantTabState extends State<AssistantTab> {
  final _command = TextEditingController();
  final _speech = SpeechBridge();
  Uint8List? _image;
  String? _imageName;
  AssistantReply? _reply;
  bool _listening = false;
  bool _asking = false;
  String? _error;

  @override
  void dispose() {
    _command.dispose();
    super.dispose();
  }

  Future<void> _listen() async {
    setState(() {
      _listening = true;
      _error = null;
    });
    try {
      final heard = await _speech.listen();
      _command.text = heard.command;
      await _ask();
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    } finally {
      if (mounted) setState(() => _listening = false);
    }
  }

  Future<void> _attach() async {
    final photo = await pickPhoto(fromCamera: true);
    if (photo == null) return;
    setState(() {
      _image = photo.bytes;
      _imageName = photo.filename;
      _error = null;
    });
  }

  Future<void> _ask() async {
    final text = _command.text.trim();
    if (text.isEmpty) {
      setState(() => _error = 'Say or type a command first.');
      return;
    }
    setState(() {
      _asking = true;
      _error = null;
    });
    final session = context.read<Session>();
    final voice = context.read<Voice>();
    try {
      final reply = await session.api.assistant(
        text: text,
        imageBytes: _image,
        imageFilename: _imageName,
      );
      if (!mounted) return;
      setState(() => _reply = reply);
      await voice.speakAssistant(reply.answer);
      if (mounted && reply.action?['type'] == 'open_measurement') {
        _openMeasure();
      }
    } catch (e) {
      if (mounted) setState(() => _error = friendlyError(e));
    } finally {
      if (mounted) setState(() => _asking = false);
    }
  }

  void _openMeasure() {
    Navigator.of(
      context,
    ).push(MaterialPageRoute(builder: (_) => const MeasurementScreen()));
  }

  Future<void> _runAction() async {
    final action = _reply?.action;
    if (action == null) return;
    switch (action['type']) {
      case 'capture_photo':
        await _attach();
        return;
      case 'open_measurement':
        _openMeasure();
        return;
      case 'confirm_hazard_report':
        final confirmed = await showDialog<bool>(
          context: context,
          builder: (context) => AlertDialog(
            icon: const Icon(Icons.warning_amber_rounded),
            title: const Text('Send hazard report?'),
            content: Text(
              '${action['severity'].toString().toUpperCase()} · ${action['message']}',
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('Cancel'),
              ),
              FilledButton(
                onPressed: () => Navigator.pop(context, true),
                child: const Text('Alert manager'),
              ),
            ],
          ),
        );
        if (confirmed != true || !mounted) return;
        final session = context.read<Session>();
        final voice = context.read<Voice>();
        final messenger = ScaffoldMessenger.of(context);
        await session.api.raiseAlert(
          eventType: action['event_type'] as String? ?? 'inspection',
          severity: action['severity'] as String? ?? 'high',
          message: action['message'] as String? ?? 'Voice-reported hazard',
          imageBytes: _image,
        );
        if (!mounted) return;
        await voice.speakAssistant(
          'Hazard sent. The site manager has been notified.',
        );
        if (!mounted) return;
        messenger.showSnackBar(
          const SnackBar(content: Text('Hazard sent to the site manager')),
        );
        return;
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(
        title: const Text('FieldPilot assistant'),
        actions: const [VoiceAction(), AccountAction(), SizedBox(width: 4)],
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 10, 16, 24),
        children: [
          _Beacon(
            listening: _listening,
            busy: _asking,
            onTap: _listening || _asking ? null : _listen,
          ),
          const SizedBox(height: 14),
          TextField(
            controller: _command,
            textInputAction: TextInputAction.send,
            onSubmitted: (_) => _ask(),
            decoration: InputDecoration(
              labelText: 'Command',
              hintText: 'Identify this tool, measure this, check the spec…',
              border: const OutlineInputBorder(),
              suffixIcon: IconButton(
                tooltip: 'Capture photo',
                onPressed: _attach,
                icon: Icon(
                  _image == null ? Icons.camera_alt_outlined : Icons.image,
                ),
              ),
            ),
          ),
          if (_image != null) ...[
            const SizedBox(height: 8),
            Row(
              children: [
                const Icon(Icons.check_circle, color: Colors.green, size: 18),
                const SizedBox(width: 6),
                Expanded(child: Text(_imageName ?? 'Current photo attached')),
                TextButton(
                  onPressed: () => setState(() => _image = null),
                  child: const Text('Remove'),
                ),
              ],
            ),
          ],
          const SizedBox(height: 10),
          FilledButton.icon(
            onPressed: _asking ? null : _ask,
            icon: _asking
                ? const SizedBox.square(
                    dimension: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.arrow_upward),
            label: Text(_asking ? 'Asking Gemma…' : 'Ask FieldPilot'),
            style: FilledButton.styleFrom(
              minimumSize: const Size.fromHeight(50),
            ),
          ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 8,
            runSpacing: 4,
            children: [
              ActionChip(
                label: const Text('Measure'),
                avatar: const Icon(Icons.straighten, size: 18),
                onPressed: _openMeasure,
              ),
              ActionChip(
                label: const Text('Identify'),
                avatar: const Icon(Icons.center_focus_strong, size: 18),
                onPressed: () {
                  _command.text =
                      'Identify this object and tell me any visible safety concern.';
                  _attach();
                },
              ),
              ActionChip(
                label: const Text('Check spec'),
                avatar: const Icon(Icons.description_outlined, size: 18),
                onPressed: () => _command.text =
                    'What does the site specification require here?',
              ),
              ActionChip(
                label: const Text('Ask manager'),
                avatar: const Icon(Icons.supervisor_account_outlined, size: 18),
                onPressed: () => Navigator.of(
                  context,
                ).push(MaterialPageRoute(builder: (_) => const AskTab())),
              ),
            ],
          ),
          if (_error != null) ...[
            const SizedBox(height: 12),
            ErrorBanner(message: _error!),
          ],
          if (_reply != null) ...[
            const SizedBox(height: 16),
            _ReplyCard(reply: _reply!, onAction: _runAction),
          ],
          const SizedBox(height: 18),
          Text(
            'WHAT YOU CAN SAY',
            style: theme.textTheme.labelSmall?.copyWith(
              letterSpacing: 1.5,
              color: _steel,
              fontFamily: 'monospace',
            ),
          ),
          const SizedBox(height: 8),
          const _Example(text: '“Hey FieldPilot, identify this valve.”'),
          const _Example(text: '“Hey FieldPilot, measure this rebar spacing.”'),
          const _Example(
            text: '“Hey FieldPilot, report smoke near the stairs.”',
          ),
        ],
      ),
    );
  }
}

class _Beacon extends StatelessWidget {
  final bool listening;
  final bool busy;
  final VoidCallback? onTap;
  const _Beacon({
    required this.listening,
    required this.busy,
    required this.onTap,
  });
  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.fromLTRB(18, 20, 18, 18),
    decoration: BoxDecoration(
      color: _concrete,
      borderRadius: BorderRadius.circular(24),
    ),
    child: Column(
      children: [
        Text(
          listening
              ? 'LISTENING FOR “HEY FIELDPILOT”'
              : busy
              ? 'GEMMA IS CHECKING'
              : 'VOICE BEACON READY',
          style: const TextStyle(
            color: _hardhat,
            fontFamily: 'monospace',
            letterSpacing: 1.2,
            fontWeight: FontWeight.w700,
          ),
        ),
        const SizedBox(height: 16),
        Semantics(
          button: true,
          label: 'Arm Hey FieldPilot voice command',
          child: InkResponse(
            onTap: onTap,
            radius: 66,
            child: Container(
              width: 118,
              height: 118,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                border: Border.all(
                  color: _hardhat.withValues(alpha: 0.28),
                  width: 12,
                ),
              ),
              padding: const EdgeInsets.all(12),
              child: Container(
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  border: Border.all(
                    color: _hardhat.withValues(alpha: 0.56),
                    width: 7,
                  ),
                ),
                child: CircleAvatar(
                  backgroundColor: _hardhat,
                  foregroundColor: _concrete,
                  child: Icon(
                    listening
                        ? Icons.hearing
                        : busy
                        ? Icons.hourglass_top
                        : Icons.mic,
                    size: 38,
                  ),
                ),
              ),
            ),
          ),
        ),
        const SizedBox(height: 12),
        Text(
          listening
              ? 'Say the wake phrase, then your command.'
              : 'Tap once, then speak naturally.',
          style: const TextStyle(color: Colors.white70),
        ),
      ],
    ),
  );
}

class _ReplyCard extends StatelessWidget {
  final AssistantReply reply;
  final VoidCallback onAction;
  const _ReplyCard({required this.reply, required this.onAction});
  @override
  Widget build(BuildContext context) {
    final actionLabel = switch (reply.action?['type']) {
      'open_measurement' => 'Open measurement',
      'capture_photo' => 'Capture photo',
      'confirm_hazard_report' => 'Review hazard report',
      _ => null,
    };
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(18),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(
                  reply.degraded ? Icons.cloud_off : Icons.auto_awesome,
                  color: reply.degraded ? _steel : _hardhat,
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    reply.degraded ? 'Safe fallback' : 'FieldPilot',
                    style: Theme.of(context).textTheme.titleMedium?.copyWith(
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                ),
                Text(
                  reply.intent.toUpperCase(),
                  style: const TextStyle(
                    fontFamily: 'monospace',
                    fontSize: 11,
                    color: _steel,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 12),
            Text(reply.answer, style: Theme.of(context).textTheme.bodyLarge),
            if (actionLabel != null) ...[
              const SizedBox(height: 14),
              FilledButton(onPressed: onAction, child: Text(actionLabel)),
            ],
            const SizedBox(height: 12),
            Text(
              reply.safetyNote,
              style: Theme.of(
                context,
              ).textTheme.bodySmall?.copyWith(color: _steel),
            ),
          ],
        ),
      ),
    );
  }
}

class _Example extends StatelessWidget {
  final String text;
  const _Example({required this.text});
  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 4),
    child: Row(
      children: [
        const Icon(Icons.chevron_right, color: _hardhat),
        Expanded(child: Text(text)),
      ],
    ),
  );
}
