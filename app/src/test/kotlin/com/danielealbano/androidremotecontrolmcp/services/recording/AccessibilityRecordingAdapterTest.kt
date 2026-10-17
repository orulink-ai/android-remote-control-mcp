package com.danielealbano.androidremotecontrolmcp.services.recording

import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test

class AccessibilityRecordingAdapterTest {
    private val source = mockk<AccessibilityNodeInfo>(relaxed = true)
    private val event = mockk<AccessibilityEvent>(relaxed = true)

    init {
        every { event.eventType } returns AccessibilityEvent.TYPE_VIEW_CLICKED
        every { event.packageName } returns "com.xingin.xhs"
        every { event.source } returns source
        every { source.packageName } returns "com.xingin.xhs"
        every { source.parent } returns null
        every { source.className } returns "Button"
        every { source.viewIdResourceName } returns "com.xingin.xhs:id/like"
        every { source.text } returns "点赞"
        every { source.contentDescription } returns null
    }

    @Test
    fun `click is detached from source node and recycled`() {
        val result = AccessibilityRecordingAdapter.extract(event)!!
        assertEquals("click", result.type)
        assertEquals("点赞", result.selector.text)
        assertEquals("com.xingin.xhs:id/like", result.selector.resourceId)
        @Suppress("DEPRECATION")
        verify(exactly = 1) { source.recycle() }
    }

    @Test
    fun `password event never accesses source`() {
        every { event.isPassword } returns true
        assertNull(AccessibilityRecordingAdapter.extract(event))
        verify(exactly = 0) { event.source }
    }

    @Test
    fun `password ancestor prevents text reads`() {
        val parent = mockk<AccessibilityNodeInfo>(relaxed = true)
        every { parent.parent } returns null
        every { parent.isPassword } returns true
        every { source.parent } returns parent
        assertNull(AccessibilityRecordingAdapter.extract(event))
        verify(exactly = 0) { source.text }
        verify(exactly = 0) { source.contentDescription }
    }

    @Test
    fun `deep password ancestor is excluded before reading sensitive value`() {
        val ancestors = List(6) { mockk<AccessibilityNodeInfo>(relaxed = true) }
        every { source.parent } returns ancestors.first()
        ancestors.forEachIndexed { index, node ->
            every { node.parent } returns ancestors.getOrNull(index + 1)
            every { node.isPassword } returns (index == 5)
        }
        assertNull(AccessibilityRecordingAdapter.extract(event))
        verify(exactly = 0) { source.text }
    }

    @Test
    fun `keyboard source under mismatched target event is excluded`() {
        every { source.packageName } returns "com.samsung.android.honeyboard"
        assertNull(AccessibilityRecordingAdapter.extract(event))
        verify(exactly = 0) { source.text }
    }

    @Test
    fun `text value is captured independently from stable selector`() {
        every { event.eventType } returns AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED
        every { source.text } returns "小狮日记"
        val result = AccessibilityRecordingAdapter.extract(event)!!
        assertEquals("text", result.type)
        assertEquals("小狮日记", result.value)
        assertNull(result.selector.text)
    }

    @Test
    fun `window changes never fabricate user actions`() {
        every { event.eventType } returns AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED
        assertNull(AccessibilityRecordingAdapter.extract(event))
        verify(exactly = 0) { event.source }
    }
}
