import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;

import 'models.dart';

/// Thin HTTP client for the FieldPilot backend's worker-facing REST API.
///
/// Holds the base URL and bearer token as plain fields rather than reading them from
/// storage per-call — [ApiClient] is created fresh by [Session] whenever either changes, so
/// there is never a stale-token call in flight.
///
/// Paths here are **server-relative and unprefixed** (`/auth/login`, not `/api/auth/login`).
/// Any prefix belongs in [baseUrl], which makes both deployments work from one code path:
///
///   * straight at the backend — `http://<host>:8100` → `http://<host>:8100/auth/login`
///   * through the dashboard's dev proxy — `http://<host>:3000/api`
///
/// This used to hardcode `/api`, which is the Next.js rewrite convention and 404s against the
/// backend itself. A phone should not need the web dev server running to talk to the API.
class ApiClient {
  final String baseUrl;
  final String? token;
  final Duration timeout;

  ApiClient({required this.baseUrl, this.token, this.timeout = const Duration(seconds: 20)});

  Uri _uri(String path, [Map<String, String>? query]) {
    final trimmed = baseUrl.endsWith('/') ? baseUrl.substring(0, baseUrl.length - 1) : baseUrl;
    return Uri.parse('$trimmed$path').replace(queryParameters: query);
  }

  Map<String, String> get _headers => {
        if (token != null) 'Authorization': 'Bearer $token',
      };

  Future<dynamic> _decode(http.Response response) async {
    if (response.statusCode >= 200 && response.statusCode < 300) {
      if (response.body.isEmpty) return null;
      return jsonDecode(response.body);
    }
    String detail = 'Request failed (${response.statusCode})';
    try {
      final body = jsonDecode(response.body);
      if (body is Map && body['detail'] != null) detail = body['detail'].toString();
    } catch (_) {
      // a non-JSON error body (a proxy's HTML page, a connection-reset page) — the status
      // code is still meaningful even when the body is not
    }
    throw ApiException(response.statusCode, detail);
  }

  Future<dynamic> _get(String path, [Map<String, String>? query]) async {
    final response =
        await http.get(_uri(path, query), headers: _headers).timeout(timeout);
    return _decode(response);
  }

  Future<dynamic> _postJson(String path, Map<String, dynamic> body) async {
    final response = await http
        .post(_uri(path),
            headers: {..._headers, 'Content-Type': 'application/json'},
            body: jsonEncode(body))
        .timeout(timeout);
    return _decode(response);
  }

  Future<dynamic> _postEmpty(String path) async {
    final response = await http.post(_uri(path), headers: _headers).timeout(timeout);
    return _decode(response);
  }

  Future<dynamic> _multipart(
    String path, {
    required Map<String, String> fields,
    Uint8List? imageBytes,
    String? imageFilename,
    Uint8List? audioBytes,
    String? audioFilename,
  }) async {
    final request = http.MultipartRequest('POST', _uri(path));
    request.headers.addAll(_headers);
    request.fields.addAll(fields);
    if (imageBytes != null) {
      request.files.add(http.MultipartFile.fromBytes(
        'image',
        imageBytes,
        filename: imageFilename ?? 'photo.jpg',
      ));
    }
    if (audioBytes != null) {
      request.files.add(http.MultipartFile.fromBytes(
        'audio',
        audioBytes,
        filename: audioFilename ?? 'voice.m4a',
      ));
    }
    final streamed = await request.send().timeout(timeout);
    final response = await http.Response.fromStream(streamed);
    return _decode(response);
  }

  // -- auth --------------------------------------------------------------

  Future<(String token, WorkerUser user)> login(String username, String password) async {
    final json = await _postJson('/auth/login', {
      'username': username,
      'password': password,
    }) as Map<String, dynamic>;
    return (json['token'] as String, WorkerUser.fromJson(json['user'] as Map<String, dynamic>));
  }

  Future<void> logout() async {
    try {
      await _postEmpty('/auth/logout');
    } catch (_) {
      // logging out locally must succeed even if the network call does not
    }
  }

  Future<WorkerUser> me() async =>
      WorkerUser.fromJson(await _get('/auth/me') as Map<String, dynamic>);

  // -- worker: alerts + zone ----------------------------------------------

