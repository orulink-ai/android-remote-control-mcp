package com.danielealbano.androidremotecontrolmcp.ui.viewmodels

import androidx.lifecycle.ViewModelStore
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordedSelector
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordedWorkflow
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordedWorkflowEvent
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordingStatus
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordingUiState
import com.danielealbano.androidremotecontrolmcp.services.recording.WorkflowRecorder
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJob
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobCoordinator
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobIssue
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobStatus
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobSummary
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobUiState
import io.mockk.coEvery
import io.mockk.coVerify
import io.mockk.every
import io.mockk.mockk
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test

@OptIn(ExperimentalCoroutinesApi::class)
class WorkflowExecutionViewModelTest {
    private val dispatcher = StandardTestDispatcher()
    private val viewModelStore = ViewModelStore()
    private val recorder = mockk<WorkflowRecorder>()
    private val coordinator = mockk<WorkflowJobCoordinator>()
    private val recording = MutableStateFlow(review())
    private val queue = MutableStateFlow(WorkflowJobUiState())
    private lateinit var viewModel: WorkflowExecutionViewModel

    @BeforeEach
    fun setUp() {
        Dispatchers.setMain(dispatcher)
        every { recorder.state } returns recording
        every { recorder.exportJson() } returns RAW_RECORDING
        every { coordinator.state } returns queue
        coEvery { coordinator.submit(any<String>()) } returns true
        viewModel = WorkflowExecutionViewModel(recorder, coordinator)
        viewModelStore.put("workflow", viewModel)
    }

    @AfterEach
    fun tearDown() {
        viewModelStore.clear()
        Dispatchers.resetMain()
    }

    @Test
    fun `offline companion still accepts the recording into its queue`() =
        runTest(dispatcher) {
            viewModel.submitCurrent()
            advanceUntilIdle()
            coVerify(exactly = 1) { coordinator.submit(RAW_RECORDING) }
            assertFalse(viewModel.state.value.companionOnline)
        }

    @Test
    fun `double tapping before submit finishes cannot queue duplicate jobs`() =
        runTest(dispatcher) {
            val submitted = CompletableDeferred<Boolean>()
            coEvery { coordinator.submit(any<String>()) } coAnswers { submitted.await() }
            viewModel.submitCurrent()
            viewModel.submitCurrent()
            advanceUntilIdle()
            assertTrue(viewModel.state.value.submitting)
            coVerify(exactly = 1) { coordinator.submit(RAW_RECORDING) }
            submitted.complete(true)
            advanceUntilIdle()
            assertFalse(viewModel.state.value.submitting)
        }

    @Test
    fun `empty or unfinished recordings cannot request execution`() =
        runTest(dispatcher) {
            recording.value = review().copy(events = emptyList())
            viewModel.submitCurrent()
            advanceUntilIdle()
            assertEquals(WorkflowExecutionIssue.NO_RECORDING, viewModel.state.value.issue)
            coVerify(exactly = 0) { coordinator.submit(any<String>()) }
        }

    @Test
    fun `an unfinished recording with events still cannot request execution`() =
        runTest(dispatcher) {
            recording.value = review().copy(status = RecordingStatus.RECORDING)
            viewModel.submitCurrent()
            advanceUntilIdle()
            assertEquals(WorkflowExecutionIssue.NO_RECORDING, viewModel.state.value.issue)
            coVerify(exactly = 0) { coordinator.submit(any<String>()) }
        }

    @Test
    fun `a recording for another app cannot run this fixed template`() =
        runTest(dispatcher) {
            recording.value = review().copy(targetPackage = "com.example.other")
            viewModel.submitCurrent()
            advanceUntilIdle()
            assertEquals(WorkflowExecutionIssue.UNSUPPORTED_APP, viewModel.state.value.issue)
            coVerify(exactly = 0) { coordinator.submit(any<String>()) }
        }

