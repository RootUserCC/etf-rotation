# -*- coding: utf-8 -*-
"""server.py 的无窗口启动引导（供计划任务/启动.bat 用 base pythonw.exe 调用）。

.venv 里的 pythonw.exe 是 uv 转发器，它会再拉起一个控制台版 python.exe 子进程，
从而弹出一个终端窗口（关掉窗口服务器就被杀）。因此改用 base 解释器的
pythonw.exe（真 GUI 程序，无窗口）运行本脚本，再把 venv 的 site-packages
加进 sys.path 后执行 server.py。
"""
import os
import runpy
import site
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
site.addsitedir(os.path.join(HERE, '.venv', 'Lib', 'site-packages'))
os.chdir(HERE)
sys.argv = [os.path.join(HERE, 'server.py')]
runpy.run_path(sys.argv[0], run_name='__main__')
