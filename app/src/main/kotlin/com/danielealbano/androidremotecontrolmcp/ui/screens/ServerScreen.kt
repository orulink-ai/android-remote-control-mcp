@file:Suppress("FunctionNaming", "LongMethod", "MagicNumber")

package com.danielealbano.androidremotecontrolmcp.ui.screens

import android.content.Intent
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.os.Handler
import android.os.Looper
import android.widget.Toast
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.danielealbano.androidremotecontrolmcp.R
import com.danielealbano.androidremotecontrolmcp.data.model.BindingAddress
import com.danielealbano.androidremotecontrolmcp.ui.ApprovalActivity
import com.danielealbano.androidremotecontrolmcp.ui.components.BatteryOptimizationCard
import com.danielealbano.androidremotecontrolmcp.ui.components.CalloutCard
import com.danielealbano.androidremotecontrolmcp.ui.components.ConnectionInfoCard
import com.danielealbano.androidremotecontrolmcp.ui.components.OrulinkBrandMark
import com.danielealbano.androidremotecontrolmcp.ui.components.PrivacyModeCard
import com.danielealbano.androidremotecontrolmcp.ui.components.ServerLogsSection
import com.danielealbano.androidremotecontrolmcp.ui.components.ServerStatusCard
import com.danielealbano.androidremotecontrolmcp.ui.components.WorkflowRecordingCard
import com.danielealbano.androidremotecontrolmcp.ui.viewmodels.ChannelViewModel
import com.danielealbano.androidremotecontrolmcp.ui.viewmodels.ControlExperienceViewModel
import com.danielealbano.androidremotecontrolmcp.ui.viewmodels.LogsViewModel
import com.danielealbano.androidremotecontrolmcp.ui.viewmodels.MainViewModel
import com.danielealbano.androidremotecontrolmcp.ui.viewmodels.PrivacyViewModel
import com.danielealbano.androidremotecontrolmcp.utils.NetworkUtils