    @Test
    fun `an existing active job disables another request`() =
        runTest(dispatcher) {
            queue.value = WorkflowJobUiState(latestJob = job(WorkflowJobStatus.QUEUED))
            viewModel.submitCurrent()
            advanceUntilIdle()
            assertFalse(viewModel.state.value.canSubmit)
            coVerify(exactly = 0) { coordinator.submit(any<String>()) }
        }

    @Test
    fun `running and completed job results remain visible without changing the draft`() =
        runTest(dispatcher) {
            queue.value = WorkflowJobUiState(latestJob = job(WorkflowJobStatus.RUNNING), companionOnline = true)
            advanceUntilIdle()
            assertEquals(
                WorkflowJobStatus.RUNNING,
                viewModel.state.value.job
                    ?.status,
            )
            val summary = WorkflowJobSummary(piElapsedMs = 1234.0, modelCalls = 2, fallbackRequests = 1)
            queue.value = queue.value.copy(latestJob = job(WorkflowJobStatus.SUCCEEDED).copy(summary = summary))
            advanceUntilIdle()
            assertEquals(
                summary,
                viewModel.state.value.job
                    ?.summary,
            )
            assertTrue(viewModel.state.value.canSubmit)
            assertEquals(RecordingStatus.REVIEW, recording.value.status)
        }

    @Test
    fun `previous recording job is labelled as previous while still blocking overlapping execution`() =
        runTest(dispatcher) {
            queue.value = WorkflowJobUiState(latestJob = job(WorkflowJobStatus.RUNNING))
            advanceUntilIdle()
            assertTrue(viewModel.state.value.isCurrentRecordingJob)
            recording.value = review().copy(recordingId = "new-draft")
            advanceUntilIdle()
            assertFalse(viewModel.state.value.isCurrentRecordingJob)
            assertFalse(viewModel.state.value.canSubmit)
            assertEquals(
                "demo",
                viewModel.state.value.job
                    ?.recordingId,
            )
            queue.value = WorkflowJobUiState(latestJob = job(WorkflowJobStatus.SUCCEEDED))
            advanceUntilIdle()
            assertFalse(viewModel.state.value.isCurrentRecordingJob)
            assertTrue(viewModel.state.value.canSubmit)
        }

    @Test
    fun `review changing before queued submission starts cannot submit the old draft`() =
        runTest(dispatcher) {
            viewModel.submitCurrent()
            recording.value = review().copy(recordingId = "new-draft")
            advanceUntilIdle()
            assertEquals(
                "RECORDING_CHANGED",
                viewModel.state.value.issue
                    ?.name,
            )
            coVerify(exactly = 0) { coordinator.submit(any<String>()) }
        }

    @Test
    fun `storage errors expose a recoverable issue and preserve the recording`() =
        runTest(dispatcher) {
            coEvery { coordinator.submit(any<String>()) } coAnswers {
                queue.value = WorkflowJobUiState(error = WorkflowJobIssue.STORAGE_FAILURE)
                false
            }
            viewModel.submitCurrent()
            advanceUntilIdle()
            assertEquals(WorkflowJobIssue.STORAGE_FAILURE, viewModel.state.value.queueIssue)
            assertTrue(viewModel.state.value.canSubmit)
            assertEquals("demo", recording.value.recordingId)
        }

    @Test
    fun `a missing exported payload never produces an empty queue request`() =
        runTest(dispatcher) {
            every { recorder.exportJson() } returns null
            viewModel.submitCurrent()
            advanceUntilIdle()
            assertEquals(WorkflowExecutionIssue.NO_RECORDING, viewModel.state.value.issue)
            coVerify(exactly = 0) { coordinator.submit(any<String>()) }
        }

    @Test
    fun `export must match the recording currently shown for review`() =
        runTest(dispatcher) {
            val original = rawRecording()
            for (changed in listOf(
                original.copy(id = "another-recording"),
                original.copy(name = "Another demo"),
                original.copy(targetPackage = "another.app"),
                original.copy(events = original.events.map { it.copy(value = "different") }),
            )) {
                every { recorder.exportJson() } returns Json.encodeToString(changed)
                viewModel.submitCurrent()
                advanceUntilIdle()
                assertEquals(
                    "RECORDING_CHANGED",
                    viewModel.state.value.issue
                        ?.name,
                )
                assertEquals(review(), recording.value)
                coVerify(exactly = 0) { coordinator.submit(any<String>()) }
            }
        }

