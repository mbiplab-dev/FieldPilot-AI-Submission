import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/image_pick.dart';
import '../core/session.dart';
import '../widgets/common.dart';

class _HazardType {
  final String eventType;
  final String label;
  final IconData icon;
  final String defaultSeverity;
  const _HazardType(this.eventType, this.label, this.icon, this.defaultSeverity);
}

const _hazardTypes = [
  _HazardType('fall', 'Person down', Icons.personal_injury_outlined, 'critical'),
  _HazardType('fire', 'Fire', Icons.local_fire_department_outlined, 'critical'),
  _HazardType('gas', 'Gas leak', Icons.warning_amber_rounded, 'critical'),
  _HazardType('proximity', 'Machinery danger', Icons.precision_manufacturing_outlined, 'high'),
  _HazardType('crack', 'Structural damage', Icons.foundation_outlined, 'high'),
  _HazardType('ppe', 'Missing PPE', Icons.health_and_safety_outlined, 'high'),
  _HazardType('inspection', 'Other hazard', Icons.report_problem_outlined, 'medium'),
];

/// The panic button. A worker who sees something dangerous should be able to report it in two
/// taps without composing a sentence — every button here is large, high-contrast, and reachable
/// one-handed, because this is a safety action taken in a hurry, not a form to be filled in
/// carefully.
class ReportTab extends StatelessWidget {
  const ReportTab({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Report a hazard'),
        actions: const [AccountAction(), SizedBox(width: 4)],
      ),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: GridView.builder(
          gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
            crossAxisCount: 2,
            mainAxisSpacing: 14,
            crossAxisSpacing: 14,
            childAspectRatio: 1.05,
          ),
          itemCount: _hazardTypes.length,
          itemBuilder: (context, i) {
            final type = _hazardTypes[i];
            return _HazardButton(
              type: type,
              onTap: () => _openReportSheet(context, type),
            );
          },
        ),
      ),
    );
  }

  void _openReportSheet(BuildContext context, _HazardType type) {
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      builder: (_) => _ReportSheet(type: type),
    );
  }
}

class _HazardButton extends StatelessWidget {
  final _HazardType type;
  final VoidCallback onTap;
  const _HazardButton({required this.type, required this.onTap});

  @override
  Widget build(BuildContext context) {
    final color = severityColor(type.defaultSeverity);
    return Material(
      color: color.withValues(alpha: 0.10),
      borderRadius: BorderRadius.circular(18),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(18),
        child: Container(
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(18),
            border: Border.all(color: color.withValues(alpha: 0.4), width: 1.5),
          ),
          padding: const EdgeInsets.all(12),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Icon(type.icon, size: 40, color: color),
              const SizedBox(height: 10),
              Text(
                type.label,
                textAlign: TextAlign.center,
                style: TextStyle(fontWeight: FontWeight.w700, color: color, fontSize: 15),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _ReportSheet extends StatefulWidget {
  final _HazardType type;
  const _ReportSheet({required this.type});

  @override
  State<_ReportSheet> createState() => _ReportSheetState();
}

class _ReportSheetState extends State<_ReportSheet> {
  late String _severity;
  final _note = TextEditingController();
  Uint8List? _imageBytes;
  bool _sending = false;
  String? _error;
  bool _sent = false;

  @override
  void initState() {
    super.initState();
    _severity = widget.type.defaultSeverity;
  }

  @override
  void dispose() {
    _note.dispose();
    super.dispose();
  }

  Future<void> _attachPhoto(bool fromCamera) async {
    final picked = await pickPhoto(fromCamera: fromCamera);
    if (picked != null) setState(() => _imageBytes = picked.bytes);
  }

  Future<void> _submit() async {
    setState(() {
      _sending = true;
      _error = null;
    });
    try {
      await context.read<Session>().api.raiseAlert(
            eventType: widget.type.eventType,
            severity: _severity,
            message: _note.text.trim().isEmpty
                ? '${widget.type.label} reported by worker'
                : _note.text.trim(),
            imageBytes: _imageBytes,
          );
      if (!mounted) return;
      setState(() => _sent = true);
    } catch (e) {
      setState(() => _error = friendlyError(e));
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  static const _severities = ['low', 'medium', 'high', 'critical'];

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final color = severityColor(_severity);

    if (_sent) {
      return Padding(
        padding: EdgeInsets.only(
          left: 24, right: 24, top: 32, bottom: 32 + MediaQuery.of(context).viewInsets.bottom,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.check_circle, color: Colors.green, size: 56),
            const SizedBox(height: 12),
            Text('Alert sent', style: theme.textTheme.titleLarge),
            const SizedBox(height: 6),
            Text('${widget.type.label} has been reported. The site manager has been notified.',
                textAlign: TextAlign.center, style: theme.textTheme.bodyMedium),
            const SizedBox(height: 20),
            FilledButton(
              style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(52)),
              onPressed: () => Navigator.of(context).pop(),
              child: const Text('Done'),
            ),
          ],
        ),
      );
    }

    return Padding(
      padding: EdgeInsets.only(
        left: 20, right: 20, top: 16, bottom: 20 + MediaQuery.of(context).viewInsets.bottom,
      ),
      child: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Icon(widget.type.icon, color: color, size: 28),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(widget.type.label,
                      style: theme.textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700)),
                ),
              ],
            ),
            const SizedBox(height: 16),
            Text('Severity', style: theme.textTheme.labelLarge),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              children: _severities.map((s) {
                final selected = s == _severity;
                final c = severityColor(s);
                return ChoiceChip(
                  label: Text(s.toUpperCase()),
                  selected: selected,
                  onSelected: (_) => setState(() => _severity = s),
                  selectedColor: c.withValues(alpha: 0.25),
                  labelStyle: TextStyle(
                    color: selected ? c : theme.colorScheme.onSurface,
                    fontWeight: selected ? FontWeight.w700 : FontWeight.w500,
                  ),
                );
              }).toList(),
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _note,
              maxLines: 2,
              textCapitalization: TextCapitalization.sentences,
              decoration: const InputDecoration(
                labelText: 'What\'s happening? (optional)',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            if (_imageBytes != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Stack(
                  children: [
                    ClipRRect(
                      borderRadius: BorderRadius.circular(8),
                      child: Image.memory(_imageBytes!, height: 90, fit: BoxFit.cover, width: double.infinity),
                    ),
                    Positioned(
                      top: 4,
                      right: 4,
                      child: IconButton.filled(
                        icon: const Icon(Icons.close, size: 16),
                        onPressed: () => setState(() => _imageBytes = null),
                      ),
                    ),
                  ],
                ),
              )
            else
              Row(
                children: [
                  Expanded(
                    child: OutlinedButton.icon(
                      onPressed: () => _attachPhoto(true),
                      icon: const Icon(Icons.photo_camera_outlined),
                      label: const Text('Photo'),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: OutlinedButton.icon(
                      onPressed: () => _attachPhoto(false),
                      icon: const Icon(Icons.image_outlined),
                      label: const Text('Choose'),
                    ),
                  ),
                ],
              ),
            if (_error != null) ...[
              const SizedBox(height: 8),
              Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
            ],
            const SizedBox(height: 16),
            FilledButton(
              style: FilledButton.styleFrom(
                minimumSize: const Size.fromHeight(56),
                backgroundColor: color,
              ),
              onPressed: _sending ? null : _submit,
              child: _sending
                  ? const SizedBox(
                      width: 22, height: 22,
                      child: CircularProgressIndicator(strokeWidth: 2.4, color: Colors.white))
                  : const Text('SEND ALERT', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 16)),
            ),
          ],
        ),
      ),
    );
  }
}
