import importlib.util, json, socket, struct, threading, unittest
from unittest.mock import Mock, patch
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
    def test_explicit_legacy_workflow(self):
        self.config["workflows"]=["start-minecraft.yml","start-server.yml"]
        self.run["path"]=".github/workflows/start-server.yml"
        self.assertTrue(self.valid())
    def test_failed_legacy_cloud_run_is_not_a_signal(self):
        self.config["workflows"]=["start-minecraft.yml","start-server.yml"]
        self.run.update(path=".github/workflows/start-server.yml",conclusion="failure")
        self.assertFalse(self.valid())
    def test_unknown_workflow_stays_rejected(self):
        self.config["workflows"]=["start-minecraft.yml","start-server.yml"]
        self.run["path"]=".github/workflows/reboot-server.yml"
        self.assertFalse(self.valid())
    def test_unauthorized_rerun_rejected(self):
        self.run["triggering_actor"]={"login":"other"};self.assertFalse(self.valid())
    def test_naive_date_rejected(self):
        self.run["created_at"]="2026-09-14T00:59:30";self.assertFalse(self.valid())
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

class IdleTests(unittest.TestCase):
    def test_empty_threshold(self):
        t=m.IdleTimer(30)
        self.assertFalse(t.observe(0,100)); self.assertFalse(t.observe(0,129)); self.assertTrue(t.observe(0,130))
    def test_failure_resets_empty_period(self):
        t=m.IdleTimer(30)
        t.observe(0,100); self.assertFalse(t.observe(None,129))
        self.assertFalse(t.observe(0,500)); self.assertTrue(t.observe(0,530))
    def test_player_resets_empty_period(self):
        t=m.IdleTimer(30)
        t.observe(0,100); self.assertFalse(t.observe(1,129))
        self.assertFalse(t.observe(0,150)); self.assertTrue(t.observe(0,180))

class SupervisorTests(unittest.TestCase):
    def test_fresh_child_not_stalled(self):
        self.assertFalse(m.listener_stalled({},12,100,200,300))
    def test_child_no_heartbeat_stalled(self):
        self.assertTrue(m.listener_stalled({},12,100,401,300))
    def test_fresh_heartbeat(self):
        state={"listener_pid":12,"last_poll_utc":datetime.fromtimestamp(400,timezone.utc).isoformat()}
        self.assertFalse(m.listener_stalled(state,12,100,401,300))
    def test_other_pid_heartbeat_cannot_mask_stall(self):
        state={"listener_pid":99,"last_poll_utc":datetime.fromtimestamp(400,timezone.utc).isoformat()}
        self.assertTrue(m.listener_stalled(state,12,100,401,300))
    def test_bad_heartbeat_stalls(self):
        self.assertTrue(m.listener_stalled({"listener_pid":12,"last_poll_utc":"bad"},12,100,401,300))

class WorkflowPollingTests(unittest.TestCase):
    def test_workflow_validation(self):
        self.assertEqual(m.wake_workflows({"workflow":"start-minecraft.yml"}),["start-minecraft.yml"])
        for value in [[],"all",["../evil.yml"],["a.yml?x=1"]]:
            with self.assertRaises(ValueError):m.wake_workflows({"workflows":value})
    def test_one_workflow_failure_does_not_block_other(self):
        gh=Mock();gh.api.side_effect=[RuntimeError("test"),{"workflow_runs":[{"id":2,"created_at":"2026-09-18T00:00:00Z"}]}]
        runs,errors=m.poll_wake_runs(gh,{"repository":"owner/ops","workflows":["start-minecraft.yml","start-server.yml"]})
        self.assertEqual([r["id"] for r in runs],[2]);self.assertEqual(errors,{"start-minecraft.yml":"RuntimeError"})
    def test_dedup_and_order(self):
        gh=Mock();gh.api.side_effect=[{"workflow_runs":[{"id":2,"created_at":"b"},{"id":1,"created_at":"a"}]},{"workflow_runs":[{"id":2,"created_at":"b"}]}]
        runs,errors=m.poll_wake_runs(gh,{"repository":"owner/ops","workflows":["a.yml","b.yml"]})
        self.assertEqual([r["id"] for r in runs],[1,2]);self.assertEqual(errors,{})
    def test_legacy_workflow_has_no_cloud_start(self):
        text=(Path(__file__).parents[1]/".github/workflows/start-server.yml").read_text()
        self.assertIn("types: [start-server]",text)
        self.assertNotIn("ecs_control",text);self.assertNotIn("secrets.",text);self.assertNotIn("uses:",text)

