@file:Suppress("FunctionNaming", "LongMethod", "MagicNumber")

package com.danielealbano.androidremotecontrolmcp.ui.screens

import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.danielealbano.androidremotecontrolmcp.R
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordedWorkflowEvent
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordingIssue
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordingStatus
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordingUiState
import com.danielealbano.androidremotecontrolmcp.ui.components.WorkflowExecutionCard
import com.danielealbano.androidremotecontrolmcp.ui.viewmodels.RecordingUiError
import com.danielealbano.androidremotecontrolmcp.ui.viewmodels.RecordingViewModel
import com.danielealbano.androidremotecontrolmcp.ui.viewmodels.WorkflowExecutionViewModel
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun RecordingScreen(
    onBack: () -> Unit,
    onOpenPermissions: () -> Unit,
    viewModel: RecordingViewModel = hiltViewModel(),
    executionViewModel: WorkflowExecutionViewModel = hiltViewModel(),
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val uiError by viewModel.error.collectAsStateWithLifecycle()
    val executionState by executionViewModel.state.collectAsStateWithLifecycle()
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var name by rememberSaveable { mutableStateOf("") }
    var showDiscard by rememberSaveable { mutableStateOf(false) }
    var pendingExport by remember { mutableStateOf<String?>(null) }
    val exportedMessage = stringResource(R.string.recording_exported)
    val exportFailedMessage = stringResource(R.string.recording_export_failed)
    val defaultName = stringResource(R.string.recording_default_name)
    val exportLauncher =
        rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("application/json")) { uri ->
            val json = pendingExport
            pendingExport = null
            if (uri != null && json != null) {
                scope.launch {
                    val success =
                        withContext(Dispatchers.IO) {
                            runCatching {
                                val output = context.contentResolver.openOutputStream(uri) ?: error("Cannot open destination")
                                output.bufferedWriter(Charsets.UTF_8).use { it.write(json) }
                            }.isSuccess
                        }
                    Toast.makeText(context, if (success) exportedMessage else exportFailedMessage, Toast.LENGTH_LONG).show()
                }
            }
        }

    Column(Modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text(stringResource(R.string.recording_title)) },
            windowInsets = WindowInsets(0),
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = stringResource(R.string.recording_back))
                }
            },
        )
        Column(
            modifier = Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            RecordingIntroduction(state)
            uiError?.let { RecordingError(it, onOpenPermissions) }
            if (uiError == null) {
                state.issue?.let { issue ->
                    Text(
                        stringResource(recordingIssueLabel(issue)),
                        color = MaterialTheme.colorScheme.error,
                        style = MaterialTheme.typography.bodyMedium,
                    )
                } ?: state.error?.let { message ->
                    Text(message, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyMedium)
                }
            }
            when (state.status) {
                RecordingStatus.IDLE -> {
                    OutlinedTextField(
                        value = name,
                        onValueChange = { name = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text(stringResource(R.string.recording_name_label)) },
                        placeholder = { Text(defaultName) },
                        singleLine = true,
                    )
                    Text(stringResource(R.string.recording_scope), style = MaterialTheme.typography.bodyMedium)
                    Button(
                        onClick = { viewModel.start(name.ifBlank { defaultName }) },
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text(stringResource(R.string.recording_start)) }
                }

                RecordingStatus.RECORDING, RecordingStatus.PAUSED -> {
                    RecordingControls(
                        state = state,
                        onPause = viewModel::pause,
                        onResume = viewModel::resume,
                        onStop = viewModel::stop,
                        onOpenTarget = viewModel::openTarget,
                        onDiscard = { showDiscard = true },
                    )
                    RecordedSteps(state.events)
                }

                RecordingStatus.REVIEW -> {
                    Text(state.name, style = MaterialTheme.typography.titleLarge)
                    Text(stringResource(R.string.recording_review_body), style = MaterialTheme.typography.bodyMedium)
                    WorkflowExecutionCard(
                        state = executionState,
                        onSubmit = executionViewModel::submitCurrent,
                        onCancelQueued = executionViewModel::cancelQueued,
                    )
                    OutlinedButton(
                        enabled = state.canExport,
                        onClick = {
                            pendingExport = viewModel.exportJson()
                            if (pendingExport != null) {
                                exportLauncher.launch("workflow-${state.recordingId ?: "draft"}.json")
                            } else {
                                Toast.makeText(context, exportFailedMessage, Toast.LENGTH_LONG).show()
                            }
                        },
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text(stringResource(R.string.recording_export)) }
                    OutlinedButton(
                        onClick = { showDiscard = true },
                        enabled = !executionState.submitting && executionState.job?.status?.isActive != true,
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        Text(stringResource(R.string.recording_new))
                    }
                    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer)) {
                        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            Text(stringResource(R.string.recording_parameter_title), style = MaterialTheme.typography.titleSmall)
                            Text(stringResource(R.string.recording_parameter_body), style = MaterialTheme.typography.bodyMedium)
                        }
                    }
                    RecordedSteps(state.events)
                }
            }
        }
    }
    if (showDiscard) {
        AlertDialog(
            onDismissRequest = { showDiscard = false },
            title = { Text(stringResource(R.string.recording_discard_title)) },
            text = { Text(stringResource(R.string.recording_discard_body)) },
            confirmButton = {
                TextButton(onClick = {
                    viewModel.discard()
                    showDiscard = false
                }) { Text(stringResource(R.string.recording_discard)) }
            },
            dismissButton = {
                TextButton(onClick = { showDiscard = false }) { Text(stringResource(R.string.recording_keep)) }
            },
        )
    }
}

