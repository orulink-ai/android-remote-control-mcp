package com.danielealbano.androidremotecontrolmcp.services.recording

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.util.UUID

/** Bounded state machine; all mutations run under one lock, with no Android or disk dependencies. */
@Suppress("TooManyFunctions")
class RecordingSession(
    private val ownPackage: String,
    private val clock: () -> Long = System::currentTimeMillis,
    private val newId: () -> String = { UUID.randomUUID().toString() },
) {
    private val mutableState = MutableStateFlow(RecordingUiState())
    val state: StateFlow<RecordingUiState> = mutableState.asStateFlow()
    private var startedAt = 0L
    private var completed: RecordedWorkflow? = null

    @Synchronized
    fun start(
        name: String,
        targetPackage: String,
        connected: Boolean,
    ) {
        when {
            state.value.status in setOf(RecordingStatus.RECORDING, RecordingStatus.PAUSED) -> {
                fail("请先结束或放弃当前录制。", RecordingIssue.ACTIVE_RECORDING)
            }

            name.isBlank() || name.length > MAX_NAME -> {
                fail("请输入 1–100 字的流程名称。", RecordingIssue.INVALID_NAME)
            }

            !PACKAGE_NAME.matches(targetPackage) || targetPackage == ownPackage -> {
                fail("请选择要录制的其他应用。", RecordingIssue.INVALID_TARGET)
            }

            !connected -> {
                fail("请先启用无障碍服务。", RecordingIssue.SERVICE_UNAVAILABLE)
            }

            else -> {
                startedAt = clock()
                completed = null
                mutableState.value =
                    RecordingUiState(
                        status = RecordingStatus.RECORDING,
                        name = name.trim(),
                        targetPackage = targetPackage,
                        recordingId = newId(),
                    )
            }
        }
    }

    @Synchronized
    fun pause() {
        if (state.value.status == RecordingStatus.RECORDING) {
            mutableState.value = state.value.copy(status = RecordingStatus.PAUSED)
        }
    }

    @Synchronized
    fun resume(connected: Boolean) {
        if (state.value.status != RecordingStatus.PAUSED) return
        if (!connected) {
            fail("无障碍服务已断开，请重新启用后继续。", RecordingIssue.SERVICE_UNAVAILABLE)
        } else if (state.value.events.size >= MAX_EVENTS) {
            fail("已达到 $MAX_EVENTS 步上限，请结束并导出。", RecordingIssue.EVENT_LIMIT)
        } else {
            mutableState.value = state.value.copy(status = RecordingStatus.RECORDING, error = null, issue = null)
        }
    }

    @Synchronized
    fun serviceDisconnected() {
        if (state.value.status == RecordingStatus.RECORDING) {
            mutableState.value =
                state.value.copy(status = RecordingStatus.PAUSED, error = "无障碍服务已断开，录制已暂停。", issue = RecordingIssue.SERVICE_DISCONNECTED)
        }
    }

    @Synchronized
    fun record(input: RecordingInput) {
        val current = state.value
        if (current.status != RecordingStatus.RECORDING || input.isPassword) return
        if (input.selector.packageName != current.targetPackage || input.type !in TYPES) return
        val selector = input.selector.sanitized()
        if (listOf(selector.resourceId, selector.text, selector.contentDescription).all { it.isNullOrBlank() }) return
        val event =
            RecordedWorkflowEvent(
                sequence = current.events.size + 1,
                elapsedMs = (clock() - startedAt).coerceAtLeast(0L),
                type = input.type,
                selector = selector,
                value = input.value?.take(MAX_TEXT),
                scrollFromIndex = input.scrollFromIndex,
                scrollToIndex = input.scrollToIndex,
            )
        val previous = current.events.lastOrNull()
        val coalesce = input.type == "text" && previous?.type == "text" && previous.selector == selector
        if (!coalesce && current.events.size >= MAX_EVENTS) {
            mutableState.value =
                current.copy(status = RecordingStatus.PAUSED, error = "已达到 $MAX_EVENTS 步上限，请结束并导出。", issue = RecordingIssue.EVENT_LIMIT)
            return
        }
        val events =
            if (coalesce) current.events.dropLast(1) + event.copy(sequence = previous!!.sequence) else current.events + event
        mutableState.value = current.copy(events = events)
    }

    @Synchronized
    fun stop(): RecordedWorkflow? {
        val current = state.value
        if (current.status !in setOf(RecordingStatus.RECORDING, RecordingStatus.PAUSED)) return null
        val result =
            RecordedWorkflow(
                id = requireNotNull(current.recordingId),
                name = current.name,
                targetPackage = current.targetPackage,
                startedAtEpochMs = startedAt,
                endedAtEpochMs = clock(),
                events = current.events,
            )
        completed = result
        mutableState.value = current.copy(status = RecordingStatus.REVIEW, error = null, issue = null)
        return result
    }

    @Synchronized
    fun discard() {
        completed = null
        mutableState.value = RecordingUiState()
    }

    @Synchronized
    fun loadReview(
        recording: RecordedWorkflow,
        path: String,
    ) {
        if (state.value.status != RecordingStatus.IDLE) return
        completed = recording
        startedAt = recording.startedAtEpochMs
        mutableState.value =
            RecordingUiState(
                status = RecordingStatus.REVIEW,
                name = recording.name,
                targetPackage = recording.targetPackage,
                events = recording.events,
                recordingId = recording.id,
                exportPath = path,
            )
    }

    @Synchronized
    fun saved(
        id: String,
        path: String,
    ) {
        if (state.value.recordingId == id) mutableState.value = state.value.copy(exportPath = path)
    }

    @Synchronized
    fun saveFailed(id: String) {
        if (state.value.recordingId == id) fail("无法保存到应用私有目录；仍可使用导出保存草稿。", RecordingIssue.SAVE_FAILED)
    }

    @Synchronized
    fun captureFailed() {
        if (state.value.status == RecordingStatus.RECORDING) {
            mutableState.value =
                state.value.copy(
                    status = RecordingStatus.PAUSED,
                    error = "无法读取界面事件，录制已暂停；请检查界面后继续。",
                    issue = RecordingIssue.CAPTURE_FAILED,
                )
        }
    }

    @Synchronized
    fun completedRecording(): RecordedWorkflow? = completed?.takeIf { state.value.status == RecordingStatus.REVIEW }

    private fun fail(
        message: String,
        issue: RecordingIssue,
    ) {
        mutableState.value = state.value.copy(error = message, issue = issue)
    }

    private fun RecordedSelector.sanitized(): RecordedSelector =
        copy(
            className = className?.take(MAX_TEXT),
            resourceId = resourceId?.take(MAX_TEXT),
            text = text?.take(MAX_TEXT),
            contentDescription = contentDescription?.take(MAX_TEXT),
        )

    companion object {
        const val MAX_EVENTS = 200
        const val MAX_TEXT = 512
        private const val MAX_NAME = 100
        private val PACKAGE_NAME = Regex("[A-Za-z][A-Za-z0-9_]*(\\.[A-Za-z0-9_]+)+")
        private val TYPES = setOf("click", "long_click", "text", "scroll")
    }
}
