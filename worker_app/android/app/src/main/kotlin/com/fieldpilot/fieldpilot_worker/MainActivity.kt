package com.fieldpilot.fieldpilot_worker

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity(), RecognitionListener {
    private val channelName = "fieldpilot/speech"
    private val audioPermissionRequest = 8011
    private var recognizer: SpeechRecognizer? = null
    private var pendingResult: MethodChannel.Result? = null
    private var startAfterPermission = false

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, channelName)
            .setMethodCallHandler(::handleSpeechCall)
    }

    private fun handleSpeechCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "available" -> result.success(SpeechRecognizer.isRecognitionAvailable(this))
            "listenOnce" -> {
                if (pendingResult != null) {
                    result.error("busy", "Speech recognition is already listening.", null)
                    return
                }
                pendingResult = result
                if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) !=
                    PackageManager.PERMISSION_GRANTED
                ) {
                    startAfterPermission = true
                    ActivityCompat.requestPermissions(
                        this, arrayOf(Manifest.permission.RECORD_AUDIO), audioPermissionRequest
                    )
                } else {
                    startListening()
                }
            }
            "cancel" -> {
                recognizer?.cancel()
                finishError("cancelled", "Listening cancelled.")
                result.success(null)
            }
            else -> result.notImplemented()
        }
    }

    private fun startListening() {
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            finishError("unavailable", "No speech recognition service is installed.")
            return
        }
        recognizer?.destroy()
        recognizer = if (
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
            SpeechRecognizer.isOnDeviceRecognitionAvailable(this)
        ) {
            SpeechRecognizer.createOnDeviceSpeechRecognizer(this)
        } else {
            SpeechRecognizer.createSpeechRecognizer(this)
        }
        recognizer?.setRecognitionListener(this)
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-IN")
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_PREFERENCE, "en-IN")
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 3)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 900L)
        }
        recognizer?.startListening(intent)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != audioPermissionRequest || !startAfterPermission) return
        startAfterPermission = false
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
            startListening()
        } else {
            finishError("permission_denied", "Microphone permission is required for voice commands.")
        }
    }

    override fun onResults(results: Bundle?) {
        val matches = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION).orEmpty()
        val transcript = matches.firstOrNull()?.trim().orEmpty()
        if (transcript.isEmpty()) {
            finishError("no_match", "No speech was understood. Try again closer to the phone.")
        } else {
            val result = pendingResult
            pendingResult = null
            recognizer?.destroy()
            recognizer = null
            result?.success(mapOf("transcript" to transcript, "alternatives" to matches))
        }
    }

    override fun onError(error: Int) {
        val message = when (error) {
            SpeechRecognizer.ERROR_AUDIO -> "The microphone could not capture audio."
            SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "Microphone permission was denied."
            SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT ->
                "Speech recognition is offline on this device. Install an offline language pack."
            SpeechRecognizer.ERROR_NO_MATCH -> "No speech was understood. Say the wake phrase and command clearly."
            SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "Speech recognition is busy. Try again."
            SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "No speech was heard."
            else -> "Speech recognition stopped (code $error)."
        }
        finishError("recognition_error", message)
    }

    private fun finishError(code: String, message: String) {
        val result = pendingResult
        pendingResult = null
        recognizer?.destroy()
        recognizer = null
        result?.error(code, message, null)
    }

    override fun onDestroy() {
        recognizer?.destroy()
        recognizer = null
        pendingResult = null
        super.onDestroy()
    }

    override fun onReadyForSpeech(params: Bundle?) = Unit
    override fun onBeginningOfSpeech() = Unit
    override fun onRmsChanged(rmsdB: Float) = Unit
    override fun onBufferReceived(buffer: ByteArray?) = Unit
    override fun onEndOfSpeech() = Unit
    override fun onPartialResults(partialResults: Bundle?) = Unit
    override fun onEvent(eventType: Int, params: Bundle?) = Unit
}