    @Test
    fun `review changing during export cannot submit a stale snapshot`() =
        runTest(dispatcher) {
            every { recorder.exportJson() } answers {
                recording.value = review().copy(recordingId = "newer-recording")
                RAW_RECORDING
            }
            viewModel.submitCurrent()
            advanceUntilIdle()
            assertEquals(
                "RECORDING_CHANGED",
                viewModel.state.value.issue
                    ?.name,
            )
            coVerify(exactly = 0) { coordinator.submit(any<String>()) }
        }

    @Test
    fun `malformed exported recording remains a review draft`() =
        runTest(dispatcher) {
            every { recorder.exportJson() } returns "{broken"
            viewModel.submitCurrent()
            advanceUntilIdle()
            assertEquals(
                "RECORDING_CHANGED",
                viewModel.state.value.issue
                    ?.name,
            )
            assertEquals(review(), recording.value)
            coVerify(exactly = 0) { coordinator.submit(any<String>()) }
        }

    @Test
    fun `a claimed job cannot be cancelled from the queue button`() =
        runTest(dispatcher) {
            queue.value = WorkflowJobUiState(latestJob = job(WorkflowJobStatus.RUNNING))
            viewModel.cancelQueued()
            advanceUntilIdle()
            coVerify(exactly = 0) { coordinator.cancelQueued() }
        }

    @Test
    fun `double tapping cancel only sends one queue cancellation`() =
        runTest(dispatcher) {
            queue.value = WorkflowJobUiState(latestJob = job(WorkflowJobStatus.QUEUED))
            val cancelled = CompletableDeferred<Boolean>()
            coEvery { coordinator.cancelQueued() } coAnswers { cancelled.await() }
            viewModel.cancelQueued()
            viewModel.cancelQueued()
            advanceUntilIdle()
            coVerify(exactly = 1) { coordinator.cancelQueued() }
            cancelled.complete(true)
            advanceUntilIdle()
            assertFalse(viewModel.state.value.submitting)
        }

    @Test
    fun `a claim racing with cancellation shows running instead of a submission error`() =
        runTest(dispatcher) {
            queue.value = WorkflowJobUiState(latestJob = job(WorkflowJobStatus.QUEUED))
            coEvery { coordinator.cancelQueued() } coAnswers {
                queue.value = WorkflowJobUiState(latestJob = job(WorkflowJobStatus.RUNNING))
                false
            }
            viewModel.cancelQueued()
            advanceUntilIdle()
            assertEquals(
                WorkflowJobStatus.RUNNING,
                viewModel.state.value.job
                    ?.status,
            )
            assertNull(viewModel.state.value.issue)
        }

    companion object {
        private val RAW_RECORDING = Json.encodeToString(rawRecording())

        private fun rawRecording() =
            RecordedWorkflow(
                id = "demo",
                name = "Demo",
                targetPackage = RecordingViewModel.TARGET_PACKAGE,
                startedAtEpochMs = 1L,
                endedAtEpochMs = 2L,
                events = review().events,
            )

        private fun review() =
            RecordingUiState(
                status = RecordingStatus.REVIEW,
                name = "Demo",
                targetPackage = RecordingViewModel.TARGET_PACKAGE,
                recordingId = "demo",
                events = listOf(RecordedWorkflowEvent(1, 0, "click", RecordedSelector(RecordingViewModel.TARGET_PACKAGE))),
            )

        private fun job(status: WorkflowJobStatus) =
            WorkflowJob(
                id = "job-1",
                recordingId = "demo",
                name = "Demo",
                status = status,
                createdAtEpochMs = 1L,
                updatedAtEpochMs = 2L,
            )
    }
}
