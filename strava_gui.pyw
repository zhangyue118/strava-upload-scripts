#!/usr/bin/env python3
"""
双击这个文件启动界面。

Windows 上 .pyw 关联到 pythonw.exe，**不会弹出控制台窗口** —— 这正是
最终用户想要的（strava_gui.py 用 python 跑会带一个黑窗，调试时才用它）。

麻烦的地方在于：没有控制台，就意味着启动阶段一旦抛异常，用户看到的是
"双击了，什么都没发生"。所以这里兜一层，把异常弹成对话框 —— 对一个
要发给别人的工具，这层比看上去值钱。
"""

import traceback

try:
    from strava_gui import main
    main()
except Exception:
    import tkinter.messagebox as messagebox
    messagebox.showerror(
        "Strava 上传器 — 启动失败",
        "程序在启动阶段就退出了。把下面这段信息发出来才能定位：\n\n"
        + traceback.format_exc(),
    )
