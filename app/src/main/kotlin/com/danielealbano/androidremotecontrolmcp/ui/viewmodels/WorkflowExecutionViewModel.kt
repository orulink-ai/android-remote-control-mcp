package com.danielealbano.androidremotecontrolmcp.ui.viewmodels

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordedWorkflow
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordingStatus
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordingUiState
import com.danielealbano.androidremotecontrolmcp.services.recording.WorkflowRecorder
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobCoordinator
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobStatus
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobUiState
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import javax.inject.Inject

/** Submits an immutable recording to the local queue; skill generation and phone actions stay on the computer. */
@HiltViewModel
class WorkflowExecutionViewModel
    @Inject
    constructor(
        private val recorder: WorkflowRecorder,
        private val coordinator: WorkflowJobCoordinator,
    ) : ViewModel() {
        private data class SubmissionState(
            val submitting: Boolean = false,
            val issue: WorkflowExecutionIssue? = null,
        )

        private val submission = MutableStateFlow(SubmissionState())
        val state =
            combine(recorder.state, coordinator.state, submission, ::project)
                .stateIn(
                    viewModelScope,
                    SharingStarted.Eagerly,
                    project(recorder.state.value, coordinator.state.value, submission.value),
                )

        fun submitCurrent() {
            if (submission.value.submitting || coordinator.state.value.latestJob
                    ?.status
                    ?.isActive == true
            ) {
                return
            }
            val recording = recorder.state.value.let { it.copy(events = it.events.toList()) }
            if (recording.status != RecordingStatus.REVIEW || recording.events.isEmpty()) {
                submission.update { it.copy(issue = WorkflowExecutionIssue.NO_RECORDING) }
                return
            }
            if (recording.targetPackage != RecordingViewModel.TARGET_PACKAGE) {
                submission.update { it.copy(issue = WorkflowExecutionIssue.UNSUPPORTED_APP) }
                return
            }
            val payload = runCatching { recorder.exportJson() }.getOrNull()
            if (payload.isNullOrBlank()) {
                submission.update { it.copy(issue = WorkflowExecutionIssue.NO_RECORDING) }
                return
            }
            val exported = runCatching { Json.decodeFromString<RecordedWorkflow>(payload) }.getOrNull()
            if (exported == null || !matchesReview(exported, recording) || !matchesReview(exported, recorder.state.value)) {
                submission.update { it.copy(issue = WorkflowExecutionIssue.RECORDING_CHANGED) }
                return
            }
            // Set this before launching: a second click can arrive before Compose sees the new queue state.
            submission.value = SubmissionState(submitting = true)
            viewModelScope.launch {
                try {
                    if (!matchesReview(exported, recorder.state.value)) {
                        submission.update { it.copy(issue = WorkflowExecutionIssue.RECORDING_CHANGED) }
                        return@launch
                    }
                    if (!coordinator.submit(payload) && coordinator.state.value.error == null) {
                        submission.update { it.copy(issue = WorkflowExecutionIssue.SUBMISSION_FAILED) }
                    }
                } catch (cancelled: CancellationException) {
                    throw cancelled
                } catch (_: Exception) {
                    submission.update { it.copy(issue = WorkflowExecutionIssue.SUBMISSION_FAILED) }
                } finally {
                    submission.update { it.copy(submitting = false) }
                }
            }
        }

        fun cancelQueued() {
            if (submission.value.submitting || coordinator.state.value.latestJob
                    ?.status != WorkflowJobStatus.QUEUED
            ) {
                return
            }
            submission.value = SubmissionState(submitting = true)
            viewModelScope.launch {
                try {
                    val cancelled = coordinator.cancelQueued()
                    val queue = coordinator.state.value
                    if (!cancelled && queue.latestJob?.status == WorkflowJobStatus.QUEUED && queue.error == null) {
                        submission.update { it.copy(issue = WorkflowExecutionIssue.SUBMISSION_FAILED) }
                    }
                } catch (cancelled: CancellationException) {
                    throw cancelled
                } catch (_: Exception) {
                    submission.update { it.copy(issue = WorkflowExecutionIssue.SUBMISSION_FAILED) }
                } finally {
                    submission.update { it.copy(submitting = false) }
                }
            }
        }

        private fun matchesReview(
            exported: RecordedWorkflow,
            review: RecordingUiState,
        ): Boolean =
            review.status == RecordingStatus.REVIEW &&
                exported.id == review.recordingId && exported.name == review.name &&
                exported.targetPackage == review.targetPackage && exported.events == review.events

        private fun project(
            recording: RecordingUiState,
            queue: WorkflowJobUiState,
            local: SubmissionState,
        ): WorkflowExecutionUiState =
            WorkflowExecutionUiState(
                job = queue.latestJob,
                isCurrentRecordingJob = queue.latestJob?.recordingId?.let { it == recording.recordingId } ?: false,
                companionOnline = queue.companionOnline,
                submitting = local.submitting,
                canSubmit =
                    recording.status == RecordingStatus.REVIEW &&
                        recording.events.isNotEmpty() &&
                        recording.targetPackage == RecordingViewModel.TARGET_PACKAGE &&
                        !local.submitting && queue.latestJob?.status?.isActive != true,
                issue = local.issue,
                queueIssue = queue.error,
            )
    }
