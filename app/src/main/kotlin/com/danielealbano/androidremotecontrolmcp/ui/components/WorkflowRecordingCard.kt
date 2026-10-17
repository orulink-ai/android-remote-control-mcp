@file:Suppress("FunctionNaming")

package com.danielealbano.androidremotecontrolmcp.ui.components

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.danielealbano.androidremotecontrolmcp.R
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordingStatus
import com.danielealbano.androidremotecontrolmcp.ui.viewmodels.RecordingViewModel

@Composable
fun WorkflowRecordingCard(
    onOpen: () -> Unit,
    viewModel: RecordingViewModel = hiltViewModel(),
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val active = state.status == RecordingStatus.RECORDING || state.status == RecordingStatus.PAUSED
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer),
    ) {
        Column(Modifier.padding(16.dp)) {
            Text(
                stringResource(
                    when (state.status) {
                        RecordingStatus.RECORDING -> R.string.recording_status_active
                        RecordingStatus.PAUSED -> R.string.recording_status_paused
                        else -> R.string.recording_home_title
                    },
                ),
                style = MaterialTheme.typography.titleMedium,
            )
            if (active) {
                Text(stringResource(R.string.recording_step_count, state.stepCount), style = MaterialTheme.typography.bodyMedium)
            } else {
                Text(stringResource(R.string.recording_home_body), style = MaterialTheme.typography.bodyMedium)
            }
            TextButton(onClick = onOpen) {
                Text(stringResource(if (active) R.string.recording_manage else R.string.recording_home_action))
            }
        }
    }
}
