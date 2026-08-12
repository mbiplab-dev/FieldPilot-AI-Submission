import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/image_pick.dart';
import '../core/models.dart';
import '../core/session.dart';
import '../widgets/common.dart';

const _referenceColor = Color(0xFFF2B705);
const _measureColor = Color(0xFF35B8C8);

/// Four-tap, reference-calibrated measurement for a hackathon-safe phone workflow.
///
/// Taps 1–2 mark a known physical reference; taps 3–4 mark the requested dimension. The backend
/// performs only Euclidean geometry and refuses malformed points. No VLM is asked to invent scale.
class MeasurementScreen extends StatefulWidget {
  const MeasurementScreen({super.key});

  @override
  State<MeasurementScreen> createState() => _MeasurementScreenState();
}

class _MeasurementScreenState extends State<MeasurementScreen> {
  final _reference = TextEditingController(text: '100');
  final _spec = TextEditingController();
  final _tolerance = TextEditingController(text: '5');
  Uint8List? _image;
  double _imageWidth = 0;
  double _imageHeight = 0;
  final List<Offset> _points = [];
  MeasurementResult? _result;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _reference.dispose();
    _spec.dispose();
    _tolerance.dispose();
    super.dispose();
  }

  Future<void> _capture(bool camera) async {
    final photo = await pickPhoto(fromCamera: camera);
    if (photo == null) return;
    ui.decodeImageFromList(photo.bytes, (decoded) {
      if (!mounted) return;
      setState(() {
        _image = photo.bytes;
        _imageWidth = decoded.width.toDouble();
        _imageHeight = decoded.height.toDouble();
        _points.clear();
        _result = null;
        _error = null;
      });
      decoded.dispose();
    });
  }

  void _tap(TapDownDetails details, Size size) {
    if (_image == null) return;
    setState(() {
      if (_points.length == 4) {
        _points.clear();
        _result = null;
      }
      _points.add(
        Offset(
          details.localPosition.dx / size.width * _imageWidth,
          details.localPosition.dy / size.height * _imageHeight,
        ),
      );
      _error = null;
    });
  }

  Future<void> _measure() async {
    if (_points.length != 4) {
      setState(() => _error = 'Mark all four points first.');
      return;
    }
    final reference = double.tryParse(_reference.text.trim());
    final spec = _spec.text.trim().isEmpty
        ? null
        : double.tryParse(_spec.text.trim());
    final tolerance = double.tryParse(_tolerance.text.trim());
    if (reference == null ||
        reference <= 0 ||
        tolerance == null ||
        tolerance < 0) {
      setState(() => _error = 'Enter a valid reference length and tolerance.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final result = await context.read<Session>().api.measure(
        referencePoints: _points.take(2).map((p) => [p.dx, p.dy]).toList(),
        measurementPoints: _points.skip(2).map((p) => [p.dx, p.dy]).toList(),
        referenceMm: reference,
        specMm: spec,
        toleranceMm: tolerance,
        imageWidth: _imageWidth,
        imageHeight: _imageHeight,
      );
      if (mounted) setState(() => _result = result);
    } catch (e) {
      if (mounted) setState(() => _error = friendlyError(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Calibrated measure')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text(
            'Reference first. Target second.',
            style: theme.textTheme.headlineSmall?.copyWith(
              fontWeight: FontWeight.w800,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            'Tap two ends of a known object, then two ends of the dimension. Keep both on the same plane.',
            style: theme.textTheme.bodyMedium,
          ),
          const SizedBox(height: 16),
          if (_image == null)
            _PhotoPrompt(
              onCamera: () => _capture(true),
              onGallery: () => _capture(false),
            )
          else
            _PointCanvas(
              image: _image!,
              imageWidth: _imageWidth,
              imageHeight: _imageHeight,
              points: _points,
              onTap: _tap,
            ),
          const SizedBox(height: 12),
          if (_image != null)
            Text(
              _points.length < 2
                  ? 'Amber · mark known reference (${_points.length}/2)'
                  : _points.length < 4
                  ? 'Blue · mark target (${_points.length - 2}/2)'
                  : 'Four points ready · tap the image to start over',
              style: theme.textTheme.labelLarge?.copyWith(
                color: _points.length < 2 ? _referenceColor : _measureColor,
                fontFamily: 'monospace',
              ),
            ),
          const SizedBox(height: 14),
          Row(
            children: [
              Expanded(
                child: _NumberField(
                  controller: _reference,
                  label: 'Reference (mm)',
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: _NumberField(
                  controller: _spec,
                  label: 'Spec (mm)',
                  optional: true,
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: _NumberField(
                  controller: _tolerance,
                  label: '± tolerance',
                ),
              ),
            ],
          ),
          if (_error != null) ...[
            const SizedBox(height: 12),
            ErrorBanner(message: _error!),
          ],
          const SizedBox(height: 14),
          FilledButton.icon(
            onPressed: _busy || _image == null ? null : _measure,
            icon: _busy
                ? const SizedBox.square(
                    dimension: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.straighten),
            label: Text(_busy ? 'Calculating…' : 'Calculate dimension'),
            style: FilledButton.styleFrom(
              minimumSize: const Size.fromHeight(54),
            ),
          ),
          if (_result != null) ...[
            const SizedBox(height: 16),
            _ResultCard(result: _result!),
          ],
        ],
      ),
    );
  }
}

class _PointCanvas extends StatelessWidget {
  final Uint8List image;
  final double imageWidth;
  final double imageHeight;
  final List<Offset> points;
  final void Function(TapDownDetails, Size) onTap;
  const _PointCanvas({
    required this.image,
    required this.imageWidth,
    required this.imageHeight,
    required this.points,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return ClipRRect(
      borderRadius: BorderRadius.circular(16),
      child: AspectRatio(
        aspectRatio: imageWidth / imageHeight,
        child: LayoutBuilder(
          builder: (context, constraints) {
            final size = Size(constraints.maxWidth, constraints.maxHeight);
            Offset display(Offset p) => Offset(
              p.dx / imageWidth * size.width,
              p.dy / imageHeight * size.height,
            );
            return GestureDetector(
              onTapDown: (details) => onTap(details, size),
              child: Stack(
                fit: StackFit.expand,
                children: [
                  Image.memory(image, fit: BoxFit.fill),
                  CustomPaint(
                    painter: _PointPainter(points.map(display).toList()),
                  ),
                ],
              ),
            );
          },
        ),
      ),
    );
  }
}

class _PointPainter extends CustomPainter {
  final List<Offset> points;
  _PointPainter(this.points);
  @override
  void paint(Canvas canvas, Size size) {
    for (var i = 0; i < points.length; i++) {
      final color = i < 2 ? _referenceColor : _measureColor;
      canvas.drawCircle(
        points[i],
        10,
        Paint()..color = Colors.black.withValues(alpha: 0.55),
      );
      canvas.drawCircle(points[i], 7, Paint()..color = color);
    }
    if (points.length >= 2) {
      final paint = Paint()
        ..color = _referenceColor
        ..strokeWidth = 3;
      canvas.drawLine(points[0], points[1], paint);
    }
    if (points.length >= 4) {
      final paint = Paint()
        ..color = _measureColor
        ..strokeWidth = 3;
      canvas.drawLine(points[2], points[3], paint);
    }
  }

  @override
  bool shouldRepaint(covariant _PointPainter oldDelegate) =>
      oldDelegate.points != points;
}

class _PhotoPrompt extends StatelessWidget {
  final VoidCallback onCamera;
  final VoidCallback onGallery;
  const _PhotoPrompt({required this.onCamera, required this.onGallery});
  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.all(24),
    decoration: BoxDecoration(
      borderRadius: BorderRadius.circular(18),
      border: Border.all(color: Theme.of(context).colorScheme.outlineVariant),
    ),
    child: Column(
      children: [
        const Icon(Icons.center_focus_strong, size: 48),
        const SizedBox(height: 12),
        const Text('Capture the reference and target in one frame.'),
        const SizedBox(height: 16),
        Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            FilledButton.icon(
              onPressed: onCamera,
              icon: const Icon(Icons.camera_alt),
              label: const Text('Camera'),
            ),
            const SizedBox(width: 10),
            OutlinedButton.icon(
              onPressed: onGallery,
              icon: const Icon(Icons.photo_library),
              label: const Text('Gallery'),
            ),
          ],
        ),
      ],
    ),
  );
}

