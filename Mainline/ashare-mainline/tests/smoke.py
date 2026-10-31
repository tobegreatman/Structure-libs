#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冒烟测试：把 tests/probe.js 注入 index.html 的副本，用无头 Edge 跑一遍。

用法：
    python tests/smoke.py
    EDGE="C:/Program Files/.../msedge.exe" python tests/smoke.py

退出码 0 = 全部通过；1 = 有 FAIL 或页面没跑起来。
说明：页面是单文件 + 本地 vendor，没有构建步骤，这个脚本就是唯一的自动化回归。
"""
import io
import os
import re
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(BASE, 'index.html')
PROBE = os.path.join(BASE, 'tests', 'probe.js')
TMP_HTML = os.path.join(BASE, '_smoke.html')

CANDIDATES = [
    os.environ.get('EDGE', ''),
    r'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    r'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
    r'C:/Program Files/Google/Chrome/Application/chrome.exe',
    r'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
]


def find_browser():
    for p in CANDIDATES:
        if p and os.path.exists(p):
            return p
    sys.exit('找不到 Edge/Chrome，可用环境变量 EDGE 指定可执行文件路径')


def main():
    html = io.open(INDEX, encoding='utf-8').read()
    probe = io.open(PROBE, encoding='utf-8').read()
    # 以文件形式引入，避免把测试代码内联进副本后难以定位报错行
    io.open(os.path.join(BASE, 'tests', '_probe.tmp.js'), 'w', encoding='utf-8', newline='\n').write(probe)
    html = html.replace('</body>', '<script src="tests/_probe.tmp.js"></script>\n</body>')
    io.open(TMP_HTML, 'w', encoding='utf-8', newline='\n').write(html)

    profile = tempfile.mkdtemp(prefix='aml-smoke-')
    cmd = [
        find_browser(), '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
        '--user-data-dir=' + profile.replace('\\', '/'),
        '--dump-dom', '--virtual-time-budget=4000',
        'file:///' + TMP_HTML.replace('\\', '/'),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=90)
        dom = res.stdout.decode('utf-8', 'replace')
    finally:
        for p in (TMP_HTML, os.path.join(BASE, 'tests', '_probe.tmp.js')):
            try:
                os.remove(p)
            except OSError:
                pass

    m = re.search(r'<div id="probe">([\s\S]*?)</div>', dom)
    if not m:
        print('页面没有产出测试结果（脚本报错或未挂载）。DOM 片段：')
        print(dom[:800])
        return 1

    text = m.group(1)
    print(text)
    total = len(text.strip().splitlines())
    fails = text.count('FAIL ')
    print('\n%d 项，%d 失败' % (total, fails))
    return 0 if fails == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
