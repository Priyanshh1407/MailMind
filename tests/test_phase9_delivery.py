"""Delivery safeguards and a synthetic interview rehearsal; no private files."""
import json,os,socket,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from scripts import launch
from scripts.service_runner import wait_for_stdin_stop
from scripts.demo_interview import demo

class DeliveryTests(unittest.TestCase):
    def test_child_stdin_control_releases_after_supervisor_message(self):
        read_fd,write_fd=os.pipe();stop=threading.Event()
        try:
            reader=threading.Thread(target=wait_for_stdin_stop,args=(stop,read_fd))
            reader.start();os.write(write_fd,b'STOP\n');reader.join(1)
            self.assertTrue(stop.is_set())
            self.assertFalse(reader.is_alive())
        finally:
            os.close(read_fd);os.close(write_fd)

    def test_port_conflict_does_not_stop_existing_listener(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen();port=listener.getsockname()[1]
            with self.assertRaisesRegex(RuntimeError,'existing processes were not changed'):launch.check_ports((port,))
            self.assertEqual(listener.getsockname()[1],port)
    def test_missing_state_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:self.assertEqual(launch.stop(directory)['status'],'not_running')
        process=Mock();process.poll.return_value=1
        with patch.object(launch,'urlopen',side_effect=AssertionError('Failed service must not be polled')):
            with self.assertRaisesRegex(RuntimeError,'exited before becoming ready'):launch.wait_ready(process,'http://127.0.0.1:8000/',timeout=1)
    def test_log_readiness_requires_the_owned_process_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'service.log';path.write_text('MailMind semantic indexer ready')
            process=Mock();process.poll.return_value=None
            launch.wait_log_ready(process,path,'semantic indexer ready',timeout=1)
        self.assertGreaterEqual(launch.INDEXER_READY_TIMEOUT_SECONDS,120)
    def test_stale_state_cannot_signal_a_reused_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'services.json';path.write_text(json.dumps({'schema':1,'workspace':str(launch.ROOT),'pid':999999999,'created':0}))
            with patch.object(launch,'urlopen',side_effect=AssertionError('No control call')):
                self.assertEqual(launch.stop(directory)['processes_signalled'],0)
    def test_foreign_workspace_state_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'services.json';path.write_text(json.dumps({'schema':1,'workspace':'foreign'}))
            with self.assertRaises(RuntimeError):launch.stop(directory)
            self.assertTrue(path.exists())
    def test_owned_services_receive_graceful_stdin_stop(self):
        process=Mock();process.poll.side_effect=[None,0,0]
        self.assertEqual(launch.shutdown([process],grace=1),0);process.stdin.write.assert_called_once_with(b'STOP\n');process.terminate.assert_not_called()
    def test_unresponsive_owned_child_has_bounded_fallback(self):
        process=Mock();process.poll.return_value=None
        self.assertEqual(launch.shutdown([process],grace=0),1);process.terminate.assert_called_once()
    def test_supervisor_identifies_the_exited_service(self):
        api=Mock();api.poll.return_value=None
        worker=Mock();worker.poll.return_value=-1073741819
        failed=launch.exited_service({'api':{'process':api},'worker':{'process':worker}})
        self.assertEqual((failed[0],failed[2]),('worker',-1073741819))
    def test_service_restart_budget_is_bounded_to_a_recent_window(self):
        window=launch.SERVICE_RESTART_WINDOW_SECONDS
        history=[1,2,3]
        self.assertEqual(launch.restart_times_within_window(history,window+10),[window+10])
        recent=[100,101,102]
        self.assertIsNone(launch.restart_times_within_window(recent,103))
        self.assertIn('worker',launch.RECOVERABLE_SERVICES)
        self.assertNotIn('api',launch.RECOVERABLE_SERVICES)
    def test_snapshot_build_allowlist_excludes_private_roots(self):
        text=Path('.dockerignore').read_text();self.assertTrue(text.splitlines()[2]=='**')
        for path in ['!data/','!models/','!.env','!credentials.json','!token.json','!venv/']:self.assertNotIn(path,text)
    def test_all_direct_dependencies_and_closure_are_pinned(self):
        for filename in ['requirements.txt','requirements.lock']:
            for line in Path(filename).read_text().splitlines():
                if line and not line.startswith('#'):self.assertRegex(line,r'^[A-Za-z0-9_.-]+==[^ ]+$')
    def test_frontend_exact_versions_match_lock(self):
        package=json.loads(Path('frontend/package.json').read_text());lock=json.loads(Path('frontend/package-lock.json').read_text())
        for group in ['dependencies','devDependencies']:
            for name,value in package[group].items():self.assertEqual(value,lock['packages']['node_modules/'+name]['version'])
    def test_compose_parse_and_no_github_ci(self):
        import yaml
        self.assertFalse(Path('.github/workflows/checks.yml').exists())
        compose=yaml.safe_load(Path('docker-compose.yml').read_text());self.assertEqual(set(compose['services']),{'api','worker','frontend'})
        self.assertEqual(compose['services']['api']['ports'],['127.0.0.1:8000:8000'])
    def test_old_process_name_and_port_kills_are_removed(self):
        for filename in ['start_all.bat','start_all.sh','end_all.bat','end_all.sh']:
            text=Path(filename).read_text()
            for obsolete in ['nodemon','taskkill','wmic','netstat']:self.assertNotIn(obsolete,text)
    def test_semantic_indexing_has_an_owned_service_separate_from_gmail(self):
        launcher=Path('scripts/launch.py').read_text()
        runner=Path('scripts/service_runner.py').read_text()
        self.assertIn("spawn('indexer'",launcher)
        self.assertIn("'api','worker','indexer'",runner)
    def test_interview_rehearsal_checks_api_recovery_and_account_boundaries(self):
        result=demo();self.assertTrue(result['all_passed']);self.assertGreaterEqual(len(result['checks']),15)
