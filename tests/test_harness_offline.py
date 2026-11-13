"""双向编排的离线回归，所有模型调用均使用替身。"""
import contextlib
import datetime
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'template' / 'user-guard' / 'skills' / 'cae-harness' / 'scripts' / 'cae_harness.py'
spec = importlib.util.spec_from_file_location('cae_harness_test', SCRIPT)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def backends(budget=120000):
    return {name: {'available': True, 'binary': name + ('.exe' if os.name == 'nt' else ''),
                   'safe_input_budget_tokens': budget, 'model': None, 'windows_interop': False, 'models': []}
            for name in ('codex', 'agy')}


AUDIT = {'summary': '已核对', 'findings': [], 'read_files': ['a.py'], 'unread_files': []}
TASK = {'summary': '已推导', 'content': '证明正文', 'evidence': ['输入定义'], 'unverified': []}


class HarnessTests(unittest.TestCase):
    def setUp(self):
        harness._CANCELLED.clear()
        harness._SESSION_QUOTA_BLOCKED.clear()
        self.temp = tempfile.TemporaryDirectory(prefix='cae-test-')
        self.root = Path(self.temp.name)

    def tearDown(self):
        harness._CANCELLED.clear()
        harness._SESSION_QUOTA_BLOCKED.clear()
        self.temp.cleanup()

    def capture(self, function, args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = function(args)
        return code, json.loads(stream.getvalue())

    def args(self, *tokens):
        return harness.build_arg_parser().parse_args(list(tokens))

    def test_backend_absolute_paths_match_planned_relative_files(self):
        source = self.root / 'input.py'
        source.write_text('value = 1\n', encoding='utf-8')
        with patch.object(harness.shutil, 'which', return_value='wslpath'), patch.object(harness, '_is_posix', return_value=True):
            with patch.object(harness, '_wsl_path', return_value=str(source)):
                self.assertEqual(harness._normalize_rel_file('X:/task/input.py', self.root), 'input.py')
            with patch.object(harness, '_wsl_path', side_effect=harness.HarnessError('unmounted')):
                self.assertEqual(harness._normalize_rel_file('Q:/unmounted/input.py', self.root), 'Q:/unmounted/input.py')
        self.assertEqual(harness._normalize_rel_file(str(source), self.root), 'input.py')
        self.assertEqual(harness._normalize_rel_file('../outside.py', self.root), '../outside.py')

    def test_caller_symmetry_and_explicit_only_backends(self):
        engines = backends()
        engines['external'] = {'available': True}
        engines['sdk'] = {'available': True}
        self.assertEqual(harness.choose_engine(engines, 'codex'), 'agy:default')
        self.assertEqual(harness.choose_engine(engines, 'antigravity'), 'codex:default')
        for name in ('external:x', 'sdk:x'):
            with self.assertRaises(harness.HarnessError):
                harness.choose_engine(engines, 'codex', name)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(harness.resolve_caller('auto'), 'caller')
        with self.assertRaises(harness.HarnessError):
            harness.choose_engine(engines, 'caller')

    def test_probe_metadata_is_not_verified_capacity(self):
        (self.root / 'config.toml').write_text('model = "chosen"\nmodel_context_window = 999999\n', encoding='utf-8')
        (self.root / 'models_cache.json').write_text(json.dumps({'models': [{'slug': 'chosen', 'context_window': 500000,
            'max_context_window': 872000, 'effective_context_window_percent': 95}]}), encoding='utf-8')
        with patch.object(harness, '_find_binary', return_value='fake'), patch.object(harness, '_codex_home', return_value=(self.root, False)), \
             patch.dict(os.environ, {'CAE_AGY_HOME': str(self.root)}, clear=True), patch.object(harness, 'run_process') as runner:
            result = harness.probe_backends()
        runner.assert_not_called()
        self.assertEqual(set(result), {'codex', 'agy'})
        self.assertEqual(result['codex']['inferred_window_tokens'], 872000)
        self.assertIsNone(result['codex']['effective_compaction_tokens'])
        self.assertEqual(result['codex']['verified_status'], 'unverified')
        self.assertIsNone(result['agy']['configured_window_tokens'])
        self.assertNotIn('supports_true_1m', result['codex'])

    def test_probe_context_metadata_belongs_to_selected_model(self):
        cache = {'models': [{'slug': 'catalog-first', 'priority': 1, 'context_window': 500000},
                            {'slug': 'user-choice', 'priority': 2, 'context_window': 200000}]}
        (self.root / 'models_cache.json').write_text(json.dumps(cache), encoding='utf-8')
        with patch.object(harness, '_find_binary', return_value='fake'), \
             patch.object(harness, '_codex_home', return_value=(self.root, False)), \
             patch.dict(os.environ, {'CAE_AGY_HOME': str(self.root)}, clear=True):
            for slug, expected in (('user-choice', 200000), ('unknown-choice', None)):
                (self.root / 'config.toml').write_text(f'model = "{slug}"\n', encoding='utf-8')
                result = harness.probe_backends()['codex']
                self.assertEqual(result['cached_default_window_tokens'], expected)
                self.assertEqual(result['context_metadata_model'], slug if expected else None)

    def test_assess_reads_catalogs_and_preserves_frontend_and_cache(self):
        codex_root, agy_root = self.root / 'codex', self.root / 'agy'
        codex_root.mkdir()
        cli = agy_root / 'antigravity-cli'
        (cli / 'cache').mkdir(parents=True)
        (agy_root / 'config').mkdir()
        (codex_root / 'cache').mkdir()
        files = {
            codex_root / 'config.toml': 'model = "interactive-choice"\nmodel_reasoning_effort = "ultra"\n',
            codex_root / 'models_cache.json': json.dumps({'fetched_at': '2020-01-01T08:00:00+08:00', 'models': [
                {'slug': 'code-model', 'priority': 1, 'context_window': 500000, 'supported_efforts': ['high', 'xhigh', 'ultra']},
                {'slug': 'math-model', 'priority': 2, 'description': 'Frontier intelligence for the most demanding work.',
                 'supported_efforts': ['high', 'max', 'ultra']},
                {'slug': 'fast-model', 'priority': 3, 'description': 'Lightweight model', 'supported_efforts': ['low']},
            ]}),
            codex_root / 'cache' / 'cae_quota_state.json': '{"exhausted": true, "cooldown_until_epoch": 1}',
            cli / 'settings.json': '{"model": "Interactive CLI Choice"}',
            agy_root / 'config' / 'config.json': '{"userSettings": {"model": "Interactive Desktop Choice"}}',
            cli / 'cache' / 'cae_models_cache.json': '{"models": [{"slug": "vendor-fast-low"}, {"slug": "vendor-core-high"}]}',
        }
        for path, contents in files.items():
            path.write_text(contents, encoding='utf-8')
        with patch.dict(os.environ, {'CAE_CODEX_HOME': str(codex_root), 'CAE_AGY_HOME': str(agy_root)}, clear=True), \
             patch.object(harness, '_find_binary', side_effect=['native-codex', 'native-agy']), \
             patch.object(harness, 'run_process') as runner, patch.object(harness, 'invoke_engine') as invoke:
            code, report = self.capture(harness.cmd_assess, self.args('assess', '--caller', 'codex'))
        runner.assert_not_called()
        invoke.assert_not_called()
        self.assertEqual(code, 0)
        self.assertFalse(report['runtime_verified'])
        self.assertFalse(report['controls_native_spawn_agent'])
        codex, agy = (report['backends'][name] for name in ('codex', 'agy'))
        self.assertEqual((codex['caller_relation'], agy['caller_relation']), ('local', 'counterpart'))
        self.assertEqual(codex['model'], 'interactive-choice')
        self.assertEqual(agy['configured_model_source'], str(cli / 'settings.json'))
        self.assertEqual(agy['config_files']['desktop']['path'], str(agy_root / 'config' / 'config.json'))
        self.assertEqual(codex['model_catalog']['fetched_at'], '2020-01-01T00:00:00+00:00')
        self.assertEqual(codex['model_catalog']['freshness'], 'stale')
        self.assertEqual(agy['model_catalog']['freshness'], 'unknown')
        self.assertIsNone(agy['model_catalog']['age_seconds'])
        math = report['recommendations']['codex']['math']
        self.assertEqual((math['model'], math['reasoning_effort'], math['tier_reason'], math['profile_source'], math['effort_source']),
                         ('math-model', 'max', 'task_specialty_math_max', 'specialty_deep_reasoning', 'catalog_declared'))
        surface = report['recommendations']['codex']['surface_review']
        self.assertEqual((surface['model'], surface['reasoning_effort']), ('fast-model', 'low'))
        self.assertEqual(set(path for path in self.root.rglob('*') if path.is_file()), set(files))
        for path, contents in files.items():
            self.assertEqual(path.read_text(encoding='utf-8'), contents)

    def test_catalog_freshness_uses_collection_time_only(self):
        cache = self.root / 'cache.json'
        now = datetime.datetime(2026, 1, 8, tzinfo=datetime.timezone.utc)
        self.assertEqual(harness._catalog_metadata(cache, {}, [], 'cache', now)['freshness'], 'missing')
        cache.write_text('{}', encoding='utf-8')
        self.assertEqual(harness._catalog_metadata(cache, {}, [], 'cache', now)['freshness'], 'empty_or_unreadable')
        for stamp, expected, age in (
            (None, 'unknown', None), ('2026-01-08T00:00:00', 'unknown', None),
            ('2026-01-08T00:00:01Z', 'future_timestamp', None), ('2026-01-08T00:00:00Z', 'within_assessment_window', 0),
            ('2026-01-07T23:59:59.509331500Z', 'within_assessment_window', 0),
            ('2026-01-01T00:00:00Z', 'stale', harness.CATALOG_MAX_AGE_SECONDS),
        ):
            with self.subTest(stamp=stamp):
                catalog = harness._catalog_metadata(cache, {'fetched_at': stamp}, [{}], 'cache', now)
                self.assertEqual((catalog['freshness'], catalog['age_seconds']), (expected, age))

    def test_assess_discovery_uses_cli_listing_without_refreshing_cache(self):
        cache = self.root / '.gemini' / 'antigravity-cli' / 'cache' / 'cae_models_cache.json'
        cache.parent.mkdir(parents=True)
        cache.write_text('{"models": [{"slug": "cached-high"}]}', encoding='utf-8')
        response = subprocess.CompletedProcess(['agy', 'models'], 0, 'vendor-core-xhigh\tVendor Core (XHigh)\n', '')
        with patch.dict(os.environ, {}, clear=True), patch.object(harness.Path, 'home', return_value=self.root), \
             patch.object(harness, '_find_binary', side_effect=[None, 'agy']), \
             patch.object(harness, 'run_process', return_value=response) as runner:
            code, report = self.capture(harness.cmd_assess, self.args('assess', '--discover-models'))
        self.assertEqual(code, 0)
        runner.assert_called_once_with(['agy', 'models'], Path.cwd(), timeout_sec=30)
        agy = report['backends']['agy']
        self.assertEqual(agy['verified_status'], 'model_listing_only')
        self.assertEqual(agy['model_catalog']['timestamp_source'], 'cli_query_time_utc')
        self.assertEqual(agy['cached_model_catalog']['freshness'], 'unknown')
        self.assertEqual(cache.read_text(encoding='utf-8'), '{"models": [{"slug": "cached-high"}]}')

    def test_non_delegating_profile_does_not_inherit_or_guess_ultra(self):
        engines = backends()
        engines['codex'].update(model='only-delegating', reasoning_effort='ultra', models=[
            {'slug': 'only-delegating', 'priority': 1, 'supported_efforts': ['ultra']},
            {'slug': 'eligible', 'priority': 2, 'supported_efforts': ['high']},
        ])
        engines['agy']['models'] = [{'slug': 'vendor-first-ultra'}, {'slug': 'vendor-second-high'}]
        self.assertEqual(harness.resolve_execution_profile(engines, 'codex:default', 'max')['model'], 'eligible')
        self.assertEqual(harness.resolve_execution_profile(engines, 'agy:default', 'max')['model'], 'vendor-second-high')
        self.assertEqual(harness.resolve_execution_profile(engines, 'agy:default', 'ultra')['model'], 'vendor-first-ultra')
        unverified = harness.resolve_execution_profile(engines, 'codex:unlisted', 'high')
        self.assertEqual((unverified['cli_effort'], unverified['effort_source']), ('high', 'unverified_request'))
        unverified = harness.resolve_execution_profile(engines, 'agy:unlisted-high', 'high')
        self.assertEqual(unverified['effort_source'], 'unverified_request')
        for name in engines:
            engines[name]['models'].pop()
            blocked = harness.resolve_execution_profile(engines, name + ':default', 'max')
            self.assertIsNone(blocked['cli_effort'])
            self.assertEqual(blocked['effort_source'], 'no_non_delegating_effort')
            with patch.object(harness, 'probe_backends', return_value=engines), patch.object(harness, 'invoke_engine') as invoke:
                with self.assertRaises(harness.HarnessError):
                    harness.cmd_task(self.args('task', '--engine', name + ':default', '--kind', 'math',
                                               '--target', str(self.root), '--task', '检查结果'))
                invoke.assert_not_called()
        with patch.dict(os.environ, {'CAE_DEFAULT_TIER': 'ultra'}, clear=True):
            self.assertEqual(harness.select_task_tier(text='不要使用 ultra，检查结果')[0], 'high')

    def test_explicit_model_has_own_budget_cap(self):
        engines = backends(700000)
        engines['codex']['models'] = [{'slug': 'small', 'context_window': 500000, 'max_context_window': 872000}]
        self.assertEqual(harness._budget_for_spec(engines, 'codex:small'), 450000)
        engines['codex']['configured_window_tokens'] = 900000
        self.assertEqual(harness._budget_for_spec(engines, 'codex:small'), 784800)

    def test_plan_skips_oversize_with_explicit_coverage(self):
        records = [{'rel_path': 'small.lean', 'tokens': 20}, {'rel_path': 'large.f90', 'tokens': 200}]
        plan = harness.select_audit_plan(220, records, backends(100), caller='codex', shard_tokens=100)
        self.assertEqual(plan['left_engine'], 'agy:default')
        self.assertEqual(plan['right_engine'], 'codex:default')
        self.assertEqual(len(plan['skipped_files']), 2)
        self.assertEqual(plan['left_units'][0]['files'], ['small.lean'])
        self.assertIn('跨分片', plan['coverage_notice'])
        with self.assertRaises(harness.HarnessError):
            harness.select_audit_plan(220, records, backends(100), strategy='single-pass', caller='codex')

    def test_whole_alias_and_unconfigured_preview(self):
        records = [{'rel_path': 'solver.py', 'tokens': 20}]
        plan = harness.select_audit_plan(20, records, backends(), strategy='whole-1m', caller='antigravity')
        self.assertEqual(plan['mode'], 'single-pass')
        self.assertIn('deprecation_notice', plan)
        plan = harness.select_audit_plan(20, records, {}, allow_unconfigured=True)
        self.assertEqual(plan['mode'], 'unconfigured')

    def test_large_and_research_files_registered(self):
        for name in ('proof.lean', 'paper.tex', 'source.inc'):
            (self.root / name).write_text('内容', encoding='utf-8')
        (self.root / 'large.f90').write_bytes(b'x' * (2 * 1024 * 1024 + 1))
        (self.root / 'empty.py').touch()
        skipped = []
        records = harness.collect_target_files(self.root, skipped)
        self.assertEqual(len(records), 4)
        self.assertEqual(skipped[0]['reason'], 'empty_file')

    def test_invalid_outputs_do_not_become_clean_audits(self):
        for raw in ('', 'request failed', '{}', '{"summary":"ok","findings":"none"}',
                    '{"status":"ERROR","structured_output":{"summary":"ok","findings":[]}}',
                    '{"status":"partial","structured_output":{"summary":"ok","findings":[]}}'):
            with self.subTest(raw=raw), self.assertRaises(harness.HarnessError):
                harness._extract_json_object(raw)
        with self.assertRaises(harness.OutputParseError):
            harness._extract_json_object(json.dumps({**TASK, 'content': ''}), harness.TASK_JSON_SCHEMA)

    def test_stream_result_and_schema_validation(self):
        raw = json.dumps({'event': 'init'}) + '\n' + json.dumps({'event': 'result', 'result': {'status': 'OK', 'structured_output': TASK}})
        self.assertEqual(harness._extract_json_object(raw, harness.TASK_JSON_SCHEMA), TASK)
        bad = {**AUDIT, 'findings': [{'file': 'a', 'line_start': True, 'line_end': 2, 'severity': 'high',
                                   'category': 'x', 'description': 'x', 'evidence': 'x'}]}
        with self.assertRaises(harness.OutputParseError):
            harness._extract_json_object(json.dumps(bad))

    def test_codex_model_forwarded_and_failure_with_output_rejected(self):
        def runner(command, cwd, input_text, timeout_sec, **kwargs):
            self.assertIn('-m', command)
            self.assertEqual(command[command.index('-m') + 1], 'chosen')
            Path(command[command.index('-o') + 1]).write_text(json.dumps(AUDIT), encoding='utf-8')
            return subprocess.CompletedProcess(command, 2, '', 'failure')
        with patch.object(harness, 'run_process', side_effect=runner), self.assertRaises(harness.HarnessError):
            harness.invoke_readonly_engine('codex:chosen', self.root, '任务', backends())

    def test_agency_long_prompt_uses_stdin_and_task_agent(self):
        prompt = '中文&|<>"%' * 8000
        def runner(command, cwd, input_text, timeout_sec, **kwargs):
            self.assertNotIn(prompt, command)
            self.assertEqual(json.loads(input_text)['message']['content'], prompt)
            self.assertEqual(command[command.index('--agent') + 1], 'readonly-collaborator')
            self.assertEqual(command[command.index('--print-timeout') + 1], '600s')
            self.assertNotIn('--dangerously-skip-permissions', command)
            self.assertEqual(kwargs['env']['CAE_HARNESS_DEPTH'], '1')
            stream = '\n'.join([
                json.dumps({'event': 'init', 'init': {'agent': 'readonly-collaborator'}}),
                json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'num_turns': 1, 'structured_output': TASK, 'response': json.dumps(TASK)}}),
            ])
            return subprocess.CompletedProcess(command, 0, stream, '')
        with patch.object(harness, 'run_process', side_effect=runner):
            self.assertEqual(harness.invoke_engine('agy:chosen', self.root, prompt, backends(), harness.TASK_JSON_SCHEMA), TASK)
        fallback = json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': None, 'response': json.dumps(TASK)}})
        self.assertEqual(harness._extract_json_object(fallback, harness.TASK_JSON_SCHEMA), TASK)

    def test_agy_edit_mode_and_finished_reason_statuses(self):
        def edit_runner(command, cwd, input_text, timeout_sec, **kwargs):
            self.assertIn('--mode', command)
            self.assertEqual(command[command.index('--mode') + 1], 'accept-edits')
            self.assertIn('--dangerously-skip-permissions', command)
            self.assertEqual(command[command.index('--print-timeout') + 1], '45s')
            return subprocess.CompletedProcess(command, 0, json.dumps({'event': 'result', 'result': {'structured_output': TASK}}), '')
        with patch.object(harness, 'run_process', side_effect=edit_runner):
            out = harness.invoke_edit_worker('agy:chosen', self.root, '修改', backends(), timeout_sec=45)
        self.assertEqual(json.loads(out), TASK)
        for reason, expected_status in (('print_timeout', 'timeout'), ('interrupted', 'cancelled'), ('error', 'failed')):
            payload = json.dumps({'event': 'result', 'result': {'structured_output': TASK, 'finished_reason': reason}})
            with self.subTest(reason=reason), self.assertRaises(harness.HarnessError) as caught:
                harness._extract_json_object(payload, harness.TASK_JSON_SCHEMA)
            self.assertEqual(caught.exception.status, expected_status)

    def test_wsl_codex_desktop_discovery_and_agy_model_parsing(self):
        fake_local = self.root / 'AppData' / 'Local'
        bin_dir = fake_local / 'OpenAI' / 'Codex' / 'bin' / 'v1'
        bin_dir.mkdir(parents=True)
        fake_exe = bin_dir / 'codex.exe'
        fake_exe.write_bytes(b'MZ')
        def fake_which(cmd):
            return {'wslpath': '/usr/bin/wslpath', 'cmd.exe': '/mnt/c/Windows/System32/cmd.exe'}.get(cmd)
        with patch.dict(os.environ, {}, clear=True), patch.object(harness, '_is_posix', return_value=True), \
             patch.object(harness.Path, 'home', return_value=self.root / 'linux-home'), \
             patch.object(harness.shutil, 'which', side_effect=fake_which), \
             patch.object(harness, '_windows_env', return_value='X:\\Users\\test\\AppData\\Local'), \
             patch.object(harness, '_wsl_path', return_value=str(fake_local)):
            found = harness._find_binary('codex')
            self.assertEqual(found, str(fake_exe))
            home, interop = harness._codex_home(found)
            self.assertTrue(interop)
            self.assertEqual(home, self.root / '.codex')
        sample_models = 'Fetching available models...\nagy-flagship-high\tFlagship Model (High)\nthirdparty-pro-high\tThirdParty Pro (High)\n'
        with patch.dict(os.environ, {}, clear=True), patch.object(harness.Path, 'home', return_value=self.root), \
             patch.object(harness, '_find_binary', return_value='fake-agy'), \
             patch.object(harness, '_codex_home', return_value=(self.root, False)), \
             patch.object(harness, 'run_process', return_value=subprocess.CompletedProcess(['agy', 'models'], 0, sample_models, '')):
            discovered = harness.probe_backends(discover_models=True)['agy']
        self.assertTrue(discovered['model_list_parsed'])
        self.assertEqual(discovered['models'][0], {'slug': 'agy-flagship-high', 'display_name': 'Flagship Model (High)'})
        self.assertEqual(len(discovered['models']), 2)

    def test_gate_worker_failure_never_calls_passing_verifier(self):
        verify = Mock(return_value=(0, 'base passes'))
        def worker(prompt, number):
            raise RuntimeError('worker failed')
        result = harness.run_physics_gate_loop(self.root, '改代码', 'verify', worker, verify, max_rounds=2)
        verify.assert_not_called()
        self.assertFalse(result['gate_passed'])
        self.assertEqual(result['status'], 'failed')

    def test_gate_retries_failed_verification(self):
        verify = Mock(side_effect=[(1, 'residual too large'), (0, 'ok')])
        result = harness.run_physics_gate_loop(self.root, '改代码', 'verify', lambda p, n: 'done', verify)
        self.assertTrue(result['gate_passed'])
        self.assertEqual(result['rounds_used'], 2)
        self.assertTrue(result['handoff_to_caller'])

    def test_scope_violation_preserves_existing_dirty_files(self):
        existing = self.root / 'existing.txt'
        existing.write_text('user change', encoding='utf-8')
        def worker(prompt, number):
            (self.root / 'outside.txt').write_text('new', encoding='utf-8')
            return 'done'
        verify = Mock(return_value=(0, 'ok'))
        result = harness.run_physics_gate_loop(self.root, '改代码', 'verify', worker, verify, write_scope=['allowed.py'])
        self.assertEqual(result['status'], 'scope_violation')
        self.assertEqual(result['scope_violations'], ['outside.txt'])
        self.assertEqual(existing.read_text(encoding='utf-8'), 'user change')
        self.assertTrue((self.root / 'outside.txt').exists())
        verify.assert_not_called()

    def test_verification_build_outputs_are_recorded_separately(self):
        (self.root / 'src').mkdir()
        def worker(prompt, number):
            (self.root / 'src' / 'main.py').write_text('value=1', encoding='utf-8')
            return 'done'
        def verifier(command, root):
            (root / 'build').mkdir(exist_ok=True)
            (root / 'build' / 'result.txt').write_text('ok', encoding='utf-8')
            return 0, 'ok'
        result = harness.run_physics_gate_loop(self.root, '改代码', 'verify', worker, verifier, write_scope=['src/*.py'])
        self.assertTrue(result['gate_passed'])
        self.assertEqual(result['verification_changed_files'], ['build/result.txt'])
        self.assertEqual(result['worker_changed_files'], ['src/main.py'])
        def bad_worker(prompt, number):
            (self.root / 'build' / 'result.txt').write_text('fabricated', encoding='utf-8')
            return 'done'
        result = harness.run_physics_gate_loop(self.root, '改代码', 'verify', bad_worker, verifier, write_scope=['src/*.py'])
        self.assertEqual(result['status'], 'scope_violation')
        self.assertEqual(result['scope_violations'], ['build/result.txt'])

    def test_task_reports_changes_even_when_worker_times_out(self):
        args = self.args('task', '--target', str(self.root), '--kind', 'math', '--task', '推导', '--caller', 'codex')
        def worker(*args, **kwargs):
            (self.root / 'unexpected.txt').write_text('changed', encoding='utf-8')
            raise harness.HarnessError('timeout', 'timeout')
        with patch.object(harness, '_args_backends', return_value=backends()), patch.object(harness, 'invoke_engine', side_effect=worker):
            code, report = self.capture(harness.cmd_task, args)
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'scope_violation')
        self.assertEqual(report['changed_files'], ['unexpected.txt'])

    def test_audit_partial_and_all_failed(self):
        (self.root / 'a.py').write_text('x=1', encoding='utf-8')
        args = self.args('audit', '--target', str(self.root), '--caller', 'codex')
        def worker(engine, *args):
            if engine.startswith('codex'):
                raise harness.OutputParseError('empty')
            return AUDIT
        with patch.object(harness, '_args_backends', return_value=backends()), patch.object(harness, 'invoke_readonly_engine', side_effect=worker):
            code, report = self.capture(harness.cmd_audit, args)
        self.assertEqual(code, 3)
        self.assertEqual(report['completed_count'], 1)
        with patch.object(harness, '_args_backends', return_value=backends()), patch.object(harness, 'invoke_readonly_engine', side_effect=harness.OutputParseError('bad')):
            code, report = self.capture(harness.cmd_audit, args)
        self.assertEqual(code, 4)
        self.assertEqual(report['completed_count'], 0)

    def test_audit_partial_reading_cannot_report_full_completion(self):
        (self.root / 'a.py').write_text('x=1', encoding='utf-8')
        (self.root / 'b.py').write_text('x=2', encoding='utf-8')
        args = self.args('audit', '--target', str(self.root), '--caller', 'codex', '--single')
        with patch.object(harness, '_args_backends', return_value=backends()), patch.object(harness, 'invoke_readonly_engine', return_value=AUDIT):
            code, report = self.capture(harness.cmd_audit, args)
        self.assertEqual(code, 3)
        self.assertEqual(report['completed_count'], 0)
        unit = report['completed_units'][0]
        self.assertEqual(unit['unread_files'][0]['file'], 'b.py')
        self.assertFalse(unit['reading_coverage_verified'])

    def test_location_pair_preserves_both_findings(self):
        left = {'file': 'a.py', 'line_start': 2, 'line_end': 2, 'severity': 'high',
                'category': 'mass', 'description': 'left', 'evidence': 'left evidence'}
        right = {**left, 'line_start': 5, 'line_end': 5, 'category': 'energy', 'evidence': 'right evidence'}
        report = harness.merge_blind_findings([left], [right], 'codex:a', 'agy:b', self.root)
        pair = report['consensus_findings'][0]
        self.assertEqual(pair['right_finding']['category'], 'energy')
        self.assertEqual(pair['right_finding']['evidence'], 'right evidence')
        self.assertEqual(pair['match_basis'], 'location_overlap_only')

    def test_task_dry_run_reads_bom_and_creates_nothing(self):
        task_file = self.root / '任务 input.txt'
        task_file.write_text('请推导', encoding='utf-8-sig')
        args = self.args('task', '--target', str(self.root), '--kind', 'math', '--task-file', str(task_file), '--caller', 'antigravity', '--dry-run')
        before = harness.snapshot_files(self.root)
        with patch.object(harness, '_args_backends', return_value=backends()), patch.object(harness, 'invoke_engine') as worker:
            code, report = self.capture(harness.cmd_task, args)
        worker.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(report['engine'], 'codex:default')
        self.assertEqual(harness.snapshot_files(self.root), before)
        self.assertEqual(harness._task_text(args), '请推导')

    def test_local_process_utf8_and_timeout_tree(self):
        result = harness.run_process([sys.executable, '-B', '-c', 'import sys; print(sys.stdin.read())'], self.root, '中文', 3)
        self.assertEqual(result.stdout.strip(), '中文')
        marker = self.root / 'grandchild-survived'
        child = 'import time; from pathlib import Path; time.sleep(1.2); Path(' + repr(str(marker)) + ').touch()'
        parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-B","-c",' + repr(child) + ']); time.sleep(30)'
        with self.assertRaises(harness.HarnessError) as caught:
            harness.run_process([sys.executable, '-B', '-c', parent], self.root, timeout_sec=0.4)
        self.assertEqual(caught.exception.status, 'timeout')
        time.sleep(1.3)
        self.assertFalse(marker.exists())

    def test_cancelled_process_has_non_success_status(self):
        timer = threading.Timer(0.2, harness._CANCELLED.set)
        timer.start()
        try:
            with self.assertRaises(harness.HarnessError) as caught:
                harness.run_process([sys.executable, '-B', '-c', 'import time; time.sleep(30)'], self.root, timeout_sec=5)
            self.assertEqual(caught.exception.status, 'cancelled')
        finally:
            timer.cancel()

    def test_interop_supervisor_uses_real_pid_and_utf8(self):
        def convert(path, windows=True):
            return 'Z:\\literal path\\' + Path(path).name
        with patch.object(harness, '_wsl_path', side_effect=convert):
            command, pid_file = harness._interop_command(['codex.exe', 'exec', '-C', 'Z:\\dir'], self.root, self.root, '中文', str(self.root))
        script = harness.base64.b64decode(command[-1]).decode('utf-16le')
        self.assertIn('[string]$PID', script)
        self.assertLess(script.index('[string]$PID'), script.index('[void]$p.Start()'))
        self.assertIn('StandardInput.BaseStream.Write', script)
        self.assertIn('finally', script)
        self.assertEqual((self.root / 'input.txt').read_text(encoding='utf-8'), '中文')
        self.assertEqual(pid_file.name, 'native.pid')

    def test_large_window_still_allows_existing_single_pass(self):
        (self.root / 'config.toml').write_text('model = "large"\nmodel_context_window = 872000\n', encoding='utf-8')
        (self.root / 'models_cache.json').write_text(json.dumps({'models': [
            {'slug': 'large', 'context_window': 500000, 'max_context_window': 872000},
            {'slug': 'small', 'context_window': 100000, 'max_context_window': 100000}]}), encoding='utf-8')
        with patch.object(harness, '_find_binary', return_value='fake'), patch.object(harness, '_codex_home', return_value=(self.root, False)):
            engines = harness.probe_backends()
        self.assertEqual(engines['codex']['safe_input_budget_tokens'], 784800)
        records = [{'rel_path': 'solver.f90', 'tokens': 193000}]
        plan = harness.select_audit_plan(193000, records, engines, dual_blind=False, caller='antigravity')
        self.assertEqual(plan['mode'], 'single-pass')
        self.assertEqual(harness._budget_for_spec(engines, 'codex:small'), 90000)

    def test_file_target_is_retained_in_prompt_and_receipt(self):
        file = self.root / 'specific.lean'
        file.write_text('theorem', encoding='utf-8')
        args = self.args('task', '--kind', 'math', '--target', str(file), '--task', '核对', '--caller', 'codex')
        def worker(engine, root, prompt, *args):
            self.assertIn(str(file), prompt)
            return TASK
        with patch.object(harness, '_args_backends', return_value=backends()), patch.object(harness, 'invoke_engine', side_effect=worker):
            code, report = self.capture(harness.cmd_task, args)
        self.assertEqual(code, 0)
        self.assertEqual(report['requested_target'], str(file))

    def test_directory_symlink_change_is_detected(self):
        target = self.root / 'dir'
        target.mkdir()
        before = harness.snapshot_files(self.root)
        link = self.root / 'link'
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != 'nt':
                raise
            env = os.environ.copy()
            env['CAE_TEST_LINK'] = str(link)
            env['CAE_TEST_TARGET'] = str(target)
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                "$ErrorActionPreference = 'Stop'; New-Item -ItemType Junction -Path $env:CAE_TEST_LINK -Target $env:CAE_TEST_TARGET | Out-Null"],
                env=env, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(harness.changed_files(before, harness.snapshot_files(self.root)), ['link'])
        (target / 'file.py').write_text('x=1', encoding='utf-8')
        skipped = []
        records = harness.collect_target_files(self.root, skipped)
        self.assertEqual([item['rel_path'] for item in records], ['dir/file.py'])
        self.assertEqual(skipped[0]['reason'], 'directory_link')

    def test_wsl_ext4_unc_staging_and_write_guard(self):
        nested = self.root / 'src'
        nested.mkdir()
        (nested / 'flux.f90').write_text('u = u + fl\n', encoding='utf-8')
        win_temp = self.root / 'win-temp'
        win_temp.mkdir()
        engines = backends()
        engines['codex']['binary'] = '/mnt/c/Codex/codex.exe'
        engines['codex']['windows_interop'] = True
        engines['codex']['config_home'] = str(self.root / '.codex')
        def fake_which(cmd):
            return {'powershell.exe': '/mnt/c/ps.exe', 'taskkill.exe': '/mnt/c/tk.exe', 'wslpath': '/usr/bin/wslpath'}.get(cmd)
        def fake_wsl_path(path, windows=True):
            p = Path(path)
            if not windows:
                return str(win_temp)
            if str(p).startswith(str(win_temp)):
                rel = p.relative_to(win_temp).as_posix().replace('/', '\\')
                return 'C:\\Temp\\' + rel
            return '\\\\wsl.localhost\\Ubuntu\\home\\alice\\proj'
        def fake_run(command, cwd, input_text, timeout_sec, **kwargs):
            self.assertTrue(str(cwd).startswith(str(win_temp)))
            staged_file = Path(cwd) / 'src' / 'flux.f90'
            self.assertTrue(staged_file.is_file())
            out_file = Path(cwd).parent / 'result.json'
            out_file.write_text(json.dumps({
                'summary': '已审', 'read_files': ['C:\\Temp\\' + staged_file.relative_to(win_temp).as_posix().replace('/', '\\')],
                'unread_files': [], 'findings': [{
                    'file': 'C:\\Temp\\' + staged_file.relative_to(win_temp).as_posix().replace('/', '\\'),
                    'line_start': 1, 'line_end': 1, 'severity': 'high', 'category': 'flux',
                    'description': '符号错误', 'evidence': 'u = u + fl',
                }],
            }), encoding='utf-8')
            return subprocess.CompletedProcess(command, 0, '', '')
        with patch.object(harness, '_is_posix', return_value=True), \
             patch.object(harness.shutil, 'which', side_effect=fake_which), \
             patch.object(harness, '_windows_env', return_value='C:\\Temp'), \
             patch.object(harness, '_wsl_path', side_effect=fake_wsl_path), \
             patch.object(harness, 'run_process', side_effect=fake_run):
            parsed = harness.invoke_readonly_engine('codex:default', self.root, '审查', engines, 30, ['src/flux.f90'])
            self.assertEqual(parsed['read_files'], ['src/flux.f90'])
            self.assertEqual(parsed['findings'][0]['file'], 'src/flux.f90')
            with self.assertRaisesRegex(harness.HarnessError, '纯 Linux 文件系统路径无法在 Windows codex.exe 沙箱内直接写入'):
                harness.invoke_edit_worker('codex:default', self.root, '修改', engines, 30)

    def test_specialty_model_routing_and_thinking_tier_grading(self):
        engines = backends()
        engines['codex']['models'] = harness._model_records({'models': [
            {'slug': 'model-legacy-fast', 'priority': 0, 'visibility': 'list',
             'context_window': 272000, 'max_context_window': 872000,
             'description': 'Older fast and efficient model.',
             'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}, {'effort': 'high'}, {'effort': 'max'}]},
            {'slug': 'model-workhorse', 'priority': 1, 'visibility': 'list',
             'context_window': 500000, 'max_context_window': 872000,
             'description': 'Latest workhorse model for coding and everyday work.',
             'supported_reasoning_levels': [
                 {'effort': 'low', 'description': 'Fast responses'},
                 {'effort': 'medium', 'description': 'Balanced'},
                 {'effort': 'high', 'description': 'Greater reasoning depth'},
                 {'effort': 'xhigh', 'description': 'Extra high reasoning depth'},
                 {'effort': 'max', 'description': 'Maximum reasoning depth for the hardest problems'},
                 {'effort': 'ultra', 'description': 'Maximum reasoning with automatic task delegation'},
             ]},
            {'slug': 'model-demanding', 'priority': 2, 'visibility': 'list',
             'context_window': 272000, 'max_context_window': 872000,
             'description': 'Frontier intelligence for the most demanding work.',
             'supported_reasoning_levels': [
                 {'effort': 'low', 'description': 'Fast responses'},
                 {'effort': 'medium', 'description': 'Balanced'},
                 {'effort': 'high', 'description': 'Greater reasoning depth'},
                 {'effort': 'xhigh', 'description': 'Extra high reasoning depth'},
                 {'effort': 'max', 'description': 'Maximum reasoning depth for the hardest problems'},
                 {'effort': 'ultra', 'description': 'Maximum reasoning with automatic task delegation'},
             ]},
            {'slug': 'model-hidden', 'priority': 3, 'visibility': 'hide',
             'description': 'Fast and affordable agentic coding model.',
             'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}, {'effort': 'high'}]},
            {'slug': 'model-fast', 'priority': 4, 'visibility': 'list',
             'context_window': 272000, 'max_context_window': 872000,
             'description': 'Fast and affordable model for easier tasks.',
             'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}, {'effort': 'high'}, {'effort': 'max'}]},
        ]})
        # 使用含 'mini' 子串的合成前缀 'omini'，防止子串误判把旗舰当成轻量模型
        engines['agy']['models'] = [
            {'slug': 'omini-prime-medium', 'display_name': 'Omini Prime (Medium)'},
            {'slug': 'omini-prime-high', 'display_name': 'Omini Prime (High)'},
            {'slug': 'omini-prime-low', 'display_name': 'Omini Prime (Low)'},
            {'slug': 'omini-deep-high', 'display_name': 'Omini Deep (High)'},
            {'slug': 'omini-deep-xhigh', 'display_name': 'Omini Deep (XHigh)'},
            {'slug': 'omini-deep-medium', 'display_name': 'Omini Deep (Medium)'},
            {'slug': 'omini-deep-low', 'display_name': 'Omini Deep (Low)'},
            {'slug': 'omini-flash-high', 'display_name': 'Omini Flash (High)'},
            {'slug': 'omini-flash-low', 'display_name': 'Omini Flash (Low)'},
            {'slug': 'othervendor-pro-high', 'display_name': 'OtherVendor Pro (High)'},
            {'slug': 'othervendor-pro-low', 'display_name': 'OtherVendor Pro (Low)'},
        ]
        # 1. 数理推导与路线攻坚：选深度推理专长模型 + 单模型纯思考最高挡 (max/xhigh)，默认跳过套娃 ultra
        tier, reason = harness.select_task_tier('auto', 'math', '推导一维守恒律离散残差', 1, 500)
        self.assertEqual((tier, reason), ('max', 'task_specialty_math_max'))
        c_math = harness.resolve_execution_profile(engines, 'codex:default', tier, '推导一维守恒律离散残差', kind='math', estimated_tokens=500)
        self.assertEqual((c_math['model'], c_math['reasoning_effort'], c_math['profile_source']),
                         ('model-demanding', 'max', 'specialty_deep_reasoning'))
        a_math = harness.resolve_execution_profile(engines, 'agy:default', tier, '推导一维守恒律离散残差', kind='math', estimated_tokens=500)
        self.assertEqual((a_math['model'], a_math['reasoning_effort'], a_math['cli_effort'], a_math['profile_source']),
                         ('omini-deep-xhigh', 'xhigh', None, 'specialty_deep_reasoning'))

        # 输入超深度推理模型默认窗口 75% 时，自动回退至大上下文工程主力模型
        c_math_big = harness.resolve_execution_profile(engines, 'codex:default', 'max', '大输入推导', kind='math', estimated_tokens=230000)
        self.assertEqual((c_math_big['model'], c_math_big['reasoning_effort']), ('model-workhorse', 'max'))

        # 显式指定 ultra 时才放行委派挡位
        tier_ultra, reason_ultra = harness.select_task_tier('ultra', 'math', '推导', 1, 500)
        self.assertEqual((tier_ultra, reason_ultra), ('ultra', 'explicit_tier_flag'))
        c_ultra = harness.resolve_execution_profile(engines, 'codex:default', tier_ultra, '推导', kind='math')
        self.assertEqual((c_ultra['model'], c_ultra['reasoning_effort']), ('model-demanding', 'ultra'))

        # 2. 常规编码与审查：选大窗口工程主力模型 + high 挡；含高阶数值/守恒特征时自动升至 max
        tier_code, reason_code = harness.select_task_tier('auto', 'code', '整理命令行参数解析辅助函数', 2, 1500)
        self.assertEqual((tier_code, reason_code), ('high', 'task_routine_high'))
        c_code = harness.resolve_execution_profile(engines, 'codex:default', tier_code, '整理命令行参数解析辅助函数', kind='code', estimated_tokens=1500)
        self.assertEqual((c_code['model'], c_code['reasoning_effort'], c_code['profile_source']),
                         ('model-workhorse', 'high', 'frontier_catalog'))
        a_code = harness.resolve_execution_profile(engines, 'agy:default', tier_code, '整理命令行参数解析辅助函数', kind='code', estimated_tokens=1500)
        self.assertEqual((a_code['model'], a_code['reasoning_effort']), ('omini-prime-high', 'high'))

        # 否定词不应误触发降挡，且 DGSEM/边界条件审查自动拉满单模型纯思考最高挡并匹配深度推理模型
        tier_neg, reason_neg = harness.select_task_tier('auto', 'review', '不要低档，仔细检查 DGSEM 边界条件与守恒性', 2, 1000)
        self.assertEqual((tier_neg, reason_neg), ('max', 'complexity_high_domain_max'))
        c_rev_deep = harness.resolve_execution_profile(engines, 'codex:default', tier_neg, '仔细检查 DGSEM 边界条件与守恒性', kind='review')
        self.assertEqual((c_rev_deep['model'], c_rev_deep['reasoning_effort']), ('model-demanding', 'max'))

        # 3. 短篇纯文字润色自动用 medium，长篇润色用 high
        tier_pol, reason_pol = harness.select_task_tier('auto', 'polish', '润色这句话使其符合CFD规范书面语：迎风格式在CFL大于1时失稳', 1, 600)
        self.assertEqual((tier_pol, reason_pol), ('medium', 'complexity_lightweight_polish'))
        c_med = harness.resolve_execution_profile(engines, 'codex:default', tier_pol, kind='polish')
        self.assertEqual((c_med['model'], c_med['reasoning_effort']), ('model-workhorse', 'medium'))
        a_med = harness.resolve_execution_profile(engines, 'agy:default', tier_pol, kind='polish')
        self.assertEqual(a_med['model'], 'omini-prime-medium')
        tier_pol_long, reason_pol_long = harness.select_task_tier('auto', 'polish', '整章学术论文语言润色', 5, 6000)
        self.assertEqual((tier_pol_long, reason_pol_long), ('high', 'complexity_long_polish'))

        # 4. 省额度/表面检查自动匹配当代轻量高速模型（跳过隐藏与 Older 旧代，且绝不误烧异构第三方配额池）
        tier_low, reason_low = harness.select_task_tier('auto', 'review', '省额度用低档快速看一下', 1, 400)
        self.assertEqual((tier_low, reason_low), ('low', 'user_description_low'))
        c_low = harness.resolve_execution_profile(engines, 'codex:default', tier_low, '省额度用低档快速看一下', kind='review')
        self.assertEqual((c_low['model'], c_low['reasoning_effort'], c_low['profile_source']),
                         ('model-fast', 'low', 'specialty_fast_lightweight'))
        a_low = harness.resolve_execution_profile(engines, 'agy:default', tier_low, '省额度用低档快速看一下', kind='review')
        self.assertEqual((a_low['model'], a_low['profile_source']), ('omini-flash-low', 'specialty_fast_lightweight'))
        a_low_no_lite = harness.resolve_execution_profile(engines, 'agy:default', 'low', '不要轻量模型，用低档看一下', kind='review')
        self.assertEqual(a_low_no_lite['model'], 'omini-prime-low')
        c_low_no_lite = harness.resolve_execution_profile(engines, 'codex:default', 'low', '不要轻量模型，用低档看一下', kind='review')
        self.assertEqual((c_low_no_lite['model'], c_low_no_lite['reasoning_effort']), ('model-workhorse', 'low'))
        a_explicit_3p = harness.resolve_execution_profile(engines, 'agy:othervendor-pro-high', 'high', '指定模型审查', kind='review')
        self.assertEqual(a_explicit_3p['model'], 'othervendor-pro-high')

        # 5. 新一代轻量高速标识 (hydrogen) 与混动路由器共存：深思与主力锁定 flagship (carbon)，low 挡精准命中同代 hydrogen（不误用旧代 flash），显式/模糊调用均可用
        g4_engines = backends()
        g4_engines['agy']['models'] = [
            {'slug': 'omega-hydrogen-xhigh', 'display_name': 'Omega 4 Hydrogen XHigh'},
            {'slug': 'omega-carbon-medium', 'display_name': 'Omega 4 Carbon (Medium)'},
            {'slug': 'omega-carbon-high', 'display_name': 'Omega 4 Carbon (High)'},
            {'slug': 'omega-carbon-low', 'display_name': 'Omega 4 Carbon (Low)'},
            {'slug': 'omega-argon-high', 'display_name': 'Omega 4 Argon (High)'},
            {'slug': 'omega-hydrogen-low', 'display_name': 'Omega 4 Hydrogen (Low)'},
            {'slug': 'omega-pro-orchestrator', 'display_name': 'Orchestrator (Carbon)'},
            {'slug': 'omega-pro-orchestrator-argon', 'display_name': 'Orchestrator (Argon)'},
            {'slug': 'omega-3.8-flash-low', 'display_name': 'Omega 3.8 Flash (Low)'},
        ]
        g4_work = harness.resolve_execution_profile(g4_engines, 'agy:default', 'high', '常规功能实现', kind='code')
        g4_deep = harness.resolve_execution_profile(g4_engines, 'agy:default', 'max', '推导熵守恒格式', kind='math')
        g4_fast = harness.resolve_execution_profile(g4_engines, 'agy:default', 'low', '省额度快速扫查', kind='review')
        g4_no_light = harness.resolve_execution_profile(g4_engines, 'agy:default', 'low', '不要 hydrogen 低档看下', kind='review')
        self.assertEqual((g4_work['model'], g4_work['profile_source']), ('omega-carbon-high', 'frontier_catalog'))
        self.assertEqual((g4_deep['model'], g4_deep['profile_source']), ('omega-carbon-high', 'specialty_deep_reasoning'))
        self.assertEqual((g4_fast['model'], g4_fast['profile_source']), ('omega-hydrogen-low', 'specialty_fast_lightweight'))
        self.assertEqual(g4_no_light['model'], 'omega-carbon-low')
        self.assertEqual(harness.resolve_execution_profile(g4_engines, 'agy:Orchestrator (Carbon)', 'high')['model'], 'omega-pro-orchestrator')
        self.assertEqual(harness.resolve_execution_profile(g4_engines, 'agy:orchestrator-argon', 'high')['model'], 'omega-pro-orchestrator-argon')
        self.assertEqual(harness.resolve_execution_profile(g4_engines, 'agy:carbon', 'medium')['model'], 'omega-carbon-medium')
        self.assertEqual(harness.resolve_execution_profile(engines, 'codex:fast', 'low')['model'], 'model-fast')

        # 自然语言显式/模糊点名模型 + CFD 物理术语/否定词零误触 + Codex 模糊预算 + agy:<alias> 目录发现
        nl_argon = harness.resolve_execution_profile(g4_engines, 'agy:default', 'high', '请调用 Argon 模型复核推导', kind='review')
        self.assertEqual((nl_argon['model'], nl_argon['profile_source']), ('omega-argon-high', 'prompt_fuzzy_model'))
        nl_carbon_med = harness.resolve_execution_profile(g4_engines, 'agy:default', 'high', '请调用 Carbon (Medium) 模型复核', kind='review')
        self.assertEqual((nl_carbon_med['model'], nl_carbon_med['profile_source']), ('omega-carbon-medium', 'prompt_fuzzy_model'))
        nl_orch = harness.resolve_execution_profile(g4_engines, 'agy:default', 'high', '请使用混动 Orchestrator 路由检查', kind='review')
        self.assertEqual((nl_orch['model'], nl_orch['profile_source']), ('omega-pro-orchestrator', 'prompt_fuzzy_model'))
        nl_codex = harness.resolve_execution_profile(engines, 'codex:default', 'max', '请调用 demanding 模型推导', kind='math')
        self.assertEqual((nl_codex['model'], nl_codex['profile_source']), ('model-demanding', 'prompt_fuzzy_model'))

        cfd_safe = harness.resolve_execution_profile(
            g4_engines, 'agy:default', 'high',
            '计算 hydrogen detonation 爆轰波与 carbon 热流，采用 k-omega 模型与 Navier-Stokes 模型离散；采用 hydrogen 模型计算爆轰', kind='code',
        )
        self.assertEqual((cfd_safe['model'], cfd_safe['profile_source']), ('omega-carbon-high', 'frontier_catalog'))
        self.assertIsNone(harness._match_effort_level(['ultra'], 'high', ['ultra']))
        neg_prompt = harness.resolve_execution_profile(g4_engines, 'agy:default', 'high', '不要用 Argon 模型，常规复核', kind='review')
        self.assertEqual((neg_prompt['model'], neg_prompt['profile_source']), ('omega-carbon-high', 'frontier_catalog'))

        self.assertEqual(harness._budget_for_spec(engines, 'codex:demanding'), int(272000 * 0.90))

        fake_agy_bin = self.root / ('agy.exe' if os.name == 'nt' else 'agy')
        fake_agy_bin.write_text('stub', encoding='utf-8')
        fresh_backends = backends()
        fresh_backends['agy']['binary'] = str(fake_agy_bin)
        with patch.object(harness, 'probe_backends', return_value=g4_engines) as mock_probe:
            harness._ensure_live_agy_catalog(self.args('probe'), fresh_backends, ('agy:carbon',))
        mock_probe.assert_called_once()
        self.assertEqual(fresh_backends['agy']['models'], g4_engines['agy']['models'])

        # 单文件表面拼写检查自动降至 low
        tier_typo, reason_typo = harness.select_task_tier('auto', 'review', '检查注释错别字与 typo', 1, 300)
        self.assertEqual((tier_typo, reason_typo), ('low', 'complexity_trivial_surface_task'))

        # 验证 CLI 参数传递：codex 附带 model_reasoning_effort="max"（非 ultra），agy 使用挡位后缀 slug 且不重复附加 --effort
        def check_codex_cmd(command, cwd, input_text, timeout_sec, **kwargs):
            self.assertIn('-m', command)
            self.assertEqual(command[command.index('-m') + 1], 'model-demanding')
            self.assertIn('model_reasoning_effort="max"', command)
            self.assertIn('approval_policy="never"', command)
            Path(command[command.index('-o') + 1]).write_text(json.dumps(TASK), encoding='utf-8')
            return subprocess.CompletedProcess(command, 0, '', '')

        def check_agy_cmd(command, cwd, input_text, timeout_sec, **kwargs):
            self.assertIn('--model', command)
            self.assertEqual(command[command.index('--model') + 1], 'omini-deep-xhigh')
            self.assertNotIn('--effort', command)
            stream = json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': TASK}})
            return subprocess.CompletedProcess(command, 0, stream, '')

        with patch.object(harness, 'run_process', side_effect=check_codex_cmd):
            out = harness.invoke_engine(c_math['spec'], self.root, '推导', engines, harness.TASK_JSON_SCHEMA, True, 30, 'readonly-collaborator', None, c_math['cli_effort'])
            self.assertEqual(out, TASK)
        with patch.object(harness, 'run_process', side_effect=check_agy_cmd):
            out = harness.invoke_engine(a_math['spec'], self.root, '推导', engines, harness.TASK_JSON_SCHEMA, True, 30, 'readonly-collaborator', None, a_math['cli_effort'])
            self.assertEqual(out, TASK)

    def test_gate_round2_auto_escalates_thinking_tier_to_max(self):
        target_py = self.root / 'helper.py'
        target_py.write_text('x = 0\n', encoding='utf-8')
        engines = backends()
        engines['codex']['models'] = [
            {'slug': 'model-workhorse', 'priority': 1, 'visibility': 'list',
             'supported_efforts': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
             'delegating_efforts': ['ultra']},
        ]
        seen_efforts = []

        def fake_worker(spec, root, prompt, _b, timeout_sec=600, effort=None, retry_quota=False):
            seen_efforts.append((spec, effort))
            target_py.write_text(f'x = {len(seen_efforts)}\n', encoding='utf-8')
            return json.dumps(TASK, ensure_ascii=False)

        verify_cmd = f'"{sys.executable}" -B -c "import pathlib,sys; sys.exit(0 if \'x = 2\' in pathlib.Path(\'helper.py\').read_text() else 1)"'
        args = self.args('gate', '--caller', 'antigravity', '--target', str(self.root),
                         '--task', '整理返回值赋值逻辑', '--write-scope', '*.py', '--verify-cmd', verify_cmd, '--max-rounds', '3')
        with patch.object(harness, '_args_backends', return_value=engines), patch.object(harness, 'invoke_edit_worker', side_effect=fake_worker):
            code, report = self.capture(harness.cmd_gate, args)
        self.assertEqual(code, 0)
        self.assertEqual(seen_efforts, [('codex:model-workhorse', 'high'), ('codex:model-workhorse', 'max')])
        self.assertEqual(report['rounds_used'], 2)
        self.assertEqual(report['selected_tier'], 'max')
        self.assertEqual(report['tier_reason'], 'gate_retry_escalated_max')

    def test_wsl_unauthenticated_linux_agy_falls_back_to_windows_agy_exe(self):
        linux_agy = self.root / 'linux-home' / '.gemini' / 'bin' / 'agy'
        linux_agy.parent.mkdir(parents=True)
        linux_agy.write_bytes(b'ELF')
        win_home = self.root / 'win-home'
        win_agy = win_home / '.gemini' / 'bin' / 'agy.exe'
        win_agy.parent.mkdir(parents=True)
        win_agy.write_bytes(b'MZ')
        win_temp = self.root / 'win-temp'
        win_temp.mkdir()

        engines = backends()
        engines['agy'].update(binary=str(linux_agy), windows_interop=False,
                              config_home=str(linux_agy.parents[1]), authentication_status='not_checked')

        def fake_which(cmd):
            return {'powershell.exe': '/mnt/c/ps.exe', 'taskkill.exe': '/mnt/c/tk.exe', 'wslpath': '/usr/bin/wslpath'}.get(cmd)

        def fake_wsl_path(path, windows=True):
            p = Path(path)
            if not windows:
                return str(win_temp)
            return 'D:\\work\\' + p.name

        calls = []

        def fake_run(command, cwd, input_text=None, timeout_sec=600, **kwargs):
            calls.append(command)
            if command == [str(linux_agy), 'models']:
                return subprocess.CompletedProcess(command, 1, '', 'Error: Please sign in to view available models.')
            self.assertEqual(command[0], 'powershell.exe')
            stream = json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': TASK}})
            return subprocess.CompletedProcess(command, 0, stream, '')

        with patch.dict(os.environ, {}, clear=True), patch.object(harness, '_is_posix', return_value=True), \
             patch.object(harness.shutil, 'which', side_effect=fake_which), \
             patch.object(harness, '_find_windows_agy_in_wsl', return_value=str(win_agy)), \
             patch.object(harness, '_windows_env', return_value='C:\\Temp'), \
             patch.object(harness, '_wsl_path', side_effect=fake_wsl_path), \
             patch.object(harness, 'run_process', side_effect=fake_run):
            out = harness.invoke_engine('agy:suite-prime-high', self.root, '审查', engines, harness.TASK_JSON_SCHEMA)
        self.assertEqual(out, TASK)
        self.assertEqual(len(calls), 2)
        self.assertTrue(engines['agy']['windows_interop'])
        self.assertEqual(engines['agy']['binary'], str(win_agy))

    def test_quota_exhaustion_cooldown_and_preflight_gate(self):
        import datetime as _dt
        cfg_dir = self.root / 'codex-cfg'
        cfg_dir.mkdir()
        ws_dir = self.root / 'workspace'
        ws_dir.mkdir()
        target_file = ws_dir / 'solver.py'
        target_file.write_text('u = 1\n', encoding='utf-8')
        engines = backends()
        engines['codex']['config_home'] = str(cfg_dir)

        future_dt = (_dt.datetime.now() + _dt.timedelta(days=3)).replace(second=0, microsecond=0)
        reset_str = f"{future_dt.strftime('%b')} {future_dt.day}th, {future_dt.strftime('%Y %I:%M %p')}"
        quota_stderr = f'ERROR: You\u2019ve hit your usage limit. Purchase more credits or try again at {reset_str}.\n'
        fail_proc = subprocess.CompletedProcess(['codex'], 1, '', quota_stderr)
        args_run = self.args('task', '--caller', 'antigravity', '--kind', 'math', '--target', str(target_file), '--task', '核对守恒律')

        # 1. 首次触发限额：返回退出码 5 (quota_exhausted)，写入本地冷却缓存并提供用户二选一选项
        with patch.object(harness, '_args_backends', return_value=engines), patch.object(harness, 'run_process', return_value=fail_proc):
            code1, report1 = self.capture(harness.cmd_task, args_run)
        self.assertEqual(code1, 5)
        self.assertEqual(report1['status'], 'quota_exhausted')
        self.assertFalse(report1['preflight_blocked'])
        self.assertTrue(report1['user_confirmation_required'])
        self.assertEqual(report1['quota_cooldown']['reset_hint'], reset_str)
        self.assertEqual(report1['quota_cooldown']['cooldown_source'], 'parsed_reset_time')
        quota_file = cfg_dir / 'cache' / 'cae_quota_state.json'
        self.assertTrue(quota_file.is_file())

        # 2. 跨进程新会话（清空内存态）再次发起时：事前直接拦截，绝不启动后端子进程
        harness._SESSION_QUOTA_BLOCKED.clear()
        with patch.object(harness, '_args_backends', return_value=engines), patch.object(harness, 'run_process') as not_called:
            code2, report2 = self.capture(harness.cmd_task, args_run)
        not_called.assert_not_called()
        self.assertEqual(code2, 5)
        self.assertEqual(report2['status'], 'quota_exhausted')
        self.assertTrue(report2['preflight_blocked'])
        self.assertEqual(len(report2['user_choices']), 2)

        # 3. dry-run 预演仍正常返回 0，并标注 exhausted_cached 状态
        args_dry = self.args('task', '--caller', 'antigravity', '--kind', 'math', '--target', str(target_file), '--task', '核对守恒律', '--dry-run')
        with patch.object(harness, '_args_backends', return_value=engines):
            code_dry, report_dry = self.capture(harness.cmd_task, args_dry)
        self.assertEqual(code_dry, 0)
        self.assertEqual(report_dry['quota_status'], 'exhausted_cached')

        # 4. gate 多轮循环遇到 quota_exhausted 在第 1 轮立即熔断退出
        def quota_worker(_prompt, _round):
            raise harness.HarnessError('quota', 'quota_exhausted', preflight_blocked=False, user_confirmation_required=True, user_choices=['1', '2'])
        gate_res = harness.run_physics_gate_loop(ws_dir, '改代码', 'verify', quota_worker, max_rounds=3)
        self.assertEqual(gate_res['status'], 'quota_exhausted')
        self.assertEqual(gate_res['rounds_used'], 1)
        self.assertTrue(gate_res['user_confirmation_required'])

        # 5. 用户充值确认后传入 --retry-quota：放行执行并在成功后自动清除冷却缓存文件
        def ok_runner(command, *_a, **_kw):
            Path(command[command.index('-o') + 1]).write_text(json.dumps(TASK), encoding='utf-8')
            return subprocess.CompletedProcess(command, 0, '', '')
        args_retry = self.args('task', '--caller', 'antigravity', '--kind', 'math', '--target', str(target_file), '--task', '核对守恒律', '--retry-quota')
        with patch.object(harness, '_args_backends', return_value=engines), patch.object(harness, 'run_process', side_effect=ok_runner):
            code3, report3 = self.capture(harness.cmd_task, args_retry)
        self.assertEqual(code3, 0)
        self.assertEqual(report3['status'], 'ok')
        self.assertFalse(quota_file.exists())
        self.assertEqual(report3['quota_status'], 'unknown_until_called')

        # 6. 过期冷却文件自动清理放行
        quota_file.parent.mkdir(parents=True, exist_ok=True)
        quota_file.write_text(json.dumps({'backend': 'codex', 'exhausted': True, 'cooldown_until_epoch': 1.0}), encoding='utf-8')
        self.assertIsNone(harness._load_active_quota_cooldown('codex', engines['codex']))
        self.assertFalse(quota_file.exists())

    def test_unknown_and_nonpositive_arguments_fail(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.args('task', '--kind', 'math', '--target', '.', '--task', 'x', '--timeout', '0')
        with self.assertRaises(harness.HarnessError):
            harness._scope_patterns(['../outside'])
        audit_p = harness._build_audit_prompt({'files': ['a.py']}, '守恒')
        self.assertIn('git worktree add', audit_p)
        captured_prompts = []
        harness.run_physics_gate_loop(self.root, '修通量', 'exit 0', lambda p, _n: (captured_prompts.append(p), '{}')[1],
                                      verify_fn=lambda _c, _d: (0, 'ok'), max_rounds=1)
        self.assertIn('git worktree add', captured_prompts[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
