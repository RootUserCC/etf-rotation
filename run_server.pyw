# -*- coding: utf-8 -*-
"""server.py 的无窗口启动引导（供计划任务/启动.bat 用 pythonw.exe 调用）。

将启动异常落盘到 server_err.log，避免 pythonw 无窗口模式下静默失败。
"""
import os
import runpy
import site
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
venv_site = os.path.join(HERE, '.venv', 'Lib', 'site-packages')
if os.path.isdir(venv_site):
    site.addsitedir(venv_site)
os.chdir(HERE)

# pythonw.exe 下 sys.stdout / sys.stderr 默认为 None，重定向到 devnull 防止第三方库写输出报错
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')

try:
    sys.argv = [os.path.join(HERE, 'server.py')]
    runpy.run_path(sys.argv[0], run_name='__main__')
except Exception:
    with open(os.path.join(HERE, 'server_err.log'), 'w', encoding='utf-8') as f:
        traceback.print_exc(file=f)
    raise
