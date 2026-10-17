package com.danielealbano.androidremotecontrolmcp.services.recording

import kotlinx.serialization.Serializable

/** A recording is evidence of emitted accessibility events, not a verified executable workflow. */
@Serializable
data class RecordedWorkflow(
    val id: String,
    val name: String,
    val targetPackage: String,
    val startedAtEpochMs: Long,
    val endedAtEpochMs: Long,
    val events: List<RecordedWorkflowEvent>,
    val schemaVersion: Int = 1,
    val source: String = "android-accessibility-events",
    val needsReview: Boolean = true,
    val hasPairedVerification: Boolean = false,
)

@Serializable
data class RecordedSelector(
    val packageName: String,
    val className: String? = null,
    val resourceId: String? = null,
    val text: String? = null,
    val contentDescription: String? = null,
)

@Serializable
data class RecordedWorkflowEvent(
    val sequence: Int,
    val elapsedMs: Long,
    val type: String,
    val selector: RecordedSelector,
    val value: String? = null,
    val scrollFromIndex: Int? = null,
    val scrollToIndex: Int? = null,
)

enum class RecordingStatus { IDLE, RECORDING, PAUSED, REVIEW }

enum class RecordingIssue {
    INVALID_NAME,
    INVALID_TARGET,
    SERVICE_UNAVAILABLE,
    ACTIVE_RECORDING,
    EVENT_LIMIT,
    SERVICE_DISCONNECTED,
    SAVE_FAILED,
    CAPTURE_FAILED,
}

data class RecordingUiState(
    val status: RecordingStatus = RecordingStatus.IDLE,
    val name: String = "",
    val targetPackage: String = "",
    val events: List<RecordedWorkflowEvent> = emptyList(),
    val error: String? = null,
    val issue: RecordingIssue? = null,
    val recordingId: String? = null,
    val exportPath: String? = null,
) {
    val stepCount: Int get() = events.size
    val canExport: Boolean get() = status == RecordingStatus.REVIEW && events.isNotEmpty()
}

/** Already detached from AccessibilityEvent: never retain Android node handles. */
data class RecordingInput(
    val type: String,
    val selector: RecordedSelector,
    val value: String? = null,
    val isPassword: Boolean = false,
    val scrollFromIndex: Int? = null,
    val scrollToIndex: Int? = null,
)
