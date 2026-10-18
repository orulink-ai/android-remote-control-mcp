package com.danielealbano.androidremotecontrolmcp.services.recording

/** Detached and bounded accessibility observations. No Android node survives event handling. */
data class RecordingNodeSnapshot(
    val bounds: RecordedBounds,
    val labels: List<String>,
    val clickable: Boolean,
)

data class RecordingCaptureSnapshot(
    val source: RecordingNodeSnapshot,
    val screenNodes: List<RecordingNodeSnapshot>,
    val screenWidth: Int,
    val screenHeight: Int,
    val complete: Boolean = true,
)

data class RecordedSemanticCandidate(
    val role: String,
    val contextTitle: String,
    val evidence: RecordedEventEvidence,
)

/** Fixed-task draft interpretation. Actual target, action and outcome still require fresh PI verification. */
object XhsSemanticCandidateAdapter {
    const val TARGET_PACKAGE = "com.xingin.xhs"
    const val TARGET_TITLE = "当我闯进一个限时48小时的创造营"
    const val TARGET_AUTHOR = "小狮日记"

    private val favoriteLabel = Regex("(?:已)?收藏(?:\\s*\\d+(?:\\.\\d+)?[万千kKwW]?)?")
    private val likeLabel = Regex("(?:\\d+(?:\\.\\d+)?[万千kKwW]?|(?:已点赞|点赞|赞)(?:\\s*\\d+(?:\\.\\d+)?[万千kKwW]?)?)")

    fun keepLabel(value: String): String? =
        when {
            TARGET_TITLE in value -> TARGET_TITLE
            value == TARGET_AUTHOR || value == "作者$TARGET_AUTHOR" -> "作者$TARGET_AUTHOR"
            favoriteLabel.matches(value.trim()) || likeLabel.matches(value.trim()) -> value.trim().take(64)
            else -> null
        }

    fun identify(snapshot: RecordingCaptureSnapshot): RecordedSemanticCandidate? {
        val source = snapshot.source
        val width = snapshot.screenWidth
        val height = snapshot.screenHeight
        if (width <= 0 || height <= 0 || !source.clickable || !source.bounds.inside(width, height)) return null
        val titleVisible = snapshot.screenNodes.any { TARGET_TITLE in it.labels }
        val authorVisible = snapshot.screenNodes.any { "作者$TARGET_AUTHOR" in it.labels || TARGET_AUTHOR in it.labels }
        if (snapshot.complete && titleVisible && authorVisible) {
            val anchors =
                snapshot.screenNodes
                    .filter {
                        it.clickable && it.bounds.inside(width, height) && it.bounds.top >= height * FOOTER_START &&
                            it.labels.any(favoriteLabel::matches)
                    }.distinctBy { it.bounds }
            val favorite = anchors.singleOrNull()
            if (favorite != null) {
                val role =
                    if (source.bounds == favorite.bounds) {
                        "collect_toggle"
                    } else {
                        val likes =
                            snapshot.screenNodes
                                .filter { node ->
                                    node.clickable && node.bounds != favorite.bounds && node.bounds.inside(width, height) &&
                                        node.labels.any(likeLabel::matches) && adjacentLeft(node.bounds, favorite.bounds, width)
                                }.distinctBy { it.bounds }
                        if (likes.singleOrNull()?.bounds == source.bounds) "like_toggle" else null
                    }
                if (role != null) {
                    return RecordedSemanticCandidate(
                        role,
                        TARGET_TITLE,
                        RecordedEventEvidence(
                            source = "xhs-toolbar-candidate",
                            sourceBounds = source.bounds,
                            screenWidth = width,
                            screenHeight = height,
                            favoriteAnchorBounds = favorite.bounds,
                            sourceLabels = source.labels.distinct().take(MAX_LABELS),
                            favoriteAnchorLabels =
                                favorite.labels
                                    .filter(favoriteLabel::matches)
                                    .distinct()
                                    .take(MAX_LABELS),
                            contextAuthor = TARGET_AUTHOR,
                        ),
                    )
                }
            }
        }
        if (TARGET_TITLE in source.labels && source.bounds.bottom - source.bounds.top < height * MAX_POST_HEIGHT) {
            return RecordedSemanticCandidate(
                "open_target",
                TARGET_TITLE,
                RecordedEventEvidence(
                    source = "source-descendants-v1",
                    sourceBounds = source.bounds,
                    screenWidth = width,
                    screenHeight = height,
                    sourceLabels = listOf(TARGET_TITLE),
                ),
            )
        }
        return null
    }

    private fun adjacentLeft(
        source: RecordedBounds,
        anchor: RecordedBounds,
        width: Int,
    ): Boolean {
        val sourceHeight = source.bottom - source.top
        val anchorHeight = anchor.bottom - anchor.top
        val ratio = sourceHeight.toDouble() / anchorHeight
        val overlap = (minOf(source.bottom, anchor.bottom) - maxOf(source.top, anchor.top)).coerceAtLeast(0)
        return ratio in MIN_HEIGHT_RATIO..MAX_HEIGHT_RATIO &&
            overlap.toDouble() / minOf(sourceHeight, anchorHeight) >= MIN_ROW_OVERLAP &&
            (source.left + source.right) / 2.0 < anchor.left &&
            kotlin.math.abs(anchor.left - source.right) <= width * MAX_ADJACENT_GAP
    }

    const val MAX_LABELS = 8
    private const val FOOTER_START = 0.75
    private const val MAX_POST_HEIGHT = 0.8
    private const val MIN_HEIGHT_RATIO = 0.75
    private const val MAX_HEIGHT_RATIO = 1.33
    private const val MIN_ROW_OVERLAP = 0.8
    private const val MAX_ADJACENT_GAP = 0.015
}
