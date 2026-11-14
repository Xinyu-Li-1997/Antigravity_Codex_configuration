"""双向编排的离线回归，所有模型调用均使用替身。"""
import contextlib
import base64
import datetime
import importlib.util
import io
import json
import os
import signal
import stat
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
for installed_key in ('USER_GUARD_INSTALLED_CODEX_DIR', 'USER_GUARD_INSTALLED_DIR'):
    installed_root = os.environ.get(installed_key)
    installed_script = Path(installed_root) / 'skills' / 'cae-harness' / 'scripts' / 'cae_harness.py' if installed_root else None
    if installed_script is not None and installed_script.is_file():
        SCRIPT = installed_script
        break
spec = importlib.util.spec_from_file_location('cae_harness_test', SCRIPT)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def backends(budget=120000):
    catalogs = {'codex': [{'slug': 'fixture-code', 'provider': 'openai', 'supported_efforts': ['high', 'max']}],
                'agy': [{'slug': 'gemini-7-pro-high'}]}
    fetched_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {name: {'available': True, 'binary': name + ('.exe' if os.name == 'nt' else ''),
                   'safe_input_budget_tokens': budget, 'requested_input_budget_tokens': budget,
                   'model': None, 'windows_interop': False, 'models': catalogs[name],
                   'authentication_status': 'authenticated',
                   'model_catalog': {'source': name + '_client_cache', 'model_count': len(catalogs[name]),
                                     'fetched_at': fetched_at, 'freshness': 'within_assessment_window'}}
            for name in ('codex', 'agy')}


AUDIT = {'summary': '已核对', 'findings': [], 'read_files': ['a.py'], 'unread_files': []}
TASK = {'summary': '已推导', 'content': '证明正文', 'evidence': ['输入定义'], 'unverified': []}


