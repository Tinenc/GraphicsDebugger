# -*- coding: utf-8 -*-
"""
Export Window with Real-time Log Display
"""

from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

__author__ = "Maek"
__date__ = "2025-11-11"

import os
from PySide2 import QtWidgets, QtCore, QtGui


class ExportLogWindow(QtWidgets.QDialog):
    """Window for displaying export progress and logs"""
    
    # Signal for updating log from different thread
    log_signal = QtCore.Signal(str)
    progress_signal = QtCore.Signal(int, int)  # current, total
    finished_signal = QtCore.Signal(bool, str)  # success, message
    
    def __init__(self, start_index, end_index, output_folder, parent=None):
        super(ExportLogWindow, self).__init__(parent)
        self.start_index = start_index
        self.end_index = end_index
        self.output_folder = output_folder
        self.is_cancelled = False
        
        self.setWindowTitle("Batch FBX Export - Running")
        self.setMinimumSize(700, 500)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMaximizeButtonHint)
        
        self.init_ui()
        
        # Connect signals
        self.log_signal.connect(self.append_log)
        self.progress_signal.connect(self.update_progress)
        self.finished_signal.connect(self.on_export_finished)
    
    def init_ui(self):
        """Initialize the UI components"""
        layout = QtWidgets.QVBoxLayout(self)
        
        # Info section
        info_group = QtWidgets.QGroupBox("Export Information")
        info_layout = QtWidgets.QFormLayout()
        info_layout.addRow("Start Index:", QtWidgets.QLabel(str(self.start_index)))
        info_layout.addRow("End Index:", QtWidgets.QLabel(str(self.end_index)))
        info_layout.addRow("Output Folder:", QtWidgets.QLabel(self.output_folder))
        info_group.setLayout(info_layout)
        layout.addWidget(info_group)
        
        # Progress bar
        progress_group = QtWidgets.QGroupBox("Progress")
        progress_layout = QtWidgets.QVBoxLayout()
        
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 2px solid grey;
                border-radius: 5px;
                text-align: center;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background-color: #4CAF50;
                width: 10px;
            }
        """)
        
        self.progress_label = QtWidgets.QLabel("Initializing...")
        self.progress_label.setAlignment(QtCore.Qt.AlignCenter)
        
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.progress_label)
        progress_group.setLayout(progress_layout)
        layout.addWidget(progress_group)
        
        # Log display
        log_group = QtWidgets.QGroupBox("Export Log")
        log_layout = QtWidgets.QVBoxLayout()
        
        self.log_text = QtWidgets.QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QtGui.QFont("Consolas", 9))
        self.log_text.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: 1px solid #3e3e3e;
            }
        """)
        
        log_layout.addWidget(self.log_text)
        log_group.setLayout(log_layout)
        layout.addWidget(log_group)
        
        # Buttons
        button_layout = QtWidgets.QHBoxLayout()
        
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.on_cancel)
        self.cancel_button.setStyleSheet("background-color: #d32f2f; color: white;")
        
        self.open_folder_button = QtWidgets.QPushButton("Open Output Folder")
        self.open_folder_button.clicked.connect(self.open_output_folder)
        self.open_folder_button.setEnabled(False)
        
        self.close_button = QtWidgets.QPushButton("Close")
        self.close_button.clicked.connect(self.accept)
        self.close_button.setEnabled(False)
        
        button_layout.addWidget(self.cancel_button)
        button_layout.addStretch()
        button_layout.addWidget(self.open_folder_button)
        button_layout.addWidget(self.close_button)
        
        layout.addLayout(button_layout)
        
        self.setLayout(layout)
    
    def append_log(self, message):
        """Append a log message to the text display"""
        self.log_text.append(message)
        # Auto-scroll to bottom
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
    
    def update_progress(self, current, total):
        """Update progress bar"""
        if total > 0:
            percentage = int((current / total) * 100)
            self.progress_bar.setValue(percentage)
            self.progress_label.setText("Exported {0} / {1} draw calls".format(current, total))
    
    def on_export_finished(self, success, message):
        """Called when export is finished"""
        self.cancel_button.setEnabled(False)
        self.close_button.setEnabled(True)
        self.open_folder_button.setEnabled(True)
        
        if success:
            self.setWindowTitle("Batch FBX Export - Completed")
            self.progress_bar.setStyleSheet("""
                QProgressBar {
                    border: 2px solid grey;
                    border-radius: 5px;
                    text-align: center;
                    font-weight: bold;
                }
                QProgressBar::chunk {
                    background-color: #4CAF50;
                    width: 10px;
                }
            """)
            self.append_log("\n" + "="*60)
            self.append_log("✓ EXPORT COMPLETED SUCCESSFULLY!")
            self.append_log(message)
            self.append_log("="*60)
        else:
            self.setWindowTitle("Batch FBX Export - Failed")
            self.progress_bar.setStyleSheet("""
                QProgressBar {
                    border: 2px solid grey;
                    border-radius: 5px;
                    text-align: center;
                    font-weight: bold;
                }
                QProgressBar::chunk {
                    background-color: #f44336;
                    width: 10px;
                }
            """)
            self.append_log("\n" + "="*60)
            self.append_log("✗ EXPORT FAILED OR CANCELLED")
            self.append_log(message)
            self.append_log("="*60)
    
    def on_cancel(self):
        """Cancel the export process"""
        self.is_cancelled = True
        self.append_log("\n[WARNING] Cancellation requested by user...")
        self.cancel_button.setEnabled(False)
    
    def open_output_folder(self):
        """Open the output folder in file explorer"""
        if os.path.exists(self.output_folder):
            os.startfile(self.output_folder)
        else:
            QtWidgets.QMessageBox.warning(
                self,
                "Folder Not Found",
                "Output folder does not exist:\n{}".format(self.output_folder)
            )
    
    def log(self, message):
        """Thread-safe log method"""
        self.log_signal.emit(str(message))
    
    def set_progress(self, current, total):
        """Thread-safe progress update"""
        self.progress_signal.emit(current, total)
    
    def finish(self, success, message):
        """Thread-safe finish notification"""
        self.finished_signal.emit(success, message)

