import 'dart:typed_data';

import 'package:fieldpilot_worker/core/camera_stream.dart';
import 'package:flutter_test/flutter_test.dart';

/// The header is the only new wire contract this change adds: the edge server
/// (`fieldpilot/display/server.py::decode_frame`) tells a raw phone frame apart from the
/// browser page's JPEG by these exact bytes. Getting the field order or endianness wrong here
/// would not throw on either side — it would silently shear every frame the phone sends — so it
/// is worth pinning byte-for-byte without needing a real camera.
void main() {
  group('buildRawFramePayload', () {
    test('writes the FPR1 magic, big-endian geometry, then the plane bytes verbatim', () {
      final plane = Uint8List.fromList([1, 2, 3, 4, 5]);
      final payload = buildRawFramePayload(
        width: 640,
        height: 480,
        stride: 640,
        planeBytes: plane,
      );

      expect(payload.length, 12 + plane.length);
      expect(payload.sublist(0, 4), 'FPR1'.codeUnits);

      final header = ByteData.sublistView(payload, 0, 12);
      expect(header.getUint16(4, Endian.big), 640);
      expect(header.getUint16(6, Endian.big), 480);
      expect(header.getUint16(8, Endian.big), 640);
      expect(header.getUint16(10, Endian.big), nv21FormatCode);
      expect(payload.sublist(12), plane);
    });

    test('reports the true row stride even when it exceeds width, so the server can de-pad it', () {
      // A camera that pads every row for hardware alignment must not have that padding hidden
      // from the server — decode_frame needs the honest stride to slice it back off.
      final payload = buildRawFramePayload(
        width: 640,
        height: 480,
        stride: 704,
        planeBytes: Uint8List(0),
      );

      final header = ByteData.sublistView(payload, 0, 12);
      expect(header.getUint16(4, Endian.big), 640);
      expect(header.getUint16(8, Endian.big), 704);
    });

    test('defaults to the NV21 format code', () {
      final payload = buildRawFramePayload(
        width: 2,
        height: 2,
        stride: 2,
        planeBytes: Uint8List(0),
      );
      final header = ByteData.sublistView(payload, 0, 12);
      expect(header.getUint16(10, Endian.big), 1);
      expect(nv21FormatCode, 1);
    });
  });
}
