#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 prompt / schema / sample / template 注入 index.html 的 EMBEDDED 常量。
整块重建，幂等：重复运行结果一致。"""
import json, io, os, re

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = BASE

def rd(p):
    with io.open(p, 'r', encoding='utf-8') as f:
        return f.read()

prompt   = rd(os.path.join(ROOT, 'prompt', 'ashare-mainline-prompt.md'))
schema   = json.loads(rd(os.path.join(ROOT, 'schema', 'ashare-mainline.schema.json')))
sample   = json.loads(rd(os.path.join(ROOT, 'samples', 'sample-report.json')))
template = json.loads(rd(os.path.join(ROOT, 'samples', 'empty-template.json')))

def indent(text, n=2):
    pad = ' ' * n
    return '\n'.join(pad + ln if ln.strip() else ln for ln in text.split('\n'))

def js_obj(o):
    return indent(json.dumps(o, ensure_ascii=False, indent=2))

def js_str(s):
    # json.dumps 已处理引号/换行；再把 </ 转义以绝不可能提前闭合 script
    return json.dumps(s, ensure_ascii=False).replace('</', '<\\/')

block = (
    'const EMBEDDED = {\n'
    '  sample: ' + js_obj(sample) + ',\n'
    '  schema: ' + js_obj(schema) + ',\n'
    '  template: ' + js_obj(template) + ',\n'
    '  prompt: ' + js_str(prompt) + '\n'
    '};'
)

html_path = os.path.join(ROOT, 'index.html')
html = rd(html_path)

pat = re.compile(r'const EMBEDDED = \{[\s\S]*?\n\};')
if not pat.search(html):
    raise SystemExit('未找到 EMBEDDED 块')
html = pat.sub(lambda m: block, html, count=1)

with io.open(html_path, 'w', encoding='utf-8', newline='\n') as f:
    f.write(html)

print('注入完成: index.html  %.1f KB' % (len(html.encode('utf-8')) / 1024))
print('  prompt 字符数: %d' % len(prompt))
print('  mainlines: %d' % len(sample['mainlines']))
for m in sample['mainlines']:
    s = sum(b['contribution'] for b in m['score_breakdown'] if b.get('contribution') is not None) - (m.get('penalty') or 0)
    print('  %-16s score=%.1f  recomputed=%.2f  diff=%.2f' % (m['name'], m['score'], s, abs(s - m['score'])))
