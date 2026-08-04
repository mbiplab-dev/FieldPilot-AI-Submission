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

  /// LAN address of the FieldPilot backend. A worker's phone is not the same device as the
  /// server, so `localhost` is wrong for anyone but a desktop test build — this is deliberately
  /// editable on the login screen rather than hard-coded.
  static const defaultServer = 'http://10.44.51.32:8100';

  String serverUrl = defaultServer;
  String? _token;
  WorkerUser? user;
  bool loading = true; // true until the stored token (if any) has been checked

  ApiClient get api => ApiClient(baseUrl: serverUrl, token: _token);

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
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_tokenKey);
    notifyListeners();
  }
}
