#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RenderDoc 自动绕过工具
整合多种绕过技术的一键式工具
"""

import os
import sys
import shutil
import subprocess
import time
import argparse
from pathlib import Path

class RenderDocBypass:
    def __init__(self, renderdoc_path, game_exe, output_dir="C:\\Temp\\TinecmaTools"):
        self.renderdoc_path = Path(renderdoc_path)
        self.game_exe = Path(game_exe)
        self.output_dir = Path(output_dir)
        self.game_process = None
        
    def step1_prepare_files(self):
        """步骤1: 准备并伪装文件"""
        print("\n" + "="*60)
        print("步骤 1: 文件准备与伪装")
        print("="*60)
        
        # 创建输出目录
        self.output_dir.mkdir(parents=True, exist_ok=True)
        print(f"[+] 创建目录: {self.output_dir}")
        
        # 文件映射
        files_to_copy = {
            "renderdoc.dll": "TinecmaTools.dll",
            "renderdoc.exe": "TinecmaTools.exe",
            "qrenderdoc.exe": "TinecmaToolsUI.exe",
            "renderdoccmd.exe": "TinecmaToolsCmd.exe"
        }
        
        for old_name, new_name in files_to_copy.items():
            src = self.renderdoc_path / old_name
            dst = self.output_dir / new_name
            
            if src.exists():
                shutil.copy2(src, dst)
                print(f"[+] 复制并重命名: {old_name} -> {new_name}")
            else:
                print(f"[-] 未找到: {old_name}")
        
        print("[完成] 文件准备完成")
        return True
    
    def step2_modify_strings(self):
        """步骤2: 修改二进制文件中的字符串特征"""
        print("\n" + "="*60)
        print("步骤 2: 修改特征字符串")
        print("="*60)
        
        dll_path = self.output_dir / "graphics_core.dll"
        
        if not dll_path.exists():
            print("[错误] DLL 文件不存在")
            return False
        
        # 读取文件
        with open(dll_path, 'rb') as f:
            data = bytearray(f.read())
        
        # 要替换的字符串对
        replacements = [
            (b'RenderDoc', b'TinecmaTl'),
            (b'renderdoc', b'tinecmatl'),
            (b'RENDERDOC', b'TINECMATL'),
        ]
        
        total_replaced = 0
        for old_bytes, new_bytes in replacements:
            count = 0
            start = 0
            while True:
                pos = data.find(old_bytes, start)
                if pos == -1:
                    break
                data[pos:pos+len(old_bytes)] = new_bytes
                count += 1
                start = pos + len(old_bytes)
            
            if count > 0:
                print(f"[+] 替换 '{old_bytes.decode()}': {count} 处")
                total_replaced += count
        
        # 写回文件
        if total_replaced > 0:
            with open(dll_path, 'wb') as f:
                f.write(data)
            print(f"[完成] 共修改 {total_replaced} 处特征字符串")
            return True
        else:
            print("[提示] 未找到需要替换的字符串")
            return True
    
    def step3_start_game(self):
        """步骤3: 启动游戏"""
        print("\n" + "="*60)
        print("步骤 3: 启动目标游戏")
        print("="*60)
        
        if not self.game_exe.exists():
            print(f"[错误] 游戏不存在: {self.game_exe}")
            return False
        
        print(f"[*] 启动: {self.game_exe.name}")
        try:
            self.game_process = subprocess.Popen(
                str(self.game_exe),
                cwd=str(self.game_exe.parent)
            )
            print(f"[+] 游戏进程 PID: {self.game_process.pid}")
            return True
        except Exception as e:
            print(f"[错误] 启动失败: {e}")
            return False
    
    def step4_wait_and_inject(self, delay=10):
        """步骤4: 等待并注入 DLL"""
        print("\n" + "="*60)
        print("步骤 4: 延迟注入")
        print("="*60)
        
        print(f"[*] 等待 {delay} 秒让游戏完全启动...")
        for i in range(delay, 0, -1):
            print(f"    倒计时: {i} 秒", end='\r')
            time.sleep(1)
        print("\n")
        
        # 使用 PowerShell 注入脚本
        dll_path = self.output_dir / "TinecmaTools.dll"
        inject_script = self.output_dir / "inject_temp.ps1"
        
        ps_code = f"""
$processId = {self.game_process.pid}
$dllPath = "{dll_path}"

Add-Type @"
using System;
using System.Runtime.InteropServices;

public class Inject {{
    [DllImport("kernel32.dll")]
    public static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    
    [DllImport("kernel32.dll")]
    public static extern IntPtr VirtualAllocEx(IntPtr hProcess, IntPtr addr, uint size, uint type, uint protect);
    
    [DllImport("kernel32.dll")]
    public static extern bool WriteProcessMemory(IntPtr hProcess, IntPtr addr, byte[] buffer, uint size, out int written);
    
    [DllImport("kernel32.dll")]
    public static extern IntPtr CreateRemoteThread(IntPtr hProcess, IntPtr attr, uint stack, IntPtr start, IntPtr param, uint flags, IntPtr tid);
    
