package com.danielealbano.androidremotecontrolmcp.services.accessibility

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.accessibilityservice.GestureDescription
import android.accessibilityservice.InputMethod
import android.content.ComponentCallbacks2
import android.content.Context
import android.content.Intent
import android.content.res.Configuration
import android.graphics.Bitmap
import android.graphics.Rect
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.Display
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import com.danielealbano.androidremotecontrolmcp.services.control.ControlOverlayController
import com.danielealbano.androidremotecontrolmcp.services.control.ControlSession
import com.danielealbano.androidremotecontrolmcp.services.mcp.McpServerService
import com.danielealbano.androidremotecontrolmcp.services.recording.WorkflowRecorder
import com.danielealbano.androidremotecontrolmcp.services.screencapture.ScreenshotRequestGate
import dagger.hilt.EntryPoint
import dagger.hilt.InstallIn
import dagger.hilt.android.EntryPointAccessors
import dagger.hilt.components.SingletonComponent
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.util.concurrent.Executor
import kotlin.coroutines.resume

@Suppress("TooManyFunctions")
class McpAccessibilityService : AccessibilityService() {
    // AccessibilityNodeCache is in the same package — no import needed
    @EntryPoint
    @InstallIn(SingletonComponent::class)
    interface NodeCacheEntryPoint {
        fun nodeCache(): AccessibilityNodeCache

        fun controlSession(): ControlSession

        fun workflowRecorder(): WorkflowRecorder
    }

    private var serviceScope: CoroutineScope? = null

    private var nodeCache: AccessibilityNodeCache? = null

    private var cacheInvalidationDebouncer: CacheInvalidationDebouncer? = null

    private var controlOverlay: ControlOverlayController? = null

    private var workflowRecorder: WorkflowRecorder? = null

    private val screenshotRequestGate = ScreenshotRequestGate(clockMs = SystemClock::uptimeMillis)

    @Volatile
    private var currentPackageName: String? = null

    @Volatile
    private var currentActivityName: String? = null

    override fun onServiceConnected() {
        super.onServiceConnected()

        instance = this
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
        serviceScope = scope
        controlOverlay = createControlOverlay(scope)
        controlOverlay?.setEnabled(controlOverlayEnabled)
        nodeCache = resolveNodeCache()
        workflowRecorder = resolveWorkflowRecorder()
        workflowRecorder?.onServiceConnected()
        cacheInvalidationDebouncer =
            CacheInvalidationDebouncer(
                scope = scope,
                debounceMillis = CACHE_INVALIDATION_DEBOUNCE_MS,
                onSettled = { invalidateCache(nodeCache) },
            )

        configureServiceInfo()

        Log.i(TAG, "Accessibility service connected")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return
        workflowRecorder?.onAccessibilityEvent(event)

        when (event.eventType) {
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> {
                event.packageName?.toString()?.let { packageName ->
                    currentPackageName = packageName
                }
                event.className?.toString()?.let { className ->
                    currentActivityName = className
                }
                Log.d(
                    TAG,
                    "Window state changed: package=$currentPackageName, " +
                        "activity=$currentActivityName",
                )
            }

            AccessibilityEvent.TYPE_WINDOWS_CHANGED -> {
                // Soft-keyboard show/hide and other window add/remove/bounds changes arrive here.
                Log.d(TAG, "Windows changed (e.g. soft-keyboard show/hide)")
            }

            AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED -> {
                Log.d(TAG, "Window content changed: package=${event.packageName}")
            }

            else -> {
                // Ignored event types
            }
        }

        // A structural window change (keyboard show/hide, rotation, activity/dialog transition)
        // shifts element bounds. Because cached node ids are derived from bounds, and which
        // elements are present/actionable also changes, the cached id->node entries become stale.
        // Schedule a debounced invalidation so the cache is dropped once — and only once — AFTER
        // the transition settles, so the next node lookup resolves against the live tree.
        scheduleCacheInvalidationIfNeeded(event.eventType, cacheInvalidationDebouncer)
    }

    override fun onInterrupt() {
        Log.w(TAG, "Accessibility service interrupted")
        workflowRecorder?.pause()
    }

    override fun onDestroy() {
        Log.i(TAG, "Accessibility service destroying")
        workflowRecorder?.onServiceDisconnected()
        workflowRecorder = null

        // Stop any pending debounced invalidation before tearing down the scope it runs on.
        cacheInvalidationDebouncer?.cancel()
        cacheInvalidationDebouncer = null

        // Flush the node cache — all AccessibilityNodeInfo references become invalid.
        nodeCache?.clear()
        nodeCache = null

        controlOverlay?.destroy()
        controlOverlay = null
        serviceScope?.cancel()
        serviceScope = null
        currentPackageName = null
        currentActivityName = null
        inputMethodInstance = null
        instance = null

        super.onDestroy()
    }

