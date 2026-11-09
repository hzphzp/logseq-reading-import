#!/bin/zsh
cd -- "${0:A:h}"
/usr/bin/python3 weread_import.py --apply
RESULT=$?
if [ "$RESULT" -ne 0 ]; then print "本次未完成，请保留窗口中的错误信息。"; fi
read "REPLY?按回车关闭窗口"
exit "$RESULT"
