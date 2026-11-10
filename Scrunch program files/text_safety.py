"""Central choke point for anything that reaches the UI or gets spoken aloud.

Strip filesystem paths down to their basename and drop long hex IDs (session
IDs, checkpoint IDs, track/clip UUIDs) so error text never gets read out as a
string of letters and numbers. Applied once, at the response boundary in
server.py, rather than rewritten at every raise site.
"""
import re

_PATH=re.compile(r'(?:/[\w.\-]+){2,}/?')          # POSIX paths, 2+ segments
_HEX=re.compile(r'\b[0-9a-fA-F]{8,}\b')            # session/checkpoint/track hex IDs
_SPACE=re.compile(r'[ \t]{2,}')

def _basename(match):
    path=match.group(0).rstrip('/')
    return path.rsplit('/',1)[-1] or path

def sanitize(text):
    if not isinstance(text,str) or not text:
        return text
    text=_PATH.sub(_basename,text)
    text=_HEX.sub('',text)
    text=_SPACE.sub(' ',text)
    return re.sub(r' (?=[.,:;)])','',text).strip()