    override fun onLowMemory() {
        super.onLowMemory()
        Log.w(TAG, "Low memory condition reported")
    }

    @Suppress("DEPRECATION")
    override fun onTrimMemory(level: Int) {
        super.onTrimMemory(level)
        val levelName =
            when (level) {
                ComponentCallbacks2.TRIM_MEMORY_RUNNING_MODERATE -> "RUNNING_MODERATE"
                ComponentCallbacks2.TRIM_MEMORY_RUNNING_LOW -> "RUNNING_LOW"
                ComponentCallbacks2.TRIM_MEMORY_RUNNING_CRITICAL -> "RUNNING_CRITICAL"
                ComponentCallbacks2.TRIM_MEMORY_UI_HIDDEN -> "UI_HIDDEN"
                ComponentCallbacks2.TRIM_MEMORY_BACKGROUND -> "BACKGROUND"
                ComponentCallbacks2.TRIM_MEMORY_MODERATE -> "MODERATE"
                ComponentCallbacks2.TRIM_MEMORY_COMPLETE -> "COMPLETE"
                else -> "UNKNOWN($level)"
            }
        Log.w(TAG, "Trim memory: level=$levelName")
    }

    /**
     * Returns the root [AccessibilityNodeInfo] of the currently active window,
     * or null if no window is available.
     */
    fun getRootNode(): AccessibilityNodeInfo? = rootInActiveWindow

    /**
     * Returns all on-screen windows via [AccessibilityService.getWindows].
     * Requires [AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS].
     *
     * @return List of [AccessibilityWindowInfo], or empty list if unavailable.
     */
    fun getAccessibilityWindows(): List<AccessibilityWindowInfo> =
        try {
            windows?.filterNot {
                it.type == AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY &&
                    ControlOverlayController.isControlWindow(it.title)
            } ?: emptyList()
        } catch (
            @Suppress("TooGenericExceptionCaught") e: Exception,
        ) {
            Log.w(TAG, "getWindows() failed: ${e.message}")
            emptyList()
        }

    /**
     * Returns the package name of the currently focused application,
     * or null if unknown.
     */
    fun getCurrentPackageName(): String? = currentPackageName

    /**
     * Returns the class name (activity name) of the currently focused window,
     * or null if unknown.
     */
    fun getCurrentActivityName(): String? = currentActivityName

    /**
     * Returns true if the service is connected and ready to process requests.
     * Does NOT check for an active window — multi-window support handles
     * window availability at tree-parsing time.
     */
    fun isReady(): Boolean = instance != null

    /**
     * Returns the [CoroutineScope] for this service, or null if not connected.
     */
    fun getServiceScope(): CoroutineScope? = serviceScope

    /**
     * Returns the current screen dimensions, density, and orientation.
     *
     * @return [ScreenInfo] with width, height, densityDpi, and orientation.
     */
    fun getScreenInfo(): ScreenInfo {
        val windowManager = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val metrics = windowManager.currentWindowMetrics
        val bounds = metrics.bounds
        val width = bounds.width()
        val height = bounds.height()

        val displayMetrics = resources.displayMetrics
        val densityDpi = displayMetrics.densityDpi

        val orientation =
            when (resources.configuration.orientation) {
                Configuration.ORIENTATION_LANDSCAPE -> ScreenInfo.ORIENTATION_LANDSCAPE
                else -> ScreenInfo.ORIENTATION_PORTRAIT
            }

        return ScreenInfo(
            width = width,
            height = height,
            densityDpi = densityDpi,
            orientation = orientation,
        )
    }

    override fun onCreateInputMethod(): InputMethod {
        val method = McpInputMethod(this)
        inputMethodInstance = method
        return method
    }

    private fun createControlOverlay(scope: CoroutineScope): ControlOverlayController? =
        runCatching {
            val session =
                EntryPointAccessors
                    .fromApplication(applicationContext, NodeCacheEntryPoint::class.java)
                    .controlSession()
            ControlOverlayController(this, session, scope) {
                startService(Intent(this, McpServerService::class.java).setAction(McpServerService.ACTION_STOP))
            }
        }.onFailure { Log.w(TAG, "Could not initialize control overlay", it) }.getOrNull()

