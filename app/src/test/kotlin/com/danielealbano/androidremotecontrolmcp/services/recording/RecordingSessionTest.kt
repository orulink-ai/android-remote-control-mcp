package com.danielealbano.androidremotecontrolmcp.services.recording

import kotlinx.serialization.json.Json
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.nio.file.Files

class RecordingSessionTest {
    private var now = 1_000L
    private val session = RecordingSession("our.app", { now }, { "recording-id" })
    private val selector = RecordedSelector("com.xingin.xhs", "Button", "like", "点赞", null)

    @Test
    fun `recording requires name package and connected accessibility`() {
        session.start("", "com.xingin.xhs", true)
        assertEquals(RecordingStatus.IDLE, session.state.value.status)
        session.start("任务", "our.app", true)
        assertEquals(RecordingStatus.IDLE, session.state.value.status)
        session.start("任务", "com.xingin.xhs", false)
        assertEquals(RecordingStatus.IDLE, session.state.value.status)
        assertTrue(session.state.value.error != null)
    }

    @Test
    fun `recording ignores other packages passwords and paused events`() {
        session.start("任务", "com.xingin.xhs", true)
        session.record(RecordingInput("click", selector.copy(packageName = "keyboard")))
        session.record(RecordingInput("text", selector, "secret", isPassword = true))
        session.pause()
        session.record(RecordingInput("click", selector))
        assertEquals(0, session.state.value.stepCount)
        session.resume(true)
        now += 50
        session.record(RecordingInput("click", selector))
        assertEquals(1, session.state.value.stepCount)
        assertEquals(
            50,
            session.state.value.events
                .single()
                .elapsedMs,
        )
    }

    @Test
    fun `text changes coalesce by stable selector and keep final value`() {
        session.start("搜索", "com.xingin.xhs", true)
        val field = selector.copy(className = "EditText", resourceId = "search", text = null)
        session.record(RecordingInput("text", field, "小"))
        session.record(RecordingInput("text", field, "小狮日记"))
        session.record(RecordingInput("click", selector))
        assertEquals(2, session.state.value.stepCount)
        assertEquals(
            "小狮日记",
            session.state.value.events
                .first()
                .value,
        )
    }

    @Test
    fun `service disconnection pauses explicitly and cannot resume without service`() {
        session.start("任务", "com.xingin.xhs", true)
        session.serviceDisconnected()
        assertEquals(RecordingStatus.PAUSED, session.state.value.status)
        assertTrue(session.state.value.error != null)
        session.resume(false)
        assertEquals(RecordingStatus.PAUSED, session.state.value.status)
    }

    @Test
    fun `event limit pauses without silently dropping an unbounded recording`() {
        session.start("任务", "com.xingin.xhs", true)
        repeat(RecordingSession.MAX_EVENTS + 1) { session.record(RecordingInput("click", selector)) }
        assertEquals(RecordingSession.MAX_EVENTS, session.state.value.stepCount)
        assertEquals(RecordingStatus.PAUSED, session.state.value.status)
        assertTrue(session.state.value.error != null)
    }

    @Test
    fun `stop creates reviewable raw recording with stable selectors and no success claim`() {
        session.start("任务", "com.xingin.xhs", true)
        session.record(RecordingInput("click", selector))
        val recording = session.stop()!!
        val json = Json.encodeToString(recording)
        assertEquals(RecordingStatus.REVIEW, session.state.value.status)
        assertEquals("android-accessibility-events", recording.source)
        assertTrue(recording.needsReview)
        assertFalse(recording.hasPairedVerification)
        assertTrue(json.contains("resourceId"))
        assertFalse(json.contains("nodeId"))
        session.discard()
        assertEquals(RecordingStatus.IDLE, session.state.value.status)
    }

    @Test
    fun `repeated stop does not enqueue duplicate persistence`() {
        session.start("任务", "com.xingin.xhs", true)
        session.record(RecordingInput("click", selector))
        assertTrue(session.stop() != null)
        assertEquals(null, session.stop())
        assertEquals(RecordingStatus.REVIEW, session.state.value.status)
    }

    @Test
    fun `empty or malformed selectors never become replayable steps`() {
        session.start("任务", "com.xingin.xhs", true)
        session.record(RecordingInput("click", RecordedSelector("com.xingin.xhs")))
        session.record(RecordingInput("unknown", selector))
        assertEquals(0, session.state.value.stepCount)
        session.stop()
        assertFalse(session.state.value.canExport)
    }

    @Test
    fun `file store persists latest and reloads as review without resuming capture`() {
        val dir = Files.createTempDirectory("workflow-recordings-test").toFile()
        try {
            session.start("任务", "com.xingin.xhs", true)
            session.record(RecordingInput("click", selector))
            val recording = session.stop()!!
            val store = RecordingFileStore(dir)
            val saved = store.save(recording)
            assertTrue(saved.canonicalFile.parentFile == dir.canonicalFile)
            assertEquals(recording, store.latest())
            val fresh = RecordingSession("our.app")
            fresh.loadReview(store.latest()!!, saved.absolutePath)
            assertEquals(RecordingStatus.REVIEW, fresh.state.value.status)
            assertTrue(fresh.state.value.canExport)
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun `store skips corrupt files and rejects traversal recording ids`() {
        val dir = Files.createTempDirectory("workflow-recordings-test").toFile()
        try {
            dir.resolve("broken.json").writeText("{")
            assertEquals(null, RecordingFileStore(dir).latest())
            session.start("任务", "com.xingin.xhs", true)
            session.record(RecordingInput("click", selector))
            val record = session.stop()!!.copy(id = "../outside")
            assertTrue(runCatching { RecordingFileStore(dir).save(record) }.isFailure)
        } finally {
            dir.deleteRecursively()
        }
    }
}
