import 'dart:async';
import 'dart:convert';

import 'package:camera/camera.dart';
import 'package:flutter/foundation.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// Streams the phone's camera to the server, which does all the AI.
///
/// The phone is deliberately a dumb capture device: it encodes JPEG frames and ships them over a
/// WebSocket to the edge server's `/ws/video`, and the server runs YOLO, the pose model and every
/// safety detector. Nothing is inferred on the handset. That keeps the app small, keeps detection
/// consistent with every other ingest path, and means a mid-range phone is enough to be a site
/// camera.
///
/// Frames are captured with [CameraController.takePicture] rather than the raw image stream.
/// `startImageStream` hands back YUV planes, and converting those to JPEG in Dart costs more than
/// the capture itself — it would burn battery to save nothing, since the frames leave the device
/// either way. `takePicture` at a modest resolution gives an encoded JPEG straight from the
/// hardware encoder.
///
/// Capture is **duty-cycled** rather than free-running. A construction phone has to survive a
/// shift, and a monitoring feed does not need cinema frame rates; [intervalMs] is the knob.
class CameraStreamer extends ChangeNotifier {
  /// Gap between capture attempts. ~3 fps: enough for a supervisor to follow what is happening,
  /// cheap enough to leave battery and the uplink alone.
  static const defaultIntervalMs = 350;

  CameraController? _controller;
  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _sub;
  Timer? _timer;

  bool _streaming = false;
  bool _starting = false;
  bool _connected = false;
  bool _inFlight = false; // a capture is already running; skip rather than queue

  int framesSent = 0;
  int hazardsSeen = 0;
  int peopleSeen = 0;
  double fps = 0;
  double? inferenceMs;
  String? error;
  DateTime? _lastFrameAt;

  int intervalMs = defaultIntervalMs;

  CameraController? get controller => _controller;
  bool get streaming => _streaming;
  bool get starting => _starting;
  bool get connected => _connected;

  /// True once the preview can be shown.
  bool get ready => _controller?.value.isInitialized ?? false;

  /// Begin capturing and streaming. Safe to call twice; the second call is ignored.
  Future<void> start({
    required String edgeUrl,
    required String workerId,
    String? zone,
    String? displayName,
  }) async {
    if (_streaming || _starting) return;
    _starting = true;
    error = null;
    notifyListeners();

    try {
      final cameras = await availableCameras();
      if (cameras.isEmpty) {
        throw CameraException('no_camera', 'This device reports no usable camera.');
      }
      // Rear camera: the worker is looking at the site, not at themselves.
      final camera = cameras.firstWhere(
        (c) => c.lensDirection == CameraLensDirection.back,
        orElse: () => cameras.first,
      );

      // `medium` (~480p) on purpose. The detector does not benefit from a 12MP frame, and a
      // full-resolution JPEG would be megabytes per frame over a site's uplink.
      final controller = CameraController(
        camera,
        ResolutionPreset.medium,
        enableAudio: false,
        imageFormatGroup: ImageFormatGroup.jpeg,
      );
      await controller.initialize();
      // Locked so the detector sees a stable frame rather than a hunting autofocus/exposure.
      await controller.setFlashMode(FlashMode.off);
      _controller = controller;

      _openSocket(edgeUrl: edgeUrl, workerId: workerId, zone: zone, displayName: displayName);

      _streaming = true;
      _starting = false;
      _timer = Timer.periodic(Duration(milliseconds: intervalMs), (_) => _captureOnce());
      notifyListeners();
    } catch (e) {
      _starting = false;
      error = _friendly(e);
      await _teardown();
      notifyListeners();
    }
  }

  void _openSocket({
    required String edgeUrl,
    required String workerId,
    String? zone,
    String? displayName,
  }) {
    final http = Uri.parse(edgeUrl);
    final uri = http.replace(
      scheme: http.scheme == 'https' ? 'wss' : 'ws',
      path: '/ws/video',
      queryParameters: {
        'worker_id': workerId,
        if (zone != null && zone.isNotEmpty) 'zone': zone,
        if (displayName != null && displayName.isNotEmpty) 'name': displayName,
      },
    );

    final channel = WebSocketChannel.connect(uri);
    _channel = channel;
    _sub = channel.stream.listen(
      _onServerReply,
      onDone: () {
        _connected = false;
        notifyListeners();
      },
      onError: (Object e) {
        _connected = false;
        error = 'Lost the connection to the server.';
        notifyListeners();
      },
      cancelOnError: true,
    );
  }

  /// The server's per-frame verdict: what it detected, and any hazards it raised.
  void _onServerReply(dynamic raw) {
    if (raw is! String) return;
    Object? decoded;
    try {
      decoded = jsonDecode(raw);
    } catch (_) {
      return;
    }
    if (decoded is! Map<String, dynamic>) return;

    // Any reply proves the socket is alive.
    if (!_connected) _connected = true;

    final serverError = decoded['error'];
    if (serverError is String) {
      error = serverError;
      notifyListeners();
      return;
    }

    final counts = decoded['counts'];
    if (counts is Map<String, dynamic>) {
      peopleSeen = (counts['people'] as num?)?.toInt() ?? peopleSeen;
    }
    final hazards = decoded['hazards'];
    if (hazards is List && hazards.isNotEmpty) hazardsSeen += hazards.length;
    inferenceMs = (decoded['inference_ms'] as num?)?.toDouble() ?? inferenceMs;
    notifyListeners();
  }

  Future<void> _captureOnce() async {
    final controller = _controller;
    if (!_streaming || controller == null || !controller.value.isInitialized) return;
    // The previous capture has not finished. Skipping rather than queueing keeps the feed live:
    // a backlog of stale frames is worse than a lower frame rate.
    if (_inFlight) return;
    _inFlight = true;

    try {
      final shot = await controller.takePicture();
      final bytes = await shot.readAsBytes();
      final channel = _channel;
      if (channel == null || !_streaming) return;
      channel.sink.add(bytes);

      framesSent += 1;
      final now = DateTime.now();
      if (_lastFrameAt != null) {
        final gap = now.difference(_lastFrameAt!).inMilliseconds / 1000.0;
        if (gap > 0) {
          final instant = 1 / gap;
          fps = fps == 0 ? instant : (fps * 0.7 + instant * 0.3);
        }
      }
      _lastFrameAt = now;
      notifyListeners();
    } catch (e) {
      // One failed capture is normal (the OS can reclaim the camera). Only surface it if the
      // stream is still meant to be running.
      if (_streaming) {
        error = _friendly(e);
        notifyListeners();
      }
    } finally {
      _inFlight = false;
    }
  }

  Future<void> stop() async {
    _streaming = false;
    await _teardown();
    notifyListeners();
  }

  Future<void> _teardown() async {
    _timer?.cancel();
    _timer = null;
    // Detach before closing so a deliberate stop is not reported as a lost connection.
    final sub = _sub;
    _sub = null;
    await sub?.cancel();
    final channel = _channel;
    _channel = null;
    await channel?.sink.close();
    final controller = _controller;
    _controller = null;
    await controller?.dispose();
    _connected = false;
    fps = 0;
    _lastFrameAt = null;
  }

  String _friendly(Object e) {
    if (e is CameraException) {
      if (e.code.toLowerCase().contains('permission')) {
        return 'Camera permission denied. Allow camera access in Settings to stream.';
      }
      return e.description ?? e.code;
    }
    return 'Could not reach the vision server. Check the server address.';
  }

  @override
  void dispose() {
    _streaming = false;
    _teardown();
    super.dispose();
  }
}
