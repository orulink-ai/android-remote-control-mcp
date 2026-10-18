package com.danielealbano.androidremotecontrolmcp.services.recording

import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo

/** Captures only fixed-title/author/button markers and count labels; unrelated neighbour text is discarded. */
internal object RecordingContextCapture {
    fun capture(
        source: AccessibilityNodeInfo,
        width: Int,
        height: Int,
    ): RecordingCaptureSnapshot? {
        if (width !in 1..MAX_DIMENSION || height !in 1..MAX_DIMENSION) return null
        val sourceScan = Scan(source, MAX_SOURCE_NODES)
        val clicked = sourceScan.visit(source, 0) ?: return null
        val root = rootOf(source) ?: return null
        return try {
            val screen = Scan(source, MAX_SCREEN_NODES)
            screen.visit(root, 0)
            RecordingCaptureSnapshot(clicked, screen.nodes, width, height, sourceScan.complete && screen.complete)
        } finally {
            if (root !== source) recycle(root)
        }
    }

    private fun rootOf(source: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        var current = source
        repeat(MAX_DEPTH) {
            val parent = current.parent ?: return current
            if (current !== source) recycle(current)
            if (parent.isPassword) {
                recycle(parent)
                return null
            }
            current = parent
        }
        if (current !== source) recycle(current)
        return null
    }

    private class Scan(
        private val originalSource: AccessibilityNodeInfo,
        private val limit: Int,
    ) {
        val nodes = mutableListOf<RecordingNodeSnapshot>()
        var complete = true
        private var visited = 0

        fun visit(
            node: AccessibilityNodeInfo,
            depth: Int,
        ): RecordingNodeSnapshot? {
            if (visited >= limit || depth >= MAX_DEPTH) {
                complete = false
                return null
            }
            visited++
            if (node.isPassword || node.isEditable ||
                node.packageName?.toString() != XhsSemanticCandidateAdapter.TARGET_PACKAGE
            ) {
                return null
            }
            val labels = mutableListOf<String>()
            if (!node.isEditable) {
                listOf(node.text, node.contentDescription).forEach { raw ->
                    raw
                        ?.toString()
                        ?.take(RecordingSession.MAX_TEXT)
                        ?.let(XhsSemanticCandidateAdapter::keepLabel)
                        ?.let(labels::add)
                }
            }
            val count = node.childCount
            if (count > MAX_CHILDREN) complete = false
            repeat(count.coerceIn(0, MAX_CHILDREN)) { index ->
                if (visited >= limit) {
                    complete = false
                    return@repeat
                }
                val child = node.getChild(index) ?: return@repeat
                try {
                    visit(child, depth + 1)?.labels?.let(labels::addAll)
                } finally {
                    if (child !== originalSource) recycle(child)
                }
            }
            val rect = Rect()
            node.getBoundsInScreen(rect)
            return RecordingNodeSnapshot(
                RecordedBounds(rect.left, rect.top, rect.right, rect.bottom),
                labels.distinct().take(XhsSemanticCandidateAdapter.MAX_LABELS),
                node.isClickable,
            ).also(nodes::add)
        }
    }

    @Suppress("DEPRECATION")
    private fun recycle(node: AccessibilityNodeInfo) = node.recycle()

    const val MAX_DIMENSION = 16_384
    private const val MAX_DEPTH = 32
    private const val MAX_CHILDREN = 48
    private const val MAX_SOURCE_NODES = 48
    private const val MAX_SCREEN_NODES = 256
}
