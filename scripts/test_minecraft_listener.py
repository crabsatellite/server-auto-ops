import importlib.util, json, socket, struct, threading, unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
spec=importlib.util.spec_from_file_location("listener",Path(__file__).with_name("minecraft_listener.py"))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class SignalTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,9,14,1,0,tzinfo=timezone.utc)
        self.config={"repository":"owner/ops","workflow":"start-minecraft.yml","branch":"master","allowed_actors":["owner"],"request_ttl_seconds":600}
        self.run={"id":12,"status":"completed","conclusion":"success","event":"repository_dispatch","head_branch":"master",
            "path":".github/workflows/start-minecraft.yml","repository":{"full_name":"owner/ops"},"actor":{"login":"owner"},
            "created_at":(self.now-timedelta(seconds=30)).isoformat()}
    def valid(self):return m.eligible(self.run,self.config,self.now-timedelta(minutes=5),[],self.now)
    def test_valid(self):self.assertTrue(self.valid())
    def test_duplicate(self):self.assertFalse(m.eligible(self.run,self.config,self.now-timedelta(minutes=5),["12"],self.now))
    def test_historical(self):self.assertFalse(m.eligible(self.run,self.config,self.now,[],self.now))
    def test_expired(self):
        self.run["created_at"]=(self.now-timedelta(hours=1)).isoformat();self.assertFalse(self.valid())
    def test_future(self):
        self.run["created_at"]=(self.now+timedelta(seconds=1)).isoformat();self.assertFalse(self.valid())
    def test_untrusted_sources(self):
        for field,value in [("event","pull_request"),("head_branch","evil"),("conclusion","failure"),("status","in_progress"),("path",".github/workflows/start-server.yml"),("actor",{"login":"other"}),("repository",{"full_name":"other/ops"})]:
            with self.subTest(field=field):
                previous=self.run[field];self.run[field]=value;self.assertFalse(self.valid());self.run[field]=previous
    def test_bad_date(self):
        self.run["created_at"]="bad";self.assertFalse(self.valid())

class ProtocolTests(unittest.TestCase):
    def test_varints(self):
        self.assertEqual(m.varint(0),b"\0");self.assertEqual(m.varint(128),b"\x80\x01");self.assertEqual(m.varint(767),b"\xff\x05")
    def fake_status(self,obj):
        server=socket.socket();server.bind(("127.0.0.1",0));server.listen(1);port=server.getsockname()[1]
        def serve():
            try:
                conn,_=server.accept()
                with conn:
                    n=m.recv_varint(conn);m.recv_exact(conn,n)
                    n=m.recv_varint(conn);m.recv_exact(conn,n)
                    raw=json.dumps(obj).encode();packet=b"\0"+m.varint(len(raw))+raw
                    conn.sendall(m.varint(len(packet))+packet)
            finally:server.close()
        t=threading.Thread(target=serve);t.start()
        try:return m.status(port=port)
        finally:t.join(3)
    def test_real_status_framing(self):self.assertEqual(self.fake_status({"players":{"online":5}})["players"]["online"],5)
    def test_missing_count_is_not_empty(self):
        with self.assertRaises(ValueError):self.fake_status({"version":{"name":"test"}})
    def test_current_process_alive(self):
        import os
        self.assertTrue(m.pid_alive(os.getpid()));self.assertFalse(m.pid_alive(None))

if __name__=="__main__":unittest.main()