class _NumberField extends StatelessWidget {
  final TextEditingController controller;
  final String label;
  final bool optional;
  const _NumberField({
    required this.controller,
    required this.label,
    this.optional = false,
  });
  @override
  Widget build(BuildContext context) => TextField(
    controller: controller,
    keyboardType: const TextInputType.numberWithOptions(decimal: true),
    decoration: InputDecoration(
      labelText: label,
      hintText: optional ? 'optional' : null,
      border: const OutlineInputBorder(),
    ),
  );
}

class _ResultCard extends StatelessWidget {
  final MeasurementResult result;
  const _ResultCard({required this.result});
  @override
  Widget build(BuildContext context) {
    final pass = result.withinTolerance;
    final color = pass == null
        ? _measureColor
        : pass
        ? Colors.green
        : Theme.of(context).colorScheme.error;
    return Card(
      color: color.withValues(alpha: 0.10),
      child: Padding(
        padding: const EdgeInsets.all(18),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '${result.measuredMm.toStringAsFixed(1)} mm',
              style: Theme.of(context).textTheme.headlineMedium?.copyWith(
                fontWeight: FontWeight.w900,
                color: color,
              ),
            ),
            if (result.specMm != null)
              Text(
                'Spec ${result.specMm!.toStringAsFixed(1)} mm · deviation '
                '${result.deviationMm!.toStringAsFixed(1)} mm',
              ),
            if (pass != null)
              Text(
                pass ? 'Within entered tolerance' : 'Outside entered tolerance',
                style: TextStyle(color: color, fontWeight: FontWeight.w700),
              ),
            const SizedBox(height: 10),
            Text(
              result.limitations,
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ],
        ),
      ),
    );
  }
}
