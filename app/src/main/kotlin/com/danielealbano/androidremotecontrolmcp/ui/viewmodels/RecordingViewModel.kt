package com.danielealbano.androidremotecontrolmcp.ui.viewmodels

import android.content.Context
import androidx.lifecycle.ViewModel
import com.danielealbano.androidremotecontrolmcp.services.accessibility.AccessibilityServiceProvider
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordingStatus
import com.danielealbano.androidremotecontrolmcp.services.recording.WorkflowRecorder
import dagger.hilt.android.lifecycle.HiltViewModel
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import javax.inject.Inject

enum class RecordingUiError {
    ACCESSIBILITY_UNAVAILABLE,
    APP_UNAVAILABLE,
    APP_LAUNCH_FAILED,
}

/** Keeps app navigation outside the recorder and starts capture before leaving this app. */
@HiltViewModel
class RecordingViewModel
    @Inject
    constructor(
        private val recorder: WorkflowRecorder,
        private val launcher: RecordingAppLauncher,
        private val accessibility: AccessibilityServiceProvider,
    ) : ViewModel() {
        val state = recorder.state
        private val mutableError = MutableStateFlow<RecordingUiError?>(null)
        val error = mutableError.asStateFlow()

        fun start(name: String) {
            if (state.value.status != RecordingStatus.IDLE) return
            mutableError.value = null
            if (!accessibility.isReady()) {
                mutableError.value = RecordingUiError.ACCESSIBILITY_UNAVAILABLE
                return
            }
            if (!launcher.canLaunch(TARGET_PACKAGE)) {
                mutableError.value = RecordingUiError.APP_UNAVAILABLE
                return
            }
            recorder.start(name.trim(), TARGET_PACKAGE)
            if (state.value.status != RecordingStatus.RECORDING) return
            if (!launcher.launch(TARGET_PACKAGE)) {
                recorder.pause()
                mutableError.value = RecordingUiError.APP_LAUNCH_FAILED
            }
        }

        fun openTarget() {
            mutableError.value = null
            if (!launcher.launch(TARGET_PACKAGE)) mutableError.value = RecordingUiError.APP_LAUNCH_FAILED
        }

        fun pause() = recorder.pause()

        fun resume() {
            if (!accessibility.isReady()) {
                mutableError.value = RecordingUiError.ACCESSIBILITY_UNAVAILABLE
                return
            }
            mutableError.value = null
            recorder.resume()
        }

        fun stop() = recorder.stop()

        fun discard() {
            mutableError.value = null
            recorder.discard()
        }

        fun exportJson(): String? = recorder.exportJson()

        companion object {
            const val TARGET_PACKAGE = "com.xingin.xhs"
        }
    }

/** Resolves installed launch activities rather than assuming an app-specific activity name. */
class RecordingAppLauncher
    @Inject
    constructor(
        @param:ApplicationContext private val context: Context,
    ) {
        fun canLaunch(packageName: String): Boolean = context.packageManager.getLaunchIntentForPackage(packageName) != null

        fun launch(packageName: String): Boolean {
            val intent = context.packageManager.getLaunchIntentForPackage(packageName) ?: return false
            return runCatching { context.startActivity(intent) }.isSuccess
        }
    }
