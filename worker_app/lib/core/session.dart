import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'api_client.dart';
import 'models.dart';

/// The one piece of global app state: who is signed in, and to which server.
///
/// Demo-grade persistence, matching the web dashboard's own admission in
/// `frontend/src/lib/session.ts`: the token lives in local device storage. Fine for a
/// single-site demo; not a pattern to carry into a multi-tenant deployment.
class Session extends ChangeNotifier {
  static const _tokenKey = 'fieldpilot.token';
  static const _serverKey = 'fieldpilot.server';

  /// Where the backend lives. Editable on the login screen, because there is no address that is
  /// right everywhere:
  ///
  ///   * `http://localhost:8100` works over a USB cable after
  ///     `adb reverse tcp:8100 tcp:8100`, which is the most reliable option during development —
  ///     it survives the laptop's LAN address changing and needs no shared Wi-Fi.
  ///   * `http://<laptop-lan-ip>:8100` for a phone on the same Wi-Fi.
  ///
  /// `localhost` is the default precisely because a hardcoded LAN IP goes stale the moment DHCP
  /// reassigns it, which is a confusing "could not reach the server" for something that is not
  /// actually broken.
  static const defaultServer = 'http://localhost:8100';

  String serverUrl = defaultServer;
  String? _token;
  WorkerUser? user;
  bool loading = true; // true until the stored token (if any) has been checked

  /// The zone this worker is currently checked into, or null.
  ///
  /// Held here rather than only inside the Zone tab because the live socket is scoped by zone —
  /// the hub decides which colleagues' advisories reach this phone from the zone it registered.
  /// Keeping it in the session is what lets the socket follow the worker around the site.
  String? currentZoneId;

  ApiClient get api => ApiClient(baseUrl: serverUrl, token: _token);

  /// The bearer token, for the one caller that cannot send it as a header: [LiveFeed]'s
  /// WebSocket handshake, which has to pass it as `?token=...` instead.
  String? get token => _token;

  bool get isSignedIn => _token != null && user != null;

  Future<void> restore() async {
    final prefs = await SharedPreferences.getInstance();
    serverUrl = prefs.getString(_serverKey) ?? defaultServer;
    final stored = prefs.getString(_tokenKey);
    if (stored != null) {
      _token = stored;
      try {
        user = await api.me();
      } catch (_) {
        // the stored token is stale/expired — fall through to signed-out rather than looping
        _token = null;
        await prefs.remove(_tokenKey);
      }
    }
    loading = false;
    notifyListeners();
  }

  /// Where the vision edge server listens. Camera frames go here rather than to the REST backend:
  /// the edge is the process holding the loaded detector weights, and it already accepts JPEG
  /// frames on `/ws/video` from the browser-camera page.
  ///
  /// Derived from [serverUrl] by swapping the port, because in every deployment so far the two
  /// run on the same host. Falls back to the raw host if the URL cannot be parsed.
  static const edgePort = 8000;

  String get edgeUrl {
    final uri = Uri.tryParse(serverUrl);
    if (uri == null || uri.host.isEmpty) return 'http://localhost:$edgePort';
    return uri.replace(port: edgePort, path: '').toString();
  }

  /// Reported by the Zone tab after every check-in/out, so the live socket can re-scope.
  void setCurrentZone(String? zoneId) {
    if (currentZoneId == zoneId) return;
    currentZoneId = zoneId;
    notifyListeners();
  }

  Future<void> setServer(String url) async {
    serverUrl = url.trim();
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_serverKey, serverUrl);
    notifyListeners();
  }

  Future<void> login(String username, String password) async {
    final (token, loggedInUser) = await ApiClient(baseUrl: serverUrl).login(username, password);
    _token = token;
    user = loggedInUser;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_tokenKey, token);
    notifyListeners();
  }

  Future<void> logout() async {
    final client = api;
    _token = null;
    user = null;
    currentZoneId = null;
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_tokenKey);
    notifyListeners();
    await client.logout();
  }

  /// Called by any screen that gets a 401 mid-session (the token expired server-side).
  Future<void> forceSignOut() async {
    if (_token == null) return;
    _token = null;
    user = null;
    currentZoneId = null;
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_tokenKey);
    notifyListeners();
  }
}
