import 'dart:typed_data';

import 'package:image_picker/image_picker.dart';

/// A single photo, chosen via the device camera or its photo gallery.
class PickedPhoto {
  final Uint8List bytes;
  final String filename;
  PickedPhoto(this.bytes, this.filename);
}

Future<PickedPhoto?> pickPhoto({required bool fromCamera}) async {
  final file = await ImagePicker().pickImage(
    source: fromCamera ? ImageSource.camera : ImageSource.gallery,
    maxWidth: 1600,
    imageQuality: 82,
  );
  if (file == null) return null;
  return PickedPhoto(await file.readAsBytes(), file.name);
}
