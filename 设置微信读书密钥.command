#!/bin/zsh
/usr/bin/python3 - <<'PYKEY'
import getpass, os
from pathlib import Path
p=Path.home()/'.config/logseq-import/weread-api-key'
key=getpass.getpass('粘贴微信读书 API Key（输入不会显示），然后回车：').strip()
if not key.startswith('wrk-') or any(c.isspace() for c in key):
    raise SystemExit('密钥格式应为 wrk- 开头，请重新运行。')
p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
fd=os.open(str(p),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,'w') as f:f.write(key+'\n')
os.chmod(p,0o600)
print('已保存到本机私密配置；无需把密钥发到聊天。')
PYKEY
read 'REPLY?按回车关闭'
