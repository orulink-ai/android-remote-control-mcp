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
    val semanticRole: String? = null,
    val contextTitle: String? = null,
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
    val evidence: RecordedEventEvidence? = null,
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
    val evidence: RecordedEventEvidence? = null,
)

/** Capture geometry is evidence for a draft candidate, never a replay coordinate. */
@Serializable
data class RecordedBounds(
    val left: Int,
    val top: Int,
    val right: Int,
    val bottom: Int,
) {
    fun inside(
        width: Int,
        height: Int,
    ): Boolean = left >= 0 && top >= 0 && right > left && bottom > top && right <= width && bottom <= height
}

@Serializable
data class RecordedEventEvidence(
    val source: String,
    val sourceBounds: RecordedBounds,
    val screenWidth: Int,
    val screenHeight: Int,
    val favoriteAnchorBounds: RecordedBounds? = null,
    val sourceLabels: List<String> = emptyList(),
    val favoriteAnchorLabels: List<String> = emptyList(),
    val contextAuthor: String? = null,
) {
    fun supportsCandidate(
        role: String?,
        title: String?,
    ): Boolean =
        title == XhsSemanticCandidateAdapter.TARGET_TITLE &&
            when (role) {
                "like_toggle", "collect_toggle" -> {
                    source == "xhs-toolbar-candidate" &&
                        favoriteAnchorBounds != null && favoriteAnchorLabels.isNotEmpty() &&
                        favoriteAnchorLabels.all { it.startsWith("收藏") || it.startsWith("已收藏") } &&
                        contextAuthor == XhsSemanticCandidateAdapter.TARGET_AUTHOR
                }

                "open_target" -> {
                    source == "source-descendants-v1" && XhsSemanticCandidateAdapter.TARGET_TITLE in sourceLabels
                }

                else -> {
                    false
                }
            }

    fun isBounded(): Boolean =
        source in setOf("xhs-toolbar-candidate", "source-descendants-v1", "unresolved-source") &&
            screenWidth in 1..16_384 && screenHeight in 1..16_384 &&
            sourceBounds.inside(screenWidth, screenHeight) &&
            (favoriteAnchorBounds == null || favoriteAnchorBounds.inside(screenWidth, screenHeight)) &&
            sourceLabels.size <= 8 && favoriteAnchorLabels.size <= 8 &&
            (sourceLabels + favoriteAnchorLabels).all { it.length <= 64 && XhsSemanticCandidateAdapter.keepLabel(it) != null } &&
            (contextAuthor == null || contextAuthor == XhsSemanticCandidateAdapter.TARGET_AUTHOR)
}
