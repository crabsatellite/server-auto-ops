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

if __name__=="__main__":unittest.main()
