import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// One message pushed from the backend's broadcast hub.
class LiveFrame {
  final String topic;
  final String? zone;
  final double ts;
  final Map<String, dynamic> data;

  LiveFrame({required this.topic, required this.zone, required this.ts, required this.data});

  String? get alertId => data['alert_id'] as String?;
  String? get severity => data['severity'] as String?;

  /// The sentence to speak, authored by the backend for this audience.
  String? get speech => data['speech'] as String?;

  String? get message => data['message'] as String?;

  static LiveFrame? tryParse(String raw) {
    Object? parsed;
    try {
      parsed = jsonDecode(raw);
    } catch (_) {
      return null;
    }
    if (parsed is! Map<String, dynamic>) return null;
    final topic = parsed['topic'];
    // `pong` is a keep-alive answer, not site activity.
    if (topic is! String || topic == 'pong') return null;
    final data = parsed['data'];
    return LiveFrame(
      topic: topic,
      zone: parsed['zone'] as String?,
      ts: (parsed['ts'] as num?)?.toDouble() ?? 0,
      data: data is Map<String, dynamic> ? data : const {},
    );
  }
}

/// Live push from the backend, so a hazard reaches the worker in seconds rather than on the next
/// poll. Connects as `kind=device` with the worker's id and zone, which is what lets the hub
/// address this phone directly:
///
///   * `alert`    — the primary, second-person verdict about *this* worker. Only their own device
///                  receives it (the hub filters by `worker_id`).
///   * `advisory` — a downgraded warning about a colleague's hazard in the same zone.
///
/// Live push is an optimisation, never a requirement: every screen keeps its polling refresh and
/// uses [connected] only to show a degraded indicator. A site with dead zones is the normal case,
/// so dropping the socket must never cost the worker data they can still fetch.
class LiveFeed extends ChangeNotifier {
  static const _pingInterval = Duration(seconds: 25);
  static const _maxBackoff = Duration(seconds: 15);

  /// Newest first, capped — a phone has no use for an unbounded backlog.
  static const _bufferSize = 40;

  final _frames = <LiveFrame>[];
  final _random = Random();

  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _sub;
  Timer? _pingTimer;
  Timer? _reconnectTimer;

  String? _baseUrl;
  String? _workerId;
  String? _zone;

  bool _connected = false;
  bool _disposed = false;
  int _attempt = 0;

  /// Called for every frame received. Set by the app to drive speech.
  void Function(LiveFrame frame)? onFrame;

  bool get connected => _connected;
  List<LiveFrame> get frames => List.unmodifiable(_frames);
  LiveFrame? get last => _frames.isEmpty ? null : _frames.first;

  /// (Re)point the socket. Called on sign-in and whenever the worker's zone changes, since the
  /// hub scopes advisories by the zone the device registered.
  void connect({required String baseUrl, required String? workerId, String? zone}) {
    if (_disposed) return;
    final unchanged = _baseUrl == baseUrl && _workerId == workerId && _zone == zone;
    // Idempotent on purpose: this is called on every session change, and tearing down a socket
    // that is connected — or a backoff timer that is mid-retry — would turn a routine rebuild
    // into a reconnect loop that never settles.
    if (unchanged && (_channel != null || _reconnectTimer != null)) return;
    _baseUrl = baseUrl;
    _workerId = workerId;
    _zone = zone;
    _attempt = 0;
    _open();
  }

  void disconnect() {
    _teardown();
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _baseUrl = null;
    if (_connected) {
      _connected = false;
      notifyListeners();
    }
  }

  Uri? _uri() {
    final base = _baseUrl;
    if (base == null) return null;
    // The API base is http(s); the socket shares host and port but swaps scheme.
    final http = Uri.parse(base);
    final query = <String, String>{'kind': 'device'};
    if (_workerId != null && _workerId!.isNotEmpty) query['worker_id'] = _workerId!;
    if (_zone != null && _zone!.isNotEmpty) query['zone'] = _zone!;
    return http.replace(
      scheme: http.scheme == 'https' ? 'wss' : 'ws',
      path: '/ws',
      queryParameters: query,
    );
  }

  void _open() {
    if (_disposed) return;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _teardown();

    final uri = _uri();
    if (uri == null) return;

    try {
      final channel = WebSocketChannel.connect(uri);
      _channel = channel;
      _sub = channel.stream.listen(
        _onMessage,
        onDone: _scheduleReconnect,
        onError: (Object _) => _scheduleReconnect(),
        cancelOnError: true,
      );
      // `WebSocketChannel.connect` is lazy: the handshake is only attempted on first use, so
      // "connected" is confirmed by the first frame or a successful ping rather than assumed here.
      _pingTimer = Timer.periodic(_pingInterval, (_) => _ping());
      _ping();
    } catch (_) {
      _scheduleReconnect();
    }
  }

  void _ping() {
    try {
      _channel?.sink.add(jsonEncode({'type': 'ping'}));
    } catch (_) {
      _scheduleReconnect();
    }
  }

  void _onMessage(dynamic raw) {
    if (_disposed) return;
    if (!_connected) {
      // Any inbound traffic — including the server's `hello` and `pong` — proves the socket is up.
      _connected = true;
      _attempt = 0;
      notifyListeners();
    }
    if (raw is! String) return;
    final frame = LiveFrame.tryParse(raw);
    if (frame == null) return;

    _frames.insert(0, frame);
    if (_frames.length > _bufferSize) _frames.removeRange(_bufferSize, _frames.length);
    notifyListeners();
    onFrame?.call(frame);
  }

  void _teardown() {
    _pingTimer?.cancel();
    _pingTimer = null;
    final sub = _sub;
    _sub = null;
    final channel = _channel;
    _channel = null;
    // Detach the listener before closing, so a deliberate close does not schedule a reconnect.
    sub?.cancel();
    channel?.sink.close();
  }

  void _scheduleReconnect() {
    if (_disposed || _reconnectTimer != null || _baseUrl == null) return;
    _teardown();
    if (_connected) {
      _connected = false;
      notifyListeners();
    }
    _attempt += 1;
    // Exponential backoff with jitter, capped — a site-wide outage must not become a retry storm
    // against the backend from every phone at once.
    final backoffMs = min(
      _maxBackoff.inMilliseconds,
      500 * pow(2, min(_attempt - 1, 5)).toInt(),
    );
    _reconnectTimer = Timer(
      Duration(milliseconds: backoffMs + _random.nextInt(250)),
      _open,
    );
  }

  @override
  void dispose() {
    _disposed = true;
    _reconnectTimer?.cancel();
    _teardown();
    super.dispose();
  }
}