@Composable
private fun RecordingIntroduction(state: RecordingUiState) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer)) {
        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(
                stringResource(
                    when (state.status) {
                        RecordingStatus.IDLE -> R.string.recording_intro_title
                        RecordingStatus.RECORDING -> R.string.recording_status_active
                        RecordingStatus.PAUSED -> R.string.recording_status_paused
                        RecordingStatus.REVIEW -> R.string.recording_status_review
                    },
                ),
                style = MaterialTheme.typography.titleMedium,
            )
            Text(
                stringResource(
                    if (state.status == RecordingStatus.IDLE) R.string.recording_intro_body else R.string.recording_step_count,
                    state.stepCount,
                ),
                style = MaterialTheme.typography.bodyMedium,
            )
            if (state.status == RecordingStatus.RECORDING || state.status == RecordingStatus.PAUSED) {
                Text(stringResource(R.string.recording_return_hint), style = MaterialTheme.typography.bodyMedium)
            }
        }
    }
}

@Composable
private fun RecordingControls(
    state: RecordingUiState,
    onPause: () -> Unit,
    onResume: () -> Unit,
    onStop: () -> Unit,
    onOpenTarget: () -> Unit,
    onDiscard: () -> Unit,
) {
    Row(Modifier.fillMaxWidth()) {
        OutlinedButton(
            onClick = if (state.status == RecordingStatus.RECORDING) onPause else onResume,
            modifier = Modifier.weight(1f),
        ) {
            Text(stringResource(if (state.status == RecordingStatus.RECORDING) R.string.recording_pause else R.string.recording_resume))
        }
        Spacer(Modifier.width(12.dp))
        Button(onClick = onStop, modifier = Modifier.weight(1f)) { Text(stringResource(R.string.recording_stop)) }
    }
    OutlinedButton(onClick = onOpenTarget, modifier = Modifier.fillMaxWidth()) {
        Text(stringResource(R.string.recording_open_target))
    }
    TextButton(onClick = onDiscard) { Text(stringResource(R.string.recording_discard)) }
}

@Composable
private fun RecordedSteps(events: List<RecordedWorkflowEvent>) {
    Text(stringResource(R.string.recording_steps_title), style = MaterialTheme.typography.titleMedium)
    if (events.isEmpty()) {
        Text(stringResource(R.string.recording_steps_empty), color = MaterialTheme.colorScheme.onSurfaceVariant)
        return
    }
    events.forEach { event ->
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(
                "${event.sequence}. ${stringResource(recordedActionLabel(event.type))}",
                style = MaterialTheme.typography.titleSmall,
            )
            val target =
                listOf(event.selector.contentDescription, event.selector.text, event.selector.resourceId)
                    .firstOrNull { !it.isNullOrBlank() }
            val candidateLabel =
                when (event.selector.semanticRole) {
                    "like_toggle" -> R.string.recording_candidate_like
                    "collect_toggle" -> R.string.recording_candidate_collect
                    "open_target" -> R.string.recording_candidate_post
                    else -> null
                }
            Text(
                candidateLabel?.let { stringResource(it) } ?: target ?: stringResource(R.string.recording_unlabelled_target),
                style = MaterialTheme.typography.bodyMedium,
            )
            if (!event.value.isNullOrBlank()) {
                Text(event.value, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            HorizontalDivider(Modifier.padding(top = 8.dp))
        }
    }
}

private fun recordedActionLabel(type: String): Int =
    when (type) {
        "click", "CLICK", "TYPE_VIEW_CLICKED" -> R.string.recording_action_click
        "long_click", "LONG_CLICK", "TYPE_VIEW_LONG_CLICKED" -> R.string.recording_action_long_click
        "text", "text_changed", "input", "TYPE_VIEW_TEXT_CHANGED" -> R.string.recording_action_input
        "scroll", "SCROLL", "TYPE_VIEW_SCROLLED" -> R.string.recording_action_scroll
        else -> R.string.recording_action_observe
    }

@Composable
private fun RecordingError(
    error: RecordingUiError,
    onOpenPermissions: () -> Unit,
) {
    Column {
        Text(
            stringResource(
                when (error) {
                    RecordingUiError.ACCESSIBILITY_UNAVAILABLE -> R.string.recording_error_accessibility
                    RecordingUiError.APP_UNAVAILABLE -> R.string.recording_error_app_missing
                    RecordingUiError.APP_LAUNCH_FAILED -> R.string.recording_error_app_launch
                },
            ),
            color = MaterialTheme.colorScheme.error,
        )
        if (error == RecordingUiError.ACCESSIBILITY_UNAVAILABLE) {
            TextButton(onClick = onOpenPermissions) { Text(stringResource(R.string.permission_warning_action)) }
        }
    }
}

private fun recordingIssueLabel(issue: RecordingIssue): Int =
    when (issue) {
        RecordingIssue.INVALID_NAME -> R.string.recording_issue_name
        RecordingIssue.INVALID_TARGET -> R.string.recording_issue_target
        RecordingIssue.SERVICE_UNAVAILABLE -> R.string.recording_error_accessibility
        RecordingIssue.ACTIVE_RECORDING -> R.string.recording_issue_active
        RecordingIssue.EVENT_LIMIT -> R.string.recording_issue_limit
        RecordingIssue.SERVICE_DISCONNECTED -> R.string.recording_issue_disconnected
        RecordingIssue.SAVE_FAILED -> R.string.recording_issue_save
        RecordingIssue.CAPTURE_FAILED -> R.string.recording_issue_capture
    }
