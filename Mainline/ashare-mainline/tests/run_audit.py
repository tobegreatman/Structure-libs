import io,os,re,subprocess,tempfile,sys
BASE=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html=io.open(os.path.join(BASE,'index.html'),encoding='utf-8').read()
rep=io.open(os.path.join(BASE,'aml-20261101-v16.json'),encoding='utf-8').read()
html=html.replace('</body>','<script id="rep" type="application/json">'+rep+'</script>\n<script src="tests/_audit_probe.js"></script>\n</body>')
tmp=os.path.join(BASE,'_audit.html')
io.open(tmp,'w',encoding='utf-8',newline='\n').write(html)
browser=None
for p in (r'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',r'C:/Program Files/Microsoft/Edge/Application/msedge.exe',r'C:/Program Files/Google/Chrome/Application/chrome.exe'):
    if os.path.exists(p): browser=p; break
prof=tempfile.mkdtemp(prefix='aml-audit-')
cmd=[browser,'--headless=new','--disable-gpu','--no-sandbox','--no-first-run','--user-data-dir='+prof.replace('\\','/'),'--dump-dom','--virtual-time-budget=5000','file:///'+tmp.replace('\\','/')]
dom=subprocess.run(cmd,capture_output=True,timeout=120).stdout.decode('utf-8','replace')
m=re.search(r'<div id="probe">([\s\S]*?)</div>',dom)
print(m.group(1) if m else 'NO PROBE\n'+dom[:600])
os.remove(tmp)
