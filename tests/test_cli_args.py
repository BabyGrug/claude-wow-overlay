import json, os, sys, types, queue, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
class _Any(types.ModuleType):
    def __getattr__(self, n): return mock.MagicMock()
for _m in ("tkinter","tkinter.ttk","tkinter.font","tkinter.messagebox","tkinter.filedialog","PIL","PIL.Image","PIL.ImageTk","PIL.ImageGrab","PIL.ImageDraw","keyboard","ctypes.wintypes","winreg","pystray"):
    try: __import__(_m)
    except Exception: sys.modules[_m]=_Any(_m)
import ctypes
ctypes.windll=mock.MagicMock(); ctypes.WinDLL=mock.MagicMock(); ctypes.WINFUNCTYPE=ctypes.CFUNCTYPE
_o,_e=sys.stdout,sys.stderr
import overlay
sys.stdout,sys.stderr=_o,_e; sys.excepthook=sys.__excepthook__

class FakeProc:
    def __init__(self, lines): self.stdout=iter(lines); self.stderr=None; self.returncode=0
    def wait(self, timeout=None): return 0
    def terminate(self): pass

class T(unittest.TestCase):
    def test_permission_flags_and_answer(self):
        lines=[json.dumps({"type":"system","subtype":"init"})+"\n",
               json.dumps({"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}})+"\n",
               json.dumps({"type":"result","subtype":"success","is_error":False,"result":"Hi"})+"\n"]
        captured={}
        def fake_popen(cmd, **kw): captured['cmd']=cmd; return FakeProc(lines)
        self_=types.SimpleNamespace(ui_queue=queue.Queue(), _stopped_by_user=False, session_id=None,
                                    settings={"effort":"medium","session_id":None}, _current_proc=None)
        for name in dir(overlay.OverlayApp) if hasattr(overlay,'OverlayApp') else []:
            pass
        cls=[c for c in vars(overlay).values() if isinstance(c,type) and hasattr(c,'_run_grok_cli')][0]
        for n,v in vars(cls).items():
            if callable(v) and n.startswith('_') and n!='_run_grok_cli' and not n.startswith('__'):
                setattr(self_, n, types.MethodType(v, self_))
        with mock.patch.object(overlay,'_internet_available',return_value=True), \
             mock.patch.object(overlay.subprocess,'Popen',side_effect=fake_popen), \
             mock.patch.object(overlay,'save_settings'), \
             mock.patch.object(overlay,'SETTINGS_PATH','/tmp/gwo/settings.json'):
            try: cls._run_grok_cli(self_, "where is X?", "grok")
            except Exception as e:
                if 'cmd' not in captured: raise
        cmd=captured['cmd']
        i=cmd.index("--permission-mode"); self.assertEqual(cmd[i+1],"dontAsk")
        allows=[cmd[j+1] for j,a in enumerate(cmd) if a=="--allow"]
        self.assertEqual(set(allows),{"WebFetch","WebSearch"})
        self.assertNotIn("--always-approve",cmd); self.assertNotIn("--dangerously-skip-permissions",cmd)
        items=[]
        while not self_.ui_queue.empty(): items.append(self_.ui_queue.get())
        self.assertIn(("answer_chunk","Hi"),items)
        self.assertEqual(overlay.APP_VERSION,"1.3.1")
if __name__=="__main__": unittest.main()
