package com.danielealbano.androidremotecontrolmcp.ui.viewmodels

import com.danielealbano.androidremotecontrolmcp.services.accessibility.AccessibilityServiceProvider
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordingStatus
import com.danielealbano.androidremotecontrolmcp.services.recording.RecordingUiState
import com.danielealbano.androidremotecontrolmcp.services.recording.WorkflowRecorder
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import io.mockk.verifyOrder
import kotlinx.coroutines.flow.MutableStateFlow
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test

class RecordingViewModelTest {
    private val recorder = mockk<WorkflowRecorder>(relaxed = true)
    private val launcher = mockk<RecordingAppLauncher>(relaxed = true)
    private val accessibility = mockk<AccessibilityServiceProvider>()
    private val state = MutableStateFlow(RecordingUiState())
    private lateinit var viewModel: RecordingViewModel

    @BeforeEach
    fun setUp() {
        every { recorder.state } returns state
        every { accessibility.isReady() } returns true
        every { launcher.canLaunch(any()) } returns true
        every { launcher.launch(any()) } returns true
        every { recorder.start(any(), any()) } answers {
            state.value = RecordingUiState(status = RecordingStatus.RECORDING, name = firstArg(), targetPackage = secondArg())
        }
        viewModel = RecordingViewModel(recorder, launcher, accessibility)
    }

    @Test
    fun `starts recorder before opening the target app`() {
        viewModel.start("Search and save")
        verifyOrder {
            recorder.start("Search and save", RecordingViewModel.TARGET_PACKAGE)
            launcher.launch(RecordingViewModel.TARGET_PACKAGE)
        }
        assertNull(viewModel.error.value)
    }

    @Test
    fun `unavailable accessibility prevents starting or opening app`() {
        every { accessibility.isReady() } returns false
        viewModel.start("Demo")
        assertEquals(RecordingUiError.ACCESSIBILITY_UNAVAILABLE, viewModel.error.value)
        verify(exactly = 0) { recorder.start(any(), any()) }
        verify(exactly = 0) { launcher.launch(any()) }
    }

    @Test
    fun `missing target app does not leave a recording running`() {
        every { launcher.canLaunch(any()) } returns false
        viewModel.start("Demo")
        assertEquals(RecordingUiError.APP_UNAVAILABLE, viewModel.error.value)
        verify(exactly = 0) { recorder.start(any(), any()) }
    }

    @Test
    fun `failed launch pauses capture so user can recover the draft`() {
        every { launcher.launch(any()) } returns false
        viewModel.start("Demo")
        verify { recorder.pause() }
        assertEquals(RecordingUiError.APP_LAUNCH_FAILED, viewModel.error.value)
    }

    @Test
    fun `recorder refusing start must not launch app`() {
        every { recorder.start(any(), any()) } answers {}
        viewModel.start("Demo")
        verify(exactly = 0) { launcher.launch(any()) }
    }

    @Test
    fun `starting twice cannot replace an active recording`() {
        state.value =
            RecordingUiState(status = RecordingStatus.RECORDING, name = "Existing", targetPackage = RecordingViewModel.TARGET_PACKAGE)
        viewModel.start("Replacement")
        verify(exactly = 0) { recorder.start(any(), any()) }
        verify(exactly = 0) { launcher.launch(any()) }
        assertEquals("Existing", state.value.name)
    }

    @Test
    fun `export reads the stopped draft without modifying it`() {
        every { recorder.exportJson() } returns "{\"schemaVersion\":1}"
        assertEquals("{\"schemaVersion\":1}", viewModel.exportJson())
        verify(exactly = 0) { recorder.discard() }
    }
}
