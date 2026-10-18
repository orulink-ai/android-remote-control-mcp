package com.danielealbano.androidremotecontrolmcp.services.recording

import android.content.Context
import android.view.accessibility.AccessibilityEvent
import com.danielealbano.androidremotecontrolmcp.services.accessibility.AccessibilityServiceProvider
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import java.io.File
import javax.inject.Inject
import javax.inject.Singleton

/** App-local recording. Captured geometry is draft evidence, never a replay coordinate. */
@Singleton
class WorkflowRecorder
    @Inject
    constructor(
        @ApplicationContext context: Context,
        private val accessibility: AccessibilityServiceProvider,
    ) {
        private val session = RecordingSession(context.packageName)
        private val files = RecordingFileStore(File(context.filesDir, "workflow-recordings"))
        private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

        @Volatile private var connected = false
        val state = session.state

        init {
            scope.launch {
                files.latest()?.let { session.loadReview(it, files.path(it).absolutePath) }
            }
        }

        fun start(
            name: String,
            targetPackage: String,
        ) = session.start(name, targetPackage, connected)

        fun pause() = session.pause()

        fun resume() = session.resume(connected)

        fun discard() = session.discard()

        fun stop() {
            val recording = session.stop() ?: return
            scope.launch {
                runCatching { files.save(recording) }
                    .onSuccess { session.saved(recording.id, it.absolutePath) }
                    .onFailure { session.saveFailed(recording.id) }
            }
        }

        fun exportJson(): String? = session.completedRecording()?.takeIf { it.events.isNotEmpty() }?.let(files::encode)

        fun exportFile(): File? =
            state.value.exportPath
                ?.let(::File)
                ?.takeIf { it.isFile }

        fun onServiceConnected() {
            connected = true
        }

        fun onServiceDisconnected() {
            connected = false
            session.serviceDisconnected()
        }

        fun onAccessibilityEvent(event: AccessibilityEvent) {
            val current = state.value
            if (current.status != RecordingStatus.RECORDING || event.packageName?.toString() != current.targetPackage) return
            runCatching {
                val screen =
                    if (event.eventType == AccessibilityEvent.TYPE_VIEW_CLICKED &&
                        current.targetPackage == XhsSemanticCandidateAdapter.TARGET_PACKAGE
                    ) {
                        accessibility.getScreenInfo()
                    } else {
                        null
                    }
                AccessibilityRecordingAdapter.extract(event, screen?.width, screen?.height)?.let(session::record)
            }.onFailure { session.captureFailed() }
        }
    }