class HarnessTests(unittest.TestCase):
    def setUp(self):
        harness._CANCELLED.clear()
        harness._SESSION_QUOTA_BLOCKED.clear()
        self.temp = tempfile.TemporaryDirectory(prefix='cae-test-')
        self.root = Path(self.temp.name).resolve()

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

    def test_top_level_toml_handles_quoted_keys_numbers_and_comments(self):
        path = self.root / 'config.toml'
        path.write_text(
            'arbitrary = [\n  "value", # ]\n]\n'
            'text = """\nmodel_context_window = 17\n[ignored]\n"""\n'
            '"model" = "gpt-9-fixture#literal" # comment\n'
            "'model_context_window' = 500_000 # not 500\n"
            '"model_auto_compact_token_limit" = 0x6_DD_D0 # 450000\n'
            'model_reasoning_effort = "h\\u0069gh"\n'
            '[profiles.fixture]\nmodel_context_window = 7\n',
            encoding='utf-8-sig')
        expected = {'model': 'gpt-9-fixture#literal', 'model_context_window': 500000,
                    'model_auto_compact_token_limit': 450000, 'model_reasoning_effort': 'high'}
        self.assertEqual(harness._top_level_toml(path), expected)
        path.write_text("model_context_window = 500_000x\n'other' = 300\n", encoding='utf-8')
        self.assertEqual(harness._top_level_toml(path), {})

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
                {'slug': 'code-model', 'provider': 'openai', 'priority': 1, 'context_window': 500000, 'supported_efforts': ['high', 'xhigh', 'ultra']},
                {'slug': 'math-model', 'provider': 'openai', 'priority': 2, 'description': 'Frontier intelligence for the most demanding work.',
                 'supported_efforts': ['high', 'max', 'ultra']},
                {'slug': 'fast-model', 'provider': 'openai', 'priority': 3, 'description': 'Lightweight model', 'supported_efforts': ['low']},
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
        self.assertEqual((surface['model'], surface['selected_tier'], surface['reasoning_effort']), ('code-model', 'max', 'xhigh'))
        eco_review = report['recommendations']['codex']['economy_review']
        self.assertEqual((eco_review['model'], eco_review['reasoning_effort']), ('code-model', 'high'))
        eco_surface = report['recommendations']['codex']['economy_surface_review']
        self.assertEqual((eco_surface['model'], eco_surface['reasoning_effort']), ('fast-model', 'low'))
        matrix = {row['model']: row for row in codex['capability_matrix']}
        self.assertEqual(matrix['code-model']['capacity']['model_default_window_tokens'], 500000)
        self.assertIsNone(matrix['code-model']['capacity']['model_declared_max_window_tokens'])
        self.assertEqual(matrix['math-model']['declared_efforts'], ['high', 'max', 'ultra'])
        self.assertEqual(matrix['math-model']['catalog_freshness'], 'stale')
        self.assertEqual({row['category'] for row in agy['capability_matrix']}, {'unknown'})
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

    def test_codex_stdio_lists_models_after_initialization_without_turn(self):
        fake = self.root / 'model_list_fixture.py'
        fake.write_text("""
import json, sys

def read():
    return json.loads(sys.stdin.readline())

def reply(request, result):
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)

initialize = read()
assert initialize['method'] == 'initialize'
assert initialize['params']['capabilities']['experimentalApi'] is True
reply(initialize, {})
assert read() == {'method': 'initialized'}
request = read()
assert request['method'] == 'model/list'
assert request['params'] == {'includeHidden': False, 'limit': 100}
reply(request, {'data': [{'model': 'gpt-9-alpha'}], 'nextCursor': 'page-2'})
request = read()
assert request['method'] == 'model/list'
assert request['params']['cursor'] == 'page-2'
reply(request, {'data': [{'model': 'gpt-9-beta'}], 'nextCursor': None})
assert sys.stdin.readline() == ''
""", encoding='utf-8')
        listing = harness._query_codex_stdio([sys.executable, '-B', str(fake)], self.root, None, None, 10)
        self.assertEqual(set(listing), {'gpt-9-alpha', 'gpt-9-beta'})

    def test_codex_refresh_joins_current_catalog_and_exact_cache_slug(self):
        engines = backends()
        info = engines['codex']
        info.update(config_home=str(self.root), models=[
            {'slug': 'gpt-9-obsolete', 'context_window': 900000, 'max_context_window': 950000},
        ], model_catalog={'freshness': 'stale'})
        cache = self.root / 'models_cache.json'

        def refresh(actual_info):
            self.assertEqual(actual_info['config_home'], str(self.root))
            cache.write_text(json.dumps({'fetched_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'models': [
                {'slug': 'gpt-9-current', 'context_window': 300000, 'max_context_window': 500000},
                {'slug': 'gpt-9-obsolete', 'context_window': 900000, 'max_context_window': 950000},
            ]}), encoding='utf-8')
            return {'gpt-9-current': {'model': 'gpt-9-current', 'supportedReasoningEfforts': [{'effort': 'xhigh'}]},
                    'gpt-9-unmatched': {'model': 'gpt-9-unmatched', 'max_context_window': 999999}}

        with patch.object(harness, '_query_codex_listing', side_effect=refresh) as query:
            harness._prepare_execution(engines, ('codex:default',), harness._QuotaBatch())
            harness._prepare_execution(engines, ('codex:default',), harness._QuotaBatch())
        query.assert_called_once()
        chosen = harness.resolve_execution_profile(engines, 'codex:default', 'max', estimated_tokens=170000)
        self.assertEqual(chosen['model'], 'gpt-9-current')
        self.assertEqual(chosen['capacity']['cli_context_window_tokens'], 500000)
        self.assertEqual(chosen['declared_effort'], 'xhigh')
        self.assertEqual(chosen['model_listing']['source'], 'codex_app_server_model_list')
        self.assertIsNone(harness.resolve_execution_profile(
            engines, 'codex:gpt-9-unmatched')['capacity']['cli_context_window_tokens'])
        self.assertIsNone(harness.resolve_execution_profile(
            engines, 'codex:gpt-9-obsolete')['capacity']['cli_context_window_tokens'])

    def test_codex_stale_after_refresh_blocks_exec_and_quota_precedes_listing(self):
        engines = backends()
        info = engines['codex']
        config = self.root / 'config'
        config.mkdir()
        info.update(config_home=str(config), model_catalog={'freshness': 'future_timestamp'})
        cache = config / 'models_cache.json'
        cache.write_text(json.dumps({'fetched_at': '2020-01-01T00:00:00Z', 'models': info['models']}), encoding='utf-8')
        args = self.args('task', '--target', str(self.root), '--kind', 'review', '--task', '核对', '--engine', 'codex:default')
        models = {'gpt-9-current': {'model': 'gpt-9-current'}}
        with patch.object(harness, '_args_backends', return_value=engines), \
             patch.object(harness, '_query_codex_listing', return_value=models) as query, \
             patch.object(harness, 'run_process') as worker:
            code, report = self.capture(harness.cmd_task, args)
            self.assertEqual(code, 1)
            self.assertEqual(report['model_catalog']['freshness'], 'stale')
            self.assertEqual(report['requested_engine'], 'codex:default')
            query.assert_called_once_with(info)
            query.reset_mock()
            harness._record_quota_exhausted('codex', info, 'hit your usage limit')
            code, report = self.capture(harness.cmd_task, args)
            self.assertEqual(code, 5)
            self.assertTrue(report['preflight_blocked'])
            query.assert_not_called()
            worker.assert_not_called()

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
            {'slug': 'only-delegating', 'provider': 'openai', 'priority': 1, 'supported_efforts': ['ultra']},
            {'slug': 'eligible', 'provider': 'openai', 'priority': 2, 'supported_efforts': ['high']},
        ])
        engines['agy']['models'] = [{'slug': 'gemini-6-pro-ultra'}, {'slug': 'gemini-5-pro-high'}]
        self.assertEqual(harness.resolve_execution_profile(engines, 'codex:default', 'max')['model'], 'eligible')
        self.assertEqual(harness.resolve_execution_profile(engines, 'agy:default', 'max')['model'], 'gemini-5-pro-high')
        self.assertEqual(harness.resolve_execution_profile(engines, 'agy:default', 'ultra')['model'], 'gemini-6-pro-ultra')
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
            self.assertEqual(harness.select_task_tier(text='不要使用 ultra，检查结果')[0], 'max')
        with patch.dict(os.environ, {'CAE_DEFAULT_TIER': 'high'}, clear=True):
            self.assertEqual(harness.select_task_tier(text='检查结果'), ('high', 'env_default_tier'))

    def test_explicit_model_has_own_budget_cap(self):
        engines = backends()
        engines['codex'].update(configured_window_tokens=900000, requested_input_budget_tokens=None, models=[
            {'slug': 'larger', 'context_window': 500000, 'max_context_window': 872000},
            {'slug': 'small', 'context_window': 100000, 'max_context_window': 128000},
            {'slug': 'default-only', 'context_window': 200000}])
        for model, expected_window in (('larger', 872000), ('small', 128000), ('default-only', 200000)):
            with self.subTest(model=model):
                profile = harness.resolve_execution_profile(engines, f'codex:{model}')
                self.assertEqual(profile['capacity']['cli_context_window_tokens'], expected_window)
                self.assertEqual(profile['capacity']['input_budget_tokens'], expected_window * 9 // 10)
        unknown = harness.resolve_execution_profile(engines, 'codex:unlisted')
        self.assertIsNone(unknown['capacity']['cli_context_window_tokens'])
        self.assertIsNotNone(unknown['execution_blocked_reason'])
        engines['codex'].pop('configured_window_tokens')
        unknown = harness.resolve_execution_profile(engines, 'codex:unlisted')
        self.assertIsNone(unknown['execution_blocked_reason'])
        self.assertEqual(unknown['capacity']['input_budget_tokens'], 120000)

    def test_catalog_effective_window_caps_budget_without_changing_cli_window(self):
        engines = backends()
        info = engines['codex']
        info.update(requested_input_budget_tokens=850000, models=[
            {'slug': 'gpt-9-fixture', 'context_window': 500000, 'max_context_window': 872000,
             'effective_context_window_percent': 95},
        ])

        def capacity():
            return harness.resolve_execution_profile(engines, 'codex:gpt-9-fixture')['capacity']

        explicit = capacity()
        self.assertEqual(explicit['cli_context_window_tokens'], 872000)
        self.assertEqual(explicit['catalog_effective_window_tokens'], 828400)
        self.assertEqual(explicit['effective_window_source'], 'catalog_percent_of_selected_window')
        self.assertEqual(explicit['input_budget_tokens'], 828400)
        self.assertFalse(explicit['capacity_verified'])

        info['requested_input_budget_tokens'] = None
        default = capacity()
        self.assertEqual(default['planning_cap_tokens'], 828400)
        self.assertEqual(default['input_budget_tokens'], 745560)
        self.assertEqual(default['policy_reserve_tokens'], 82840)

        info['effective_compaction_tokens'] = 500000
        compact = capacity()
        self.assertEqual(compact['catalog_effective_window_tokens'], 828400)
        self.assertEqual(compact['planning_cap_tokens'], 500000)
        self.assertEqual(compact['input_budget_tokens'], 450000)

        info.pop('effective_compaction_tokens')
        info['models'][0]['effective_context_window_percent'] = 101
        unknown = capacity()
        self.assertIsNone(unknown['catalog_effective_window_tokens'])
        self.assertEqual(unknown['planning_cap_tokens'], 872000)
        self.assertEqual(unknown['input_budget_tokens'], 784800)

    def test_plan_skips_oversize_with_explicit_coverage(self):
        records = [{'rel_path': 'small.lean', 'tokens': 20}, {'rel_path': 'large.f90', 'tokens': 2000}]
        plan = harness.select_audit_plan(2020, records, backends(1000), caller='codex', shard_tokens=100)
        self.assertEqual(plan['left_engine'], 'agy:default')
        self.assertEqual(plan['right_engine'], 'codex:default')
        self.assertEqual(len(plan['skipped_files']), 2)
        self.assertEqual(plan['left_units'][0]['files'], ['small.lean'])
        self.assertIn('跨分片', plan['coverage_notice'])
        with self.assertRaises(harness.HarnessError):
            harness.select_audit_plan(2020, records, backends(1000), strategy='single-pass', caller='codex')

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

    def test_gate_snapshot_failures_stop_at_the_unverified_stage(self):
        phases = (
            ('preflight', [PermissionError('initial denied')], 0, 0),
            ('post_worker', [{}, PermissionError('worker denied')], 1, 0),
            ('post_verify', [{}, {}, PermissionError('verifier denied')], 1, 1),
            ('final', [{}, {}, {}, PermissionError('final denied')], 1, 1),
            ('pre_worker', [{}, {}, {}, PermissionError('retry denied')], 1, 1),
        )
        for phase, snapshots, worker_calls, verifier_calls in phases:
            with self.subTest(phase=phase):
                worker = Mock(return_value='worker receipt')
                verifier = Mock(return_value=(1 if phase == 'pre_worker' else 0, 'verifier receipt'))
                with patch.object(harness, 'snapshot_files', side_effect=snapshots) as snapshot:
                    result = harness.run_physics_gate_loop(self.root, '改代码', 'verify', worker, verifier, max_rounds=3)
                self.assertEqual(snapshot.call_count, len(snapshots))
                self.assertEqual(worker.call_count, worker_calls)
                self.assertEqual(verifier.call_count, verifier_calls)
                self.assertEqual(result['rounds_used'], worker_calls)
                self.assertEqual(result['status'], 'failed')
                self.assertFalse(result['gate_passed'])
                self.assertFalse(result['scope_verified'])
                self.assertEqual(result['scope_check'], 'unverified')
                self.assertEqual(result['scope_error_phase'], phase)
                self.assertIsNone(result['changed_files'])
                if worker_calls:
                    self.assertEqual(result['history'][0]['worker_summary'], 'worker receipt')
                if verifier_calls:
                    self.assertEqual(result['history'][0]['verify_output_tail'], 'verifier receipt')

    def test_gate_cli_retains_actual_worker_profile_on_post_worker_snapshot_failure(self):
        engines = backends()
        denied = self.root / 'balance.py'
        open_file = Path.open

        def unreadable(path, *args, **kwargs):
            if path == denied and args == ('rb',):
                raise PermissionError(13, 'denied after worker', str(path))
            return open_file(path, *args, **kwargs)

        def run(command, cwd, input_text=None, timeout_sec=600, **kwargs):
            self.assertFalse(kwargs.get('shell'))
            denied.write_text('balance = 0\n', encoding='utf-8')
            Path(command[command.index('-o') + 1]).write_text(json.dumps(TASK), encoding='utf-8')
            return subprocess.CompletedProcess(command, 0, '', '')

        args = self.args('task', '--engine', 'codex:default', '--target', str(self.root), '--kind', 'code',
                         '--task', '写出平衡量', '--verify-cmd', 'verify', '--write-scope', 'balance.py')
        with patch.object(harness, '_args_backends', return_value=engines), \
             patch.object(harness, 'run_process', side_effect=run) as runner, \
             patch.object(Path, 'open', unreadable):
            code, report = self.capture(harness.cmd_task, args)
        self.assertEqual(code, 1)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(report['scope_error_phase'], 'post_worker')
        self.assertEqual(report['scope_error_path'], 'balance.py')
        self.assertFalse(report['scope_verified'])
        self.assertEqual(report['rounds_used'], 1)
        entry = report['history'][0]
        self.assertTrue(entry['invocation']['invocation_started'])
        self.assertEqual(entry['invocation']['returncode'], 0)
        self.assertEqual(entry['execution_profile']['model'], 'fixture-code')
        self.assertEqual(json.loads(entry['worker_summary']), TASK)
        self.assertIsNone(entry['verify_exit_code'])

    def test_snapshot_reports_unreadable_root_and_subdirectory(self):
        nested = self.root / 'unreadable'
        nested.mkdir()
        scandir = os.scandir
        for blocked, rel in ((self.root, '.'), (nested, 'unreadable')):
            with self.subTest(blocked=blocked):
                def deny(path):
                    if Path(path) == blocked:
                        raise PermissionError(13, 'scandir denied', str(path))
                    return scandir(path)

                with patch.object(harness.os, 'scandir', side_effect=deny), \
                     self.assertRaises(harness.HarnessError) as error:
                    harness.snapshot_files(self.root)
                self.assertEqual(error.exception.details['snapshot_path'], rel)

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
        def worker(engine, *args, **kwargs):
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
            {'slug': 'large', 'provider': 'openai', 'context_window': 500000, 'max_context_window': 872000},
            {'slug': 'small', 'provider': 'openai', 'context_window': 100000, 'max_context_window': 100000}]}), encoding='utf-8')
        with patch.object(harness, '_find_binary', return_value='fake'), patch.object(harness, '_codex_home', return_value=(self.root, False)):
            engines = harness.probe_backends()
        self.assertEqual(engines['codex']['safe_input_budget_tokens'], 784800)
        records = [{'rel_path': 'solver.f90', 'tokens': 193000}]
        plan = harness.select_audit_plan(193000, records, engines, dual_blind=False, caller='antigravity')
        self.assertEqual(plan['mode'], 'single-pass')
        self.assertEqual(harness._budget_for_spec(engines, 'codex:small'), 90000)

    def test_audit_plans_and_calls_the_same_model_and_window(self):
        path = self.root / 'solver.f90'
        path.write_bytes(b'a' * 579000)  # 193k by the planning estimate
        (self.root / 'more.f90').write_bytes(b'b' * 822000)
        engines = backends()
        engines['codex'].update(model='interactive', configured_window_tokens=900000,
                                effective_compaction_tokens=450000, requested_input_budget_tokens=None,
                                model_catalog={**engines['codex']['model_catalog'], 'model_count': 2},
                                models=[
                                    {'slug': 'interactive', 'context_window': 100000, 'max_context_window': 110000},
                                    {'slug': 'gpt-9-code', 'context_window': 272000, 'max_context_window': 500000,
                                     'supported_efforts': ['high', 'max']},
                                ])
        commands = []

        def runner(command, cwd, input_text, timeout_sec, **kwargs):
            self.assertIn('model_context_window=500000', command)
            self.assertIn('model_reasoning_effort="max"', command)
            self.assertEqual(command[command.index('-m') + 1], 'gpt-9-code')
            commands.append(command)
            name = 'solver.f90' if '- solver.f90\n' in input_text else 'more.f90'
            output = {**AUDIT, 'read_files': [name]}
            Path(command[command.index('-o') + 1]).write_text(json.dumps(output), encoding='utf-8')
            return subprocess.CompletedProcess(command, 0, '', '')

        args = self.args('audit', '--target', str(self.root), '--single', '--left', 'codex:default', '--focus', '检查格式转换')
        with patch.object(harness, '_args_backends', return_value=engines), patch.object(harness, 'run_process', side_effect=runner):
            code, report = self.capture(harness.cmd_audit, args)
        self.assertEqual(code, 0)
        self.assertEqual(len(commands), 2)
        self.assertEqual(report['plan']['mode'], 'sharded')
        self.assertEqual(report['plan']['skipped_files'], [])
        left = report['plan']['execution_profiles']['left']
        self.assertEqual(left['model'], 'gpt-9-code')
        self.assertEqual(left['catalog_usage'], 'unverified_client_cache')
        self.assertEqual(left['capacity']['selected_window_tokens'], 500000)
        self.assertEqual(left['capacity']['input_budget_tokens'], 405000)
        self.assertTrue(all(unit['exceeds_soft_shard_target'] for unit in report['plan']['left_units']))
        self.assertTrue(all(unit['estimated_prompt_tokens'] > 0 for unit in report['plan']['left_units']))
        self.assertFalse(report['reading_coverage_verified'])

    def test_audit_focus_consumes_budget_and_out_of_scope_findings_are_rejected(self):
        records = [{'rel_path': 'a.py', 'tokens': 20}]
        args = {'dual_blind': False, 'caller': 'antigravity'}
        self.assertEqual(harness.select_audit_plan(20, records, backends(1000), **args)['mode'], 'single-pass')
        plan = harness.select_audit_plan(20, records, backends(1000), focus='x' * 2400, **args)
        self.assertEqual(plan['left_units'], [])
        self.assertEqual(plan['skipped_files'][0]['reason'], 'over_input_budget')
        (self.root / 'a.py').write_text('x=1', encoding='utf-8')
        result = {**AUDIT, 'findings': [{'file': '../other.py', 'line_start': 1, 'line_end': 1,
                                         'severity': 'high', 'category': 'test', 'description': 'outside', 'evidence': 'outside'}]}
        cli_args = self.args('audit', '--target', str(self.root), '--single', '--left', 'codex:default')
        with patch.object(harness, '_args_backends', return_value=backends()), \
             patch.object(harness, 'invoke_readonly_engine', return_value=result):
            code, report = self.capture(harness.cmd_audit, cli_args)
        self.assertEqual(code, 4)
        self.assertEqual(report['received_count'], 0)
        self.assertEqual(report['single_source_count'], 0)

    def test_file_target_is_retained_in_prompt_and_receipt(self):
        file = self.root / 'specific.lean'
        file.write_text('theorem', encoding='utf-8')
        args = self.args('task', '--kind', 'math', '--target', str(file), '--task', '核对', '--caller', 'codex')
        def worker(engine, root, prompt, *args, **kwargs):
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
        engines['codex']['requested_input_budget_tokens'] = None
        engines['codex']['models'] = harness._model_records({'models': [
            {'provider': 'openai', 'slug': 'model-legacy-fast', 'priority': 0, 'visibility': 'list',
             'context_window': 272000, 'max_context_window': 872000,
             'description': 'Older fast and efficient model.',
             'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}, {'effort': 'high'}, {'effort': 'max'}]},
            {'provider': 'openai', 'slug': 'model-workhorse', 'priority': 1, 'visibility': 'list',
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
            {'provider': 'openai', 'slug': 'model-demanding', 'priority': 2, 'visibility': 'list',
             'context_window': 272000, 'max_context_window': 300000,
             'description': 'Frontier intelligence for the most demanding work.',
             'supported_reasoning_levels': [
                 {'effort': 'low', 'description': 'Fast responses'},
                 {'effort': 'medium', 'description': 'Balanced'},
                 {'effort': 'high', 'description': 'Greater reasoning depth'},
                 {'effort': 'xhigh', 'description': 'Extra high reasoning depth'},
                 {'effort': 'max', 'description': 'Maximum reasoning depth for the hardest problems'},
                 {'effort': 'ultra', 'description': 'Maximum reasoning with automatic task delegation'},
             ]},
            {'provider': 'openai', 'slug': 'model-hidden', 'priority': 3, 'visibility': 'hide',
             'description': 'Fast and affordable agentic coding model.',
             'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}, {'effort': 'high'}]},
            {'provider': 'openai', 'slug': 'model-fast', 'priority': 4, 'visibility': 'list',
             'context_window': 272000, 'max_context_window': 872000,
             'description': 'Fast and affordable model for easier tasks.',
             'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}, {'effort': 'high'}, {'effort': 'max'}]},
        ]})
        # 'gemini' 含 'mini' 子串，不能据此把所有原生模型当成轻量模型
        engines['agy']['models'] = [
            {'slug': 'gemini-7-pro-medium', 'display_name': 'Gemini 7 Pro (Medium)'},
            {'slug': 'gemini-7-pro-high', 'display_name': 'Gemini 7 Pro (High)'},
            {'slug': 'gemini-7-pro-low', 'display_name': 'Gemini 7 Pro (Low)'},
            {'slug': 'gemini-7-deep-high', 'display_name': 'Gemini 7 Deep (High)'},
            {'slug': 'gemini-7-deep-xhigh', 'display_name': 'Gemini 7 Deep (XHigh)'},
            {'slug': 'gemini-7-deep-medium', 'display_name': 'Gemini 7 Deep (Medium)'},
            {'slug': 'gemini-7-deep-low', 'display_name': 'Gemini 7 Deep (Low)'},
            {'slug': 'gemini-7-flash-high', 'display_name': 'Gemini 7 Flash (High)'},
            {'slug': 'gemini-7-flash-low', 'display_name': 'Gemini 7 Flash (Low)'},
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
                         ('gemini-7-deep-xhigh', 'xhigh', None, 'specialty_deep_reasoning'))

        # 输入超过深度推理模型的规划预算时，自动回退至大上下文工程主力模型
        c_math_big = harness.resolve_execution_profile(engines, 'codex:default', 'max', '大输入推导', kind='math', estimated_tokens=400000)
        self.assertEqual((c_math_big['model'], c_math_big['reasoning_effort']), ('model-workhorse', 'max'))

        # 显式指定 ultra 时才放行委派挡位
        tier_ultra, reason_ultra = harness.select_task_tier('ultra', 'math', '推导', 1, 500)
        self.assertEqual((tier_ultra, reason_ultra), ('ultra', 'explicit_tier_flag'))
        c_ultra = harness.resolve_execution_profile(engines, 'codex:default', tier_ultra, '推导', kind='math')
        self.assertEqual((c_ultra['model'], c_ultra['reasoning_effort']), ('model-demanding', 'ultra'))

        # 2. 常规编码与审查：默认（选项 A）选大窗口工程旗舰模型 + max 挡；要求省 token/降挡时（选项 B）切为 high 挡
        tier_code, reason_code = harness.select_task_tier('auto', 'code', '整理命令行参数解析辅助函数', 2, 1500)
        self.assertEqual((tier_code, reason_code), ('max', 'default_flagship_max'))
        c_code = harness.resolve_execution_profile(engines, 'codex:default', tier_code, '整理命令行参数解析辅助函数', kind='code', estimated_tokens=1500)
        self.assertEqual((c_code['model'], c_code['reasoning_effort'], c_code['profile_source']),
                         ('model-workhorse', 'max', 'frontier_catalog'))
        a_code = harness.resolve_execution_profile(engines, 'agy:default', tier_code, '整理命令行参数解析辅助函数', kind='code', estimated_tokens=1500)
        self.assertEqual((a_code['model'], a_code['reasoning_effort']), ('gemini-7-pro-high', 'high'))
        tier_code_eco, reason_code_eco = harness.select_task_tier('auto', 'code', '省点 token 整理命令行参数解析辅助函数', 2, 1500)
        self.assertEqual((tier_code_eco, reason_code_eco), ('high', 'economy_adaptive_high'))
        c_code_eco = harness.resolve_execution_profile(engines, 'codex:default', tier_code_eco, '省点 token 整理命令行参数解析辅助函数', kind='code', estimated_tokens=1500)
        self.assertEqual((c_code_eco['model'], c_code_eco['reasoning_effort'], c_code_eco['profile_source']),
                         ('model-workhorse', 'high', 'frontier_catalog'))

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
        self.assertEqual(a_med['model'], 'gemini-7-pro-medium')
        tier_pol_long, reason_pol_long = harness.select_task_tier('auto', 'polish', '整章学术论文语言润色', 5, 6000)
        self.assertEqual((tier_pol_long, reason_pol_long), ('high', 'complexity_long_polish'))

        # 4. 省额度/表面检查自动匹配当代轻量高速模型（跳过隐藏与 Older 旧代，且绝不误烧异构第三方配额池）
        tier_low, reason_low = harness.select_task_tier('auto', 'review', '省额度用低档快速看一下', 1, 400)
        self.assertEqual((tier_low, reason_low), ('low', 'user_description_low'))
        c_low = harness.resolve_execution_profile(engines, 'codex:default', tier_low, '省额度用低档快速看一下', kind='review')
        self.assertEqual((c_low['model'], c_low['reasoning_effort'], c_low['profile_source']),
                         ('model-fast', 'low', 'specialty_fast_lightweight'))
        a_low = harness.resolve_execution_profile(engines, 'agy:default', tier_low, '省额度用低档快速看一下', kind='review')
        self.assertEqual((a_low['model'], a_low['profile_source']), ('gemini-7-flash-low', 'specialty_fast_lightweight'))
        a_low_no_lite = harness.resolve_execution_profile(engines, 'agy:default', 'low', '不要轻量模型，用低档看一下', kind='review')
        self.assertEqual(a_low_no_lite['model'], 'gemini-7-pro-low')
        c_low_no_lite = harness.resolve_execution_profile(engines, 'codex:default', 'low', '不要轻量模型，用低档看一下', kind='review')
        self.assertEqual((c_low_no_lite['model'], c_low_no_lite['reasoning_effort']), ('model-workhorse', 'low'))
        a_explicit_3p = harness.resolve_execution_profile(engines, 'agy:othervendor-pro-high', 'high', '指定模型审查', kind='review')
        self.assertEqual(a_explicit_3p['model'], 'othervendor-pro-high')

        # 5. 新一代轻量高速标识 (hydrogen) 与混动路由器共存：深思与主力锁定 flagship (carbon)，low 挡精准命中同代 hydrogen（不误用旧代 flash），显式/模糊调用均可用
        g4_engines = backends()
        g4_engines['agy']['models'] = [
            {'slug': 'gemini-4-hydrogen-xhigh', 'display_name': 'Gemini 4 Hydrogen XHigh'},
            {'slug': 'gemini-4-carbon-medium', 'display_name': 'Gemini 4 Carbon (Medium)'},
            {'slug': 'gemini-4-carbon-high', 'display_name': 'Gemini 4 Carbon (High)'},
            {'slug': 'gemini-4-carbon-low', 'display_name': 'Gemini 4 Carbon (Low)'},
            {'slug': 'gemini-4-argon-high', 'display_name': 'Gemini 4 Argon (High)'},
            {'slug': 'gemini-4-hydrogen-low', 'display_name': 'Gemini 4 Hydrogen (Low)'},
            {'slug': 'gemini-pro-orchestrator', 'display_name': 'Orchestrator (Carbon)'},
            {'slug': 'gemini-pro-orchestrator-argon', 'display_name': 'Orchestrator (Argon)'},
            {'slug': 'gemini-3.8-flash-low', 'display_name': 'Gemini 3.8 Flash (Low)'},
        ]
        g4_work = harness.resolve_execution_profile(g4_engines, 'agy:default', 'high', '常规功能实现', kind='code')
        g4_deep = harness.resolve_execution_profile(g4_engines, 'agy:default', 'max', '推导熵守恒格式', kind='math')
        g4_fast = harness.resolve_execution_profile(g4_engines, 'agy:default', 'low', '省额度快速扫查', kind='review')
        g4_no_light = harness.resolve_execution_profile(g4_engines, 'agy:default', 'low', '不要 hydrogen 低档看下', kind='review')
        self.assertEqual((g4_work['model'], g4_work['profile_source']), ('gemini-4-carbon-high', 'frontier_catalog'))
        self.assertEqual((g4_deep['model'], g4_deep['profile_source']), ('gemini-4-carbon-high', 'specialty_deep_reasoning'))
        self.assertEqual((g4_fast['model'], g4_fast['profile_source']), ('gemini-4-hydrogen-low', 'specialty_fast_lightweight'))
        self.assertEqual(g4_no_light['model'], 'gemini-4-carbon-low')
        self.assertEqual(harness.resolve_execution_profile(g4_engines, 'agy:Orchestrator (Carbon)', 'high')['model'], 'gemini-pro-orchestrator')
        self.assertEqual(harness.resolve_execution_profile(g4_engines, 'agy:orchestrator-argon', 'high')['model'], 'gemini-pro-orchestrator-argon')
        self.assertEqual(harness.resolve_execution_profile(g4_engines, 'agy:carbon', 'medium')['model'], 'gemini-4-carbon-medium')
        with self.assertRaises(harness.HarnessError):
            harness.resolve_execution_profile(engines, 'codex:fast', 'low')

        # 自然语言显式/模糊点名模型 + CFD 物理术语/否定词零误触 + Codex 模糊预算 + agy:<alias> 目录发现
        nl_argon = harness.resolve_execution_profile(g4_engines, 'agy:default', 'high', '请调用 Argon 模型复核推导', kind='review')
        self.assertEqual((nl_argon['model'], nl_argon['profile_source']), ('gemini-4-argon-high', 'prompt_fuzzy_model'))
        nl_carbon_med = harness.resolve_execution_profile(g4_engines, 'agy:default', 'high', '请调用 Carbon (Medium) 模型复核', kind='review')
        self.assertEqual((nl_carbon_med['model'], nl_carbon_med['profile_source']), ('gemini-4-carbon-medium', 'prompt_fuzzy_model'))
        nl_orch = harness.resolve_execution_profile(g4_engines, 'agy:default', 'high', '请使用 Orchestrator (Carbon) 模型检查', kind='review')
        self.assertEqual((nl_orch['model'], nl_orch['profile_source']), ('gemini-pro-orchestrator', 'prompt_fuzzy_model'))
        nl_codex = harness.resolve_execution_profile(engines, 'codex:default', 'max', '请调用 demanding 模型推导', kind='math')
        self.assertEqual((nl_codex['model'], nl_codex['profile_source']), ('model-demanding', 'prompt_fuzzy_model'))

        cfd_safe = harness.resolve_execution_profile(
            g4_engines, 'agy:default', 'high',
            '计算 hydrogen detonation 爆轰波与 carbon 热流，采用 k-omega 模型与 Navier-Stokes 模型离散；采用 hydrogen 模型计算爆轰', kind='code',
        )
        self.assertEqual((cfd_safe['model'], cfd_safe['profile_source']), ('gemini-4-carbon-high', 'frontier_catalog'))
        self.assertIsNone(harness._match_effort_level(['ultra'], 'high', ['ultra']))
        neg_prompt = harness.resolve_execution_profile(g4_engines, 'agy:default', 'high', '不要用 Argon 模型，常规复核', kind='review')
        self.assertEqual((neg_prompt['model'], neg_prompt['profile_source']), ('gemini-4-carbon-high', 'frontier_catalog'))

        self.assertEqual(harness._budget_for_spec(engines, 'codex:demanding'), 270000)

        # 单文件表面拼写检查默认仍保持旗舰 max；仅在模糊/明确要求省 token 或降挡时降至 low
        tier_typo_default, reason_typo_default = harness.select_task_tier('auto', 'review', '检查注释错别字与 typo', 1, 300)
        self.assertEqual((tier_typo_default, reason_typo_default), ('max', 'default_flagship_max'))
        tier_typo, reason_typo = harness.select_task_tier('auto', 'review', '省点 token 检查注释错别字与 typo', 1, 300)
        self.assertEqual((tier_typo, reason_typo), ('low', 'economy_trivial_surface_low'))

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
            self.assertEqual(command[command.index('--model') + 1], 'gemini-7-deep-xhigh')
            self.assertNotIn('--effort', command)
            stream = json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': TASK}})
            return subprocess.CompletedProcess(command, 0, stream, '')

        with patch.object(harness, 'run_process', side_effect=check_codex_cmd):
            out = harness.invoke_engine(c_math['spec'], self.root, '推导', engines, harness.TASK_JSON_SCHEMA, True, 30, 'readonly-collaborator', None, c_math['cli_effort'])
            self.assertEqual(out, TASK)
        with patch.object(harness, 'run_process', side_effect=check_agy_cmd):
            out = harness.invoke_engine(a_math['spec'], self.root, '推导', engines, harness.TASK_JSON_SCHEMA, True, 30, 'readonly-collaborator', None, a_math['cli_effort'])
            self.assertEqual(out, TASK)

    def test_catalog_routing_is_permutation_invariant_and_rejects_ambiguous_aliases(self):
        engines = backends()
        engines['codex']['models'] = [
            {'slug': 'gpt-8-router', 'priority': 0, 'supported_efforts': ['max']},
            {'slug': 'hosted-gpt', 'provider': 'thirdparty', 'priority': 0, 'supported_efforts': ['max']},
            {'slug': 'unidentified', 'priority': 0, 'supported_efforts': ['max']},
            {'slug': 'gpt-8-alpha', 'priority': 1, 'supported_efforts': ['high']},
            {'slug': 'gpt-8-beta', 'priority': 1, 'supported_efforts': ['high']},
        ]
        engines['agy']['models'] = [
            {'slug': 'claude-example-high'}, {'slug': 'gpt-oss-example-high'},
            {'slug': 'unknown-high'}, {'slug': 'gemini-4-pro-orchestrator'},
            {'slug': 'gemini-3-pro-high'}, {'slug': 'gemini-4-carbon-high'}, {'slug': 'gemini-4-argon-high'},
        ]
        for _ in range(2):
            self.assertEqual(harness.resolve_execution_profile(engines, 'codex:default')['model'], 'gpt-8-alpha')
            self.assertEqual(harness.resolve_execution_profile(engines, 'agy:default')['model'], 'gemini-4-carbon-high')
            for alias in ('codex:gpt-8', 'agy:gemini'):
                with self.subTest(alias=alias), self.assertRaises(harness.HarnessError) as exc:
                    harness.resolve_execution_profile(engines, alias)
                self.assertGreater(len(exc.exception.details['matching_models']), 1)
            for name in engines:
                engines[name]['models'].reverse()
        self.assertEqual(harness.resolve_execution_profile(engines, 'agy:unknown-high')['model_category'], 'unknown')
        self.assertEqual(harness.resolve_execution_profile(engines, 'codex:hosted-gpt')['model_category'], 'hosted_or_third_party')

    def test_tier_policy_ignores_terms_in_prose_quotes_and_negations(self):
        for prompt in ('Use a fast Fourier transform on edge nodes',
                       'Refactor bridge dispatch',
                       'Summarize the discussion of ultra and xhigh',
                       'Review "root cause, WENO, concurrency" as sample text',
                       'No need to discuss conservation or deadlock',
                       '请不要分析根因及并发，核对输出排版',
                       '避免推导熵稳定格式，整理结果',
                       '只读核对文档对\u201c最高挡\u201d的描述',
                       '不要省 token，不降档，核对输出排版',
                       '只读核对文档对\u201c省额度\u201d和\u201c用低档\u201d的描述'):
            with self.subTest(prompt=prompt), patch.dict(os.environ, {'CAE_DEFAULT_TIER': 'ultra'}, clear=True):
                self.assertEqual(harness.select_task_tier(text=prompt, file_count=80, estimated_tokens=900000),
                                 ('max', 'default_flagship_max'))
            with self.subTest(prompt=prompt, env='high'), patch.dict(os.environ, {'CAE_DEFAULT_TIER': 'high'}, clear=True):
                self.assertEqual(harness.select_task_tier(text=prompt, file_count=80, estimated_tokens=900000),
                                 ('high', 'env_default_tier'))
        self.assertEqual(harness.select_task_tier(text='Review a race condition in message dispatch'),
                         ('max', 'complexity_high_domain_max'))
        self.assertEqual(harness.select_task_tier(text='核对离散守恒性'),
                         ('max', 'complexity_high_domain_max'))
        self.assertEqual(harness.select_task_tier(text='拼写检查', file_count=12, estimated_tokens=6000),
                         ('max', 'default_flagship_max'))
        self.assertEqual(harness.select_task_tier(text='省额度拼写检查', file_count=12, estimated_tokens=6000),
                         ('low', 'economy_trivial_surface_low'))
        self.assertEqual(harness.select_task_tier(text='省点 token 核对模块接口', file_count=12, estimated_tokens=6000),
                         ('high', 'economy_adaptive_high'))
        self.assertEqual(harness.select_task_tier('ultra', text='审查')[0], 'ultra')

    def test_orchestrator_fixed_configuration_for_slug_display_and_unique_alias(self):
        engines = backends()
        engines['agy']['models'] = [
            {'slug': 'gemini-pro-orchestrator', 'display_name': 'Orchestrator (Carbon)'},
            {'slug': 'gemini-pro-orchestrator-argon', 'display_name': 'Orchestrator (Argon)'},
        ]
        commands = []

        def runner(command, cwd, input_text, timeout_sec, **kwargs):
            commands.append(command)
            self.assertNotIn('--effort', command)
            model = command[command.index('--model') + 1]
            stream = '\n'.join([json.dumps({'event': 'init', 'init': {'model': model}}),
                               json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': TASK}})])
            return subprocess.CompletedProcess(command, 0, stream, '')

        with patch.object(harness, '_args_backends', return_value=engines), \
             patch.object(harness, 'run_process', side_effect=runner):
            for model in ('gemini-pro-orchestrator', 'Orchestrator (Carbon)', 'carbon-orchestrator'):
                with self.subTest(model=model):
                    args = self.args('task', '--target', str(self.root), '--kind', 'review', '--task', '核对说明',
                                     '--engine', 'agy:' + model, '--tier', 'xhigh')
                    code, report = self.capture(harness.cmd_task, args)
                    self.assertEqual(code, 0)
                    self.assertEqual(report['requested_tier'], 'xhigh')
                    self.assertEqual(report['effort_source'], 'fixed_configuration')
                    self.assertIsNone(report['declared_effort'])
                    self.assertIsNone(report['effective_effort'])
                    self.assertEqual(report['invocation']['actual_model'], 'gemini-pro-orchestrator')
            with self.assertRaises(harness.HarnessError):
                harness.resolve_execution_profile(engines, 'agy:orchestrator')
        self.assertEqual(len(commands), 3)

    def test_gate_does_not_claim_escalation_when_command_variant_is_unchanged(self):
        engines = backends()
        engines['agy']['models'] = [{'slug': 'gemini-pro-orchestrator'}]
        seen = []

        def runner(command, cwd, input_text=None, timeout_sec=600, **kwargs):
            if kwargs.get('shell'):
                return subprocess.CompletedProcess(command, 0 if len(seen) > 1 else 1, 'verify', '')
            seen.append(command)
            return subprocess.CompletedProcess(command, 0, json.dumps(TASK), '')

        args = self.args('gate', '--worker', 'agy:gemini-pro-orchestrator', '--target', str(self.root),
                         '--task', '整理赋值逻辑', '--verify-cmd', 'verify', '--max-rounds', '2')
        with patch.object(harness, '_args_backends', return_value=engines), \
             patch.object(harness, 'run_process', side_effect=runner):
            code, report = self.capture(harness.cmd_gate, args)
        self.assertEqual(code, 0)
        self.assertEqual(report['rounds_used'], 2)
        self.assertEqual(report['selected_tier'], 'max')
        self.assertEqual(report['effort_source'], 'fixed_configuration')
        self.assertFalse(report['history'][1]['escalated'])
        self.assertNotIn('--effort', report['history'][1]['invocation']['argv'])

    def test_gate_round2_auto_escalates_thinking_tier_to_max(self):
        target_py = self.root / 'helper.py'
        target_py.write_text('x = 0\n', encoding='utf-8')
        engines = backends()
        engines['codex']['models'] = [
            {'slug': 'model-workhorse', 'provider': 'openai', 'priority': 1, 'visibility': 'list',
             'supported_efforts': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
             'delegating_efforts': ['ultra']},
        ]
        seen_efforts = []

        def fake_run(command, root, input_text=None, timeout_sec=600, **kwargs):
            if kwargs.get('shell'):
                return subprocess.CompletedProcess(command, 0 if len(seen_efforts) == 2 else 1, 'verify', '')
            effort = next(value for value in command if value.startswith('model_reasoning_effort='))
            seen_efforts.append((command[command.index('-m') + 1], effort))
            target_py.write_text(f'x = {len(seen_efforts)}\n', encoding='utf-8')
            Path(command[command.index('-o') + 1]).write_text(json.dumps(TASK), encoding='utf-8')
            return subprocess.CompletedProcess(command, 0, '', '')

        verify_cmd = f'"{sys.executable}" -B -c "import pathlib,sys; sys.exit(0 if \'x = 2\' in pathlib.Path(\'helper.py\').read_text() else 1)"'
        args = self.args('gate', '--caller', 'antigravity', '--target', str(self.root),
                         '--task', '省点 token 整理返回值赋值逻辑', '--write-scope', '*.py', '--verify-cmd', verify_cmd, '--max-rounds', '3')
        with patch.object(harness, '_args_backends', return_value=engines), patch.object(harness, 'run_process', side_effect=fake_run):
            code, report = self.capture(harness.cmd_gate, args)
        self.assertEqual(code, 0)
        self.assertEqual(seen_efforts, [('model-workhorse', 'model_reasoning_effort="high"'),
                                        ('model-workhorse', 'model_reasoning_effort="max"')])
        self.assertEqual(report['rounds_used'], 2)
        self.assertEqual(report['selected_tier'], 'max')
        self.assertEqual(report['tier_reason'], 'gate_retry_escalated_max')
        self.assertFalse(report['history'][0]['escalated'])
        self.assertTrue(report['history'][1]['escalated'])
        self.assertIn('model_reasoning_effort="max"', report['history'][1]['invocation']['argv'])

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
                              config_home=str(linux_agy.parents[1]), authentication_status='not_checked',
                              model_catalog={'freshness': 'within_assessment_window'},
                              models=[{'slug': 'gemini-7-pro-high'}])
        windows_settings = win_agy.parents[1] / 'antigravity-cli' / 'settings.json'
        windows_settings.parent.mkdir(parents=True)
        windows_settings.write_text('{"model": "Windows configured model"}', encoding='utf-8')

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
            decoded = base64.b64decode(command[-1]).decode('utf-16le')
            if "$info.Arguments = 'models'" in decoded:
                return subprocess.CompletedProcess(command, 0, 'gemini-8-carbon-high\tGemini 8 Carbon (High)\n', '')
            stream = json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': TASK}})
            return subprocess.CompletedProcess(command, 0, stream, '')

        with patch.dict(os.environ, {}, clear=True), patch.object(harness, '_is_posix', return_value=True), \
             patch.object(harness.shutil, 'which', side_effect=fake_which), \
             patch.object(harness, '_find_windows_agy_in_wsl', return_value=str(win_agy)), \
             patch.object(harness, '_windows_env', return_value='C:\\Temp'), \
             patch.object(harness, '_wsl_path', side_effect=fake_wsl_path), \
             patch.object(harness, 'run_process', side_effect=fake_run):
            receipt = {}
            out = harness.invoke_engine('agy:default', self.root, '审查', engines, harness.TASK_JSON_SCHEMA, receipt=receipt)
        self.assertEqual(out, TASK)
        self.assertEqual(len(calls), 3)
        self.assertTrue(engines['agy']['windows_interop'])
        self.assertEqual(engines['agy']['binary'], str(win_agy))
        self.assertEqual(engines['agy']['model'], 'Windows configured model')
        self.assertEqual(engines['agy']['model_catalog']['source'], 'agy_models_cli')
        self.assertEqual(receipt['requested_engine'], 'agy:default')
        self.assertEqual(receipt['model_requested_to_cli'], 'gemini-8-carbon-high')
        self.assertEqual(receipt['execution_environment']['fallback_from']['binary'], str(linux_agy))

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
        events = harness._quota_events_dir(harness._quota_state_path('codex', engines['codex']))
        quota_file = events / (report1['quota_cooldown']['event_id'] + '.json')
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
        quota_file.write_text(json.dumps({'backend': 'codex', 'exhausted': True, 'cooldown_until_epoch': 1.0}), encoding='utf-8')
        self.assertIsNone(harness._load_active_quota_cooldown('codex', engines['codex']))
        self.assertFalse(quota_file.exists())

    def test_audit_quota_fuse_survives_inflight_success_and_keeps_coverage(self):
        for name in ('a.py', 'b.py', 'c.py'):
            (self.root / name).write_text('x = 1\n', encoding='utf-8')
        engines = backends()
        cfg = tempfile.TemporaryDirectory(prefix='cae-test-config-')
        self.addCleanup(cfg.cleanup)
        engines['codex']['config_home'] = cfg.name
        calls, ready, quota_saved = [], threading.Barrier(2), threading.Event()
        record_quota = harness._record_quota_exhausted

        def signal_after_quota(*args, **kwargs):
            cooldown = record_quota(*args, **kwargs)
            quota_saved.set()
            return cooldown

        def runner(command, cwd, input_text, timeout_sec, **kwargs):
            name = next(line[2:] for line in input_text.splitlines() if line.startswith('- '))
            calls.append(name)
            if name in ('a.py', 'b.py'):
                ready.wait(timeout=5)
            if name == 'a.py':
                return subprocess.CompletedProcess(command, 1, '', 'hit your usage limit')
            if name == 'b.py':
                self.assertTrue(quota_saved.wait(5))
            Path(command[command.index('-o') + 1]).write_text(
                json.dumps({**AUDIT, 'read_files': [name]}), encoding='utf-8')
            return subprocess.CompletedProcess(command, 0, '', '')

        original = harness.concurrent.futures.ThreadPoolExecutor
        args = self.args('audit', '--target', str(self.root), '--single', '--left', 'codex:default',
                         '--strategy', 'sharded', '--shard-tokens', '1', '--retry-quota')
        with patch.object(harness, '_args_backends', return_value=engines), \
             patch.object(harness.concurrent.futures, 'ThreadPoolExecutor', side_effect=lambda **kw: original(max_workers=2)), \
             patch.object(harness, '_record_quota_exhausted', side_effect=signal_after_quota), \
             patch.object(harness, 'run_process', side_effect=runner):
            code, report = self.capture(harness.cmd_audit, args)
        self.assertEqual(code, 3)
        self.assertTrue(report['quota_fuse_triggered'])
        self.assertEqual(set(calls), {'a.py', 'b.py'})
        self.assertEqual(report['completed_units'][0]['read_files'], ['b.py'])
        self.assertEqual(report['completed_count'], 1)
        self.assertTrue(next(error for error in report['errors'] if error['planned_files'] == ['c.py'])['preflight_blocked'])
        events = harness._quota_events_dir(harness._quota_state_path('codex', engines['codex']))
        self.assertTrue((events / (report['quota_cooldown']['event_id'] + '.json')).is_file())

    def test_retry_success_preserves_new_quota_failure_from_another_process(self):
        writer = self.root / 'quota_writer.py'
        writer.write_text("""
import importlib.util, json, os, sys, time
from pathlib import Path

spec = importlib.util.spec_from_file_location('harness_fixture', sys.argv[1])
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
info = {'config_home': sys.argv[2]}
assert sys.stdin.readline().strip() == 'record failure'
if sys.argv[3] == 'legacy':
    state = harness._quota_state_path('codex', info, legacy=True)
    pending = state.with_suffix('.pending')
    pending.write_text(json.dumps({'backend': 'codex', 'exhausted': True, 'event_id': 'new-from-legacy',
                                   'cooldown_until_epoch': time.time() + 3600}), encoding='utf-8')
    os.replace(pending, state)
else:
    harness._record_quota_exhausted('codex', info, 'new failure from another process')
""", encoding='utf-8')
        for mode in ('current', 'legacy'):
            with self.subTest(mode=mode):
                info = {'config_home': str(self.root / mode)}
                state = harness._quota_state_path('codex', info)
                events = harness._quota_events_dir(state)
                if mode == 'legacy':
                    state = harness._quota_state_path('codex', info, legacy=True)
                    state.parent.mkdir(parents=True)
                    first_id = 'old-legacy'
                    state.write_text(json.dumps({'backend': 'codex', 'exhausted': True, 'event_id': first_id,
                                                 'cooldown_until_epoch': time.time() + 300}), encoding='utf-8')
                    old = state
                else:
                    first_id = harness._record_quota_exhausted('codex', info, 'old cooldown')['event_id']
                    old = events / (first_id + '.json')
                retry = harness._QuotaBatch(retry=True)
                harness._enforce_preflight_quota({'codex': info}, ('codex:default',), batch=retry)
                child = subprocess.Popen([sys.executable, '-B', str(writer), str(SCRIPT), info['config_home'], mode],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                original_unlink = Path.unlink
                original_publish = harness._publish_quota_record
                child_started = threading.Event()

                def record_new_failure():
                    child_started.set()
                    _, errors = child.communicate('record failure\n', timeout=10)
                    self.assertEqual(child.returncode, 0, errors)

                def unlink_after_new_failure(path, *args, **kwargs):
                    if path == old:
                        record_new_failure()
                    return original_unlink(path, *args, **kwargs)

                def acknowledge_after_new_failure(path, record):
                    if path.parent.name == 'acknowledged':
                        record_new_failure()
                    return original_publish(path, record)

                try:
                    with patch.object(Path, 'unlink', unlink_after_new_failure), \
                         patch.object(harness, '_publish_quota_record', acknowledge_after_new_failure):
                        harness._mark_quota_success('codex', info, retry)
                    self.assertTrue(child_started.is_set())
                    cooldown = harness._load_active_quota_cooldown('codex', info, cleanup_expired=False)
                    self.assertIsNotNone(cooldown)
                    self.assertNotEqual(cooldown['event_id'], first_id)
                    with self.assertRaises(harness.HarnessError) as blocked:
                        harness._enforce_preflight_quota({'codex': info}, ('codex:default',), batch=retry)
                    self.assertEqual(blocked.exception.status, 'quota_exhausted')
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=10)

    def test_agy_stale_catalog_refresh_checks_quota_and_does_not_swallow_failures(self):
        engines = backends()
        info = engines['agy']
        info.update(model_catalog={'freshness': 'stale'}, config_home=str(self.root / 'native'))
        args = self.args('task', '--target', str(self.root), '--kind', 'review', '--task', '整理正文', '--engine', 'agy:default')
        listing = Mock(return_value=subprocess.CompletedProcess(['agy', 'models'], 1, '', 'network unavailable'))
        with patch.object(harness, '_args_backends', return_value=engines), \
             patch.object(harness, '_run_agy_listing', listing), patch.object(harness, 'invoke_engine') as worker:
            code, report = self.capture(harness.cmd_task, args)
            self.assertEqual(code, 1)
            self.assertIn('停止派工', report['error'])
            worker.assert_not_called()
            listing.assert_called_once()
            listing.reset_mock()
            harness._record_quota_exhausted('agy', info, 'usage limit')
            code, report = self.capture(harness.cmd_task, args)
            self.assertEqual(code, 5)
            self.assertTrue(report['preflight_blocked'])
            listing.assert_not_called()
            worker.assert_not_called()

    def test_agy_listing_quota_precedes_auth_fallback_and_scopes_by_execution_side(self):
        engines = backends()
        native = engines['agy']
        native.update(config_home=str(self.root), execution_side='posix', model_catalog={'freshness': 'stale'})
        windows = {**native, 'execution_side': 'windows', 'windows_interop': True}
        response = subprocess.CompletedProcess(['agy', 'models'], 1, '', 'Please sign in; resource_exhausted')
        with patch.object(harness, '_run_agy_listing', return_value=response), \
             patch.object(harness, '_fallback_agy_info') as fallback, self.assertRaises(harness.HarnessError) as caught:
            harness._refresh_agy_listing(native, harness._QuotaBatch(), refresh_model_cache=True)
        fallback.assert_not_called()
        self.assertEqual(caught.exception.status, 'quota_exhausted')
        self.assertEqual(caught.exception.details['quota_execution_side'], 'posix')
        harness._enforce_preflight_quota({'agy': windows}, ('agy:default',))
        self.assertFalse(harness._quota_state_path('agy', windows).exists())
        with self.assertRaises(harness.HarnessError):
            harness._enforce_preflight_quota(engines, ('agy:default',))

    def test_agy_auth_fallback_checks_windows_cooldown_before_listing(self):
        engines = backends()
        native = engines['agy']
        native.update(config_home=str(self.root / 'native'), execution_side='posix')
        windows = {**native, 'config_home': str(self.root / 'windows'), 'execution_side': 'windows', 'windows_interop': True}
        harness._record_quota_exhausted('agy', windows, 'usage limit')
        response = subprocess.CompletedProcess(['agy', 'models'], 1, '', 'Please sign in to view available models.')
        with patch.object(harness, '_run_agy_listing', return_value=response) as listing, \
             patch.object(harness, '_fallback_agy_info', return_value=windows), self.assertRaises(harness.HarnessError) as caught:
            harness._refresh_agy_listing(native, harness._QuotaBatch(), refresh_model_cache=True)
        listing.assert_called_once_with(native)
        self.assertTrue(caught.exception.details['preflight_blocked'])
        self.assertEqual(caught.exception.details['quota_execution_side'], 'windows')

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

    def test_codex_multigen_alias_agy_generation_priority_and_math_cues(self):
        engines = backends()
        engines['codex']['models'] = [
            {'slug': 'gpt-6.1-sol', 'display_name': 'GPT-6.1-Sol', 'provider': 'openai', 'priority': 1,
             'context_window': 500000, 'max_context_window': 872000, 'effective_context_window_percent': 95,
             'supported_efforts': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']},
            {'slug': 'gpt-6-astra', 'display_name': 'GPT-6-Astra', 'provider': 'openai', 'priority': 2,
             'description': 'Frontier intelligence for the most demanding work.',
             'context_window': 272000, 'max_context_window': 872000, 'effective_context_window_percent': 95,
             'supported_efforts': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']},
            {'slug': 'gpt-6-sol', 'display_name': 'GPT-6-Sol', 'provider': 'openai', 'priority': 5,
             'context_window': 400000, 'max_context_window': 872000, 'supported_efforts': ['high', 'max']},
            {'slug': 'gpt-5.6-sol', 'display_name': 'GPT-5.6-Sol', 'provider': 'openai', 'priority': 9,
             'context_window': 272000, 'supported_efforts': ['high']},
        ]
        sol = harness.resolve_execution_profile(engines, 'codex:sol', 'max', '实现牛顿法线性化求解器', 'code')
        self.assertEqual(sol['model'], 'gpt-6.1-sol')
        self.assertEqual(sol['reasoning_effort'], 'max')
        self.assertEqual(sol['capacity']['cli_context_window_tokens'], 872000)

        tier, reason = harness.select_task_tier('auto', 'code', '请调用 Codex 模型核对偏微分方程数学公式与牛顿法通量离散')
        self.assertEqual((tier, reason), ('max', 'complexity_high_domain_max'))
        astra = harness.resolve_execution_profile(engines, 'codex:default', tier, '请调用 Codex 模型推导控制方程特征值', 'math')
        self.assertEqual(astra['model'], 'gpt-6-astra')
        self.assertEqual(astra['reasoning_effort'], 'max')
        self.assertEqual(astra['capacity']['cli_context_window_tokens'], 872000)

        engines['agy']['models'] = [
            {'slug': 'gemini-4-carbon-high'},
            {'slug': 'gemini-3-deep-xhigh'},
        ]
        agy_prof = harness.resolve_execution_profile(engines, 'agy:default', 'max', '推导守恒律', 'math')
        self.assertEqual(agy_prof['model'], 'gemini-4-carbon-high')

    def test_agy_stream_tool_output_quota_phrase_does_not_false_trip(self):
        engines = backends()
        engines['agy']['config_home'] = str(self.root / 'agy-home')
        stream_out = '\n'.join([
            json.dumps({'type': 'init', 'model': 'gemini-7-pro-high'}),
            json.dumps({'type': 'tool_result', 'content': 'Read file says: usage limit reached in fixture doc'}),
            json.dumps({'type': 'error', 'message': 'upstream connection reset'}),
        ])
        proc = subprocess.CompletedProcess(['agy'], 1, stream_out, 'upstream connection reset')
        with patch.object(harness, 'run_process', return_value=proc), self.assertRaises(harness.HarnessError) as caught:
            harness.invoke_engine('agy:default', self.root, '检查文档', engines, harness.TASK_JSON_SCHEMA)
        self.assertEqual(caught.exception.status, 'failed')
        self.assertIsNone(harness._load_active_quota_cooldown('agy', engines['agy']))

    def test_fallback_on_quota_switches_to_local_matched_model_safely(self):
        proj = self.root / 'proj'
        proj.mkdir()
        target = proj / 'solver.py'
        target.write_text('x = 1\n', encoding='utf-8')
        engines = backends()
        engines['codex'].update(config_home=str(self.root / 'codex-home'), models=[
            {'slug': 'gpt-6-astra', 'provider': 'openai', 'description': 'Frontier intelligence for the most demanding work.',
             'context_window': 272000, 'max_context_window': 872000, 'supported_efforts': ['high', 'max']},
        ])
        engines['agy'].update(config_home=str(self.root / 'agy-home'), models=[{'slug': 'gemini-4-carbon-high'}])

        def fake_invoke(spec_name, work_dir, prompt, bk, schema, readonly=True, *a, **kw):
            prefix, _ = harness._engine_parts(spec_name)
            if prefix == 'codex':
                rec = harness._record_quota_exhausted('codex', bk['codex'], 'You have hit your usage limit', batch=kw.get('batch'))
                raise harness.HarnessError('codex 触发额度上限', 'quota_exhausted', **harness._quota_user_choices('codex', rec))
            kw.get('receipt', {})['invocation_started'] = True
            return dict(TASK)

        with patch.object(harness, '_args_backends', return_value=engines), patch.object(harness, 'invoke_engine', side_effect=fake_invoke):
            code, out = self.capture(harness.cmd_task, self.args(
                'task', '--caller', 'antigravity', '--engine', 'codex:default', '--kind', 'math',
                '--target', str(target), '--task', '推导数学公式', '--fallback-on-quota'))
        self.assertEqual(code, 0)
        self.assertEqual(out['status'], 'ok')
        self.assertEqual(out['engine'], 'agy:default')
        self.assertEqual(out['model'], 'gemini-4-carbon-high')
        self.assertEqual(out['quota_fallback']['from_engine'], 'codex:default')
        self.assertEqual(out['quota_fallback']['to_engine'], 'agy:default')
        self.assertFalse(out['quota_fallback']['preflight_blocked'])
        self.assertIsNotNone(harness._load_active_quota_cooldown('codex', engines['codex']))

        # Cooldown is now persisted: preflight fallback in gate and audit also switches cleanly to agy
        def fake_edit_worker(spec_name, work_dir, *a, **kw):
            self.assertTrue(spec_name.startswith('agy:'))
            kw.get('receipt', {})['invocation_started'] = True
            target.write_text('x = 2\n', encoding='utf-8')
            return 'updated'

        with patch.object(harness, '_args_backends', return_value=engines), \
             patch.object(harness, 'invoke_edit_worker', side_effect=fake_edit_worker):
            g_code, g_out = self.capture(harness.cmd_gate, self.args(
                'gate', '--caller', 'antigravity', '--worker', 'codex:default',
                '--target', str(target), '--task', '更新公式实现', '--verify-cmd', f'"{sys.executable}" -c "import sys; sys.exit(0)"',
                '--fallback-on-quota'))
        self.assertEqual(g_code, 0)
        self.assertEqual(g_out['engine'], 'agy:default')
        self.assertTrue(g_out['quota_fallback']['preflight_blocked'])

        with patch.object(harness, '_args_backends', return_value=engines), \
             patch.object(harness, 'invoke_readonly_engine', return_value={'summary': 'ok', 'findings': [], 'read_files': ['solver.py'], 'unread_files': []}):
            a_code, a_out = self.capture(harness.cmd_audit, self.args(
                'audit', '--caller', 'antigravity', '--target', str(proj), '--fallback-on-quota'))
        self.assertEqual(a_code, 0)
        self.assertEqual(a_out['plan']['left_engine'], 'agy:default')
        self.assertFalse(a_out['plan']['dual_blind'])
        self.assertEqual(a_out['quota_fallback']['to_engine'], 'agy:default')

        # If worker already modified workspace before hitting quota_exhausted, runtime fallback MUST NOT rerun writes
        harness._SESSION_QUOTA_BLOCKED.clear()
        engines2 = backends()
        engines2['codex']['config_home'] = str(self.root / 'codex-home-2')
        engines2['agy']['config_home'] = str(self.root / 'agy-home-2')

        def dirty_codex(spec_name, work_dir, *a, **kw):
            target.write_text('dirty = True\n', encoding='utf-8')
            rec = harness._record_quota_exhausted('codex', engines2['codex'], 'usage limit', batch=kw.get('batch'))
            raise harness.HarnessError('codex 触发额度上限', 'quota_exhausted', **harness._quota_user_choices('codex', rec))

        with patch.object(harness, '_args_backends', return_value=engines2), patch.object(harness, 'invoke_engine', side_effect=dirty_codex):
            d_code, d_out = self.capture(harness.cmd_task, self.args(
                'task', '--caller', 'antigravity', '--engine', 'codex:default', '--kind', 'code',
                '--target', str(target), '--task', '修改代码', '--fallback-on-quota'))
        self.assertEqual(d_code, harness.EXIT_CODES['quota_exhausted'])
        self.assertEqual(d_out['status'], 'quota_exhausted')
        self.assertNotIn('quota_fallback', d_out)

    def test_default_flagship_max_economy_fallback_and_main_model_independence(self):
        # 1. 反重力原生只读子代理定义不含硬编码 model 字段，保持 inherit 跟随主对话
        agents_dir = ROOT / 'template' / 'user-guard' / 'agents'
        for agent_md in sorted(agents_dir.glob('readonly-*.md')):
            frontmatter = agent_md.read_text(encoding='utf-8').split('---', 2)[1]
            self.assertNotRegex(frontmatter, r'(?m)^\s*model\s*:')

        # 2. 不管反重力前端或 Codex 前端当前选什么主控模型，都不影响 cae-harness 选模、挡位与最大窗口
        base_codex_models = [
            {'slug': 'gpt-6.1-sol', 'provider': 'openai', 'priority': 1,
             'context_window': 500000, 'max_context_window': 872000,
             'supported_efforts': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'], 'delegating_efforts': ['ultra']},
            {'slug': 'gpt-6-astra', 'provider': 'openai', 'priority': 2,
             'description': 'Frontier intelligence for the most demanding work.',
             'context_window': 272000, 'max_context_window': 872000,
             'supported_efforts': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'], 'delegating_efforts': ['ultra']},
            {'slug': 'gpt-6-luna', 'provider': 'openai', 'priority': 3,
             'description': 'Fast and affordable for easier tasks.',
             'context_window': 272000, 'max_context_window': 872000,
             'supported_efforts': ['low', 'medium', 'high', 'max']},
        ]
        base_agy_models = [
            {'slug': 'gemini-4-carbon-high', 'display_name': 'Gemini 4 Carbon (High)'},
            {'slug': 'gemini-4-carbon-medium', 'display_name': 'Gemini 4 Carbon (Medium)'},
            {'slug': 'gemini-4-hydrogen-low', 'display_name': 'Gemini 4 Hydrogen (Low)'},
            {'slug': 'gemini-pro-orchestrator', 'display_name': 'Orchestrator (Carbon)'},
        ]
        for frontend_agy_model in ('gemini-pro-orchestrator', 'gemini-4-hydrogen-low', 'claude-sonnet-4', None):
            for frontend_codex_model in ('gpt-6-luna', 'custom-router', None):
                engines = backends()
                engines['codex'].update(model=frontend_codex_model, reasoning_effort='low', models=base_codex_models)
                engines['agy'].update(model=frontend_agy_model, models=base_agy_models)
                tier_math, _ = harness.select_task_tier('auto', 'math', '推导高阶格式')
                tier_code, _ = harness.select_task_tier('auto', 'code', '编写常规辅助函数')
                tier_typo, _ = harness.select_task_tier('auto', 'review', '检查注释 typo')
                self.assertEqual((tier_math, tier_code, tier_typo), ('max', 'max', 'max'))
                p_math = harness.resolve_execution_profile(engines, 'codex:default', tier_math, '推导高阶格式', 'math')
                p_code = harness.resolve_execution_profile(engines, 'codex:default', tier_code, '编写常规辅助函数', 'code')
                p_typo = harness.resolve_execution_profile(engines, 'codex:default', tier_typo, '检查注释 typo', 'review')
                p_agy = harness.resolve_execution_profile(engines, 'agy:default', tier_code, '编写常规辅助函数', 'code')
                self.assertEqual((p_math['model'], p_math['reasoning_effort'], p_math['capacity']['cli_context_window_tokens']),
                                 ('gpt-6-astra', 'max', 872000))
                self.assertEqual((p_code['model'], p_code['reasoning_effort'], p_code['capacity']['cli_context_window_tokens']),
                                 ('gpt-6.1-sol', 'max', 872000))
                self.assertEqual((p_typo['model'], p_typo['reasoning_effort']), ('gpt-6.1-sol', 'max'))
                self.assertEqual((p_agy['model'], p_agy['reasoning_effort']), ('gemini-4-carbon-high', 'high'))

        # 3. 模糊或明确要求降挡／节省 token 时切换选项 B 分级省流；显式点名挡位严格照办
        for cue in ('省token整理逻辑', '节省 token 审查接口', '降档修改配置', '适当降挡核查日志',
                    '要不省点 token 整理逻辑', '能不能降档修改配置', 'save tokens review module'):
            self.assertEqual(harness.select_task_tier('auto', 'code', cue), ('high', 'economy_adaptive_high'))
        for surface_cue in ('省token检查错别字', '降档检查 typo', '省点额度做格式微调',
                            '能不能降档检查 typo', '轻量快速扫一下注释'):
            self.assertEqual(harness.select_task_tier('auto', 'review', surface_cue), ('low', 'economy_trivial_surface_low'))
        self.assertEqual(harness.select_task_tier('auto', 'math', '降档推导离散格式'), ('high', 'economy_adaptive_high'))
        self.assertEqual(harness.select_task_tier('auto', 'code', '用中档整理逻辑'), ('medium', 'user_description_medium'))
        self.assertEqual(harness.select_task_tier('auto', 'review', '不省token，不降档，绝不省额度，检查 typo'), ('max', 'default_flagship_max'))

    def test_agy_print_timeout_exit_code_and_terminal_statuses(self):
        # 1) agy --print-timeout 在 -p 模式退出码 3 或 stream-json 穿插 AGY_ERROR 行时须精确归类为 timeout
        engines = backends()
        observed_timeouts = []

        def fake_timeout_exit3(command, cwd, input_text, timeout_sec, **kwargs):
            observed_timeouts.append(timeout_sec)
            stderr = (
                '[agy] print timeout after 30s\n'
                'AGY_ERROR: {"short_error":"print timeout after 30s","status":"DEADLINE_EXCEEDED","reason":"print_timeout"}\n'
            )
            return subprocess.CompletedProcess(command, 3, '', stderr)

        with patch.object(harness, 'run_process', side_effect=fake_timeout_exit3):
            with self.assertRaises(harness.HarnessError) as ctx:
                harness.invoke_readonly_engine('agy:default', self.root, '审查通量', engines, 30, [])
        self.assertEqual(ctx.exception.status, 'timeout')
        self.assertEqual(ctx.exception.details.get('finished_reason'), 'print_timeout')
        # 外层 run_process 超时在 agy --print-timeout 基础上预留 15s 缓冲窗口
        self.assertEqual(observed_timeouts, [45])

        # 2) NDJSON 中夹带 AGY_ERROR / [agy] print timeout 诊断行且退出码为 0 时，_decode_output_json 同样归类为 timeout
        ndjson_timeout = (
            '{"event":"init","session_id":"s1"}\n'
            '[agy] print timeout after 30s\n'
            'AGY_ERROR: {"short_error":"print timeout after 30s","status":"DEADLINE_EXCEEDED","reason":"print_timeout"}\n'
        )
        with self.assertRaises(harness.HarnessError) as ctx_nd:
            harness._decode_output_json(ndjson_timeout)
        self.assertEqual(ctx_nd.exception.status, 'timeout')

        # 3) 官方 CLI Headless 规范中的 INVALID / WAITING / RUNNING 非正常终态须拦截为 failed（覆盖单行 JSON 与多行 NDJSON）
        for bad_status in ('INVALID', 'WAITING', 'RUNNING'):
            single_json = json.dumps({'event': 'result', 'status': bad_status, 'structured_output': {'summary': 'x'}})
            with self.assertRaises(harness.HarnessError) as ctx_single:
                harness._extract_json_object(single_json)
            self.assertEqual(ctx_single.exception.status, 'failed', bad_status)

            ndjson_stream = '{"event":"init","session_id":"s1"}\n' + single_json + '\n'
            with self.assertRaises(harness.HarnessError) as ctx_st:
                harness._decode_output_json(ndjson_stream)
            self.assertEqual(ctx_st.exception.status, 'failed', bad_status)

    def test_zero_exit_stderr_terminal_diagnostics_override_result(self):
        output = json.dumps({'event': 'result', 'structured_output': TASK})
        cases = (
            ('AGY_ERROR: {"short_error":"connection reset"}', 'failed'),
            ('AGY_ERROR: {"short_error":"usage limit reached"}', 'quota_exhausted'),
            ('AGY_ERROR: {"short_error":"print timeout after 30s","reason":"print_timeout"}', 'timeout'),
            ('[agy] print timeout after 30s', 'timeout'),
            ('error: print timeout after 30s', 'timeout'),
        )
        for index, (stderr, expected) in enumerate(cases):
            engines = backends()
            engines['agy']['config_home'] = str(self.root / ('agy-' + str(index)))
            with self.subTest(stderr=stderr), patch.object(harness, 'run_process', return_value=subprocess.CompletedProcess([], 0, output, stderr)):
                with self.assertRaises(harness.HarnessError) as caught:
                    harness.invoke_engine('agy:default', self.root, 'review', engines, harness.TASK_JSON_SCHEMA)
                self.assertEqual(caught.exception.status, expected)
                self.assertEqual(harness._load_active_quota_cooldown('agy', engines['agy']) is not None, expected == 'quota_exhausted')
        engines = backends()
        engines['agy']['config_home'] = str(self.root / 'warning-only')
        for stderr in ('warning: optional cache missing', 'warning: report discusses usage limit and print timeout after 30s'):
            with self.subTest(stderr=stderr), patch.object(harness, 'run_process', return_value=subprocess.CompletedProcess([], 0, output, stderr)):
                self.assertEqual(harness.invoke_engine('agy:default', self.root, 'review', engines, harness.TASK_JSON_SCHEMA), TASK)
                self.assertIsNone(harness._load_active_quota_cooldown('agy', engines['agy']))

    def test_agy_error_and_success_result_cannot_cancel_each_other(self):
        output = 'AGY_ERROR: {"short_error":"connection reset"}\n' + json.dumps({'event': 'result', 'structured_output': TASK})
        with self.assertRaises(harness.HarnessError) as caught:
            harness._decode_output_json(output)
        self.assertEqual(caught.exception.status, 'failed')

    def test_failure_diagnostics_exclude_result_business_content(self):
        body = {**TASK, 'content': 'The report discusses usage limit reached and print timeout after 30s.'}
        for status in ('success', 'failed'):
            engines = backends()
            engines['agy']['config_home'] = str(self.root / status)
            event = {'event': 'result', 'status': status, 'message': 'upstream connection reset',
                     'structured_output': body, 'result': body, 'content': body}
            proc = subprocess.CompletedProcess([], 1, json.dumps(event), 'upstream connection reset')
            detail = harness._backend_diagnostics(proc)
            self.assertNotIn('usage limit', detail)
            self.assertNotIn('print timeout after', detail)
            with self.subTest(status=status), patch.object(harness, 'run_process', return_value=proc), self.assertRaises(harness.HarnessError) as caught:
                harness.invoke_engine('agy:default', self.root, 'review', engines, harness.TASK_JSON_SCHEMA)
            self.assertEqual(caught.exception.status, 'failed')
            self.assertIsNone(harness._load_active_quota_cooldown('agy', engines['agy']))

    def test_blank_verify_commands_rejected_before_cli_preflight(self):
        for command in ('', '   ', '\t\r\n'):
            for subcommand in ('gate', 'task'):
                for dry_run in (False, True):
                    tokens = [subcommand, '--target', str(self.root), '--task', '修改代码', '--verify-cmd', command]
                    if subcommand == 'task':
                        tokens.extend(['--kind', 'code'])
                    if dry_run:
                        tokens.append('--dry-run')
                    handler = harness.cmd_gate if subcommand == 'gate' else harness.cmd_task
                    with self.subTest(command=command, subcommand=subcommand, dry_run=dry_run), \
                         patch.object(harness, '_target') as target, \
                         patch.object(harness, '_args_backends') as probe, \
                         patch.object(harness, 'run_process') as runner:
                        with self.assertRaisesRegex(harness.HarnessError, '^--verify-cmd 不能为空白命令$'):
                            handler(self.args(*tokens))
                        target.assert_not_called()
                        probe.assert_not_called()
                        runner.assert_not_called()

    def test_blank_verify_commands_rejected_before_gate_worker(self):
        for command in ('', '   ', '\t\n'):
            worker, verifier = Mock(), Mock(return_value=(0, 'ok'))
            with self.subTest(command=command), patch.object(harness, 'snapshot_files') as snapshot:
                with self.assertRaisesRegex(harness.HarnessError, '^--verify-cmd 不能为空白命令$'):
                    harness.run_physics_gate_loop(self.root, '修改代码', command, worker, verifier)
                snapshot.assert_not_called()
            worker.assert_not_called()
            verifier.assert_not_called()

    def test_special_files_rejected_by_snapshot_and_skipped_by_collection(self):
        special = self.root / 'special.py'
        special.write_text('placeholder', encoding='utf-8')
        real_lstat = Path.lstat
        for mode in (stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK):
            fake_stat = os.stat_result((mode | 0o600, 0, 0, 1, 0, 0, 1024, 0, 0, 0))

            def lstat(path, *args, **kwargs):
                return fake_stat if path == special else real_lstat(path, *args, **kwargs)

            with self.subTest(mode=mode), patch.object(Path, 'lstat', autospec=True, side_effect=lstat) as stats, \
                 patch.object(Path, 'open', side_effect=AssertionError('特殊文件不得打开')) as open_file:
                with self.assertRaisesRegex(harness.HarnessError, '工作区包含非常规特殊文件，无法安全快照: special.py') as caught:
                    harness.snapshot_files(self.root)
                self.assertEqual(caught.exception.details['snapshot_path'], 'special.py')
                self.assertEqual(sum(call.args[0] == special for call in stats.call_args_list), 1)
                skipped = []
                self.assertEqual(harness.collect_target_files(self.root, skipped), [])
                self.assertEqual(skipped, [{'file': 'special.py', 'reason': 'special_file'}])
                open_file.assert_not_called()

    def test_snapshot_reuses_one_lstat_for_regular_files_and_reparse_points(self):
        source = self.root / 'a.py'
        data = b'value = 1\n'
        source.write_bytes(data)
        real_lstat = Path.lstat
        for reparse in (False, True):
            fake_stat = Mock(st_mode=stat.S_IFREG | 0o640,
                             st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT if reparse else 0)

            def lstat(path, *args, **kwargs):
                return fake_stat if path == source else real_lstat(path, *args, **kwargs)

            with self.subTest(reparse=reparse), patch.object(Path, 'lstat', autospec=True, side_effect=lstat) as stats, \
                 patch.object(harness.os, 'readlink', return_value='destination') as readlink:
                report = harness.snapshot_files(self.root)
                self.assertEqual(sum(call.args[0] == source for call in stats.call_args_list), 1)
                if reparse:
                    self.assertEqual(report['a.py'], 'symlink:destination')
                    readlink.assert_called_once_with(source)
                else:
                    self.assertEqual(report['a.py'], harness.hashlib.sha256(data).hexdigest() + ':' + str(0o640))
                    readlink.assert_not_called()

    def test_assess_preserves_expired_events_and_acknowledgements_even_with_discovery(self):
        homes = {name: self.root / name for name in ('codex', 'agy')}
        for name, home in homes.items():
            info = {'config_home': str(home), 'windows_interop': False}
            events = harness._quota_events_dir(harness._quota_state_path(name, info))
            (events / 'acknowledged').mkdir(parents=True)
            expired = {'backend': name, 'execution_side': harness._execution_side(info),
                       'exhausted': True, 'cooldown_until_epoch': 1.0}
            contents = json.dumps(expired, indent=2) + '\n'
            (events / ('a' * 32 + '.json')).write_text(contents, encoding='utf-8')
            (events / 'acknowledged' / harness._quota_ack_name(expired)).write_text(contents, encoding='utf-8')
        (homes['codex'] / 'models_cache.json').write_text(json.dumps({'models': backends()['codex']['models']}), encoding='utf-8')
        cache = homes['agy'] / 'antigravity-cli' / 'cache' / 'cae_models_cache.json'
        cache.write_text('{"models": [{"slug": "cached-high"}]}', encoding='utf-8')
        before = harness.snapshot_files(self.root)
        for discover in (False, True):
            response = subprocess.CompletedProcess(['agy', 'models'], 0, 'gemini-7-pro-high\tGemini Pro (High)\n', '')
            tokens = ['assess', '--caller', 'codex'] + (['--discover-models'] if discover else [])
            with self.subTest(discover=discover), patch.dict(os.environ, {}, clear=True), \
                 patch.object(harness, '_find_binary', side_effect=lambda name: 'fixture-' + name), \
                 patch.object(harness, '_codex_home', return_value=(homes['codex'], False)), \
                 patch.object(harness, '_agy_home', return_value=(homes['agy'], False)), \
                 patch.object(harness, '_run_agy_listing', return_value=response) as listing, \
                 patch.object(Path, 'unlink', side_effect=AssertionError('assess 不得删除缓存')) as unlink:
                code, report = self.capture(harness.cmd_assess, self.args(*tokens))
                unlink.assert_not_called()
            self.assertEqual(code, 0)
            self.assertEqual(listing.call_count, int(discover))
            self.assertEqual(harness.snapshot_files(self.root), before)
            self.assertTrue(all(info['quota_cooldown'] is None for info in report['backends'].values()))

    def test_audit_snapshot_failure_retains_completed_units_and_all_findings(self):
        (self.root / 'a.py').write_text('x = 1\n' * 50, encoding='utf-8')
        common = {'file': 'a.py', 'line_start': 2, 'line_end': 2, 'severity': 'high',
                  'category': 'conservation', 'description': '守恒残差异常', 'evidence': 'residual != 0'}
        single = {**common, 'line_start': 40, 'line_end': 40, 'description': '边界通量异常'}

        def audit(engine, *args, **kwargs):
            return {**AUDIT, 'summary': engine, 'findings': [common, single] if engine.startswith('codex:') else [common]}

        args = self.args('audit', '--target', str(self.root), '--caller', 'antigravity',
                         '--left', 'codex:default', '--right', 'agy:default')
        denied = harness.HarnessError('事后文件不可读', snapshot_path='a.py')
        with patch.object(harness, '_args_backends', return_value=backends()), \
             patch.object(harness, 'invoke_readonly_engine', side_effect=audit), \
             patch.object(harness, 'snapshot_files', side_effect=[{}, denied]) as snapshot:
            code, report = self.capture(harness.cmd_audit, args)
        self.assertEqual(code, 1)
        self.assertEqual(snapshot.call_count, 2)
        self.assertEqual(report['status'], 'failed')
        self.assertIs(report['scope_verified'], False)
        self.assertEqual(report['error'], '事后文件不可读')
        self.assertEqual(report['scope_error_path'], 'a.py')
        self.assertEqual(report['completed_count'], 2)
        self.assertEqual(report['received_count'], 2)
        self.assertEqual(len(report['completed_units']), 2)
        self.assertTrue(all(unit['read_files'] == ['a.py'] and not unit['unread_files'] for unit in report['completed_units']))
        self.assertEqual(report['consensus_count'], 1)
        self.assertEqual(report['single_source_count'], 1)
        self.assertEqual(report['consensus_findings'][0]['left_finding'], common)
        self.assertEqual(report['consensus_findings'][0]['right_finding'], common)
        self.assertEqual(report['single_source_findings'][0]['description'], single['description'])

    def test_audit_snapshot_failure_preserves_terminal_failure_statuses(self):
        (self.root / 'a.py').write_text('x = 1\n', encoding='utf-8')
        args = self.args('audit', '--target', str(self.root), '--caller', 'antigravity', '--single')
        for status in ('quota_exhausted', 'cancelled', 'scope_violation'):
            with self.subTest(status=status), patch.object(harness, '_args_backends', return_value=backends()), \
                 patch.object(harness, 'invoke_readonly_engine', side_effect=harness.HarnessError('worker stopped', status)), \
                 patch.object(harness, 'snapshot_files', side_effect=[{}, PermissionError('snapshot denied')]):
                code, report = self.capture(harness.cmd_audit, args)
            self.assertEqual(code, harness.EXIT_CODES[status])
            self.assertEqual(report['status'], status)
            self.assertIs(report['scope_verified'], False)
            self.assertEqual(report['error'], 'snapshot denied')

    def test_preflight_fallback_stops_when_second_preparation_changes_workspace(self):
        for subcommand in ('audit', 'gate', 'task'):
            workspace = self.root / subcommand
            workspace.mkdir()
            (workspace / 'a.py').write_text('x = 1\n', encoding='utf-8')
            calls = []

            def prepare(engines, specs, batch, **kwargs):
                calls.append(specs)
                if len(calls) == 1:
                    raise harness.HarnessError('quota', 'quota_exhausted', quota_backend='codex', preflight_blocked=True)
                (workspace / 'unexpected.py').write_text('changed during refresh', encoding='utf-8')

            tokens = [subcommand, '--target', str(workspace), '--caller', 'antigravity', '--fallback-on-quota']
            if subcommand != 'audit':
                tokens.extend(['--task', '修改代码', '--verify-cmd', 'verify'])
            if subcommand == 'task':
                tokens.extend(['--kind', 'code'])
            handler = {'audit': harness.cmd_audit, 'gate': harness.cmd_gate, 'task': harness.cmd_task}[subcommand]
            with self.subTest(subcommand=subcommand), patch.object(harness, '_args_backends', return_value=backends()), \
                 patch.object(harness, '_prepare_execution', side_effect=prepare), \
                 patch.object(harness, 'invoke_readonly_engine') as auditor, \
                 patch.object(harness, 'invoke_edit_worker') as worker, \
                 patch.object(harness, 'invoke_engine') as invoke, patch.object(harness, 'run_process') as runner:
                code, report = self.capture(handler, self.args(*tokens))
            self.assertEqual(code, 1)
            self.assertEqual(report['status'], 'failed')
            self.assertIn('回退预检后工作区发生变化或无法核对', report['error'])
            self.assertEqual(len(calls), 2)
            self.assertTrue(any(spec and spec.startswith('agy:') for spec in calls[1]))
            auditor.assert_not_called()
            worker.assert_not_called()
            invoke.assert_not_called()
            runner.assert_not_called()
            if subcommand != 'audit':
                self.assertIs(report['verification_executed'], False)

    def test_windows_env_validates_names_before_querying_and_prefers_powershell(self):
        for name in ('', '1BAD', 'TEMP&whoami', 'TEMP|echo', "TEMP'", 'TEMP\n'):
            with self.subTest(name=name), patch.object(harness.shutil, 'which') as which, \
                 patch.object(harness, '_local_command') as query:
                with self.assertRaises(harness.HarnessError):
                    harness._windows_env(name)
                which.assert_not_called()
                query.assert_not_called()
        powershell = str(self.root / 'powershell.exe')
        for value in ('C:\\folder & name|literal%value%', '%literal-start'):
            with self.subTest(value=value), patch.object(harness.shutil, 'which', return_value=powershell) as which, \
                 patch.object(harness, '_local_command', return_value=value) as query:
                self.assertEqual(harness._windows_env('TEMP'), value)
                which.assert_called_once_with('powershell.exe')
                query.assert_called_once_with([powershell, '-NoProfile', '-NonInteractive', '-Command',
                                               "[Environment]::GetEnvironmentVariable('TEMP')"], cwd=str(self.root))

    def test_windows_env_cmd_fallback_and_undefined_values(self):
        cmd_exe = str(self.root / 'cmd.exe')
        for value in ('C:\\temp', '%TEMP%', ''):
            with self.subTest(value=value), \
                 patch.object(harness.shutil, 'which', side_effect=lambda name: cmd_exe if name == 'cmd.exe' else None), \
                 patch.object(harness, '_local_command', return_value=value) as query:
                if value == 'C:\\temp':
                    self.assertEqual(harness._windows_env('TEMP'), value)
                else:
                    with self.assertRaises(harness.HarnessError):
                        harness._windows_env('TEMP')
                query.assert_called_once_with([cmd_exe, '/d', '/c', 'echo', '%TEMP%'], cwd=str(self.root))
        with patch.object(harness.shutil, 'which', return_value='powershell.exe'), \
             patch.object(harness, '_local_command', return_value=''), self.assertRaises(harness.HarnessError):
            harness._windows_env('UNDEFINED')

    def test_nested_structured_quota_errors_are_detected_for_all_error_events(self):
        error = {'message': 'usage limit reached', 'code': 'insufficient_quota'}
        events = (
            {'event': 'result', 'result': {'status': 'ERROR', 'error': error}},
            {'event': 'error', 'result': {'error': error}},
            {'type': 'turn.failed', 'error': {'result': error}},
        )
        for index, event in enumerate(events):
            engines = backends()
            engines['agy']['config_home'] = str(self.root / ('quota-' + str(index)))
            proc = subprocess.CompletedProcess([], 1, json.dumps({'event': 'init'}) + '\n' + json.dumps(event), 'warning: optional cache missing')
            detail = harness._backend_diagnostics(proc)
            self.assertIn('usage limit reached', detail)
            self.assertIn('insufficient_quota', detail)
            with self.subTest(event=event), patch.object(harness, 'run_process', return_value=proc), \
                 self.assertRaises(harness.HarnessError) as caught:
                harness.invoke_engine('agy:default', self.root, '审查', engines, harness.TASK_JSON_SCHEMA)
            self.assertEqual(caught.exception.status, 'quota_exhausted')
            self.assertIsNotNone(harness._load_active_quota_cooldown('agy', engines['agy'], cleanup_expired=False))

    def test_structured_nonquota_errors_override_unrelated_quota_warning_text(self):
        for kind in ('error', 'result', 'turn.failed'):
            event = {'event': kind, 'result': {'status': 'ERROR', 'error': {'message': 'connection reset'},
                                             'structured_output': {**TASK, 'content': 'usage limit reached'}}}
            output = 'report mentions usage limit reached\n' + json.dumps(event)
            proc = subprocess.CompletedProcess([], 1, output, 'warning: documentation mentions usage limit')
            with self.subTest(kind=kind), patch.object(harness, 'run_process', return_value=proc), \
                 patch.object(harness, '_record_quota_exhausted') as record, self.assertRaises(harness.HarnessError) as caught:
                harness.invoke_engine('agy:default', self.root, '审查', backends(), harness.TASK_JSON_SCHEMA)
            self.assertEqual(caught.exception.status, 'failed')
            record.assert_not_called()
        for index, (stdout, stderr) in enumerate((('', 'usage limit reached'), ('usage limit reached', ''))):
            proc = subprocess.CompletedProcess([], 1, stdout, stderr)
            engines = backends()
            engines['agy']['config_home'] = str(self.root / ('unstructured-' + str(index)))
            with self.subTest(stdout=stdout, stderr=stderr), patch.object(harness, 'run_process', return_value=proc), \
                 self.assertRaises(harness.HarnessError) as caught:
                harness.invoke_engine('agy:default', self.root, '审查', engines, harness.TASK_JSON_SCHEMA)
            self.assertEqual(caught.exception.status, 'quota_exhausted')
            self.assertIs(caught.exception.details['preflight_blocked'], False)

    def test_code_task_without_verify_command_explicitly_reports_unverified(self):
        args = self.args('task', '--target', str(self.root), '--caller', 'antigravity', '--kind', 'code', '--task', '修改代码')
        with patch.object(harness, '_args_backends', return_value=backends()), \
             patch.object(harness, 'invoke_engine', return_value=TASK) as worker, \
             patch.object(harness, 'run_process') as verifier:
            code, report = self.capture(harness.cmd_task, args)
        self.assertEqual(code, 0)
        self.assertEqual(report['result'], TASK)
        self.assertIs(report['verification_executed'], False)
        self.assertEqual(report['verification_notice'], '未提供 --verify-cmd，本次代码修改未经宿主机命令实测验证，请总控运行验证后再验收。')
        worker.assert_called_once()
        verifier.assert_not_called()

    def test_readonly_tasks_do_not_receive_code_gate_notices(self):
        for kind in ('math', 'direction', 'review', 'polish'):
            args = self.args('task', '--target', str(self.root), '--caller', 'antigravity', '--kind', kind, '--task', '只读核对')
            with self.subTest(kind=kind), patch.object(harness, '_args_backends', return_value=backends()), \
                 patch.object(harness, 'invoke_engine', return_value=TASK):
                code, report = self.capture(harness.cmd_task, args)
            self.assertEqual(code, 0)
            self.assertNotIn('verification_executed', report)
            self.assertNotIn('verification_notice', report)

    def test_gate_and_code_task_report_real_verifier_execution_on_success_and_failure(self):
        for subcommand in ('gate', 'task'):
            for exit_code in (0, 1):
                marker = self.root / ('verified_' + subcommand + str(exit_code) + '.txt')
                command = (f'"{sys.executable}" -B -c "from pathlib import Path; '
                           f"Path('{marker.name}').write_text('executed', encoding='utf-8'); raise SystemExit({exit_code})\"")
                tokens = [subcommand, '--target', str(self.root), '--caller', 'antigravity', '--task', '修改代码',
                          '--verify-cmd', command, '--max-rounds', '1']
                if subcommand == 'task':
                    tokens.extend(['--kind', 'code'])
                handler = harness.cmd_gate if subcommand == 'gate' else harness.cmd_task
                with self.subTest(subcommand=subcommand, exit_code=exit_code), \
                     patch.object(harness, '_args_backends', return_value=backends()), \
                     patch.object(harness, 'invoke_edit_worker', return_value='代码修改完成'):
                    code, report = self.capture(handler, self.args(*tokens))
                self.assertEqual(code, 0 if exit_code == 0 else harness.EXIT_CODES['verification_failed'])
                self.assertEqual(marker.read_text(encoding='utf-8'), 'executed')
                self.assertIs(report['verification_executed'], True)
                self.assertIs(report['history'][0]['verification_executed'], True)
                self.assertEqual(report['history'][0]['verify_exit_code'], exit_code)
                self.assertEqual(report['gate_passed'], exit_code == 0)
                self.assertNotIn('verification_notice', report)

    def test_gate_and_code_task_report_no_verification_before_worker_success(self):
        for subcommand in ('gate', 'task'):
            for phase in ('dry_run', 'preflight', 'fallback_preflight', 'worker'):
                tokens = [subcommand, '--target', str(self.root), '--caller', 'antigravity', '--task', '修改代码',
                          '--verify-cmd', 'verify', '--max-rounds', '1']
                if subcommand == 'task':
                    tokens.extend(['--kind', 'code'])
                if phase == 'dry_run':
                    tokens.append('--dry-run')
                if phase == 'fallback_preflight':
                    tokens.append('--fallback-on-quota')
                handler = harness.cmd_gate if subcommand == 'gate' else harness.cmd_task
                failure = harness.HarnessError('preflight failed')
                preparation = ([harness.HarnessError('quota', 'quota_exhausted', quota_backend='codex'), failure]
                               if phase == 'fallback_preflight' else failure if phase == 'preflight' else None)
                with self.subTest(subcommand=subcommand, phase=phase), \
                     patch.object(harness, '_args_backends', return_value=backends()), \
                     patch.object(harness, '_prepare_execution', side_effect=preparation), \
                     patch.object(harness, 'invoke_edit_worker', side_effect=harness.HarnessError('worker failed')) as worker, \
                     patch.object(harness, 'run_process') as verifier:
                    code, report = self.capture(handler, self.args(*tokens))
                self.assertEqual(code, 0 if phase == 'dry_run' else 1)
                self.assertIs(report['verification_executed'], False)
                verifier.assert_not_called()
                if phase == 'worker':
                    worker.assert_called_once()
                    self.assertIs(report['history'][0]['verification_executed'], False)
                    self.assertFalse(report['gate_passed'])
                else:
                    worker.assert_not_called()

    def test_gate_verification_flag_survives_verifier_errors_and_snapshot_failures(self):
        before = harness.snapshot_files(self.root)
        for error in (RuntimeError('verifier failed'), harness.HarnessError('quota during verification', 'quota_exhausted')):
            verifier = Mock(side_effect=error)
            with self.subTest(error=str(error)):
                result = harness.run_physics_gate_loop(self.root, '修改代码', 'verify', Mock(return_value='done'), verifier, max_rounds=1)
            verifier.assert_called_once()
            self.assertIs(result['verification_executed'], True)
            self.assertIs(result['history'][0]['verification_executed'], True)
            self.assertIsNone(result['history'][0]['verify_exit_code'])
            self.assertFalse(result['gate_passed'])
            self.assertFalse(harness._gate_can_replay(result, before, self.root))
        for snapshots, expected in (([PermissionError('before denied')], False),
                                    ([{}, PermissionError('worker denied')], False),
                                    ([{}, {}, PermissionError('verifier denied')], True)):
            with self.subTest(expected=expected, snapshots=len(snapshots)), \
                 patch.object(harness, 'snapshot_files', side_effect=snapshots):
                result = harness.run_physics_gate_loop(self.root, '修改代码', 'verify', Mock(return_value='done'), Mock(return_value=(0, 'ok')))
            self.assertEqual(result['status'], 'failed')
            self.assertIs(result['verification_executed'], expected)
            self.assertFalse(result['scope_verified'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
