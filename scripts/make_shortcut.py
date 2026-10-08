# -*- coding: utf-8 -*-
"""通过 Windows Shell API (IPersistFile) 创建 .lnk —— 由系统自己写出二进制，
保证格式绝对合规。纯 ctypes，不依赖 COM/WScript。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys


def _guid(text):
    """调用 CLSIDFromString 解析 GUID 字符串为 GUID 结构体。"""
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, wt.LPCWSTR, ctypes.c_void_p)
    fn = ctypes.cast(ole32.CLSIDFromString, proto)
    buf = ctypes.create_string_buffer(16)
    hr = fn(text, ctypes.byref(buf))
    if hr != 0:
        raise OSError(f"CLSIDFromString({text}) failed hr=0x{hr & 0xFFFFFFFF:08X}")
    return buf


class _IPersistFile(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.c_void_p)]


# IShellLinkW 方法索引（继承自 IPersist）
IShellLinkW_QueryInterface = 0
IShellLinkW_AddRef = 1
IShellLinkW_Release = 2
IShellLinkW_GetPath = 3
IShellLinkW_GetIDList = 4
IShellLinkW_SetIDList = 5
IShellLinkW_GetDescription = 6
IShellLinkW_SetDescription = 7
IShellLinkW_GetWorkingDirectory = 8
IShellLinkW_SetWorkingDirectory = 9
IShellLinkW_GetArguments = 10
IShellLinkW_SetArguments = 11
IShellLinkW_GetHotkey = 12
IShellLinkW_SetHotkey = 13
IShellLinkW_GetShowCmd = 14
IShellLinkW_SetShowCmd = 15
IShellLinkW_GetIconLocation = 16
IShellLinkW_SetIconLocation = 17
IShellLinkW_SetRelativePath = 18
IShellLinkW_Resolve = 19
IShellLinkW_SetPath = 20

IPersistFile_Save = 6


def _vtbl_call(obj, index, restype, argtypes, *args):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn_ptr = vtbl[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    fn = ctypes.cast(fn_ptr, proto)
    return fn(obj, *args)


def create_shortcut(target, link_path, working_dir, description, icon_location):
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    CLSID_ShellLinkW = _guid("{00021401-0000-0000-C000-000000000046}")
    IID_IShellLinkW = _guid("{000214F9-0000-0000-C000-000000000046}")
    IID_IPersistFile = _guid("{0000010B-0000-0000-C000-000000000046}")

    # CoInitializeEx(COINIT_APARTMENTTHREADED)
    ole32.CoInitializeEx(None, 0x2)
    try:
        link = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_ShellLinkW), None, 1,  # CLSCTX_INPROC_SERVER
            ctypes.byref(IID_IShellLinkW), ctypes.byref(link),
        )
        if hr != 0 or not link:
            raise OSError(f"CoCreateInstance IShellLinkW failed, hr=0x{hr & 0xFFFFFFFF:08X}")

        # SetPath
        hr = _vtbl_call(link, IShellLinkW_SetPath, ctypes.c_long, [wt.LPCWSTR], target)
        if hr != 0:
            raise OSError(f"SetPath failed hr=0x{hr & 0xFFFFFFFF:08X}")

        # SetWorkingDirectory
        _vtbl_call(link, IShellLinkW_SetWorkingDirectory, ctypes.c_long, [wt.LPCWSTR], working_dir)

        # SetDescription
        _vtbl_call(link, IShellLinkW_SetDescription, ctypes.c_long, [wt.LPCWSTR], description)

        # SetIconLocation
        icon_path, _, icon_idx = icon_location.rpartition(",")
        _vtbl_call(link, IShellLinkW_SetIconLocation, ctypes.c_long,
                   [wt.LPCWSTR, ctypes.c_int], icon_path, int(icon_idx or 0))

        # SetShowCmd(SW_SHOWNORMAL = 1)
        _vtbl_call(link, IShellLinkW_SetShowCmd, ctypes.c_long, [ctypes.c_int], 1)

        # QueryInterface(IPersistFile)
        persist = ctypes.c_void_p()
        hr = _vtbl_call(link, IShellLinkW_QueryInterface, ctypes.c_long,
                        [ctypes.c_void_p, ctypes.c_void_p],
                        ctypes.byref(IID_IPersistFile), ctypes.byref(persist))
        if hr != 0 or not persist:
            raise OSError(f"QueryInterface IPersistFile failed hr=0x{hr & 0xFFFFFFFF:08X}")

        # IPersistFile::Save
        hr = _vtbl_call(persist, IPersistFile_Save, ctypes.c_long,
                        [wt.LPCWSTR, ctypes.c_int], link_path, 1)
        if hr != 0:
            raise OSError(f"IPersistFile::Save failed hr=0x{hr & 0xFFFFFFFF:08X}")

        _vtbl_call(link, IShellLinkW_Release, ctypes.c_ulong, [])
        ole32.CoUninitialize()
        return link_path
    except Exception:
        ole32.CoUninitialize()
        raise


if __name__ == "__main__":
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    link = os.path.join(desktop, "TaskHub 任务工作台.lnk")
    if os.path.exists(link):
        os.remove(link)
    create_shortcut(
        target=r"E:\project\taskhub\scripts\start_taskhub.bat",
        link_path=link,
        working_dir=r"E:\project\taskhub",
        description="TaskHub 多电脑多 agent 任务工作台 - 一键启动",
        icon_location=r"C:\Windows\System32\shell32.dll,13",
    )
    print("Created:", link, os.path.getsize(link), "bytes")
