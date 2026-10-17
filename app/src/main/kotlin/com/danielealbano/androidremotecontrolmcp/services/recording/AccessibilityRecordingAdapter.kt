package com.danielealbano.androidremotecontrolmcp.services.recording

import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/** Translate the small set of actionable events synchronously; never retain a source node. */
object AccessibilityRecordingAdapter {
    fun extract(event: AccessibilityEvent): RecordingInput? {
        val type =
            when (event.eventType) {
                AccessibilityEvent.TYPE_VIEW_CLICKED -> "click"
                AccessibilityEvent.TYPE_VIEW_LONG_CLICKED -> "long_click"
                AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED -> "text"
                AccessibilityEvent.TYPE_VIEW_SCROLLED -> "scroll"
                else -> return null
            }
        if (event.isPassword) return null
        val source = event.source ?: return null
        return try {
            if (hasPasswordAncestor(source)) return null
            val packageName = source.packageName?.toString() ?: return null
            if (packageName != event.packageName?.toString()) return null
            RecordingInput(
                type = type,
                selector =
                    RecordedSelector(
                        packageName = packageName,
                        className = source.className?.toString(),
                        resourceId = source.viewIdResourceName,
                        text = if (type == "text") null else source.text?.toString(),
                        contentDescription = source.contentDescription?.toString(),
                    ),
                value = if (type == "text") source.text?.toString() else null,
                scrollFromIndex = event.fromIndex.takeIf { type == "scroll" && it >= 0 },
                scrollToIndex = event.toIndex.takeIf { type == "scroll" && it >= 0 },
            )
        } finally {
            @Suppress("DEPRECATION")
            source.recycle()
        }
    }

    private fun hasPasswordAncestor(source: AccessibilityNodeInfo): Boolean {
        if (source.isPassword) return true
        var current = source.parent
        repeat(MAX_ANCESTORS) {
            val node = current ?: return false
            val password = node.isPassword
            current = node.parent
            @Suppress("DEPRECATION")
            node.recycle()
            if (password) {
                @Suppress("DEPRECATION")
                current?.recycle()
                return true
            }
        }
        @Suppress("DEPRECATION")
        current?.recycle()
        // An unusually deep hierarchy cannot be checked cheaply; fail closed.
        return current != null
    }

    private const val MAX_ANCESTORS = 32
}
