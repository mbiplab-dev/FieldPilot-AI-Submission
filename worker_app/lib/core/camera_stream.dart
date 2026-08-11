import 'dart:async';
import 'dart:convert';

import 'package:camera/camera.dart';
import 'package:flutter/foundation.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// The FPR1 wire format's format code for NV21 — must match `_RAW_FORMAT_NV21` in
/// `fieldpilot/display/server.py`. The header carries this explicitly (rather than the socket
/// assuming a fixed format) so the wire protocol can grow a second raw pixel format later without
/// a breaking change.
const int nv21FormatCode = 1;

/// Builds the raw-frame payload the edge server (`fieldpilot/display/server.py::decode_frame`)
/// expects on `/ws/video`: a 12-byte header, then the plane bytes verbatim.
///
///     magic(4)=`FPR1`  width(2)  height(2)  stride(2)  format(2)     — all big-endian
///
/// Distinguishing this from the browser page's JPEG frames is by magic, not length, so `stride`
/// is reported honestly even when it exceeds `width` (a camera padding every row for hardware
/// alignment) — the server, not this function, is responsible for slicing the padding back off.
@visibleForTesting
Uint8List buildRawFramePayload({
  required int width,
  required int height,
  required int stride,
  required Uint8List planeBytes,
  int format = nv21FormatCode,
}) {
  final header = ByteData(12);
  header.buffer.asUint8List().setRange(0, 4, ascii.encode('FPR1'));
  header.setUint16(4, width, Endian.big);
  header.setUint16(6, height, Endian.big);
  header.setUint16(8, stride, Endian.big);
  header.setUint16(10, format, Endian.big);

  final payload = Uint8List(12 + planeBytes.length);
  payload.setRange(0, 12, header.buffer.asUint8List());
  payload.setRange(12, payload.length, planeBytes);
  return payload;
}

/// Streams the phone's camera to the server, which does all the AI.
///
/// The phone is deliberately a dumb capture device: it ships raw camera planes over a WebSocket
/// to the edge server's `/ws/video`, and the server runs YOLO, the pose model and every safety
/// detector. Nothing is inferred on the handset. That keeps the app small, keeps detection
/// consistent with every other ingest path, and means a mid-range phone is enough to be a site
/// camera.
///
/// Frames are captured with [CameraController.startImageStream], not [CameraController.takePicture].
/// `takePicture` is a full still capture — autofocus, metering and a hardware JPEG encode — and
/// measured at 300-700 ms per shot on real hardware, which caps the feed at roughly 1.4 fps no
/// matter how tight the capture loop is. `startImageStream` instead hands back the sensor's raw
/// NV21 planes as they arrive, so the phone can push frames as fast as [targetFps] asks for
/// without paying a still-capture's cost each time. The JPEG *encode* moved to the server too
/// (that was the other half of the cost) — the server already had to decode a JPEG for the
/// browser-camera page, so decoding raw planes instead is a small addition, not a new dependency.
///
/// Sending is duty-cycled rather than free-running: the sensor can produce frames faster than
/// [targetFps], and those are dropped, not queued — a backlog of stale frames is worse for a live
/// safety feed than a lower rate (the same reasoning the old `_inFlight` guard encoded).
///
/// Bandwidth is the trade-off worth being honest about: NV21 at the `medium` preset (~480x640) is
/// about 460 KB/frame uncompressed, so streaming raw planes at 10 fps is roughly 4.6 MB/s. That is
/// nothing on the LAN this app targets, but it would be a real cost on a thin site uplink — a
/// future revision that needs to run over such a link should re-introduce on-device compression
/// rather than raise [targetFps] first.
class CameraStreamer extends ChangeNotifier {
  /// Frames captured faster than this are dropped rather than sent. 10 fps is enough for the
  /// detector to track a fall or a missing hardhat without drowning a site uplink in raw planes.
  static const defaultTargetFps = 10;

  CameraController? _controller;
  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _sub;

  bool _streaming = false;
  bool _starting = false;
  bool _connected = false;

  int framesSent = 0;
  int hazardsSeen = 0;
  int peopleSeen = 0;
  double fps = 0;
  double? inferenceMs;
  String? error;
  DateTime? _lastFrameAt;

  /// The worker's own intent, independent of whether the hardware is actually open right now.
  /// Set by [start]/[stop]; read by [resumeFromBackground] so a foreground transition only
  /// reopens the camera the worker had asked for, never one they had deliberately stopped.
  bool _wantOn = false;

