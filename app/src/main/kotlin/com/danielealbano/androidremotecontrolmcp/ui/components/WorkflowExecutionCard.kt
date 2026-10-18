@file:Suppress("FunctionNaming", "LongMethod")

package com.danielealbano.androidremotecontrolmcp.ui.components

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import com.danielealbano.androidremotecontrolmcp.R
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobIssue
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowJobStatus
import com.danielealbano.androidremotecontrolmcp.services.workflow.WorkflowTask
import com.danielealbano.androidremotecontrolmcp.ui.viewmodels.WorkflowExecutionIssue
import com.danielealbano.androidremotecontrolmcp.ui.viewmodels.WorkflowExecutionUiState
import java.util.Locale

@Composable
fun WorkflowExecutionCard(
    state: WorkflowExecutionUiState,
    onSubmit: () -> Unit,
    onCancelQueued: () -> Unit,
) {
    val task = WorkflowTask()
    val activeStatus = state.job?.status?.takeIf { it.isActive }
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer)) {
        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(stringResource(R.string.workflow_template_title), style = MaterialTheme.typography.titleMedium)
            Text(stringResource(R.string.workflow_template_scope, task.query), style = MaterialTheme.typography.bodyMedium)
            Text(stringResource(R.string.workflow_template_target, task.expectedFirstTitle), style = MaterialTheme.typography.bodySmall)
            Text(stringResource(R.string.workflow_template_finish), style = MaterialTheme.typography.bodySmall)
            Button(onClick = onSubmit, enabled = state.canSubmit, modifier = Modifier.fillMaxWidth()) {
                Text(
                    stringResource(
                        when {
                            state.submitting -> R.string.workflow_submitting
                            activeStatus != null -> jobStatusLabel(activeStatus, state.companionOnline)
                            else -> R.string.workflow_submit
                        },
                    ),
                )
            }
            Text(
                stringResource(if (state.companionOnline) R.string.workflow_companion_online else R.string.workflow_companion_offline),
                style = MaterialTheme.typography.bodySmall,
            )
            state.issue?.let {
                Text(stringResource(executionIssueLabel(it)), color = MaterialTheme.colorScheme.error)
            } ?: state.queueIssue?.let {
                Text(stringResource(queueIssueLabel(it)), color = MaterialTheme.colorScheme.error)
            }
        }
    }
    state.job?.let { job ->
        Card {
            Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(
                    stringResource(
                        if (state.isCurrentRecordingJob) R.string.workflow_latest_job else R.string.workflow_previous_job,
                        job.name,
                    ),
                    style = MaterialTheme.typography.titleSmall,
                )
                Text(
                    stringResource(jobStatusLabel(job.status, state.companionOnline)),
                    style = MaterialTheme.typography.titleMedium,
                )
                if (job.status.isActive) LinearProgressIndicator(Modifier.fillMaxWidth())
                Text(
                    stringResource(
                        when (job.status) {
                            WorkflowJobStatus.QUEUED -> R.string.workflow_queue_hint
                            WorkflowJobStatus.VALIDATING -> R.string.workflow_validating_hint
                            WorkflowJobStatus.GENERATING -> R.string.workflow_generating_hint
                            WorkflowJobStatus.RUNNING -> R.string.workflow_running_hint
                            WorkflowJobStatus.SUCCEEDED -> R.string.workflow_success_hint
                            WorkflowJobStatus.FAILED -> failedHintLabel(job.summary?.errorCode)
                            WorkflowJobStatus.CANCELLED -> R.string.workflow_cancelled_hint
                        },
                    ),
                    style = MaterialTheme.typography.bodyMedium,
                )
                job.summary?.let { summary ->
                    val unknown = stringResource(R.string.workflow_metric_unknown)
                    val elapsed =
                        summary.piElapsedMs?.let {
                            stringResource(R.string.workflow_seconds, String.format(Locale.getDefault(), "%.2f", it / 1000.0))
                        } ?: unknown
                    Text(stringResource(R.string.workflow_metric_elapsed, elapsed), style = MaterialTheme.typography.bodyMedium)
                    Text(
                        stringResource(
                            R.string.workflow_metric_requests,
                            summary.modelCalls?.toString() ?: unknown,
                            summary.fallbackRequests?.toString() ?: unknown,
                        ),
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    summary.errorCode?.let {
                        Text(stringResource(R.string.workflow_error_code, it), style = MaterialTheme.typography.bodySmall)
                    }
                }
                if (job.status == WorkflowJobStatus.QUEUED) {
                    TextButton(onClick = onCancelQueued, enabled = !state.submitting) {
                        Text(stringResource(R.string.workflow_cancel_queue))
                    }
                }
            }
        }
    }
}

private fun jobStatusLabel(
    status: WorkflowJobStatus,
    online: Boolean,
): Int =
    when (status) {
        WorkflowJobStatus.QUEUED -> if (online) R.string.workflow_status_queued else R.string.workflow_status_offline
        WorkflowJobStatus.VALIDATING -> R.string.workflow_status_validating
        WorkflowJobStatus.GENERATING -> R.string.workflow_status_generating
        WorkflowJobStatus.RUNNING -> R.string.workflow_status_running
        WorkflowJobStatus.SUCCEEDED -> R.string.workflow_status_success
        WorkflowJobStatus.FAILED -> R.string.workflow_status_failed
        WorkflowJobStatus.CANCELLED -> R.string.workflow_status_cancelled
    }

private fun executionIssueLabel(issue: WorkflowExecutionIssue): Int =
    when (issue) {
        WorkflowExecutionIssue.NO_RECORDING -> R.string.workflow_issue_no_recording
        WorkflowExecutionIssue.UNSUPPORTED_APP -> R.string.workflow_issue_scope
        WorkflowExecutionIssue.SUBMISSION_FAILED -> R.string.workflow_issue_submit
        WorkflowExecutionIssue.RECORDING_CHANGED -> R.string.workflow_issue_recording_changed
    }

private fun queueIssueLabel(issue: WorkflowJobIssue): Int =
    when (issue) {
        WorkflowJobIssue.INVALID_RECORDING -> R.string.workflow_issue_scope
        WorkflowJobIssue.ACTIVE_JOB -> R.string.workflow_issue_active
        WorkflowJobIssue.COMPANION_OFFLINE -> R.string.workflow_companion_offline
        WorkflowJobIssue.STORAGE_FAILURE -> R.string.workflow_issue_storage
    }

private fun failedHintLabel(code: String?): Int =
    when (code) {
        "UNSUPPORTED_RECORDING", "INVALID_RECORDING" -> R.string.workflow_hint_unsupported
        "VALIDATION_FAILED" -> R.string.workflow_hint_validation
        "GENERATION_FAILED" -> R.string.workflow_hint_generation
        "EXECUTION_FAILED" -> R.string.workflow_hint_execution
        "PUBLISH_FAILED" -> R.string.workflow_hint_publish
        "WORKER_LEASE_EXPIRED", "INVALID_CLAIM" -> R.string.workflow_hint_connection
        "PROCESS_RESTARTED" -> R.string.workflow_hint_restart
        else -> R.string.workflow_failed_hint
    }
