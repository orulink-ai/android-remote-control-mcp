@file:Suppress("FunctionNaming")

package com.danielealbano.androidremotecontrolmcp.ui.screens

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.danielealbano.androidremotecontrolmcp.ui.navigation.ServerRoute
import com.danielealbano.androidremotecontrolmcp.ui.viewmodels.MainViewModel

@Composable
fun ServerTabScreen(
    onNavigateToPermissions: () -> Unit,
    onNavigateToNetworkSettings: () -> Unit,
    onNavigateToTunnelSettings: () -> Unit,
    onOpenPrivacySettings: () -> Unit,
    modifier: Modifier = Modifier,
    viewModel: MainViewModel = hiltViewModel(),
) {
    val navController = rememberNavController()
    NavHost(
        navController = navController,
        startDestination = ServerRoute.Index.route,
        modifier = modifier,
    ) {
        composable(ServerRoute.Index.route) {
            ServerScreen(
                onNavigateToPermissions = onNavigateToPermissions,
                onShowAllLogs = { navController.navigate(ServerRoute.Logs.route) },
                onOpenRecording = { navController.navigate(ServerRoute.Recording.route) },
                onNavigateToNetworkSettings = onNavigateToNetworkSettings,
                onNavigateToTunnelSettings = onNavigateToTunnelSettings,
                onOpenPrivacySettings = onOpenPrivacySettings,
                viewModel = viewModel,
            )
        }
        composable(ServerRoute.Recording.route) {
            RecordingScreen(
                onBack = { navController.popBackStack() },
                onOpenPermissions = onNavigateToPermissions,
            )
        }
        composable(ServerRoute.Logs.route) {
            LogsScreen(onBack = { navController.popBackStack() })
        }
    }
}