  Future<List<Alert>> myAlerts({int limit = 100}) async {
    final json = await _get('/me/alerts', {'limit': '$limit'}) as Map<String, dynamic>;
    return (json['alerts'] as List).map((e) => Alert.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<Map<String, dynamic>> raiseAlert({
    required String eventType,
    required String severity,
    required String message,
    String? zone,
    Uint8List? imageBytes,
  }) async {
    return await _multipart(
      '/me/alerts',
      fields: {
        'event_type': eventType,
        'severity': severity,
        'message': message,
        'zone': ?zone,
      },
      imageBytes: imageBytes,
    ) as Map<String, dynamic>;
  }

  Future<ZoneOccupancy?> myZone() async {
    final json = await _get('/me/zone') as Map<String, dynamic>;
    final occ = json['occupancy'];
    return occ == null ? null : ZoneOccupancy.fromJson(occ as Map<String, dynamic>);
  }

  Future<List<ZoneInfo>> zones() async {
    final json = await _get('/zones') as Map<String, dynamic>;
    return (json['zones'] as List).map((e) => ZoneInfo.fromJson(e as Map<String, dynamic>)).toList();
  }

  /// Returns `(enteredAt, closedPreviousZoneId)`.
  Future<(double, String?)> enterZone(String zoneId) async {
    final json = await _postEmpty('/zones/$zoneId/enter') as Map<String, dynamic>;
    final closed = json['closed_previous'] as Map<String, dynamic>?;
    return ((json['entered_at'] as num?)?.toDouble() ?? 0, closed?['zone_id'] as String?);
  }

  Future<double?> leaveZone(String zoneId) async {
    final json = await _postEmpty('/zones/$zoneId/leave') as Map<String, dynamic>;
    return (json['duration_s'] as num?)?.toDouble();
  }

  // -- questions -----------------------------------------------------------

  Future<WorkerQuestion> ask({
    required String text,
    String? zone,
    Uint8List? imageBytes,
  }) async {
    final json = await _multipart(
      '/questions',
      fields: {'text': text, 'zone': ?zone},
      imageBytes: imageBytes,
    );
    return WorkerQuestion.fromJson(json as Map<String, dynamic>);
  }

  Future<List<WorkerQuestion>> myQuestions({int limit = 100}) async {
    final json = await _get('/questions', {'limit': '$limit'}) as Map<String, dynamic>;
    return (json['questions'] as List)
        .map((e) => WorkerQuestion.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<WorkerQuestion> question(String id) async =>
      WorkerQuestion.fromJson(await _get('/questions/$id') as Map<String, dynamic>);

  // -- direct messages with the site manager --------------------------------

  Future<List<DirectMessage>> myMessages() async {
    final json = await _get('/me/messages') as Map<String, dynamic>;
    return (json['messages'] as List)
        .map((e) => DirectMessage.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// `text` may be empty when there is a voice note, and vice versa; the backend rejects only
  /// the case where both are empty, so that check is left to it rather than duplicated here.
  Future<DirectMessage> sendMessage({String text = '', Uint8List? audioBytes, String? filename}) async {
    final json = await _multipart(
      '/me/messages',
      fields: {'text': text},
      audioBytes: audioBytes,
      audioFilename: filename,
    );
    return DirectMessage.fromJson(json as Map<String, dynamic>);
  }

  /// Marks the *manager's* messages in this thread read. The same path also serves a manager
  /// marking a worker's thread read; a worker calling it may only pass their own id, and the
  /// backend — not this client — is what enforces that with a 403.
  Future<void> markMessagesRead(String workerId) => _postEmpty('/messages/$workerId/read');

  Future<int> unreadMessages() async {
    final json = await _get('/messages/unread') as Map<String, dynamic>;
    return (json['unread'] as num?)?.toInt() ?? 0;
  }

  /// Resolves a `/img/...` or `/uploads/...` path from the API into an absolute URL, the way
  /// the alert image and question photo fields are served.
  String mediaUrl(String path) {
    if (path.startsWith('http://') || path.startsWith('https://')) return path;
    final trimmed = baseUrl.endsWith('/') ? baseUrl.substring(0, baseUrl.length - 1) : baseUrl;
    final rel = path.startsWith('/') ? path : '/$path';
    return '$trimmed$rel';
  }
}
