import 'package:fieldpilot_worker/core/session.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Camera frames go to the vision edge server, not the REST backend, so the app derives one
/// address from the other. Getting this wrong is a silent "camera won't connect".
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() => SharedPreferences.setMockInitialValues({}));

  Future<Session> sessionOn(String server) async {
    final s = Session();
    await s.setServer(server);
    return s;
  }

  test('the edge address keeps the host and swaps the port', () async {
    expect((await sessionOn('http://10.42.243.71:8100')).edgeUrl,
        'http://10.42.243.71:8000');
  });

  test('works for the USB-tunnelled localhost setup', () async {
    expect((await sessionOn('http://localhost:8100')).edgeUrl, 'http://localhost:8000');
  });

  test('a trailing path on the backend URL is not carried onto the edge', () async {
    // Someone pointing the app at the dashboard proxy (`:3000/api`) must still reach the edge.
    expect((await sessionOn('http://10.0.2.2:3000/api')).edgeUrl, 'http://10.0.2.2:8000');
  });

  test('https is preserved so the socket can upgrade to wss', () async {
    expect((await sessionOn('https://site.example:8100')).edgeUrl,
        'https://site.example:8000');
  });

  test('an unparseable server address degrades instead of crashing the camera tab', () async {
    expect((await sessionOn('not a url')).edgeUrl, 'http://localhost:8000');
  });

  test('the current zone is published so a feed can be labelled with it', () async {
    final s = await sessionOn('http://localhost:8100');
    expect(s.currentZoneId, isNull);

    s.setCurrentZone('zone-a');
    expect(s.currentZoneId, 'zone-a');

    var notified = 0;
    s.addListener(() => notified++);
    s.setCurrentZone('zone-a');
    expect(notified, 0, reason: 'an unchanged zone must not churn listeners');
    s.setCurrentZone('zone-b');
    expect(notified, 1);
  });
}
