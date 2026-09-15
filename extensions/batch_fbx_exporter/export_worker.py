"""
Export worker thread for asynchronous export operations
"""

from PySide2 import QtCore
from functools import partial
import time


class ExportWorker(QtCore.QThread):
    """
    Worker thread for batch FBX export
    Runs in background to keep UI responsive
    """
    
    # Signals for communication with main thread
    log_signal = QtCore.Signal(str)
    progress_signal = QtCore.Signal(int, int)  # current, total
    finished_signal = QtCore.Signal(bool, str)  # success, message
    
    def __init__(self, pyrenderdoc, start_index, end_index, output_folder, mapper, 
                 find_draws_func, export_draw_func, matrix_config=None, export_shader=False):
        super().__init__()
        self.pyrenderdoc = pyrenderdoc
        self.start_index = start_index
        self.end_index = end_index
        self.output_folder = output_folder
        self.mapper = mapper
        self.is_cancelled = False
        self.find_draws_func = find_draws_func
        self.export_draw_func = export_draw_func
        self.matrix_config = matrix_config
        self.export_shader = export_shader
        
        # Log buffering for smooth UI updates
        self.log_buffer = []
        self.last_flush_time = time.time()
        self.flush_interval = 0.15  # Flush every 150ms
        
    def cancel(self):
        """Request cancellation of export"""
        self.is_cancelled = True
    
    def _flush_log_buffer(self, force=False):
        """Flush buffered logs to UI"""
        current_time = time.time()
        
        # Flush if buffer has content and (forced or time elapsed)
        if self.log_buffer and (force or current_time - self.last_flush_time >= self.flush_interval):
            # Send all buffered logs as one message
            combined_log = "\n".join(self.log_buffer)
            self.log_signal.emit(combined_log)
            
            # Clear buffer and update time
            self.log_buffer = []
            self.last_flush_time = current_time
    
    def _log_immediate(self, msg):
        """Log message immediately (for critical messages)"""
        # Flush any buffered logs first
        self._flush_log_buffer(force=True)
        # Send this message immediately
        self.log_signal.emit(msg)
    
    def _log_buffered(self, msg):
        """Log message with buffering (for normal messages)"""
        self.log_buffer.append(msg)
        # Auto-flush if enough time has passed
        self._flush_log_buffer(force=False)
        
    def run(self):
        """Main thread execution - runs in background"""
        try:
            # Import here to avoid circular dependencies
            from functools import partial
            
            # BlockInvoke will automatically pass controller as last argument
            self.pyrenderdoc.Replay().BlockInvoke(
                partial(self._batch_export_internal)
            )
            
        except Exception as e:
            import traceback
            error_msg = "Fatal error: {0}\n{1}".format(str(e), traceback.format_exc())
            self.log_signal.emit("\n✗ " + error_msg)
            self.finished_signal.emit(False, str(e))
    
    def _batch_export_internal(self, controller):
        """Internal export function - runs on RenderDoc replay thread"""
        import os
        
        try:
            # Ensure output folder exists
            if not os.path.exists(self.output_folder):
                os.makedirs(self.output_folder)
                self._log_immediate("Created output folder: {0}".format(self.output_folder))
        except Exception as e:
            self._log_immediate("✗ Failed to create output folder: {0}".format(str(e)))
            self.finished_signal.emit(False, "Failed to create output folder")
            return
        
        # Critical: Export start info (immediate)
        self._log_immediate("="*60)
        self._log_immediate("Starting Batch FBX Export (Async Mode)")
        self._log_immediate("Range: EventID {0} - {1}".format(self.start_index, self.end_index))
        self._log_immediate("Output: {0}".format(self.output_folder))
        self._log_immediate("="*60)
        
        # Find all draw calls in range
        self._log_immediate("\nSearching for draw calls...")
        draws = self.find_draws_func(controller, self.start_index, self.end_index)
        
        if not draws:
            self._log_immediate("✗ No draw calls found in the specified range!")
            self.finished_signal.emit(False, "No draw calls found in range {0}-{1}".format(
                self.start_index, self.end_index))
            return
        
        # Critical: Found draws (immediate)
        self._log_immediate("✓ Found {0} draw calls to export\n".format(len(draws)))
        
        # Export each draw call
        success_count = 0
        
        for i, draw in enumerate(draws, 1):
            # Check cancellation
            if self.is_cancelled:
                self._log_immediate("\n✗ Export cancelled by user")
                self._flush_log_buffer(force=True)
                self.finished_signal.emit(False, "Export cancelled. Exported {0}/{1} draw calls.".format(
                    success_count, len(draws)))
                return
            
            # Update progress (normal frequency)
            self.progress_signal.emit(i-1, len(draws))
            
            # Normal log: buffered
            self._log_buffered("--- Processing {0}/{1} (EventID: {2}) ---".format(i, len(draws), draw.eventId))
            
            # Create a logger for this draw call
            class DrawCallLogger:
                def __init__(self, worker):
                    self.worker = worker
                    
                def log(self, msg):
                    # Check if it's an error or critical message
                    if any(keyword in msg for keyword in ["✗", "Error", "Failed", "Warning"]):
                        self.worker._log_immediate(msg)  # Critical: immediate
                    elif any(keyword in msg for keyword in ["✓", "Exported to:", "texture(s)"]):
                        self.worker._log_immediate(msg)  # Success: immediate
                    else:
                        self.worker._log_buffered(msg)   # Normal: buffered
            
            logger = DrawCallLogger(self)
            
            try:
                # Note: draw_index parameter removed, using draw.eventId instead
                fbx_success = self.export_draw_func(controller, draw, self.mapper, self.output_folder, logger, None, self.matrix_config)
                
                # Export shader if FBX export succeeded and shader export is enabled
                if fbx_success and self.export_shader:
                    event_folder = os.path.join(self.output_folder, str(draw.eventId))
                    self._log_buffered("  Exporting shader for EventID {0}...".format(draw.eventId))
                    
                    # Import shader export function
                    from . import export_single_event_shader
                    shader_success, shader_info = export_single_event_shader(controller, draw.eventId, event_folder)
                    
                    if shader_success:
                        self._log_immediate("  ✓ Shader exported successfully (FULL VERSION):")
                        self._log_immediate("    - SPIR-V bytecode saved")
                        self._log_immediate("    - HLSL converted")
                        self._log_immediate("    - Unity Shader generated ({0}.shader)".format(draw.eventId))
                        self._log_immediate("    - shader_info.json created")
                        self._log_immediate("    - VS/PS reference files saved")
                        self._log_immediate("    NOTE: Shader contains complete HLSL code")
                        self._log_immediate("          May need manual adjustment for Unity")
                    else:
                        self._log_immediate("  ⚠ Shader export incomplete/skipped")
                        self._log_immediate("    Possible reasons:")
                        self._log_immediate("    - Not a Vulkan capture (DirectX/OpenGL not supported)")
                        self._log_immediate("    - No shader data available")
                        self._log_immediate("    - spirv-cross.exe not found (see log above for details)")
                
                if fbx_success:
                    success_count += 1
                    # Flush buffer after each successful export to show progress
                    self._flush_log_buffer(force=True)
            except Exception as e:
                # Critical: Error (immediate)
                self._log_immediate("✗ Error exporting draw {0}: {1}".format(draw.eventId, str(e)))
                import traceback
                self._log_immediate(traceback.format_exc())
        
        # Ensure all buffered logs are sent
        self._flush_log_buffer(force=True)
        
        # Final progress
        self.progress_signal.emit(len(draws), len(draws))
        
        # Critical: Export complete (immediate)
        self._log_immediate("\n" + "="*60)
        self._log_immediate("Batch Export Completed!")
        self._log_immediate("Successfully exported: {0}/{1} draw calls".format(success_count, len(draws)))
        self._log_immediate("Output folder: {0}".format(self.output_folder))
        self._log_immediate("="*60)
        
        self.finished_signal.emit(
            success_count > 0,
            "Successfully exported {0}/{1} draw calls".format(success_count, len(draws))
        )