    /** Called on Main immediately before dispatching a gesture. */
    suspend fun prepareGestureFeedback(gesture: GestureDescription): Long =
        try {
            controlOverlay?.prepareGesture(gesture) ?: 0L
        } catch (error: CancellationException) {
            throw error
        } catch (
            @Suppress("TooGenericExceptionCaught") error: Exception,
        ) {
            Log.w(TAG, "Could not prepare gesture feedback", error)
            0L
        }

    fun finishGestureFeedback(token: Long) {
        updateControlOverlay { it.finishGesture(token) }
    }

    fun showGestureFeedback(gesture: GestureDescription) {
        updateControlOverlay { it.showGesture(gesture) }
    }

    fun showTargetFeedback(bounds: Rect) {
        val copy = Rect(bounds)
        updateControlOverlay { it.showTarget(copy) }
    }

    private fun updateControlOverlay(update: (ControlOverlayController) -> Unit) {
        serviceScope?.launch(Dispatchers.Main.immediate) {
            runCatching { controlOverlay?.let(update) }
                .onFailure { Log.w(TAG, "Could not update control feedback", it) }
        }
    }

    private fun configureServiceInfo() {
        serviceInfo =
            serviceInfo?.apply {
                eventTypes = AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED or
                    AccessibilityEvent.TYPE_WINDOWS_CHANGED or
                    AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED or
                    AccessibilityEvent.TYPE_VIEW_CLICKED or
                    AccessibilityEvent.TYPE_VIEW_LONG_CLICKED or
                    AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED or
                    AccessibilityEvent.TYPE_VIEW_SCROLLED
                feedbackType = AccessibilityServiceInfo.FEEDBACK_GENERIC
                flags = AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS or
                    AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS or
                    AccessibilityServiceInfo.FLAG_INPUT_METHOD_EDITOR
                notificationTimeout = NOTIFICATION_TIMEOUT_MS
            }
        if (serviceInfo == null) {
            Log.w(TAG, "serviceInfo is null, cannot configure accessibility service settings")
        }
    }

    /**
     * Resolves the singleton [AccessibilityNodeCache] via Hilt's application entry point.
     * Returns null (and logs) if Hilt is not initialized, in which case cache invalidation
     * becomes a no-op rather than crashing the service.
     */
    private fun resolveNodeCache(): AccessibilityNodeCache? =
        try {
            EntryPointAccessors
                .fromApplication(applicationContext, NodeCacheEntryPoint::class.java)
                .nodeCache()
        } catch (e: IllegalStateException) {
            Log.w(TAG, "Could not resolve node cache", e)
            null
        }

    private fun resolveWorkflowRecorder(): WorkflowRecorder? =
        runCatching {
            EntryPointAccessors
                .fromApplication(applicationContext, NodeCacheEntryPoint::class.java)
                .workflowRecorder()
        }.onFailure { Log.w(TAG, "Could not initialize workflow recording", it) }.getOrNull()

    /**
     * Takes a screenshot using AccessibilityService.takeScreenshot() API.
     * Does NOT require user consent.
     *
     * @param timeoutMs Maximum time to wait for screenshot capture.
     * @return Bitmap of the screenshot, or null if capture failed or timed out.
     */
    suspend fun takeScreenshotBitmap(timeoutMs: Long = SCREENSHOT_TIMEOUT_MS): Bitmap? =
        // Bound the entire request, including dispatcher/queue/interval/overlay waits.
        withTimeoutOrNull(timeoutMs) {
            withContext(Dispatchers.Main) {
                val capture: suspend () -> Bitmap? = {
                    screenshotRequestGate.capture { captureScreenshotBitmap() }
                }
                val overlay = controlOverlay
                if (overlay == null) capture() else overlay.withoutVisuals(capture)
            }
        }

    private suspend fun captureScreenshotBitmap(): Bitmap? =
        suspendCancellableCoroutine { continuation ->
            val executor = Executor { it.run() }
            val callback =
                object : TakeScreenshotCallback {
                    override fun onSuccess(screenshot: ScreenshotResult) {
                        val bitmap =
                            Bitmap.wrapHardwareBuffer(
                                screenshot.hardwareBuffer,
                                screenshot.colorSpace,
                            )
                        screenshot.hardwareBuffer.close()
                        if (continuation.isActive) {
                            continuation.resume(bitmap)
                        } else {
                            bitmap?.recycle()
                        }
                    }

                    override fun onFailure(errorCode: Int) {
                        Log.e(TAG, "Screenshot failed with error code: $errorCode")
                        if (continuation.isActive) {
                            continuation.resume(null)
                        }
                    }
                }

            takeScreenshot(Display.DEFAULT_DISPLAY, executor, callback)
        }

