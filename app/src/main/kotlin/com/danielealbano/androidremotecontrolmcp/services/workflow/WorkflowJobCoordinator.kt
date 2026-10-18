package com.danielealbano.androidremotecontrolmcp.services.workflow

import android.content.Context
import android.os.SystemClock
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordedWorkflow
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import java.io.File
import javax.inject.Inject
import javax.inject.Singleton

/** Shared by local confirmation UI and authenticated MCP tools; all file IO stays off Main. */
@Singleton
class WorkflowJobCoordinator
    @Inject
    constructor(
        @ApplicationContext context: Context,
    ) {
        private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        private val mutableState = MutableStateFlow(WorkflowJobUiState())
        val state: StateFlow<WorkflowJobUiState> = mutableState.asStateFlow()
        private val queue =
            scope.async {
                WorkflowJobQueue(WorkflowJobFileStore(File(context.filesDir, "workflow-jobs")), leaseClockMs = SystemClock::elapsedRealtime)
            }

        init {
            scope.launch {
                val engine = queue.await()
                launch { engine.state.collect { mutableState.value = engine.state.value } }
                while (isActive) {
                    delay(1_000)
                    runCatching { engine.refresh() }
                }
            }
        }

        suspend fun submit(rawJson: String): Boolean =
            operate { engine ->
                val parsed =
                    runCatching {
                        require(rawJson.toByteArray(Charsets.UTF_8).size <= MAX_INPUT_BYTES)
                        Json.decodeFromString<RecordedWorkflow>(rawJson)
                    }.getOrNull()
                if (parsed == null) engine.reportInvalidRecording() else engine.submit(parsed)
            }

        suspend fun submit(recording: RecordedWorkflow): Boolean = operate { it.submit(recording) }

        suspend fun cancelQueued(): Boolean = operate { it.cancelQueued() }

        suspend fun claim(workerId: String): WorkflowClaim = operate { it.claim(workerId) }

        suspend fun heartbeat(
            workerId: String,
            jobId: String? = null,
            claimToken: String? = null,
        ): WorkflowJobUiState = operate { it.heartbeat(workerId, jobId, claimToken) }

        suspend fun update(
            jobId: String,
            claimToken: String,
            status: WorkflowJobStatus,
            summary: WorkflowJobSummary? = null,
        ): WorkflowJob = operate { it.update(jobId, claimToken, status, summary) }

        private suspend fun <T> operate(block: (WorkflowJobQueue) -> T): T =
            withContext(Dispatchers.IO) {
                val engine = queue.await()
                try {
                    block(engine)
                } finally {
                    mutableState.value = engine.state.value
                }
            }

        companion object {
            private const val MAX_INPUT_BYTES = 1_500_000
        }
    }