    [DllImport("kernel32.dll", CharSet=CharSet.Ansi)]
    public static extern IntPtr GetProcAddress(IntPtr hModule, string name);
    
    [DllImport("kernel32.dll")]
    public static extern IntPtr GetModuleHandle(string name);
}}
"@

try {{
    $h = [Inject]::OpenProcess(0x1F0FFF, $false, $processId)
    $mem = [Inject]::VirtualAllocEx($h, [IntPtr]::Zero, $dllPath.Length + 1, 0x3000, 0x40)
    $bytes = [System.Text.Encoding]::ASCII.GetBytes($dllPath)
    [Inject]::WriteProcessMemory($h, $mem, $bytes, $bytes.Length, [ref]0) | Out-Null
    
    $k32 = [Inject]::GetModuleHandle("kernel32.dll")
    $load = [Inject]::GetProcAddress($k32, "LoadLibraryA")
    [Inject]::CreateRemoteThread($h, [IntPtr]::Zero, 0, $load, $mem, 0, [IntPtr]::Zero) | Out-Null
    
    Write-Host "[+] DLL 注入成功"
    exit 0
}} catch {{
    Write-Host "[错误] 注入失败: $_"
    exit 1
}}
"""
        
        # 写入脚本
        with open(inject_script, 'w', encoding='utf-8') as f:
            f.write(ps_code)
        
        print("[*] 执行注入...")
        try:
            result = subprocess.run(
                ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(inject_script)],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                print("[+] 注入成功!")
                print(result.stdout)
                return True
            else:
                print("[错误] 注入失败")
                print(result.stderr)
                return False
        except Exception as e:
            print(f"[错误] 执行失败: {e}")
            return False
        finally:
            # 清理临时脚本
            if inject_script.exists():
                inject_script.unlink()
    
    def step5_launch_ui(self):
        """步骤5: 启动 RenderDoc UI"""
        print("\n" + "="*60)
        print("步骤 5: 启动调试界面")
        print("="*60)
        
        ui_exe = self.output_dir / "TinecmaToolsUI.exe"
        
        if not ui_exe.exists():
            print("[错误] UI 程序不存在")
            return False
        
        print(f"[*] 启动: {ui_exe.name}")
        try:
            subprocess.Popen(str(ui_exe), cwd=str(self.output_dir))
            print("[+] UI 已启动")
            return True
        except Exception as e:
            print(f"[错误] 启动失败: {e}")
            return False
    
    def run_full_bypass(self, launch_ui=True, inject_delay=10):
        """执行完整的绕过流程"""
        print("\n" + "#"*60)
        print("# TinecmaTools - RenderDoc 反作弊绕过工具")
        print("# 自动化绕过流程")
        print("#"*60)
        
        steps = [
            ("文件准备", self.step1_prepare_files),
            ("字符串修改", self.step2_modify_strings),
            ("启动游戏", self.step3_start_game),
            ("延迟注入", lambda: self.step4_wait_and_inject(inject_delay)),
        ]
        
        if launch_ui:
            steps.append(("启动UI", self.step5_launch_ui))
        
        for step_name, step_func in steps:
            if not step_func():
                print(f"\n[失败] {step_name} 步骤失败")
                return False
        
        print("\n" + "="*60)
        print("[成功] 所有步骤完成!")
        print("="*60)
        print()
        print("现在您可以:")
        print("1. 在 TinecmaTools UI 中连接到游戏进程")
        print("2. 捕获帧进行分析")
        print("3. 调试图形渲染问题")
        print()
        
        return True


def main():
    parser = argparse.ArgumentParser(description="TinecmaTools - RenderDoc 反作弊自动绕过工具")
    parser.add_argument("--renderdoc", required=True, help="RenderDoc 安装路径")
    parser.add_argument("--game", required=True, help="游戏可执行文件路径")
    parser.add_argument("--output", default="C:\\Temp\\TinecmaTools", help="输出目录")
    parser.add_argument("--delay", type=int, default=10, help="注入延迟(秒)")
    parser.add_argument("--no-ui", action="store_true", help="不启动 UI")
    
    args = parser.parse_args()
    
    bypasser = RenderDocBypass(
        renderdoc_path=args.renderdoc,
        game_exe=args.game,
        output_dir=args.output
    )
    
    success = bypasser.run_full_bypass(
        launch_ui=not args.no_ui,
        inject_delay=args.delay
    )
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        print("TinecmaTools - RenderDoc 反作弊自动绕过工具\n")
        print("使用示例:")
        print('  python auto_bypass.py --renderdoc "C:\\Program Files\\RenderDoc" --game "C:\\Games\\YourGame\\game.exe"')
        print()
        print("参数说明:")
        print("  --renderdoc PATH  : RenderDoc 安装目录")
        print("  --game PATH       : 目标游戏可执行文件")
        print("  --output PATH     : 输出目录 (默认: C:\\Temp\\TinecmaTools)")
        print("  --delay SECONDS   : 注入延迟秒数 (默认: 10)")
        print("  --no-ui           : 不启动 RenderDoc UI")
        sys.exit(0)
    
    main()