    /**
     * Returns true if screenshot capability is available. Always true on minSdk 33+.
     */
    @Suppress("FunctionOnlyReturningConstant")
    fun canTakeScreenshot(): Boolean = true

    /**
     * Drops the framework's accessibility node cache for this service via [clearCache] (public
     * since API 33; minSdk is 33). See [AccessibilityServiceProvider.clearFrameworkNodeCache] for
     * why this is needed to defeat stale WebView reads after JavaScript DOM changes.
     *
     * This is distinct from [invalidateCache], which flushes our own id→node [nodeCache]; this
     * clears the framework-side cache that backs [rootInActiveWindow]/[getWindows] traversal.
     */
    fun clearFrameworkNodeCache() {
        clearCache()
    }

    class McpInputMethod(
        service: AccessibilityService,
    ) : InputMethod(service)

    companion object {
        private const val TAG = "MCP:AccessibilityService"
        private const val NOTIFICATION_TIMEOUT_MS = 100L
        private const val SCREENSHOT_TIMEOUT_MS = 5000L

        @Volatile
        private var controlOverlayEnabled = true

        /**
         * Length of the QUIET GAP (no further window-structure events) that must elapse before the
         * node cache is invalidated. The debounce timer resets on every event during a transition,
         * so this value is the silence required *after the last event*, not the total transition
         * duration. 250ms is an empirically chosen heuristic — long enough that a settling
         * keyboard/rotation has stopped emitting events, short enough to keep the post-transition
         * cache fresh quickly. Single tunable constant; validate/adjust against real-device traces.
         */
        private const val CACHE_INVALIDATION_DEBOUNCE_MS = 250L

        /**
         * Singleton instance of the accessibility service.
         * Set when the service connects, cleared when it is destroyed.
         * Access from other components to interact with the accessibility tree.
         */
        @Volatile
        var instance: McpAccessibilityService? = null
            private set

        @Volatile
        var inputMethodInstance: McpInputMethod? = null
            private set

        fun setControlOverlayEnabled(enabled: Boolean) {
            controlOverlayEnabled = enabled
            Handler(Looper.getMainLooper()).post { instance?.controlOverlay?.setEnabled(enabled) }
        }

        fun showToolCallIndicator(
            @Suppress("UNUSED_PARAMETER") toolName: String,
        ) {
            Handler(Looper.getMainLooper()).post { instance?.controlOverlay?.refresh() }
        }

        fun hideToolCallIndicator() {
            Handler(Looper.getMainLooper()).post { instance?.controlOverlay?.refresh() }
        }
    }
}

internal fun formatToolName(toolName: String): String = toolName.replace('_', ' ').replaceFirstChar { it.uppercase() }

/**
 * Returns true if [eventType] is a structural window change after which cached node bounds may be
 * stale and the node cache should be invalidated: [AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED]
 * (rotation, activity/dialog transition) and [AccessibilityEvent.TYPE_WINDOWS_CHANGED]
 * (soft-keyboard show/hide, window add/remove).
 *
 * Deliberately EXCLUDES [AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED], which fires far too
 * frequently (e.g. live text, progress bars) to drive cache invalidation without thrashing.
 *
 * Top-level and `internal` so the decision can be unit-tested without instantiating the service.
 */
internal fun triggersCacheInvalidation(eventType: Int): Boolean =
    eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED ||
        eventType == AccessibilityEvent.TYPE_WINDOWS_CHANGED

/**
 * Schedules a debounced cache invalidation iff [eventType] is a structural window change (see
 * [triggersCacheInvalidation]). No-op when [debouncer] is null (service not fully connected).
 *
 * Top-level and `internal` so the event-to-schedule wiring can be unit-tested without
 * instantiating the service.
 */
internal fun scheduleCacheInvalidationIfNeeded(
    eventType: Int,
    debouncer: CacheInvalidationDebouncer?,
) {
    if (triggersCacheInvalidation(eventType)) {
        debouncer?.schedule()
    }
}

/**
 * Clears [cache] if present. The whole-cache drop is the invalidation applied after a settled
 * window transition; a null [cache] (Hilt entry point unavailable) is a safe no-op.
 *
 * Top-level and `internal` so the invalidation behavior can be unit-tested without instantiating
 * the service.
 */
internal fun invalidateCache(cache: AccessibilityNodeCache?) {
    cache?.clear()
}