@Composable
fun ServerScreen(
    onNavigateToPermissions: () -> Unit,
    onShowAllLogs: () -> Unit,
    onOpenRecording: () -> Unit,
    onNavigateToNetworkSettings: () -> Unit,
    onNavigateToTunnelSettings: () -> Unit,
    onOpenPrivacySettings: () -> Unit,
    modifier: Modifier = Modifier,
    viewModel: MainViewModel = hiltViewModel(),
    channelViewModel: ChannelViewModel = hiltViewModel(),
    privacyViewModel: PrivacyViewModel = hiltViewModel(),
    controlViewModel: ControlExperienceViewModel = hiltViewModel(),
) {
    val context = LocalContext.current
    val clipboardManager = LocalClipboardManager.current
    val logsViewModel: LogsViewModel = hiltViewModel()

    val privacyConfig by privacyViewModel.privacyConfig.collectAsStateWithLifecycle()
    val privacyCardDismissed by privacyViewModel.privacyCardDismissed.collectAsStateWithLifecycle()
    val serverConfig by viewModel.serverConfig.collectAsStateWithLifecycle()
    val serverStatus by viewModel.serverStatus.collectAsStateWithLifecycle()
    val controlState by controlViewModel.controlState.collectAsStateWithLifecycle()
    val recentServerLogs by logsViewModel.recentServerLogs.collectAsStateWithLifecycle()
    val tunnelStatus by viewModel.tunnelStatus.collectAsStateWithLifecycle()

    val isAccessibilityEnabled by viewModel.isAccessibilityEnabled.collectAsStateWithLifecycle()
    val isBatteryOptimizationIgnored by viewModel.isBatteryOptimizationIgnored.collectAsStateWithLifecycle()
    val pendingApprovalCount by viewModel.pendingApprovalCount.collectAsStateWithLifecycle()

    val channelConfig by channelViewModel.eventChannelConfig.collectAsStateWithLifecycle()
    val channelStatus by channelViewModel.channelConnectionStatus.collectAsStateWithLifecycle()

    val deviceIp = currentDeviceIpAddress()
    val localOnly = serverConfig.bindingAddress == BindingAddress.LOCALHOST && !serverConfig.tunnelEnabled
    val copiedToClipboardMessage = stringResource(R.string.copied_to_clipboard)
    var showChannelNotConfiguredDialog by remember { mutableStateOf(false) }

    Column(modifier = modifier.fillMaxSize()) {
        Row(
            modifier = Modifier.padding(horizontal = 16.dp, vertical = 16.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            OrulinkBrandMark()
            Spacer(Modifier.width(12.dp))
            Column {
                Text(stringResource(R.string.orulink_home_title), style = MaterialTheme.typography.titleLarge)
                Text(
                    stringResource(R.string.orulink_home_subtitle),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        Column(
            modifier =
                Modifier
                    .weight(1f)
                    .verticalScroll(rememberScrollState())
                    .padding(16.dp),
        ) {
            ServerStatusCard(
                serverStatus = serverStatus,
                channelStatus = channelStatus,
                channelEnabled = channelConfig.enabled,
                onMcpStartClick = { viewModel.startServer(context) },
                onMcpStopClick = { viewModel.stopServer(context) },
                onChannelStartClick = {
                    if (channelConfig.endpointUrl.isBlank()) {
                        showChannelNotConfiguredDialog = true
                    } else {
                        channelViewModel.startChannel()
                    }
                },
                onChannelStopClick = { channelViewModel.stopChannel() },
                startEnabled = isAccessibilityEnabled,
                controlState = controlState,
            )

            Spacer(Modifier.height(16.dp))
            WorkflowRecordingCard(onOpen = onOpenRecording)
            Spacer(Modifier.height(16.dp))
            if (!isAccessibilityEnabled) {
                PermissionWarningCard(onClick = onNavigateToPermissions)
                Spacer(Modifier.height(16.dp))
            }
            if (!serverConfig.oauthEnabled && !serverConfig.bearerTokenEnabled) {
                NoAuthWarningCard()
                Spacer(Modifier.height(16.dp))
            }
            if (pendingApprovalCount > 0) {
                PendingApprovalsCard(
                    count = pendingApprovalCount,
                    onClick = { context.startActivity(Intent(context, ApprovalActivity::class.java)) },
                )
                Spacer(Modifier.height(16.dp))
            }

            ConnectionInfoCard(
                bindingAddress = serverConfig.bindingAddress,
                ipAddress = deviceIp,
                port = serverConfig.port,
                httpsEnabled = serverConfig.httpsEnabled,
                bearerToken = serverConfig.bearerToken,
                tunnelEnabled = serverConfig.tunnelEnabled,
                serverStatus = serverStatus,
                tunnelStatus = tunnelStatus,
                onCopyAll = { text ->
                    clipboardManager.setText(AnnotatedString(text))
                    Toast.makeText(context, copiedToClipboardMessage, Toast.LENGTH_SHORT).show()
                },
                onShare = { text ->
                    val intent =
                        Intent(Intent.ACTION_SEND).apply {
                            type = "text/plain"
                            putExtra(Intent.EXTRA_TEXT, text)
                        }
                    context.startActivity(Intent.createChooser(intent, null))
                },
            )

            Spacer(Modifier.height(16.dp))

            DeviceSetupSection(
                showNetworkOptions = localOnly,
                showBatteryOptions = !isBatteryOptimizationIgnored,
                showPrivacyOptions = !privacyConfig.enabled && !privacyCardDismissed,
                onNavigateToNetworkSettings = onNavigateToNetworkSettings,
                onNavigateToTunnelSettings = onNavigateToTunnelSettings,
                onRequestBatteryExemption = viewModel::requestBatteryOptimizationExemption,
                onOpenPrivacySettings = onOpenPrivacySettings,
                onDismissPrivacy = privacyViewModel::dismissPrivacyCard,
            )

            ServerLogsSection(
                logs = recentServerLogs,
                onShowMore = onShowAllLogs,
            )
        }
    }

    if (showChannelNotConfiguredDialog) {
        AlertDialog(
            onDismissRequest = { showChannelNotConfiguredDialog = false },
            title = { Text(stringResource(R.string.channel_not_configured_dialog_title)) },
            text = { Text(stringResource(R.string.channel_not_configured_dialog_body)) },
            confirmButton = {
                TextButton(onClick = { showChannelNotConfiguredDialog = false }) {
                    Text(stringResource(R.string.channel_not_configured_dialog_ok))
                }
            },
        )
    }
}

@Composable
private fun DeviceSetupSection(
    showNetworkOptions: Boolean,
    showBatteryOptions: Boolean,
    showPrivacyOptions: Boolean,
    onNavigateToNetworkSettings: () -> Unit,
    onNavigateToTunnelSettings: () -> Unit,
    onRequestBatteryExemption: () -> Unit,
    onOpenPrivacySettings: () -> Unit,
    onDismissPrivacy: () -> Unit,
) {
    if (showNetworkOptions) {
        NetworkAccessSuggestionCard(
            onEnableWifi = onNavigateToNetworkSettings,
            onSetUpTunnel = onNavigateToTunnelSettings,
        )
        Spacer(Modifier.height(16.dp))
    }
    if (showBatteryOptions || showPrivacyOptions) {
        Text(
            stringResource(R.string.orulink_setup_section),
            style = MaterialTheme.typography.titleMedium,
            modifier = Modifier.padding(vertical = 8.dp),
        )
    }
    if (showBatteryOptions) {
        BatteryOptimizationCard(onRequestExemption = onRequestBatteryExemption)
        Spacer(Modifier.height(16.dp))
    }
    if (showPrivacyOptions) {
        PrivacyModeCard(onSetupClick = onOpenPrivacySettings, onDismissClick = onDismissPrivacy)
        Spacer(Modifier.height(16.dp))
    }
}

/** Refreshes the displayed address when foregrounded and when the default network changes. */
@Composable
private fun currentDeviceIpAddress(): String {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    var address by remember(context) { mutableStateOf(NetworkUtils.getDeviceIpAddress(context).orEmpty()) }
    DisposableEffect(context, lifecycleOwner) {
        val handler = Handler(Looper.getMainLooper())
        val refreshAddress = Runnable { address = NetworkUtils.getDeviceIpAddress(context).orEmpty() }
        val observer =
            LifecycleEventObserver { _, event ->
                if (event == Lifecycle.Event.ON_RESUME) handler.post(refreshAddress)
            }
        val connectivity = context.getSystemService(ConnectivityManager::class.java)
        val callback =
            object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) {
                    handler.post(refreshAddress)
                }

                override fun onLost(network: Network) {
                    handler.post(refreshAddress)
                }

                override fun onLinkPropertiesChanged(
                    network: Network,
                    linkProperties: LinkProperties,
                ) {
                    handler.post(refreshAddress)
                }
            }
        lifecycleOwner.lifecycle.addObserver(observer)
        connectivity.registerDefaultNetworkCallback(callback)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
            connectivity.unregisterNetworkCallback(callback)
            handler.removeCallbacks(refreshAddress)
        }
    }
    return address
}

@Composable
private fun NoAuthWarningCard() {
    CalloutCard(
        icon = Icons.Default.Warning,
        title = stringResource(R.string.access_no_auth_warning_title),
    )
}

@Composable
private fun NetworkAccessSuggestionCard(
    onEnableWifi: () -> Unit,
    onSetUpTunnel: () -> Unit,
) {
    CalloutCard(
        icon = Icons.Default.Info,
        title = stringResource(R.string.server_network_access_suggestion_title),
    ) {
        TextButton(onClick = onEnableWifi) {
            Text(stringResource(R.string.server_network_access_suggestion_wifi))
        }
        TextButton(onClick = onSetUpTunnel) {
            Text(stringResource(R.string.server_network_access_suggestion_tunnel))
        }
    }
}

@Composable
private fun PendingApprovalsCard(
    count: Int,
    onClick: () -> Unit,
) {
    CalloutCard(
        icon = Icons.Default.Notifications,
        title = stringResource(R.string.server_pending_approvals_title, count),
    ) {
        TextButton(onClick = onClick) {
            Text(stringResource(R.string.server_pending_approvals_action))
        }
    }
}

@Composable
private fun PermissionWarningCard(onClick: () -> Unit) {
    CalloutCard(
        icon = Icons.Default.Warning,
        title = stringResource(R.string.permission_warning_title),
    ) {
        TextButton(onClick = onClick) {
            Text(stringResource(R.string.permission_warning_action))
        }
    }
}
