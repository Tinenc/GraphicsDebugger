# -*- coding: utf-8 -*-
"""
Batch Export Dialog with Index Range and Attribute Mapping
"""

from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

__author__ = "Maek"
__date__ = "2025-11-11"

import os
import tempfile
import json
from functools import partial
from PySide2 import QtWidgets, QtCore, QtGui


class MatrixInputWidget(QtWidgets.QGroupBox):
    """Matrix Auto-Read Configuration Widget"""
    
    def __init__(self, parent=None, pyrenderdoc=None):
        super(MatrixInputWidget, self).__init__("世界坐标变换矩阵 (自动读取)", parent)
        self.pyrenderdoc = pyrenderdoc
        self.setup_ui()
    
    def setup_ui(self):
        main_layout = QtWidgets.QVBoxLayout(self)
        
        # 启用开关
        self.enable_checkbox = QtWidgets.QCheckBox("启用世界坐标变换 (每个EventID实时读取)")
        self.enable_checkbox.setStyleSheet("font-weight: bold; color: #2196F3;")
        self.enable_checkbox.setToolTip("勾选后，每个EventID导出时都会实时从RenderDoc读取对应的变换矩阵")
        main_layout.addWidget(self.enable_checkbox)
        
        # 说明文字
        info_label = QtWidgets.QLabel(
            "💡 提示: 插件将在每个EventID导出时，实时从当前DrawCall的Constant Buffer中读取变换矩阵"
        )
        info_label.setStyleSheet("color: #666; font-size: 11px; padding: 5px; background-color: #F5F5F5; border-radius: 4px;")
        info_label.setWordWrap(True)
        main_layout.addWidget(info_label)
        
        # 配置参数组
        config_group = QtWidgets.QGroupBox("矩阵读取配置")
        config_layout = QtWidgets.QGridLayout(config_group)
        
        # Set参数
        config_layout.addWidget(QtWidgets.QLabel("Descriptor Set:"), 0, 0)
        self.set_spin = QtWidgets.QSpinBox()
        self.set_spin.setRange(0, 10)
        self.set_spin.setValue(3)
        self.set_spin.setFixedWidth(70)
        self.set_spin.setToolTip("Vulkan Descriptor Set编号 (通常在RenderDoc Pipeline State中显示)")
        config_layout.addWidget(self.set_spin, 0, 1)
        
        # Binding参数
        config_layout.addWidget(QtWidgets.QLabel("Binding:"), 0, 2)
        self.binding_spin = QtWidgets.QSpinBox()
        self.binding_spin.setRange(0, 20)
        self.binding_spin.setValue(1)
        self.binding_spin.setFixedWidth(70)
        self.binding_spin.setToolTip("Binding编号 (在对应的Descriptor Set中)")
        config_layout.addWidget(self.binding_spin, 0, 3)
        
        # Variable名称
        config_layout.addWidget(QtWidgets.QLabel("Variable名称:"), 1, 0)
        self.variable_edit = QtWidgets.QLineEdit("_child0")
        self.variable_edit.setPlaceholderText("如: _child0, _child1, World等")
        self.variable_edit.setToolTip("Constant Buffer中变量的名称 (大小写敏感)")
        config_layout.addWidget(self.variable_edit, 1, 1, 1, 3)
        
        # Auto-find Matrix复选框
        self.autofind_checkbox = QtWidgets.QCheckBox("🔍 自动查找最佳矩阵")
        self.autofind_checkbox.setToolTip(
            "如果指定的矩阵无效(最后一列不是0,0,0,1),\n"
            "自动遍历所有CB查找最符合标准的变换矩阵"
        )
        self.autofind_checkbox.setStyleSheet("font-weight: bold; color: #2196F3;")
        config_layout.addWidget(self.autofind_checkbox, 2, 0, 1, 4)
        
        # Transpose Matrix复选框
        self.transpose_checkbox = QtWidgets.QCheckBox("🔄 转置矩阵 (Column-major → Row-major)")
        self.transpose_checkbox.setToolTip(
            "是否对读取的原始矩阵进行转置\n"
            "• 勾选：将column-major矩阵转置为row-major (Vulkan默认)\n"
            "• 不勾选：直接使用原始矩阵，不进行转置"
        )
        self.transpose_checkbox.setChecked(True)  # 默认启用转置
        self.transpose_checkbox.setStyleSheet("font-weight: bold; color: #FF9800;")
        config_layout.addWidget(self.transpose_checkbox, 3, 0, 1, 4)
        
        # 设置列拉伸
        config_layout.setColumnStretch(4, 1)
        
        main_layout.addWidget(config_group)
        
        # 测试按钮
        test_layout = QtWidgets.QHBoxLayout()
        self.test_button = QtWidgets.QPushButton("🔍 测试读取矩阵")
        self.test_button.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                font-weight: bold;
                padding: 8px;
                border-radius: 4px;
                border: none;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3d8b40;
            }
        """)
        self.test_button.clicked.connect(self.test_read_matrix)
        test_layout.addWidget(self.test_button)
        test_layout.addStretch()
        main_layout.addLayout(test_layout)
        
        # 使用说明
        help_text = QtWidgets.QLabel(
            "<b>使用步骤:</b><br>"
            "1. 在RenderDoc中选择一个DrawCall<br>"
            "2. 查看Pipeline State找到包含变换矩阵的Constant Buffer的Set和Binding<br>"
            "3. 在Mesh Viewer中查看buffer内容，找到矩阵对应的Variable名称<br>"
            "4. 点击'测试读取矩阵'验证配置是否正确<br>"
            "5. 开始导出，每个EventID会自动读取对应的矩阵"
        )
        help_text.setStyleSheet("color: #444; font-size: 11px; padding: 10px; background-color: #E8F5E9; border-radius: 4px; border-left: 4px solid #4CAF50;")
        help_text.setWordWrap(True)
        main_layout.addWidget(help_text)
        
        # 连接启用开关
        self.enable_checkbox.toggled.connect(config_group.setEnabled)
        self.enable_checkbox.toggled.connect(self.test_button.setEnabled)
        config_group.setEnabled(False)
        self.test_button.setEnabled(False)
    
    def get_config(self):
        """获取矩阵配置 (用于实时读取)"""
        return {
            'enabled': self.enable_checkbox.isChecked(),
            'set': self.set_spin.value(),
            'binding': self.binding_spin.value(),
            'variable': self.variable_edit.text(),
            'auto_find': self.autofind_checkbox.isChecked(),
            'transpose': self.transpose_checkbox.isChecked()
        }
    
    def is_enabled(self):
        """是否启用变换"""
        return self.enable_checkbox.isChecked()
    
    def test_read_matrix(self):
        """测试从RenderDoc读取矩阵"""
        if not self.pyrenderdoc:
            QtWidgets.QMessageBox.warning(self, "错误", "RenderDoc API 不可用")
            return
        
        try:
            import renderdoc as rd
            import struct
            
            target_set = self.set_spin.value()
            target_binding = self.binding_spin.value()
            variable_name = self.variable_edit.text()
            auto_find_enabled = self.autofind_checkbox.isChecked()
            transpose_enabled = self.transpose_checkbox.isChecked()
            
            if not variable_name:
                QtWidgets.QMessageBox.warning(self, "错误", "请输入Variable名称")
                return
            
            # 存储结果
            result_matrix = [None]
            error_message = [None]
            debug_messages = [[]]  # 存储调试日志
            
            def read_matrix_test(controller):
                try:
                    # 导入必要的模块
                    import sys
                    import os
                    # 导入 __init__.py 中的 read_matrix_from_renderdoc 函数
                    sys.path.insert(0, os.path.dirname(__file__))
                    from . import read_matrix_from_renderdoc
                    
                    # 简单的日志函数
                    messages = []
                    def log_func(msg):
                        messages.append(msg)
                        print("[DEBUG] " + msg)  # 同时输出到控制台
                    
                    # 读取矩阵（传递auto_find和transpose参数）
                    matrix = read_matrix_from_renderdoc(controller, target_set, target_binding, variable_name, log_func, auto_find_enabled, transpose_enabled)
                    
                    # 保存日志
                    debug_messages[0] = messages
                    
                    if matrix:
                        result_matrix[0] = matrix
                    else:
                        error_message[0] = "\n".join(messages) if messages else "无法读取矩阵"
                except Exception as e:
                    error_message[0] = str(e)
                    import traceback
                    debug_messages[0].append("Exception: " + traceback.format_exc())
            
            # 执行测试
            self.pyrenderdoc.Replay().BlockInvoke(read_matrix_test)
            
            # 显示结果
            if result_matrix[0]:
                matrix = result_matrix[0]
                result_text = "✓ 成功读取矩阵:\n\n"
                for i in range(4):
                    row = matrix[i*4:(i+1)*4]
                    result_text += "[{0:7.4f}, {1:7.4f}, {2:7.4f}, {3:7.4f}]\n".format(*row)
                
                result_text += "\n矩阵验证:\n"
                result_text += "• matrix[15] = {0:.4f} (应接近1.0)\n".format(matrix[15])
                
                if abs(matrix[15] - 1.0) < 0.1:
                    result_text += "• ✓ 验证通过\n"
                else:
                    result_text += "• ⚠ 警告: matrix[15]不接近1.0,请确认\n"
                
                # 显示调试日志
                if debug_messages[0]:
                    result_text += "\n" + "="*50 + "\n"
                    result_text += "调试日志:\n" + "="*50 + "\n"
                    result_text += "\n".join(debug_messages[0])
                
                QtWidgets.QMessageBox.information(self, '读取成功', result_text)
            else:
                error_text = "✗ 读取失败\n\n"
                if error_message[0]:
                    error_text += "错误信息:\n" + error_message[0]
                else:
                    error_text += "请检查:\n"
                    error_text += "• Set和Binding是否正确\n"
                    error_text += "• Variable名称是否正确（大小写敏感）\n"
                    error_text += "• 是否已选中DrawCall\n"
                    error_text += "• 是否为Vulkan API"
                
                QtWidgets.QMessageBox.warning(self, "读取失败", error_text)
        
        except Exception as e:
            import traceback
            error_text = "测试时出错:\n\n{0}\n\n{1}".format(str(e), traceback.format_exc())
            QtWidgets.QMessageBox.critical(self, "错误", error_text)
    
    def save_to_settings(self, settings):
        """保存到配置"""
        settings.setValue("matrix_enabled", self.is_enabled())
        
        # 保存自动读取参数
        settings.setValue("matrix_auto_set", self.set_spin.value())
        settings.setValue("matrix_auto_binding", self.binding_spin.value())
        settings.setValue("matrix_auto_variable", self.variable_edit.text())
        settings.setValue("matrix_auto_find", self.autofind_checkbox.isChecked())
        settings.setValue("matrix_transpose", self.transpose_checkbox.isChecked())
    
    def load_from_settings(self, settings):
        """从配置加载"""
        enabled = settings.value("matrix_enabled", False)
        # Convert string to bool if needed
        if isinstance(enabled, str):
            enabled = enabled.lower() == 'true'
        self.enable_checkbox.setChecked(bool(enabled))
        
        # 加载自动读取参数
        auto_set = settings.value("matrix_auto_set", 3)
        auto_binding = settings.value("matrix_auto_binding", 1)
        auto_variable = settings.value("matrix_auto_variable", "_child0")
        auto_find = settings.value("matrix_auto_find", False)
        transpose = settings.value("matrix_transpose", True)  # 默认启用转置
        
        # Convert string to bool if needed
        if isinstance(auto_find, str):
            auto_find = auto_find.lower() == 'true'
        if isinstance(transpose, str):
            transpose = transpose.lower() == 'true'
        
        try:
            self.set_spin.setValue(int(auto_set))
            self.binding_spin.setValue(int(auto_binding))
            self.variable_edit.setText(str(auto_variable))
            self.autofind_checkbox.setChecked(bool(auto_find))
            self.transpose_checkbox.setChecked(bool(transpose))
        except:
            pass


class BatchExportDialog(object):

    title = "Batch FBX Export Settings"
    space = "%-15s"

    edit_config = {
        "POSITION": space % "Vertex Position",
        "TANGENT": space % "Vertex Tangent",
        "NORMAL": space % "Vertex Normal",
        "BINORMAL": space % "Vertex BiNormal",
        "COLOR": space % "Vertex Color",
        "UV": space % "UV",
        "UV2": space % "UV2",
        "UV3": space % "UV3",
        "UV4": space % "UV4",
        "UV5": space % "UV5",
    }

    button_dict = {}
    mapper = {}
    start_index = 0
    end_index = 1000
    output_folder = ""
    export_shader = False  # New: Export shader option

    def __init__(self, mqt, pyrenderdoc=None):
        self.mqt = mqt
        self.pyrenderdoc = pyrenderdoc
        name = "RenderDoc_%s.ini" % self.__class__.__name__
        path = os.path.join(tempfile.gettempdir(), name)
        self.settings = QtCore.QSettings(path, QtCore.QSettings.IniFormat)
        if not os.path.exists(path):
            self.template_select(0)

    def template_select(self, index):
        text = self.combo.itemText(index) if hasattr(self, "combo") else "unity"
        config = {}
        if text == "unity":
            config = {
                "POSITION": "POSITION",
                "TANGENT": "TANGENT",
                "BINORMAL": "",
                "NORMAL": "NORMAL",
                "COLOR": "COLOR",
                "UV": "TEXCOORD0",
                "UV2": "TEXCOORD1",
                "UV3": "TEXCOORD2",
                "UV4": "TEXCOORD3",
                "UV5": "TEXCOORD4",
            }
        elif text == "unreal":
            config = {
                "POSITION": "ATTRIBUTE0",
                "TANGENT": "ATTRIBUTE1",
                "BINORMAL": "",
                "NORMAL": "ATTRIBUTE2",
                "COLOR": "ATTRIBUTE13",
                "UV": "ATTRIBUTE5",
                "UV2": "ATTRIBUTE6",
                "UV3": "ATTRIBUTE7",
                "UV4": "ATTRIBUTE8",
                "UV5": "ATTRIBUTE9",
            }

        self.settings.setValue("Engine", text)
        for name, input_widget in self.button_dict.items():
            value = config.get(name, "")
            self.settings.setValue(name, value)
            self.mqt.SetWidgetText(input_widget.edit, value)

    def browse_folder(self, context, widget, text):
        """Browse for output folder"""
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            None, "Select Output Folder", self.output_folder
        )
        if folder:
            self.output_folder = folder
            self.mqt.SetWidgetText(self.folder_edit, folder)
            self.settings.setValue("OutputFolder", folder)

    def init_ui(self):
        self.widget = self.mqt.CreateToplevelWidget(self.title, None)

        # ==================== Index Range Section ====================
        index_group = self.mqt.CreateGroupBox(True)
        self.mqt.SetWidgetText(index_group, "Export Range")
        
        # Index range in one row (more compact)
        index_container = self.mqt.CreateHorizontalContainer()
        
        # Start Index
        start_label = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(start_label, "Start:")
        self.start_edit = self.mqt.CreateTextBox(True, self.on_start_change)  # ✅ True = 单行
        start_value = self.settings.value("StartIndex", "0")
        self.mqt.SetWidgetText(self.start_edit, start_value)
        
        # Convert to QLineEdit for better control
        if hasattr(self.start_edit, 'Widget'):
            start_qt_edit = self.start_edit.Widget()
        else:
            start_qt_edit = self.start_edit
        
        if isinstance(start_qt_edit, QtWidgets.QLineEdit):
            start_qt_edit.setMaxLength(6)
            start_qt_edit.setFixedWidth(80)
            start_qt_edit.setFixedHeight(24)  # 设置固定高度
            start_qt_edit.setAlignment(QtCore.Qt.AlignRight)
            # Only allow digits
            start_qt_edit.setValidator(QtGui.QIntValidator(0, 999999))
            start_qt_edit.setPlaceholderText("0")
        
        # Separator
        separator_label = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(separator_label, "  to  ")
        
        # End Index
        end_label = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(end_label, "End:")
        self.end_edit = self.mqt.CreateTextBox(True, self.on_end_change)  # ✅ True = 单行
        end_value = self.settings.value("EndIndex", "1000")
        self.mqt.SetWidgetText(self.end_edit, end_value)
        
        # Convert to QLineEdit for better control
        if hasattr(self.end_edit, 'Widget'):
            end_qt_edit = self.end_edit.Widget()
        else:
            end_qt_edit = self.end_edit
            
        if isinstance(end_qt_edit, QtWidgets.QLineEdit):
            end_qt_edit.setMaxLength(6)
            end_qt_edit.setFixedWidth(80)
            end_qt_edit.setFixedHeight(24)  # 设置固定高度
            end_qt_edit.setAlignment(QtCore.Qt.AlignRight)
            # Only allow digits
            end_qt_edit.setValidator(QtGui.QIntValidator(0, 999999))
            end_qt_edit.setPlaceholderText("999999")
        
        # Add all to one row
        self.mqt.AddWidget(index_container, start_label)
        self.mqt.AddWidget(index_container, self.start_edit)
        self.mqt.AddWidget(index_container, separator_label)
        self.mqt.AddWidget(index_container, end_label)
        self.mqt.AddWidget(index_container, self.end_edit)
        
        # Add spacer to push everything to the left
        spacer = QtWidgets.QSpacerItem(40, 20, QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum)
        index_container.layout().addItem(spacer)
        
        self.mqt.AddWidget(index_group, index_container)
        self.mqt.AddWidget(self.widget, index_group)

        # ==================== Output Folder Section ====================
        folder_group = self.mqt.CreateGroupBox(True)
        self.mqt.SetWidgetText(folder_group, "Output Settings")
        
        folder_container = self.mqt.CreateHorizontalContainer()
        folder_label = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(folder_label, "Output Folder:")
        self.folder_edit = self.mqt.CreateTextBox(True, None)  # ✅ True = 单行
        self.output_folder = self.settings.value("OutputFolder", "F:/BatchExport")
        self.mqt.SetWidgetText(self.folder_edit, self.output_folder)
        
        # 设置folder_edit的固定高度
        if hasattr(self.folder_edit, 'Widget'):
            folder_qt_edit = self.folder_edit.Widget()
        else:
            folder_qt_edit = self.folder_edit
        
        if isinstance(folder_qt_edit, QtWidgets.QLineEdit):
            folder_qt_edit.setFixedHeight(24)  # 设置固定高度
        
        browse_button = self.mqt.CreateButton(self.browse_folder)
        self.mqt.SetWidgetText(browse_button, "Browse...")
        
        # 设置browse按钮的固定高度
        if hasattr(browse_button, 'Widget'):
            browse_qt_button = browse_button.Widget()
        else:
            browse_qt_button = browse_button
        
        if isinstance(browse_qt_button, QtWidgets.QPushButton):
            browse_qt_button.setFixedHeight(24)  # 设置固定高度
        
        self.mqt.AddWidget(folder_container, folder_label)
        self.mqt.AddWidget(folder_container, self.folder_edit)
        self.mqt.AddWidget(folder_container, browse_button)
        self.mqt.AddWidget(folder_group, folder_container)
        
        # ==================== Shader Export Option ====================
        shader_container = self.mqt.CreateHorizontalContainer()
        self.shader_checkbox = QtWidgets.QCheckBox("Export Shader (VS/PS to Unity Shader)")
        shader_export_enabled = self.settings.value("ExportShader", "false") == "true"
        self.shader_checkbox.setChecked(shader_export_enabled)
        self.shader_checkbox.setFixedHeight(24)
        self.mqt.AddWidget(shader_container, self.shader_checkbox)
        
        # Add spacer
        shader_spacer = QtWidgets.QSpacerItem(40, 20, QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum)
        shader_container.layout().addItem(shader_spacer)
        
        self.mqt.AddWidget(folder_group, shader_container)
        self.mqt.AddWidget(self.widget, folder_group)

        # ==================== Attribute Mapping Section ====================
        attr_group = self.mqt.CreateGroupBox(True)
        self.mqt.SetWidgetText(attr_group, "Attribute Mapping")
        
        # Template selection
        template_container = self.mqt.CreateHorizontalContainer()
        template_label = self.mqt.CreateLabel()
        
        self.combo = QtWidgets.QComboBox()
        self.combo.addItems(["unity", "unreal"])
        self.combo.setCurrentText(self.settings.value("Engine", "unity"))
        self.combo.currentIndexChanged.connect(self.template_select)
        self.combo.setFixedHeight(24)  # 设置固定高度
        self.combo.setFixedWidth(120)  # 设置固定宽度

        self.mqt.SetWidgetText(template_label, "Template:")
        self.mqt.AddWidget(template_container, template_label)
        self.mqt.AddWidget(template_container, self.combo)
        
        # Add spacer for template
        template_spacer = QtWidgets.QSpacerItem(40, 20, QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum)
        template_container.layout().addItem(template_spacer)
        
        self.mqt.AddWidget(attr_group, template_container)

        # Attribute inputs
        self.button_dict = {}
        for name, label in self.edit_config.items():
            w = self.input_widget(label, name)
            self.button_dict[name] = w
            self.mqt.AddWidget(attr_group, w)
            # Load settings
            text = self.settings.value(name, "")
            if text and text != name:
                self.mqt.SetWidgetText(w.edit, text)
        
        self.mqt.AddWidget(self.widget, attr_group)

        # ==================== Matrix Transform Section ====================
        # 直接用Qt方式添加矩阵控件
        try:
            # 获取父widget的Qt对象
            if hasattr(self.widget, 'Widget'):
                parent_qt = self.widget.Widget()
            else:
                parent_qt = self.widget
            
            # 创建矩阵输入控件
            self.matrix_widget = MatrixInputWidget(parent_qt, self.pyrenderdoc)
            self.matrix_widget.load_from_settings(self.settings)
            
            # 直接添加到父widget的layout
            if isinstance(parent_qt, QtWidgets.QWidget) and parent_qt.layout():
                parent_qt.layout().addWidget(self.matrix_widget)
            else:
                print("Warning: Could not add matrix widget to layout")
                self.matrix_widget = None
        except Exception as e:
            print("Warning: Could not create matrix widget: {0}".format(str(e)))
            import traceback
            traceback.print_exc()
            self.matrix_widget = None

        # ==================== Action Buttons ====================
        button_container = self.mqt.CreateHorizontalContainer()
        ok_button = self.mqt.CreateButton(self.accept)
        self.mqt.SetWidgetText(ok_button, "Export")
        callback = lambda *args: self.mqt.CloseCurrentDialog(False)
        cancel_button = self.mqt.CreateButton(callback)
        self.mqt.SetWidgetText(cancel_button, "Cancel")
        
        # 设置按钮固定高度
        if hasattr(ok_button, 'Widget'):
            ok_qt = ok_button.Widget()
        else:
            ok_qt = ok_button
        if isinstance(ok_qt, QtWidgets.QPushButton):
            ok_qt.setFixedHeight(28)
            ok_qt.setMinimumWidth(80)
        
        if hasattr(cancel_button, 'Widget'):
            cancel_qt = cancel_button.Widget()
        else:
            cancel_qt = cancel_button
        if isinstance(cancel_qt, QtWidgets.QPushButton):
            cancel_qt.setFixedHeight(28)
            cancel_qt.setMinimumWidth(80)
        
        self.mqt.AddWidget(button_container, cancel_button)
        self.mqt.AddWidget(button_container, ok_button)
        self.mqt.AddWidget(self.widget, button_container)

        return self.widget

    def on_start_change(self, c, w, text):
        """Handle start index change"""
        try:
            self.start_index = int(text)
            self.settings.setValue("StartIndex", text)
        except ValueError:
            pass

    def on_end_change(self, c, w, text):
        """Handle end index change"""
        try:
            self.end_index = int(text)
            self.settings.setValue("EndIndex", text)
        except ValueError:
            pass

    def accept(self, context, widget, text):
        """Accept and collect all settings"""
        self.mapper = {}
        for name, WIDGET in self.button_dict.items():
            text = self.mqt.GetWidgetText(WIDGET.edit)
            self.mapper[name] = text
        
        self.mapper['ENGINE'] = self.combo.currentText()
        
        # Get index range
        try:
            self.start_index = int(self.mqt.GetWidgetText(self.start_edit))
            self.end_index = int(self.mqt.GetWidgetText(self.end_edit))
        except ValueError:
            self.start_index = 0
            self.end_index = 1000
        
        # Get output folder
        self.output_folder = self.mqt.GetWidgetText(self.folder_edit)
        
        # Get shader export option
        self.export_shader = self.shader_checkbox.isChecked()
        self.settings.setValue("ExportShader", "true" if self.export_shader else "false")
        print("Export Shader: {0}".format(self.export_shader))
        
        # Save and get transform matrix configuration
        if self.matrix_widget:
            try:
                self.matrix_widget.save_to_settings(self.settings)
                self.matrix_config = self.matrix_widget.get_config()
                if self.matrix_config.get('enabled', False):
                    print("Transform matrix auto-read enabled:")
                    print("  Set: {0}".format(self.matrix_config['set']))
                    print("  Binding: {0}".format(self.matrix_config['binding']))
                    print("  Variable: {0}".format(self.matrix_config['variable']))
                    print("  Auto-find: {0}".format(self.matrix_config.get('auto_find', False)))
                    print("  Transpose: {0}".format(self.matrix_config.get('transpose', True)))
                    print("  (Matrix will be read for each EventID during export)")
                else:
                    self.matrix_config = None
                    print("Transform matrix disabled")
            except Exception as e:
                print("Warning: Could not save matrix settings: {0}".format(str(e)))
                import traceback
                traceback.print_exc()
                self.matrix_config = None
        else:
            print("Warning: Matrix widget not available")
            self.matrix_config = None
        
        self.mqt.CloseCurrentDialog(True)

    def textChange(self, key, c, w, text):
        """Save attribute mapping changes"""
        self.settings.setValue(key, text)

    def input_widget(self, text, edit_text="", type=""):
        """Create input widget for attribute mapping"""
        container = self.mqt.CreateHorizontalContainer()
        label = self.mqt.CreateLabel()
        edit = self.mqt.CreateTextBox(True, partial(self.textChange, edit_text))

        self.mqt.SetWidgetText(label, text)
        self.mqt.SetWidgetText(edit, edit_text)
        
        # 设置edit的固定高度
        if hasattr(edit, 'Widget'):
            edit_qt = edit.Widget()
        else:
            edit_qt = edit
        
        if isinstance(edit_qt, QtWidgets.QLineEdit):
            edit_qt.setFixedHeight(24)  # 设置固定高度
        
        self.mqt.AddWidget(container, label)
        self.mqt.AddWidget(container, edit)

        container.label = label
        container.edit = edit
        return container

