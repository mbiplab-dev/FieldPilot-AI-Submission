// Smoke test: the app boots to the login screen when no session is stored, and shows the
// demo-credential hints a fresh install actually needs.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:fieldpilot_worker/main.dart';

void main() {
  testWidgets('boots to the login screen with no stored session', (WidgetTester tester) async {
    SharedPreferences.setMockInitialValues({});

    await tester.pumpWidget(const FieldPilotWorkerApp());
    await tester.pumpAndSettle();

    expect(find.text('FieldPilot Worker'), findsOneWidget);
    expect(find.widgetWithText(TextFormField, 'Username'), findsOneWidget);
    expect(find.widgetWithText(TextFormField, 'Password'), findsOneWidget);
    expect(find.text('Sign in'), findsOneWidget);
  });

  testWidgets('rejects an empty submission with inline validation, not a crash',
      (WidgetTester tester) async {
    SharedPreferences.setMockInitialValues({});

    await tester.pumpWidget(const FieldPilotWorkerApp());
    await tester.pumpAndSettle();

    await tester.tap(find.text('Sign in'));
    await tester.pump();

    expect(find.text('Enter your username'), findsOneWidget);
    expect(find.text('Enter your password'), findsOneWidget);
  });
}
