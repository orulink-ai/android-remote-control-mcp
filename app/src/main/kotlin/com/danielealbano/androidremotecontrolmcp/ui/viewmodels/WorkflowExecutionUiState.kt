package com.danielealbano.androidremotecontrolmcp.ui.viewmodels

import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJob
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobIssue

enum class WorkflowExecutionIssue {
    NO_RECORDING,
    UNSUPPORTED_APP,
    SUBMISSION_FAILED,
    RECORDING_CHANGED,
}

data class WorkflowExecutionUiState(
    val job: WorkflowJob? = null,
    val isCurrentRecordingJob: Boolean = false,
    val companionOnline: Boolean = false,
    val submitting: Boolean = false,
    val canSubmit: Boolean = false,
    val issue: WorkflowExecutionIssue? = null,
    val queueIssue: WorkflowJobIssue? = null,
)
