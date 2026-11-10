"""Mixing entrypoint: shared product APIs, without startup recovery or observers.

Settings belong to the recipient's Application Support directory. Measurements
remain in RAM. Starting or closing an idle demo must never change Live.
"""
import argparse
import os
from pathlib import Path
import signal
import threading
import webbrowser


def configure_environment(data=None):
    os.environ['SCRUNCH_DATA']=str(Path(data).expanduser() if data else
        Path.home()/'Library/Application Support/Scrunch/Mixing')
    os.environ['SCRUNCH_AUDIT_STORAGE']='memory'
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
        os.environ[name]='1'


def close_owned_operations(product):
    """Close only a demo audition/undecided candidate, never unrelated playback."""
    import mix_engineer
    if mix_engineer.offer():
        mix_engineer.choose('neither',play=False)
    if product.engine.AUDITION:
        product.engine.safe_stop()


def create_server(port=0):
    # Environment must be configured before importing the shared runtime.
    import server as product
    from functools import partial
    # A Mixing recipient needs functional settings/history, not MIDI handoffs.
    product.engine.save=partial(product.engine.save,write_handoff=False)
    # This distribution does not include the on-device transcription helper.
    # Defaults inherited from the full app must not select an unavailable service.
    if product.engine.STATE.get('voiceEngine') not in ('openai','gemini'):
        product.engine.STATE['voiceEngine']='openai'
    class MixingHandler(product.Handler):
        def do_GET(self):
            if not self.allowed():return self.send(403,{'error':'Use the local Mixing address.'})
            path=self.path.split('?',1)[0]
            if path in ('/','/mixing-settings','/mixing-settings.js','/mixing-settings.css'):
                name='mixing-settings'+('.js' if path.endswith('.js') else '.css' if path.endswith('.css') else '.html')
                content={'.js':'text/javascript','.css':'text/css','.html':'text/html'}[Path(name).suffix]
                return self.send(200,(product.engine.ROOT/'dist'/name).read_bytes(),content)
            if path=='/mix-audit':
                page=(product.engine.ROOT/'dist/mix-audit.html').read_text()
                page=page.replace('<a href="/">Sound design</a>','<a href="/mixing-settings">Settings</a>')
                page=page.replace('Which of Chrome Mastering\'s saved notes have measured before-and-after evidence, and which are still unproved?',
                                  'Which of my saved engineer notes have measured before-and-after evidence, and which are still unproved?')
                page=page.replace('<a href="/" class="settings-link">','<a href="/mixing-settings" class="settings-link">')
                return self.send(200,page.encode(),'text/html')
            if self.path=='/api/mixing-demo/dream-link':   # the humming page (dream_server.py, started by the Scrunch app)
                import dream
                port=int(os.environ.get('DREAM_PORT','3352'))
                return self.send(200,{'url':f'http://127.0.0.1:{port}/?k={dream.key()}'})
            if self.path=='/api/mixing-demo/readiness':
                import mixing_readiness
                return self.send(200,mixing_readiness.check())
            return super().do_GET()
        def do_POST(self):
            if self.path!='/api/mixing-demo/provider':return super().do_POST()
            import json
            import secrets
            if not self.allowed() or self.headers.get('Origin',product.ORIGIN)!=product.ORIGIN or not secrets.compare_digest(self.headers.get('X-Scrunch-Token',''),product.TOKEN):
                return self.send(403,{'error':'Reload Mixing settings to authenticate.'})
            try:
                if self.headers.get('Content-Type')!='application/json':raise ValueError('JSON required.')
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=10000:raise ValueError('Settings request is empty or too large.')
                body=json.loads(self.rfile.read(size))
                if not isinstance(body,dict):raise ValueError('JSON object required.')
                with product.engine.LOCK:
                    ident=body.get('id')
                    if not any(p.get('id')==ident for p in product.engine.STATE.get('providers',[])):
                        raise ValueError('Choose a saved engineer profile.')
                    product.engine.STATE['engineerProvider']=ident
                    product.engine.save()
                return self.send(200,{'engineerProvider':ident})
            except (ValueError,OSError) as exc:return self.send(400,{'error':str(exc)})
    httpd=product.ThreadingHTTPServer(('127.0.0.1',port),MixingHandler)
    product.PORT=httpd.server_address[1]
    product.ORIGIN=f'http://127.0.0.1:{product.PORT}'
    return httpd,product


def main(argv=None):
    parser=argparse.ArgumentParser(description='Open Scrunch Mixing on this Mac.')
    parser.add_argument('--no-open',action='store_true',help='Do not open a browser.')
    parser.add_argument('--data',help='Override the functional settings directory.')
    parser.add_argument('--port',type=int,default=0,help='Local port (default: choose an unused port).')
    args=parser.parse_args(argv)
    configure_environment(args.data)
    import mixing_readiness
    readiness=mixing_readiness.check()
    if not readiness['localAnalysisReady']:
        print('Mixing cannot start. Double-click "1 Set up Scrunch (once)" first.',flush=True)
        for row in readiness['checks']:
            if not row['ready']:print(row['name']+': '+row['detail'],flush=True)
        return 1
    httpd,product=create_server(args.port)
    url=product.ORIGIN+'/mix-audit'
    print('Scrunch Mixing: '+url,flush=True)
    print('Audio stays in memory. Chat and voice use your own configured provider. '
          'Live capture/corrections require the Scrunch Bridge; newer Live versions remain unverified.',flush=True)
    print('Close this terminal or press Control-C to quit.',flush=True)
    quitting=threading.Event()
    def quit_server(*_):
        if quitting.is_set():return
        quitting.set()
        import mix_audit
        mix_audit.cancel_capture()
        # shutdown must run outside serve_forever's thread.
        threading.Thread(target=httpd.shutdown,daemon=True).start()
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):signal.signal(sig,quit_server)
    if not args.no_open:webbrowser.open(url)
    try:httpd.serve_forever(poll_interval=.1)
    finally:
        httpd.server_close()  # Wait for in-flight product requests to finish.
        try:close_owned_operations(product)
        except Exception as exc:
            print('A pending Live trial could not be restored: '+str(exc)+
                  '. Inspect Live before restarting; no blind retry was made.',flush=True)
        import mix_audit
        mix_audit.clear_memory(product.engine.DATA/'mix-audits')
    return 0


if __name__=='__main__':raise SystemExit(main())
