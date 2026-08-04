/// Wire models for the FieldPilot backend's worker-facing API.
///
/// Kept intentionally plain (no codegen) — the backend contract is small and stable enough
/// that hand-written `fromJson` is clearer than a build step, and it keeps the app buildable
/// without `build_runner`.
library;

class ApiException implements Exception {
  final int statusCode;
  final String message;
  ApiException(this.statusCode, this.message);

  /// True for an expired/invalid session — callers use this to force a re-login rather than
  /// showing a raw error banner over a screen the user can no longer use.
  bool get isAuthFailure => statusCode == 401;

  @override
  String toString() => message;
}

class WorkerUser {
  final String userId;
  final String username;
  final String displayName;
  final String role; // "worker" | "site_manager"
  final String? workerId;

  WorkerUser({
    required this.userId,
    required this.username,
    required this.displayName,
    required this.role,
    required this.workerId,
  });

  bool get isWorker => role == 'worker';

  factory WorkerUser.fromJson(Map<String, dynamic> json) => WorkerUser(
        userId: json['user_id'] as String,
        username: json['username'] as String,
        displayName: (json['display_name'] as String?)?.trim().isNotEmpty == true
            ? json['display_name'] as String
            : json['username'] as String,
        role: json['role'] as String,
        workerId: json['worker_id'] as String?,
      );
}

class Alert {
  final String alertId;
  final String eventType;
  final String? workerId;
  final String? zone;
  final String severity; // low | medium | high | critical
  final String state; // NEW | ACTIVE | RESOLVED | SUPPRESSED
  final int hitCount;
  final double lastSeen;
  final String? message;
  final String? imageUrl;

  Alert({
    required this.alertId,
    required this.eventType,
    required this.workerId,
    required this.zone,
    required this.severity,
    required this.state,
    required this.hitCount,
    required this.lastSeen,
    required this.message,
    required this.imageUrl,
  });

  bool get isActive => state == 'NEW' || state == 'ACTIVE';

  factory Alert.fromJson(Map<String, dynamic> json) => Alert(
        alertId: json['alert_id'] as String,
        eventType: json['event_type'] as String,
        workerId: json['worker_id'] as String?,
        zone: json['zone'] as String?,
        severity: (json['severity'] as String?) ?? 'medium',
        state: (json['state'] as String?) ?? 'NEW',
        hitCount: (json['hit_count'] as num?)?.toInt() ?? 1,
        lastSeen: (json['last_seen'] as num?)?.toDouble() ?? 0,
        message: json['message'] as String?,
        imageUrl: json['image_url'] as String?,
      );
}

class ZoneInfo {
  final String zoneId;
  final String name;
  final String hazardLevel; // low | medium | high
  final bool danger;
  final bool active;
  final String description;

  ZoneInfo({
    required this.zoneId,
    required this.name,
    required this.hazardLevel,
    required this.danger,
    required this.active,
    required this.description,
  });

  factory ZoneInfo.fromJson(Map<String, dynamic> json) => ZoneInfo(
        zoneId: json['zone_id'] as String,
        name: (json['name'] as String?) ?? json['zone_id'] as String,
        hazardLevel: (json['hazard_level'] as String?) ?? 'medium',
        danger: json['danger'] as bool? ?? false,
        active: json['active'] as bool? ?? true,
        description: (json['description'] as String?) ?? '',
      );
}

class ZoneOccupancy {
  final String zoneId;
  final String? zoneName;
  final double enteredAt;

  ZoneOccupancy({required this.zoneId, required this.zoneName, required this.enteredAt});

  factory ZoneOccupancy.fromJson(Map<String, dynamic> json) => ZoneOccupancy(
        zoneId: json['zone_id'] as String,
        zoneName: json['zone_name'] as String?,
        enteredAt: (json['entered_at'] as num?)?.toDouble() ?? 0,
      );
}

class Citation {
  final String citation;
  final String? clause;
  final String? source;

  Citation({required this.citation, required this.clause, required this.source});

  factory Citation.fromJson(Map<String, dynamic> json) => Citation(
        citation: (json['citation'] as String?) ?? '',
        clause: json['clause'] as String?,
        source: json['source'] as String?,
      );
}

class WorkerQuestion {
  final String questionId;
  final String? zone;
  final String text;
  final String? imageUrl;
  final String status; // pending | answered | closed
  final String? llmAnswer;
  final bool? llmGrounded;
  final List<Citation> citations;
  final String? managerReply;
  final double createdAt;
  final double? repliedAt;

  WorkerQuestion({
    required this.questionId,
    required this.zone,
    required this.text,
    required this.imageUrl,
    required this.status,
    required this.llmAnswer,
    required this.llmGrounded,
    required this.citations,
    required this.managerReply,
    required this.createdAt,
    required this.repliedAt,
  });

  bool get hasManagerReply => managerReply != null && managerReply!.trim().isNotEmpty;

  factory WorkerQuestion.fromJson(Map<String, dynamic> json) => WorkerQuestion(
        questionId: json['question_id'] as String,
        zone: json['zone'] as String?,
        text: (json['text'] as String?) ?? '',
        imageUrl: json['image_url'] as String?,
        status: (json['status'] as String?) ?? 'pending',
        llmAnswer: json['llm_answer'] as String?,
        llmGrounded: json['llm_grounded'] as bool?,
        citations: ((json['citations'] as List?) ?? const [])
            .map((e) => Citation.fromJson(e as Map<String, dynamic>))
            .toList(),
        managerReply: json['manager_reply'] as String?,
        createdAt: (json['created_at'] as num?)?.toDouble() ?? 0,
        repliedAt: (json['replied_at'] as num?)?.toDouble(),
      );
}

/// Result of a POST /me/alerts or /zones/{id}/enter — small ad-hoc shapes that don't earn a
/// full model, kept here as typed maps via extension getters would be overkill; plain dynamic
/// access at the call site is fine for a one-off response.
