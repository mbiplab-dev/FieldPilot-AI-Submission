import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/session.dart';
import '../widgets/common.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _formKey = GlobalKey<FormState>();
  final _username = TextEditingController();
  final _password = TextEditingController();
  bool _submitting = false;
  bool _showServerField = false;
  String? _error;
  late final TextEditingController _server;

  @override
  void initState() {
    super.initState();
    _server = TextEditingController(text: context.read<Session>().serverUrl);
  }

  @override
  void dispose() {
    _username.dispose();
    _password.dispose();
    _server.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!(_formKey.currentState?.validate() ?? false)) return;
    setState(() {
      _submitting = true;
      _error = null;
    });
    final session = context.read<Session>();
    try {
      await session.setServer(_server.text);
      await session.login(_username.text.trim(), _password.text);
    } catch (e) {
      setState(() => _error = friendlyError(e));
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  void _fill(String user, String pass) {
    _username.text = user;
    _password.text = pass;
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 380),
              child: Form(
                key: _formKey,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Center(
                      child: Container(
                        width: 64,
                        height: 64,
                        decoration: BoxDecoration(
                          color: theme.colorScheme.primary,
                          borderRadius: BorderRadius.circular(16),
                        ),
                        child: Center(
                          child: Text('FP',
                              style: TextStyle(
                                  // `onPrimary`, not a literal white: Material 3's dark scheme
                                  // makes `primary` itself a light colour, and white-on-light-blue
                                  // is close to unreadable — `onPrimary` is chosen to contrast with
                                  // whatever `primary` resolves to in either mode.
                                  color: theme.colorScheme.onPrimary,
                                  fontWeight: FontWeight.w800,
                                  fontSize: 22)),
                        ),
                      ),
                    ),
                    const SizedBox(height: 16),
                    Text('FieldPilot Worker',
                        style: theme.textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w700),
                        textAlign: TextAlign.center),
                    const SizedBox(height: 4),
                    Text('Sign in to see your alerts and check into a zone',
                        style: theme.textTheme.bodyMedium
                            ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
                        textAlign: TextAlign.center),
                    const SizedBox(height: 28),
                    TextFormField(
                      controller: _username,
                      textInputAction: TextInputAction.next,
                      autofillHints: const [AutofillHints.username],
                      decoration: const InputDecoration(
                        labelText: 'Username',
                        prefixIcon: Icon(Icons.person_outline),
                        border: OutlineInputBorder(),
                      ),
                      validator: (v) =>
                          (v == null || v.trim().isEmpty) ? 'Enter your username' : null,
                    ),
                    const SizedBox(height: 12),
                    TextFormField(
                      controller: _password,
                      obscureText: true,
                      textInputAction: TextInputAction.done,
                      autofillHints: const [AutofillHints.password],
                      decoration: const InputDecoration(
                        labelText: 'Password',
                        prefixIcon: Icon(Icons.lock_outline),
                        border: OutlineInputBorder(),
                      ),
                      validator: (v) => (v == null || v.isEmpty) ? 'Enter your password' : null,
                      onFieldSubmitted: (_) => _submit(),
                    ),
                    if (_error != null) ...[
                      const SizedBox(height: 12),
                      Container(
                        padding: const EdgeInsets.all(10),
                        decoration: BoxDecoration(
                          color: theme.colorScheme.errorContainer,
                          borderRadius: BorderRadius.circular(8),
                        ),
                        child: Row(
                          children: [
                            Icon(Icons.error_outline, color: theme.colorScheme.onErrorContainer, size: 18),
                            const SizedBox(width: 8),
                            Expanded(
                              child: Text(_error!,
                                  style: TextStyle(color: theme.colorScheme.onErrorContainer)),
                            ),
                          ],
                        ),
                      ),
                    ],
                    const SizedBox(height: 20),
                    FilledButton(
                      onPressed: _submitting ? null : _submit,
                      style: FilledButton.styleFrom(
                        minimumSize: const Size.fromHeight(52),
                      ),
                      child: _submitting
                          ? SizedBox(
                              width: 20,
                              height: 20,
                              child: CircularProgressIndicator(
                                  strokeWidth: 2.4, color: theme.colorScheme.onPrimary),
                            )
                          : const Text('Sign in'),
                    ),
                    const SizedBox(height: 8),
                    TextButton(
                      onPressed: () => setState(() => _showServerField = !_showServerField),
                      child: Text(_showServerField ? 'Hide server address' : 'Server settings'),
                    ),
                    if (_showServerField) ...[
                      TextFormField(
                        controller: _server,
                        decoration: const InputDecoration(
                          labelText: 'FieldPilot server address',
                          hintText: 'http://192.168.1.20:8100',
                          prefixIcon: Icon(Icons.dns_outlined),
                          border: OutlineInputBorder(),
                        ),
                        keyboardType: TextInputType.url,
                      ),
                      const SizedBox(height: 4),
                      Text(
                        'This is the address of the site manager\'s FieldPilot server, not this phone.',
                        style: theme.textTheme.bodySmall
                            ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
                      ),
                    ],
                    const SizedBox(height: 20),
                    Container(
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: theme.colorScheme.surfaceContainerHighest,
                        borderRadius: BorderRadius.circular(10),
                      ),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text('Demo accounts', style: theme.textTheme.labelLarge),
                          const SizedBox(height: 6),
                          _DemoAccountRow(
                            label: 'Worker · worker1 / worker123',
                            onTap: () => _fill('worker1', 'worker123'),
                          ),
                          _DemoAccountRow(
                            label: 'Worker · worker2 / worker123',
                            onTap: () => _fill('worker2', 'worker123'),
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _DemoAccountRow extends StatelessWidget {
  final String label;
  final VoidCallback onTap;
  const _DemoAccountRow({required this.label, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(6),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Row(
          children: [
            const Icon(Icons.touch_app_outlined, size: 16),
            const SizedBox(width: 6),
            Expanded(child: Text(label, style: Theme.of(context).textTheme.bodySmall)),
          ],
        ),
      ),
    );
  }
}