class FrpTests(unittest.TestCase):
    def setUp(self):
        self.config={"frp":{"configured":True},"repository":"owner/ops"}
        self.guard=threading.Lock()
        self.pending=[{"id":"r1","created":m.time.time()}]
    def test_dispatch_once_through_existing_action(self):
        state={};gh=Mock()
        m.poll_frp_requests(self.config,gh,state,self.pending,self.guard)
        m.poll_frp_requests(self.config,gh,state,self.pending,self.guard)
        gh.api.assert_called_once_with("repos/owner/ops/dispatches",method="POST",
            data={"event_type":"start-minecraft","client_payload":{"frp_request":"r1"}})
        self.assertEqual(state["frp_seen"],["r1"])
    def test_failed_dispatch_remains_retryable(self):
        state={};gh=Mock();gh.api.side_effect=OSError("network")
        with self.assertRaises(OSError):m.poll_frp_requests(self.config,gh,state,self.pending,self.guard)
        self.assertEqual(state["frp_seen"],[])
    def test_expired_and_future_requests_ignored(self):
        gh=Mock();self.pending=[{"id":"old","created":m.time.time()-601},{"id":"future","created":m.time.time()+60}]
        m.poll_frp_requests(self.config,gh,{},self.pending,self.guard);gh.api.assert_not_called()
    def test_shutdown_wake_waits_without_consuming(self):
        state={};gh=Mock();self.config["state_prefix"]="unused"
        with patch.object(m,"read_json",return_value={"phase":"backing_up"}):
            m.poll_frp_requests(self.config,gh,state,self.pending,self.guard)
        gh.api.assert_not_called();self.assertEqual(state,{})
    def test_native_frpc_credentials_never_in_arguments(self):
        c={"state_prefix":"C:/data/mc","frp":{"exe":"frpc.exe","tunnel_ids":[1,2]}}
        with patch.object(m,"frp_token",return_value="private-token"),patch.object(m.subprocess,"Popen") as spawn:
            m.open_frp(c,None)
        args=spawn.call_args.args[0];kw=spawn.call_args.kwargs
        self.assertNotIn("private-token"," ".join(args))
        self.assertEqual(kw["env"]["NATFRP_TARGET"],"1,2")
        self.assertEqual(kw["env"]["NATFRP_TOKEN"],"private-token")
    def test_sakura_authorization_matches_exact_ip(self):
        response=Mock();response.status=200;response.read.return_value=b'"192.0.2.9"'
        cm=Mock();cm.__enter__=Mock(return_value=response);cm.__exit__=Mock(return_value=False)
        with patch.object(m,"frp_token",return_value="host-only"),patch.object(m.urllib.request,"urlopen",return_value=cm) as call:
            m.authorize_frp({"frp":{"game_tunnel_id":42}},"192.0.2.9")
            self.assertEqual(json.loads(call.call_args.args[0].data),{"id":42,"ip":"192.0.2.9"})
            response.read.return_value=b'"192.0.2.10"'
            with self.assertRaises(ConnectionError):m.authorize_frp({"frp":{"game_tunnel_id":42}},"192.0.2.9")

class ProxyHeaderTests(unittest.TestCase):
    def parse(self,header):
        a,b=socket.socketpair()
        try:
            a.sendall(header);a.shutdown(socket.SHUT_WR);return m.proxy_source(b)
        finally:a.close();b.close()
    def header(self,family,data):return b"\r\n\r\n\0\r\nQUIT\n"+bytes([0x21,family])+struct.pack(">H",len(data))+data
    def test_ipv4(self):
        h=self.header(0x11,socket.inet_aton("192.0.2.9")+socket.inet_aton("127.0.0.1")+struct.pack(">HH",1234,443))
        self.assertEqual(self.parse(h),("192.0.2.9",1234))
    def test_ipv6(self):
        h=self.header(0x21,socket.inet_pton(socket.AF_INET6,"2001:db8::1")+socket.inet_pton(socket.AF_INET6,"::1")+struct.pack(">HH",1234,443))
        self.assertEqual(self.parse(h),("2001:db8::1",1234))
    def test_untrusted_header_rejected(self):
        for h in [b"X"*16,self.header(0x12,b"x"*12),self.header(0x11,b"x"*11),self.header(0x11,b"x"*513)]:
            with self.subTest(h=h[:16]):
                with self.assertRaises(ValueError):self.parse(h)
    def test_truncated_header_rejected(self):
        with self.assertRaises(ConnectionError):self.parse(b"\r\n")

class WakeApiTests(unittest.TestCase):
    def setUp(self):
        self.pending=[];self.guard=threading.Lock();self.c={"port":25565,"frp":{"api_port":0,"client_token":"friend-only","cert":"cert","key":"key"}}
        tls=Mock();tls.wrap_socket.side_effect=lambda conn,**kwargs:conn
        with patch.object(m.ssl,"SSLContext",return_value=tls):self.server=m.make_frp_api(self.c,self.pending,self.guard)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join(2)
    def request(self,method,path,token="friend-only"):
        with socket.create_connection(self.server.server_address,2) as conn:
            addr=socket.inet_aton("192.0.2.9")+socket.inet_aton("127.0.0.1")+struct.pack(">HH",1234,443)
            conn.sendall(b"\r\n\r\n\0\r\nQUIT\n"+bytes([0x21,0x11])+struct.pack(">H",len(addr))+addr)
            conn.sendall((f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\nX-Real-IP: 192.0.2.99\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").encode())
            import http.client
            r=http.client.HTTPResponse(conn);r.begin();return r.status
    def test_unauthorized_cannot_wake(self):
        with patch.object(m,"authorize_frp") as auth:
            self.assertEqual(self.request("POST","/wake","wrong"),401);auth.assert_not_called()
        self.assertEqual(self.pending,[])
    def test_verified_source_not_header_controls_admission(self):
        with patch.object(m,"authorize_frp") as auth:
            self.assertEqual(self.request("POST","/wake"),204)
            auth.assert_called_once_with(self.c,"192.0.2.9")
        self.assertEqual(len(self.pending),1)
    def test_failed_admission_cannot_enqueue(self):
        with patch.object(m,"authorize_frp",side_effect=OSError("offline")):
            self.assertEqual(self.request("POST","/wake"),503)
        self.assertEqual(self.pending,[])
    def test_readiness_is_real_protocol(self):
        with patch.object(m,"status",side_effect=OSError("stopped")):
            self.assertEqual(self.request("GET","/ready"),503)
        with patch.object(m,"status",return_value={"players":{"online":0}}):
            self.assertEqual(self.request("GET","/ready"),204)

if __name__=="__main__":unittest.main()
