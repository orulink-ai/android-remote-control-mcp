package com.danielealbano.androidremotecontrolmcp.services.recording

import android.graphics.Rect
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test

class SemanticRecordingCaptureTest {
    private val likeBounds = RecordedBounds(699, 2990, 956, 3144)
    private val favoriteBounds = RecordedBounds(956, 2990, 1218, 3144)

    @Test
    fun `Android source with no label captures unique toolbar candidate without inventing success`() {
        val count = node("1605", RecordedBounds(820, 3010, 920, 3100))
        val like = node(null, likeBounds, true, listOf(count))
        val favorite = node("收藏177", favoriteBounds, true)
        val title = node(XhsSemanticCandidateAdapter.TARGET_TITLE, RecordedBounds(20, 500, 1400, 700))
        val author = node("作者小狮日记", RecordedBounds(20, 200, 800, 300))
        val root = node(null, RecordedBounds(0, 0, 1440, 3200), children = listOf(like, favorite, title, author))
        every { like.parent } returns root
        val captured = AccessibilityRecordingAdapter.extract(event(like), 1440, 3200)!!
        assertEquals("like_toggle", captured.selector.semanticRole)
        assertEquals(XhsSemanticCandidateAdapter.TARGET_TITLE, captured.selector.contextTitle)
        assertNotNull(captured.evidence)
        val session = RecordingSession("our.app")
        session.start("Demo", "com.xingin.xhs", true)
        session.record(captured)
        val recording = session.stop()!!
        assertEquals(1, recording.events.size)
        assertEquals(
            "xhs-toolbar-candidate",
            recording.events
                .single()
                .evidence
                ?.source,
        )
        assertFalse(recording.hasPairedVerification)
    }

    @Test
    fun `clicked post keeps only known title evidence from its source children`() {
        val title = node(XhsSemanticCandidateAdapter.TARGET_TITLE, RecordedBounds(20, 700, 600, 800))
        val unrelated = node("do not collect unrelated text", RecordedBounds(20, 820, 600, 900))
        val card = node(null, RecordedBounds(0, 500, 700, 1800), true, listOf(title, unrelated))
        val captured = AccessibilityRecordingAdapter.extract(event(card), 1440, 3200)!!
        assertEquals("open_target", captured.selector.semanticRole)
        assertEquals(listOf(XhsSemanticCandidateAdapter.TARGET_TITLE), captured.evidence?.sourceLabels)
    }

    @Test
    fun `context scan never reads a password child or editable neighbour text`() {
        val privateTitle = node(XhsSemanticCandidateAdapter.TARGET_TITLE, RecordedBounds(20, 700, 600, 800))
        val password = node(null, RecordedBounds(0, 500, 700, 1800), children = listOf(privateTitle))
        every { password.isPassword } returns true
        val editableTitle = node(XhsSemanticCandidateAdapter.TARGET_TITLE, RecordedBounds(20, 800, 600, 900))
        val editable = node("private typing", RecordedBounds(0, 500, 700, 1800), children = listOf(editableTitle))
        every { editable.isEditable } returns true
        val like = node("1605", likeBounds, true)
        val favorite = node("收藏177", favoriteBounds, true)
        val author = node("作者小狮日记", RecordedBounds(20, 200, 800, 300))
        val root = node(null, RecordedBounds(0, 0, 1440, 3200), children = listOf(like, favorite, password, editable, author))
        every { like.parent } returns root
        val captured = AccessibilityRecordingAdapter.extract(event(like), 1440, 3200)!!
        assertNull(captured.selector.semanticRole)
        verify(exactly = 0) { privateTitle.text }
        verify(exactly = 0) { password.text }
        verify(exactly = 0) { editable.text }
        verify(exactly = 0) { editableTitle.text }
    }

    @Test
    fun `IME edits with changing descriptions coalesce to the last real value`() {
        val input = node("小", RecordedBounds(20, 200, 1000, 300), true)
        every { input.viewIdResourceName } returns "com.xingin.xhs:id/search"
        every { input.contentDescription } returns "搜索，小"
        val change = event(input)
        every { change.eventType } returns AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED
        val session = RecordingSession("our.app")
        session.start("Demo", "com.xingin.xhs", true)
        session.record(AccessibilityRecordingAdapter.extract(change)!!)
        every { input.text } returns "小狮日记"
        every { input.contentDescription } returns "搜索，小狮日记"
        session.record(AccessibilityRecordingAdapter.extract(change)!!)
        val events = session.stop()!!.events
        assertEquals(1, events.size)
        assertEquals("小狮日记", events.single().value)
    }

    @Test
    fun `an actual text change without a resource id still retains its observed value`() {
        val input = node("小狮日记", RecordedBounds(20, 200, 1000, 300), true)
        every { input.isEditable } returns true
        val change = event(input)
        every { change.eventType } returns AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED
        val session = RecordingSession("our.app")
        session.start("Demo", "com.xingin.xhs", true)
        session.record(AccessibilityRecordingAdapter.extract(change)!!)
        assertEquals(
            "小狮日记",
            session
                .stop()!!
                .events
                .single()
                .value,
        )
    }

    private fun event(source: AccessibilityNodeInfo): AccessibilityEvent =
        mockk<AccessibilityEvent>(relaxed = true).also {
            every { it.eventType } returns AccessibilityEvent.TYPE_VIEW_CLICKED
            every { it.packageName } returns "com.xingin.xhs"
            every { it.source } returns source
        }

    private fun node(
        text: String?,
        bounds: RecordedBounds,
        clickable: Boolean = false,
        children: List<AccessibilityNodeInfo> = emptyList(),
    ): AccessibilityNodeInfo =
        mockk<AccessibilityNodeInfo>(relaxed = true).also { node ->
            every { node.packageName } returns "com.xingin.xhs"
            every { node.className } returns "android.widget.LinearLayout"
            every { node.parent } returns null
            every { node.text } returns text
            every { node.contentDescription } returns null
            every { node.viewIdResourceName } returns null
            every { node.isClickable } returns clickable
            every { node.childCount } returns children.size
            children.forEachIndexed { index, child -> every { node.getChild(index) } returns child }
            every { node.getBoundsInScreen(any()) } answers {
                val out = firstArg<Rect>()
                out.left = bounds.left
                out.top = bounds.top
                out.right = bounds.right
                out.bottom = bounds.bottom
            }
        }
}