  int targetFps = defaultTargetFps;

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
    if (workerId.isEmpty) {
      // Surfaced on the Camera tab rather than thrown: a missing worker id is a site-manager
      // configuration problem to fix, not a transient camera failure worth retrying on its own.
      // This used to be a check the tab made before calling start() at all; it moved here so the
      // auto-start path on sign-in (which has no snackbar to show) fails just as visibly.
      error = 'This account has no worker id, so a feed could not be labelled. Ask your site manager.';
      notifyListeners();
      return;
    }
    _wantOn = true;
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

      // `medium` (~480p) on purpose. The detector does not benefit from a 12MP frame, and every
      // extra pixel here is bytes the phone has to push over the socket at `targetFps`.
      final controller = CameraController(
        camera,
        ResolutionPreset.medium,
        enableAudio: false,
        imageFormatGroup: ImageFormatGroup.nv21,
      );
      await controller.initialize();
      // Locked so the detector sees a stable frame rather than a hunting autofocus/exposure.
      await controller.setFlashMode(FlashMode.off);
      _controller = controller;

      _openSocket(edgeUrl: edgeUrl, workerId: workerId, zone: zone, displayName: displayName);

      _streaming = true;
      _starting = false;
      await controller.startImageStream(_onImage);
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

  /// One frame from `startImageStream`. Runs on every frame the sensor produces, which is
  /// typically much faster than [targetFps] — the gap check below is what turns that into a
  /// bounded-rate feed instead of flooding the socket with more frames than the server (or the
  /// uplink) can keep up with.
  void _onImage(CameraImage image) {
    if (!_streaming) return;

    final now = DateTime.now();
    final minGapMs = (1000 / targetFps).round();
    if (_lastFrameAt != null && now.difference(_lastFrameAt!).inMilliseconds < minGapMs) {
      return;
    }

    final channel = _channel;
    if (channel == null) return;

    try {
      final plane = image.planes.first;
      final payload = buildRawFramePayload(
        width: image.width,
        height: image.height,
        stride: plane.bytesPerRow,
        planeBytes: plane.bytes,
      );
      channel.sink.add(payload);

      framesSent += 1;
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
      // One failed send is normal (the socket can drop mid-frame). Only surface it if the stream
      // is still meant to be running.
      if (_streaming) {
        error = _friendly(e);
        notifyListeners();
      }
    }
  }

  Future<void> stop() async {
    _wantOn = false;
    _streaming = false;
    await _teardown();
    notifyListeners();
  }

  /// Releases the camera without touching [_wantOn], so this reads as "paused", not "stopped".
  ///
  /// Called on `didChangeAppLifecycleState` rather than left for [dispose] to sort out: Android
  /// can revoke a backgrounded app's camera access out from under it, and a [CameraController]
  /// that thinks it is still streaming when the surface goes away is the classic way this crashes.
  /// Tearing it down proactively on backgrounding — the same [_teardown] a deliberate [stop] uses —
  /// means there is never a controller alive for Android to pull the rug out from under.
  Future<void> pauseForBackground() async {
    if (!_streaming) return;
    _streaming = false;
    await _teardown();
    notifyListeners();
  }

  /// Reopens the camera after [pauseForBackground] — but only if the worker had it on before the
  /// app was backgrounded. Without the [_wantOn] check, a worker who deliberately pressed STOP and
  /// then locked their phone would find the camera back on when they unlocked it, which is exactly
  /// the surveillance-without-consent failure mode this feature has to avoid.
  Future<void> resumeFromBackground({
    required String edgeUrl,
    required String workerId,
    String? zone,
    String? displayName,
  }) async {
    if (!_wantOn) return;
    await start(edgeUrl: edgeUrl, workerId: workerId, zone: zone, displayName: displayName);
  }

  /// Zeroes the running counters and any stale error message.
  ///
  /// This streamer now lives for the app's process lifetime (provided above `_SignedIn` in
  /// `main.dart`, so the camera can start the instant a session exists rather than only once the
  /// Camera tab is built) instead of being recreated per sign-in. Without an explicit reset, a
  /// second worker signing in on the same phone would inherit the first worker's frame count and
  /// last error on screen.
  void reset() {
    framesSent = 0;
    hazardsSeen = 0;
    peopleSeen = 0;
    fps = 0;
    inferenceMs = null;
    error = null;
    notifyListeners();
  }

  Future<void> _teardown() async {
    final controller = _controller;
    _controller = null;
    if (controller != null && controller.value.isStreamingImages) {
      await controller.stopImageStream();
    }
    // Detach before closing so a deliberate stop is not reported as a lost connection.
    final sub = _sub;
    _sub = null;
    await sub?.cancel();
    final channel = _channel;
    _channel = null;
    await channel?.sink.close();
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
