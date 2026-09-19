"""Delivery safeguards and a synthetic interview rehearsal; no private files."""
import json,socket,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from scripts import launch
from scripts.demo_interview import demo

class DeliveryTests(unittest.TestCase):
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
    def test_interview_rehearsal_checks_api_recovery_and_account_boundaries(self):
        result=demo();self.assertTrue(result['all_passed']);self.assertGreaterEqual(len(result['checks']),15)
