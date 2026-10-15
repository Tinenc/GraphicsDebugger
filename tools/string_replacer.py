#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RenderDoc 字符串替换工具
用于修改可执行文件中的特征字符串
"""

import sys
import os
import shutil
from pathlib import Path

class BinaryStringReplacer:
    def __init__(self, file_path):
        self.file_path = file_path
        self.backup_path = f"{file_path}.backup"
        
    def backup(self):
        """备份原文件"""
        if not os.path.exists(self.backup_path):
            shutil.copy2(self.file_path, self.backup_path)
            print(f"[+] 已备份: {self.backup_path}")
        else:
            print(f"[!] 备份已存在: {self.backup_path}")
    
    def find_string(self, search_str, encoding='utf-8'):
        """查找字符串在文件中的位置"""
        with open(self.file_path, 'rb') as f:
            data = f.read()
        
        search_bytes = search_str.encode(encoding)
        positions = []
        start = 0
        
        while True:
            pos = data.find(search_bytes, start)
            if pos == -1:
                break
            positions.append(pos)
            start = pos + 1
        
        return positions
    
    def replace_string(self, old_str, new_str, encoding='utf-8'):
        """替换字符串"""
        if len(new_str) > len(old_str):
            print(f"[错误] 新字符串长度 ({len(new_str)}) 不能超过原字符串 ({len(old_str)})")
            return False
        
        # 填充到相同长度
        new_str_padded = new_str + '\x00' * (len(old_str) - len(new_str))
        
        with open(self.file_path, 'rb') as f:
            data = bytearray(f.read())
        
        old_bytes = old_str.encode(encoding)
        new_bytes = new_str_padded.encode(encoding)
        
        count = 0
        start = 0
        
        while True:
            pos = data.find(old_bytes, start)
            if pos == -1:
                break
            
            # 替换
            data[pos:pos+len(old_bytes)] = new_bytes
            count += 1
            start = pos + len(old_bytes)
            
            print(f"[+] 在偏移 0x{pos:08X} 处替换: '{old_str}' -> '{new_str}'")
        
        if count > 0:
            with open(self.file_path, 'wb') as f:
                f.write(data)
            print(f"[完成] 共替换 {count} 处")
            return True
        else:
            print(f"[!] 未找到字符串: '{old_str}'")
            return False
    
    def replace_multiple(self, replacements):
        """批量替换"""
        success_count = 0
        for old_str, new_str in replacements.items():
            if self.replace_string(old_str, new_str):
                success_count += 1
        
        print(f"\n[统计] 成功替换 {success_count}/{len(replacements)} 个字符串")
        return success_count


def main():
    print("=" * 60)
    print("RenderDoc 字符串替换工具")
    print("=" * 60)
    print()
    
    if len(sys.argv) < 2:
        print("使用方法:")
        print("  python string_replacer.py <file_path>")
        print()
        print("示例:")
        print("  python string_replacer.py renderdoc.dll")
        return
    
    file_path = sys.argv[1]
    
    if not os.path.exists(file_path):
        print(f"[错误] 文件不存在: {file_path}")
        return
    
    replacer = BinaryStringReplacer(file_path)
    
    # 备份原文件
    replacer.backup()
    
    # 定义要替换的字符串
    replacements = {
        "RenderDoc": "TinecmaTls",  # 长度必须相同或更短
        "renderdoc": "tinecmatls",
        "RENDERDOC": "TINECMATLS",
        "qrenderdoc": "qtinecmatls",
    }
    
    print("\n[*] 开始替换字符串...")
    print("-" * 60)
    
    # 执行替换
    replacer.replace_multiple(replacements)
    
    print()
    print("[提示] 如需恢复原文件,请使用备份文件:")
    print(f"  copy {replacer.backup_path} {file_path}")


if __name__ == "__main__":
    main()
