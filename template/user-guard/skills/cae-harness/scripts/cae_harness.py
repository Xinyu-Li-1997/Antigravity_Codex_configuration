#!/usr/bin/env python3
"""Codex / Antigravity 双向任务、分片审计与可执行验收。"""
import argparse
import base64
import concurrent.futures
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

for _name in ('stdin', 'stdout', 'stderr'):
    _stream = getattr(sys, _name, None)
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace')

SOURCE_EXTENSIONS = {'.f90', '.f95', '.f03', '.f08', '.f', '.for', '.c', '.cpp', '.cc', '.cxx', '.cu', '.cuh',
                     '.h', '.hpp', '.hh', '.py', '.jl', '.m', '.rs', '.go', '.ts', '.js', '.cmake', '.toml',
                     '.yaml', '.yml', '.json', '.md', '.tex', '.bib', '.rst', '.txt', '.lean', '.inc'}
IGNORED_DIRS = {'.git', '.hg', '.svn', '__pycache__', '.pytest_cache', 'node_modules', '.venv', 'venv',
                'env', 'build', 'dist', 'target', '.idea', '.vscode', '.gemini', '.antigravity-ide'}
EXIT_CODES = {'ok': 0, 'failed': 1, 'scope_violation': 1, 'verification_failed': 2,
              'partial': 3, 'invalid_output': 4, 'quota_exhausted': 5, 'timeout': 124, 'cancelled': 130}
QUOTA_ERROR_RE = re.compile(
    r"(?:hit your usage limit|usage limit|insufficient_quota|exceeded your current quota|"
    r"purchase more credits|billing_hard_limit_reached|quota[ _-]?exceeded|quota[ _-]?exhausted|"
    r"out of quota|out of credits|resource_exhausted|daily limit exceeded)",
    re.IGNORECASE,
)
EFFORT_LADDER = ('minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
TIER_CHOICES = ('auto', 'ultra', 'max', 'xhigh', 'high', 'medium', 'low')
CATALOG_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
NEGATION_PREFIXES = ('不', '别', '勿', '莫', '非', '禁', '无须', '无需', 'not ', 'no ')
HIGH_COMPLEXITY_CUES = (
    '守恒', '通量', '黎曼', 'riemann', '雅可比', 'jacobian', '熵', 'entropy',
    'fvm', 'fem', 'fdm', 'dg', 'dgsem', 'weno', '高阶格式', '通量重构',
    '有限体积', '有限元', '谱元', '间断伽辽金', '重构', '收敛阶', '稳定性', 'cfl', '边界条件',
    '特征分解', '证明', '定理', '引理', '推导', '变分', '根因', '死锁', '竞态', '并发',
    '跨模块', '架构', '数值耗散', '激波',
)
TRIVIAL_SURFACE_CUES = ('错别字', 'typo', '拼写', '缩进', '空行', '注释微调', '文案微调', '格式微调')
AUDIT_JSON_SCHEMA = {
    'type': 'object', 'properties': {
        'summary': {'type': 'string'},
        'read_files': {'type': 'array', 'items': {'type': 'string'}},
        'unread_files': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'file': {'type': 'string'}, 'reason': {'type': 'string'}},
            'required': ['file', 'reason'], 'additionalProperties': False}},
        'findings': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'file': {'type': 'string'}, 'line_start': {'type': 'integer', 'minimum': 1},
            'line_end': {'type': 'integer', 'minimum': 1},
            'severity': {'type': 'string', 'enum': ['critical', 'high', 'medium', 'low']},
            'category': {'type': 'string'}, 'description': {'type': 'string'}, 'evidence': {'type': 'string'},
        }, 'required': ['file', 'line_start', 'line_end', 'severity', 'category', 'description', 'evidence'],
        'additionalProperties': False}},
    }, 'required': ['summary', 'findings', 'read_files', 'unread_files'], 'additionalProperties': False,
}
TASK_JSON_SCHEMA = {
    'type': 'object', 'properties': {'summary': {'type': 'string'}, 'content': {'type': 'string'},
        'evidence': {'type': 'array', 'items': {'type': 'string'}},
        'unverified': {'type': 'array', 'items': {'type': 'string'}}},
    'required': ['summary', 'content', 'evidence', 'unverified'], 'additionalProperties': False,
}
_CANCELLED = threading.Event()
_EVENT_LOCK = threading.Lock()
_SESSION_QUOTA_BLOCKED: Dict[str, Dict[str, Any]] = {}


class HarnessError(RuntimeError):
    def __init__(self, message, status='failed', **details):
        super().__init__(message)
        self.status = status
        self.details = details


class OutputParseError(HarnessError):
    def __init__(self, message):
        super().__init__(message, 'invalid_output')


def emit_event(event, **fields):
    with _EVENT_LOCK:
        print(json.dumps({'event': event, **fields}, ensure_ascii=False), file=sys.stderr, flush=True)


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}


def _top_level_toml(path):
    try:
        text = path.read_text(encoding='utf-8-sig')
    except OSError:
        return {}
    result = {}
    for line in text.splitlines():
        if line.lstrip().startswith('['):
            break
        match = re.match(r'\s*(model|model_reasoning_effort|model_context_window|model_auto_compact_token_limit)\s*=\s*(.*)', line)
        if match:
            raw = match.group(2).strip()
            if raw.startswith(('"', "'")):
                result[match.group(1)] = raw[1:raw.find(raw[0], 1)]
            else:
                number = re.match(r'\d+', raw)
                if number:
                    result[match.group(1)] = int(number.group())
    return result


def _positive_int(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _local_command(args, cwd=None):
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
    if proc.returncode:
        raise HarnessError(f'本地查询失败: {args[0]}')
    return proc.stdout.strip()


def _wsl_path(path, windows=True):
    return _local_command(['wslpath', '-w' if windows else '-u', str(path)])


def _windows_env(var_name):
    cmd_exe = shutil.which('cmd.exe')
    if not cmd_exe:
        raise HarnessError('未发现 cmd.exe，无法解析 Windows 环境变量')
    parent = Path(cmd_exe).parent
    value = _local_command(['cmd.exe', '/d', '/c', 'echo', f'%{var_name}%'], cwd=str(parent) if parent.is_dir() else None)
    if not value or value.startswith('%'):
        raise HarnessError(f'无法解析 Windows 环境变量 {var_name}')
    return value


def _is_posix():
    return os.name != 'nt'


def _find_windows_agy_in_wsl():
    if not (_is_posix() and shutil.which('wslpath')):
        return None
    found_exe = shutil.which('agy.exe')
    if found_exe:
        return found_exe
    if shutil.which('cmd.exe'):
        try:
            win_home = Path(_wsl_path(_windows_env('USERPROFILE'), windows=False))
            for candidate in (win_home / '.gemini' / 'bin' / 'agy.exe', win_home / '.local' / 'bin' / 'agy.exe'):
                if candidate.is_file():
                    return str(candidate)
        except (HarnessError, OSError):
            pass
    return None


def _find_binary(name):
    override = os.environ.get('CAE_' + name.upper() + '_BINARY')
    if override:
        candidate = Path(override).expanduser()
        found = shutil.which(override) or (str(candidate) if candidate.is_file() else None)
        if not found:
            raise HarnessError(f'指定的 {name} 程序不存在')
        return found
    found = shutil.which(name)
    if found and Path(found).suffix.lower() not in ('.cmd', '.bat'):
        return found
    home = Path.home()
    candidates = [home / '.gemini' / 'bin' / ('agy' if _is_posix() else 'agy.exe')] if name == 'agy' else []
    if name == 'codex' and not _is_posix():
        root = Path(os.environ.get('LOCALAPPDATA', '')) / 'OpenAI' / 'Codex' / 'bin'
        if root.is_dir():
            candidates.extend(sorted(root.rglob('codex.exe'), key=lambda p: p.stat().st_mtime, reverse=True))
    candidates.append(home / '.local' / 'bin' / (name if _is_posix() else name + '.exe'))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    if name == 'codex' and _is_posix() and shutil.which('wslpath'):
        found_exe = shutil.which('codex.exe')
        if found_exe:
            return found_exe
        if shutil.which('cmd.exe'):
            try:
                local_app = Path(_wsl_path(_windows_env('LOCALAPPDATA'), windows=False))
                root = local_app / 'OpenAI' / 'Codex' / 'bin'
                if root.is_dir():
                    for candidate in sorted(root.rglob('codex.exe'), key=lambda p: p.stat().st_mtime, reverse=True):
                        if candidate.is_file():
                            return str(candidate)
            except (HarnessError, OSError):
                pass
    if name == 'agy':
        win_agy = _find_windows_agy_in_wsl()
        if win_agy:
            return win_agy
    return None


def _codex_home(binary):
    interop = bool(binary and _is_posix() and binary.lower().endswith('.exe'))
    if os.environ.get('CAE_CODEX_HOME'):
        return Path(os.environ['CAE_CODEX_HOME']).expanduser(), interop
    if interop:
        for parent in Path(binary).parents:
            if parent.name.lower() == 'appdata':
                return parent.parent / '.codex', True
        windows_home = _windows_env('USERPROFILE')
        return Path(_wsl_path(Path(windows_home), windows=False)) / '.codex', True
    return Path(os.environ.get('CODEX_HOME') or str(Path.home() / '.codex')).expanduser(), False


def _agy_home(binary):
    interop = bool(binary and _is_posix() and binary.lower().endswith('.exe'))
    if os.environ.get('CAE_AGY_HOME'):
        return Path(os.environ['CAE_AGY_HOME']).expanduser(), interop
    if interop:
        for parent in Path(binary).parents:
            if parent.name.lower() == '.gemini':
                return parent, True
        windows_home = _windows_env('USERPROFILE')
        return Path(_wsl_path(Path(windows_home), windows=False)) / '.gemini', True
    return Path.home() / '.gemini', False


def _model_records(cache):
    items = cache.get('models', []) if isinstance(cache, dict) else cache
    if not isinstance(items, list):
        return []
    fields = ('slug', 'display_name', 'description', 'priority', 'visibility', 'default_reasoning_level',
              'context_window', 'max_context_window', 'effective_context_window_percent')
    records = []
    for item in items:
        if not isinstance(item, dict) or not item.get('slug'):
            continue
        rec = {key: item[key] for key in fields if key in item}
        levels = item.get('supported_reasoning_levels')
        if isinstance(levels, list):
            efforts = [lvl.get('effort') for lvl in levels if isinstance(lvl, dict) and lvl.get('effort') in EFFORT_LADDER]
            if efforts:
                rec['supported_efforts'] = efforts
            delegating = [
                lvl.get('effort')
                for lvl in levels
                if isinstance(lvl, dict)
                and lvl.get('effort') in EFFORT_LADDER
                and ('delegation' in str(lvl.get('description') or '').lower() or lvl.get('effort') == 'ultra')
            ]
            if delegating:
                rec['delegating_efforts'] = delegating
        elif isinstance(item.get('supported_efforts'), list):
            efforts = [e for e in item['supported_efforts'] if e in EFFORT_LADDER]
            rec['supported_efforts'] = efforts
            delegating = [e for e in efforts if e == 'ultra']
            if delegating:
                rec['delegating_efforts'] = delegating
        records.append(rec)
    return records


def _parse_agy_models(output):
    models = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or '\t' not in line:
            continue
        slug, display_name = line.split('\t', 1)
        slug, display_name = slug.strip(), display_name.strip()
        if slug:
            models.append({'slug': slug, 'display_name': display_name})
    return models


def _agy_slug_tier(slug):
    match = re.match(r'^(.+)-(minimal|low|medium|high|xhigh|max|ultra)$', str(slug or '').strip().lower())
    return (match.group(1), match.group(2)) if match else ('', '')


def _agy_display_tier(label):
    match = re.match(r'^(.+?)\s*\((Minimal|Low|Medium|High|XHigh|Max|Ultra)\)\s*$', str(label or '').strip(), re.IGNORECASE)
    if not match:
        return '', ''
    family = re.sub(r'[^a-z0-9.]+', '-', match.group(1).lower()).strip('-')
    return family, match.group(2).lower()


def _select_codex_frontier_record(models, configured_model=None):
    visible = [item for item in models if item.get('visibility') != 'hide'] or list(models)
    if any('priority' in item for item in visible):
        return min(visible, key=lambda item: (_positive_int(item.get('priority')) or 9999, 0))
    if configured_model:
        matched = next((item for item in visible if item.get('slug') == configured_model), None)
        if matched:
            return matched
    return visible[0] if visible else {}


def _select_codex_specialty_record(models, configured_model=None, kind='review', tier='high', prompt_text='', estimated_tokens=0):
    visible = [
        item for item in models
        if item.get('visibility') != 'hide' and 'auto-review' not in str(item.get('slug') or '').lower()
    ] or list(models)
    if not visible:
        return {}, 'cli_default'
    if tier != 'ultra':
        eligible = [item for item in visible if not _only_delegating_efforts(item, tier)]
        visible = eligible or visible
    workhorse = _select_codex_frontier_record(visible, configured_model)
    current_gen = [
        item for item in visible
        if not str(item.get('description') or '').strip().lower().startswith('older ')
    ] or visible
    ordered_current = sorted(current_gen, key=lambda item: (_positive_int(item.get('priority')) or 9999, 0))

    if tier == 'low' and not _has_negated_cue(prompt_text or '', ('轻量', '极速', 'lite', 'mini', 'fast', 'flash')):
        fast_rec = next(
            (
                item for item in ordered_current
                if re.search(
                    r'\b(?:fast and affordable|easier tasks|lightweight|mini|lite|flash)\b',
                    f"{item.get('slug', '')} {item.get('description', '')}",
                    re.IGNORECASE,
                )
            ),
            None,
        )
        if fast_rec:
            return fast_rec, 'specialty_fast_lightweight'

    wants_deep = kind in ('math', 'direction') or (
        kind == 'review'
        and tier in ('max', 'xhigh', 'ultra')
        and _has_positive_cue(prompt_text or '', ('证明', '定理', '引理', '推导', '守恒', '熵', '收敛阶', '稳定性'))
    )
    if wants_deep:
        deep_rec = next(
            (
                item for item in ordered_current[:4]
                if re.search(
                    r'\b(?:most demanding|frontier intelligence|deepest reasoning|hardest problems)\b',
                    str(item.get('description') or ''),
                    re.IGNORECASE,
                )
            ),
            None,
        )
        if deep_rec:
            deep_ctx = _positive_int(deep_rec.get('context_window'))
            if not deep_ctx or estimated_tokens <= int(deep_ctx * 0.75):
                return deep_rec, 'specialty_deep_reasoning'

    return workhorse, ('frontier_catalog' if workhorse.get('slug') else 'cli_default')


def _match_effort_level(supported, target_tier, delegating_efforts=None):
    valid = [item for item in supported if item in EFFORT_LADDER]
    if not valid:
        return None
    if target_tier != 'ultra':
        blocked = set(delegating_efforts or ()) | {'ultra'}
        non_delegating = [item for item in valid if item not in blocked]
        if not non_delegating:
            return None
        valid = non_delegating
    if target_tier in ('max', 'ultra'):
        return max(valid, key=EFFORT_LADDER.index)
    target_rank = EFFORT_LADDER.index(target_tier)
    at_or_below = [item for item in valid if EFFORT_LADDER.index(item) <= target_rank]
    return max(at_or_below, key=EFFORT_LADDER.index) if at_or_below else min(valid, key=EFFORT_LADDER.index)


def _only_delegating_efforts(record, tier):
    efforts = record.get('supported_efforts') or []
    return bool(efforts and tier != 'ultra' and not _match_effort_level(efforts, tier, record.get('delegating_efforts')))


def _codex_profile(record, slug, tier, source, fallback_effort=None, fallback_source='unknown'):
    effort = _match_effort_level(record.get('supported_efforts', []), tier, record.get('delegating_efforts'))
    if effort:
        effort_source = 'catalog_declared'
    elif _only_delegating_efforts(record, tier):
        effort_source = 'no_non_delegating_effort'
    else:
        effort = fallback_effort
        effort_source = fallback_source if effort else 'unknown'
    return {'spec': f'codex:{slug}' if slug else 'codex:default', 'model': slug or None,
            'reasoning_effort': effort, 'cli_effort': effort, 'tier': tier,
            'profile_source': source, 'effort_source': effort_source}


def _require_execution_profile(profile):
    if profile['effort_source'] == 'no_non_delegating_effort':
        raise HarnessError('当前选路未找到可用的非委派挡位，请指定其他模型或显式选择委派挡位',
                           execution_profile=profile)


def _has_negated_cue(text, cues):
    lowered = (text or '').lower()
    for cue in cues:
        target = cue.lower()
        is_ascii = target.isascii() and target.isalpha()
        start = 0
        while True:
            idx = lowered.find(target, start)
            if idx < 0:
                break
            end = idx + len(target)
            boundary_ok = (not is_ascii) or (
                (idx == 0 or not lowered[idx - 1].isalpha())
                and (end == len(lowered) or not lowered[end].isalpha())
            )
            prefix = lowered[max(0, idx - 4):idx]
            if boundary_ok and any(neg in prefix for neg in NEGATION_PREFIXES):
                return True
            start = end
    return False


def _has_positive_cue(text, cues):
    lowered = text.lower()
    for cue in cues:
        target = cue.lower()
        start = 0
        while True:
            idx = lowered.find(target, start)
            if idx < 0:
                break
            prefix = lowered[max(0, idx - 4):idx]
            if not any(neg in prefix for neg in NEGATION_PREFIXES):
                return True
            start = idx + len(target)
    return False


def select_task_tier(requested_tier='auto', kind='review', text='', file_count=1, estimated_tokens=0):
    """按任务性质与输入复杂度智能分级思考挡位：攻坚拉满单模型纯思考最高挡(max)，常规任务用 high，短润色用 medium，表面检查用 low。"""
    if requested_tier and requested_tier != 'auto':
        return requested_tier, 'explicit_tier_flag'
    sample = (text or '').strip()
    lowered = sample.lower()
    if any(re.search(r'(?<![a-z])ultra(?![a-z])', clause) and not re.search(
            r'(?:不(?:要|用)?|别|勿|无需|禁止|\bnot\b|\bno\b)[\s\w]{0,8}ultra', clause)
           for clause in re.split(r'[，。；、,;.!?！？\n]', lowered)):
        return 'ultra', 'user_description_ultra'
    if _has_positive_cue(sample, ('最大档', '最大挡', '最高档', '最高挡', '最先进', '顶配', '满血', '深度推理', 'max档', 'max挡')):
        return 'max', 'user_description_max'
    if _has_positive_cue(sample, ('超高档', '超高挡', '次顶档', '次顶挡', 'xhigh')):
        return 'xhigh', 'user_description_xhigh'
    if _has_positive_cue(sample, ('低档', '低挡', '最低档', '最低挡', '省额度', '省token', '省钱', '快速扫', '快扫', '快速看', '简单看', '轻量档', '轻量模型', '极速', 'low档', 'low挡')) or re.search(r'(?<![a-z])(?:lite|mini|fast|flash)(?![a-z])', lowered):
        return 'low', 'user_description_low'
    if _has_positive_cue(sample, ('中档', '中挡', '中等档', '中等挡', '常规档', '均衡档', 'medium档', 'medium挡')):
        return 'medium', 'user_description_medium'
    if _has_positive_cue(sample, ('高档', '高挡', '次高档', '次高挡', 'high档', 'high挡')):
        return 'high', 'user_description_high'
    if kind == 'polish' and not _has_positive_cue(sample, ('证明', '推导', '定理', '引理', '根因')):
        if file_count <= 3 and estimated_tokens <= 4_000 and len(sample) <= 2_500:
            return 'medium', 'complexity_lightweight_polish'
        return 'high', 'complexity_long_polish'
    if (kind in ('review', 'code', 'audit') and file_count <= 1 and estimated_tokens <= 2_000
            and _has_positive_cue(sample, TRIVIAL_SURFACE_CUES)
            and not _has_positive_cue(sample, ('证明', '推导', '根因', '死锁', '竞态', '守恒'))):
        return 'low', 'complexity_trivial_surface_task'
    if kind in ('math', 'direction'):
        return 'max', f'task_specialty_{kind}_max'
    if _has_positive_cue(sample, HIGH_COMPLEXITY_CUES) or file_count >= 8 or estimated_tokens >= 30_000:
        return 'max', 'complexity_high_domain_max'
    env_tier = os.environ.get('CAE_DEFAULT_TIER', '').strip().lower()
    if env_tier in TIER_CHOICES and env_tier not in ('auto', 'ultra'):
        return env_tier, 'env_default_tier'
    return 'high', 'task_routine_high'


ROUTER_MODEL_TAGS = {'orchestrator', 'router', 'auto'}
FAST_MODEL_TAGS = {'lite', 'mini', 'fast', 'flash', 'hydrogen'}
NON_AI_MODEL_STOPWORDS = {
    'turbulence', 'rans', 'les', 'dns', 'sst', 'kepsilon', 'komega', 'spalart', 'allmaras',
    'epsilon', 'navier', 'stokes', 'euler', 'boltzmann', 'bgk', 'muscl', 'tvd', 'godunov',
    'smagorinsky', 'vreman', 'courant', 'knudsen', 'reynolds', 'prandtl', 'mach',
    'weno', 'cweno', 'eno', 'dg', 'dgsem', 'fvm', 'fem', 'fdm', 'cfd', 'cae', 'eos',
    'riemann', 'roe', 'hll', 'hllc', 'hlle', 'ausm', 'rusanov', 'rk', 'bdf', 'cfl',
    'model', 'models', 'default', 'auto', 'cli', 'lite', 'lightweight',
}
PROMPT_MODEL_HINT_PATTERNS = (
    re.compile(
        r'(?:请)?(?:使用|调用|采用|切到|切换至|指定|选用|改用|用)\s*'
        r'((?:混动\s*)?[A-Za-z][A-Za-z0-9._()（） -]{1,40}?|混动(?:路由(?:器)?)?)\s*'
        r'(?:AI\s*)?(?:模型|路由(?:器)?|引擎)',
        re.IGNORECASE,
    ),
    re.compile(r'(?:指定模型|执行模型|目标模型)\s*[:=：]\s*([A-Za-z][A-Za-z0-9._()（）-]{1,40})', re.IGNORECASE),
    re.compile(
        r'(?:请)?(?:使用|调用|采用|切到|切换至|指定|选用|改用|用)\s*'
        r'(?:混动\s*)?(orchestrator(?:\s*[-()（）A-Za-z0-9]+)?)',
        re.IGNORECASE,
    ),
)


PHYSICAL_SUBSTANCE_TAGS = {
    'hydrogen', 'carbon', 'argon', 'oxygen', 'nitrogen', 'helium', 'methane', 'air', 'water', 'soot',
}
PHYSICS_MODEL_CONTEXT_RE = re.compile(
    r'(?:计算|模拟|仿真|求解|离散|预测|分析|爆轰|燃烧|反应|组分|流场|传热|热流|'
    r'detonation|combustion|reaction|species|flow|chemistry|flame)',
    re.IGNORECASE,
)


def _extract_prompt_model_hint(prompt_text):
    text = str(prompt_text or '').strip()
    if not text:
        return ''
    stop_words = NON_AI_MODEL_STOPWORDS - ROUTER_MODEL_TAGS
    for pattern in PROMPT_MODEL_HINT_PATTERNS:
        for match in pattern.finditer(text):
            prefix = text[max(0, match.start() - 4):match.start()].lower()
            if any(neg in prefix for neg in NEGATION_PREFIXES):
                continue
            cand = re.sub(r'\s+', ' ', match.group(1)).strip()
            if not cand:
                continue
            tokens = [tok for tok in re.findall(r'[a-z0-9]+', cand.lower()) if tok]
            collapsed = re.sub(r'[^a-z0-9]+', '', cand.lower())
            if collapsed in stop_words or any(tok in stop_words for tok in tokens):
                continue
            if collapsed in PHYSICAL_SUBSTANCE_TAGS:
                has_ai_qualifier = bool(re.search(r'(?:\bAI\b|\bagy\b|\bcodex\b|\bgemini\b|\borchestrator\b|[()（）])', match.group(0), re.IGNORECASE))
                tail_ctx = text[match.end():min(len(text), match.end() + 24)]
                head_ctx = text[max(0, match.start() - 16):match.start()]
                if not has_ai_qualifier and cand.islower() and (
                    PHYSICS_MODEL_CONTEXT_RE.search(tail_ctx) or PHYSICS_MODEL_CONTEXT_RE.search(head_ctx)
                ):
                    continue
            if tokens or '混动' in cand:
                return cand
    return ''


def _tokens_lower(text):
    return set(re.split(r'[^a-z0-9]+', str(text or '').lower())) - {''}


def _resolve_explicit_codex_record(models, explicit_model):
    q = str(explicit_model or '').strip()
    if not q:
        return {}
    ql = q.lower()
    exact = next((item for item in models if str(item.get('slug') or '').lower() == ql), None)
    if exact:
        return exact
    visible = [
        item for item in models
        if item.get('visibility') != 'hide' and 'auto-review' not in str(item.get('slug') or '').lower()
    ] or list(models)
    ordered = sorted(
        visible,
        key=lambda item: (
            1 if str(item.get('description') or '').strip().lower().startswith('older ') else 0,
            _positive_int(item.get('priority')) or 9999,
            0,
        ),
    )
    q_tokens = [tok for tok in re.findall(r'[a-z0-9.]+', ql) if tok]
    if not q_tokens:
        return {}
    matched = [
        item for item in ordered
        if all(tok in f"{item.get('slug', '')} {item.get('display_name', '')}".lower() for tok in q_tokens)
    ]
    return matched[0] if matched else {}


def _resolve_explicit_agy_model(models, explicit_model, tier, strict_catalog=False):
    q = str(explicit_model or '').strip()
    ql = q.lower()
    exact = next(
        (
            item['slug'] for item in models
            if str(item.get('slug') or '').lower() == ql
            or str(item.get('display_name') or '').lower() == ql
        ),
        '',
    )
    if exact:
        _, slug_tier = _agy_slug_tier(exact)
        cli_effort = None if slug_tier else (tier if tier in ('low', 'medium', 'high', 'xhigh', 'max') and tier != 'max' else None)
        return {'spec': f'agy:{exact}', 'model': exact, 'reasoning_effort': slug_tier or cli_effort,
                'cli_effort': cli_effort, 'tier': tier, 'profile_source': 'explicit_engine_model',
                'effort_source': 'catalog_variant' if slug_tier else ('unverified_request' if cli_effort else 'unknown')}

    # Fuzzy alias resolution (e.g. "carbon", "argon", "hydrogen", "orchestrator", "orchestrator-argon")
    q_clean = re.sub(r'[（）()]', ' ', ql).strip()
    q_tokens = [tok for tok in re.findall(r'[a-z0-9.]+', q_clean) if tok]
    wants_router = bool(set(q_tokens) & ROUTER_MODEL_TAGS) or ('混动' in q)
    families = {}
    router_candidates = []
    for item in models:
        slug = str(item.get('slug') or '')
        label = str(item.get('display_name') or '')
        fam, fam_tier = _agy_slug_tier(slug)
        if fam and fam_tier and not (_tokens_lower(fam) & ROUTER_MODEL_TAGS):
            families.setdefault(fam, {})[fam_tier] = slug
        else:
            router_candidates.append((slug, label))

    explicit_q_tiers = [tok for tok in q_tokens if tok in EFFORT_LADDER]
    fam_q_tokens = [tok for tok in q_tokens if tok not in EFFORT_LADDER] or q_tokens
    if not wants_router and fam_q_tokens:
        fam_hits = [
            fam for fam in families
            if all(tok in fam.lower() for tok in fam_q_tokens)
        ]
        if fam_hits:
            chosen_fam = fam_hits[0]
            target_tier = explicit_q_tiers[-1] if explicit_q_tiers else tier
            best_tier = _match_effort_level(list(families[chosen_fam]), target_tier)
            slug = families[chosen_fam].get(best_tier)
            return {'spec': f'agy:{slug or q}', 'model': slug, 'reasoning_effort': best_tier,
                    'cli_effort': None, 'tier': target_tier, 'profile_source': 'explicit_fuzzy_model',
                    'effort_source': 'catalog_variant' if best_tier else 'no_non_delegating_effort'}

    if q_tokens or wants_router:
        search_tokens = [tok for tok in q_tokens if tok not in ('orchestrator', 'router')] if wants_router else q_tokens
        hits = []
        for slug, label in router_candidates:
            hay = f'{slug} {label}'.lower()
            is_router_item = bool(_tokens_lower(hay) & ROUTER_MODEL_TAGS)
            if wants_router and not is_router_item:
                continue
            if not search_tokens and wants_router and is_router_item:
                hits.append(slug)
            elif search_tokens and all(tok in hay for tok in search_tokens):
                hits.append(slug)
        if hits:
            slug = hits[0]
            return {'spec': f'agy:{slug}', 'model': slug, 'reasoning_effort': None,
                    'cli_effort': None, 'tier': tier, 'profile_source': 'explicit_fuzzy_model', 'effort_source': 'unknown'}

    if strict_catalog:
        return None

    _, slug_tier = _agy_slug_tier(q)
    cli_effort = None if slug_tier else (tier if tier in ('low', 'medium', 'high', 'xhigh', 'max') and tier != 'max' else None)
    return {'spec': f'agy:{q}', 'model': q, 'reasoning_effort': slug_tier or cli_effort,
            'cli_effort': cli_effort, 'tier': tier, 'profile_source': 'explicit_engine_model',
            'effort_source': 'unverified_request' if slug_tier or cli_effort else 'unknown'}


def resolve_execution_profile(backends, spec, tier='high', prompt_text='', kind='review', estimated_tokens=0):
    prefix, explicit_model = _engine_parts(spec)
    info = backends.get(prefix, {})
    models = info.get('models', [])
    if prefix == 'codex':
        requested_effort = tier if tier not in ('max', 'ultra') else None
        cfg_effort = info.get('reasoning_effort')
        if cfg_effort == 'ultra' and tier != 'ultra':
            requested_effort = requested_effort or 'max'
        if explicit_model:
            record = _resolve_explicit_codex_record(models, explicit_model)
            resolved_slug = record.get('slug') or explicit_model
            source = 'explicit_engine_model' if resolved_slug == explicit_model else 'explicit_fuzzy_model'
            return _codex_profile(record, resolved_slug, tier, source, requested_effort, 'unverified_request')
        if models and prompt_text:
            hint = _extract_prompt_model_hint(prompt_text)
            if hint:
                hint_rec = _resolve_explicit_codex_record(models, hint)
                if hint_rec and hint_rec.get('slug'):
                    return _codex_profile(hint_rec, hint_rec['slug'], tier, 'prompt_fuzzy_model',
                                          requested_effort, 'unverified_request')
        record, cat_source = _select_codex_specialty_record(models, info.get('model'), kind, tier, prompt_text, estimated_tokens)
        slug = record.get('slug') or info.get('model') or ''
        source = cat_source if record.get('slug') else ('local_config' if slug else 'cli_default')
        fallback = requested_effort if slug or cfg_effort else None
        fallback_source = 'unverified_request'
        if not fallback and cfg_effort and cfg_effort in EFFORT_LADDER:
            fallback = cfg_effort
            fallback_source = 'local_config'
        return _codex_profile(record, slug, tier, source, fallback, fallback_source)
    if explicit_model:
        return _resolve_explicit_agy_model(models, explicit_model, tier)
    if models and prompt_text:
        hint = _extract_prompt_model_hint(prompt_text)
        if hint:
            hint_prof = _resolve_explicit_agy_model(models, hint, tier, strict_catalog=True)
            if hint_prof is not None:
                hint_prof['profile_source'] = 'prompt_fuzzy_model'
                return hint_prof
    families = {}
    for item in models:
        fam, fam_tier = _agy_slug_tier(item.get('slug', ''))
        if fam and fam_tier and not (_tokens_lower(fam) & ROUTER_MODEL_TAGS):
            families.setdefault(fam, {})[fam_tier] = item['slug']
    if families:
        ordered_names = list(families)
        primary_vendor = ordered_names[0].split('-', 1)[0]
        native_families = [name for name in ordered_names if name.split('-', 1)[0] == primary_vendor]
        native_families = [name for name in native_families if _match_effort_level(families[name], tier)]
        if not native_families:
            return {'spec': 'agy:default', 'model': None, 'reasoning_effort': None,
                    'cli_effort': None, 'tier': tier, 'profile_source': 'frontier_catalog',
                    'effort_source': 'no_non_delegating_effort'}
        is_fast_family = lambda name: bool(_tokens_lower(name) & FAST_MODEL_TAGS)
        non_fast = [name for name in native_families if not is_fast_family(name)] or native_families
        chosen_family = non_fast[0]
        profile_source = 'frontier_catalog'
        fast_negated = _has_negated_cue(prompt_text or '', ('轻量', '极速', 'lite', 'mini', 'fast', 'flash', 'hydrogen', '氢'))
        if tier == 'low' and not fast_negated:
            fast_family = next((name for name in native_families if is_fast_family(name)), '')
            if fast_family:
                chosen_family = fast_family
                profile_source = 'specialty_fast_lightweight'
        elif kind in ('math', 'direction') or tier == 'xhigh' or (
            kind == 'review'
            and tier in ('max', 'xhigh', 'ultra')
            and _has_positive_cue(prompt_text or '', ('证明', '定理', '引理', '推导', '守恒', '熵', '收敛阶', '稳定性'))
        ):
            deep_family = max(
                non_fast,
                key=lambda name: EFFORT_LADDER.index(_match_effort_level(families[name], 'ultra' if tier == 'ultra' else 'max')),
            )
            chosen_family = deep_family
            profile_source = 'specialty_deep_reasoning'
        best_tier = _match_effort_level(list(families[chosen_family]), tier)
        slug = families[chosen_family][best_tier]
        return {'spec': f'agy:{slug}', 'model': slug, 'reasoning_effort': best_tier,
                'cli_effort': None, 'tier': tier, 'profile_source': profile_source, 'effort_source': 'catalog_variant'}
    cfg_model = str(info.get('model') or '').strip()
    fam, cfg_tier = _agy_slug_tier(cfg_model)
    if not fam:
        fam, cfg_tier = _agy_display_tier(cfg_model)
    if fam and cfg_tier:
        if cfg_tier == 'ultra' and tier != 'ultra':
            return {'spec': 'agy:default', 'model': None, 'reasoning_effort': None,
                    'cli_effort': None, 'tier': tier, 'profile_source': 'local_settings',
                    'effort_source': 'no_non_delegating_effort'}
        if tier in ('max', 'ultra'):
            best_tier = cfg_tier if EFFORT_LADDER.index(cfg_tier) >= EFFORT_LADDER.index('high') else 'high'
        else:
            best_tier = _match_effort_level(['low', 'medium', 'high', cfg_tier], tier)
        slug = f'{fam}-{best_tier}'
        return {'spec': f'agy:{slug}', 'model': slug, 'reasoning_effort': best_tier,
                'cli_effort': None, 'tier': tier, 'profile_source': 'local_settings',
                'effort_source': 'local_settings' if best_tier == cfg_tier else 'unverified_inference'}
    return {'spec': f'agy:{cfg_model}' if cfg_model else 'agy:default', 'model': cfg_model or None,
            'reasoning_effort': None, 'cli_effort': None, 'tier': tier,
            'profile_source': 'local_settings' if cfg_model else 'cli_default', 'effort_source': 'unknown'}


def _catalog_metadata(path, data, models, source, now):
    exists = path.is_file() if path else False
    fetched_at = data.get('fetched_at') if isinstance(data, dict) else None
    stamp = None
    if isinstance(fetched_at, str):
        try:
            # Codex may record nanoseconds. Python 3.9 accepts six fractional digits.
            iso_timestamp = re.sub(r'\.(\d+)', lambda match: '.' + match.group(1)[:6].ljust(6, '0'), fetched_at)
            stamp = datetime.datetime.fromisoformat(iso_timestamp.replace('Z', '+00:00'))
            stamp = stamp.astimezone(datetime.timezone.utc) if stamp.tzinfo is not None else None
        except (ValueError, OverflowError):
            pass
    age = (now - stamp).total_seconds() if stamp else None
    if not models:
        freshness = 'empty_or_unreadable' if exists or path is None else 'missing'
    elif age is None:
        freshness = 'unknown'
    elif age < 0:
        freshness = 'future_timestamp'
    else:
        freshness = 'stale' if age >= CATALOG_MAX_AGE_SECONDS else 'within_assessment_window'
    return {'source': source, 'path': str(path) if path else None, 'cache_file_exists': exists,
            'fetched_at': stamp.isoformat() if stamp else None,
            'timestamp_source': ('cli_query_time_utc' if path is None else 'cache_fetched_at_field') if stamp else None,
            'age_seconds': int(age) if age is not None and age >= 0 else None,
            'freshness': freshness, 'assessment_window_seconds': CATALOG_MAX_AGE_SECONDS,
            'model_count': len(models)}


def _agy_config(root):
    cli_path = root / 'antigravity-cli' / 'settings.json'
    app_path = root / 'config' / 'config.json'
    settings, config = _read_json(cli_path), _read_json(app_path)
    user_settings = config.get('userSettings') if isinstance(config, dict) else None
    cli_model = settings.get('model') if isinstance(settings, dict) else None
    app_model = user_settings.get('model') if isinstance(user_settings, dict) else None
    return {'model': cli_model or app_model, 'configured_model_source': str(cli_path if cli_model else app_path) if cli_model or app_model else None,
            'config_files': {'cli': {'path': str(cli_path), 'exists': cli_path.is_file()},
                             'desktop': {'path': str(app_path), 'exists': app_path.is_file()}},
            'config_home': str(root)}


def _agy_cached_records(data):
    models = data.get('models') if isinstance(data, dict) else None
    return [item for item in models if isinstance(item, dict) and item.get('slug')] if isinstance(models, list) else []


def probe_backends(codex_input_budget=None, agy_input_budget=None, discover_models=False, *, refresh_model_cache=True):
    """普通探测只读文件，程序发现和缓存均不代表认证或线上容量验证。"""
    now = datetime.datetime.now(datetime.timezone.utc)
    binary = _find_binary('codex')
    home, interop = _codex_home(binary)
    config_file = home / 'config.toml'
    cache_file = home / 'models_cache.json'
    config = _top_level_toml(config_file)
    cache = _read_json(cache_file)
    models = _model_records(cache)
    frontier = _select_codex_frontier_record(models, config.get('model'))
    selected = next((item for item in models if item['slug'] == config.get('model')), {}) if config.get('model') else frontier
    configured = _positive_int(config.get('model_context_window'))
    cached_default = _positive_int(selected.get('context_window'))
    cached_max = _positive_int(selected.get('max_context_window'))
    inferred = configured or cached_default
    if inferred and cached_max:
        inferred = min(inferred, cached_max)
    compact = _positive_int(config.get('model_auto_compact_token_limit'))
    requested = codex_input_budget or _positive_int(os.environ.get('CAE_CODEX_INPUT_BUDGET'))
    cap = inferred
    if compact:
        cap = min(cap, compact) if cap else compact
    budget = requested or (max(1, int(cap * 0.90)) if cap else 120_000)
    if cap:
        budget = min(budget, cap)
    budget_source = 'explicit_planning_budget' if requested else ('policy_estimate_90pct' if cap else 'policy_estimate_120000')
    common = {'verified_status': 'unverified', 'authentication_status': 'not_checked', 'auto_selectable': True}
    codex = {**common, 'available': bool(binary), 'detected': bool(binary), 'binary': binary,
        'family': 'codex', 'model': config.get('model'), 'reasoning_effort': config.get('model_reasoning_effort'),
        'models': models, 'config_home': str(home),
        'config_files': {'cli': {'path': str(config_file), 'exists': config_file.is_file()}},
        'configured_model_source': str(config_file) if config.get('model') else None,
        'model_catalog': _catalog_metadata(cache_file, cache, models, 'codex_client_cache', now),
        'windows_interop': interop, 'configured_window_tokens': configured, 'cached_default_window_tokens': cached_default,
        'cached_max_window_tokens': cached_max, 'inferred_window_tokens': inferred, 'effective_compaction_tokens': compact,
        'context_metadata_model': selected.get('slug'),
        'cached_effective_context_percent': selected.get('effective_context_window_percent'),
        'requested_input_budget_tokens': requested, 'safe_input_budget_tokens': budget, 'budget_source': budget_source,
        'capability_source': 'local_config_and_cache',
        'capacity_notice': '配置和缓存供预算估算，实际模型权限、压缩阈值及完整读取仍需执行验证。'}
    agy_binary = _find_binary('agy')
    root, agy_interop = _agy_home(agy_binary)
    agy_cache_file = root / 'antigravity-cli' / 'cache' / 'cae_models_cache.json'
    source = 'agy_harness_cache'
    cached_agy = _read_json(agy_cache_file)
    cached_agy_models = _agy_cached_records(cached_agy)
    if (not cached_agy_models and not os.environ.get('CAE_AGY_HOME') and _is_posix()
            and (not agy_binary or (Path(str(agy_binary)).is_absolute() and Path(str(agy_binary)).is_file()))):
        win_agy = _find_windows_agy_in_wsl()
        if win_agy:
            win_root, _ = _agy_home(win_agy)
            win_cache_file = win_root / 'antigravity-cli' / 'cache' / 'cae_models_cache.json'
            win_cached = _read_json(win_cache_file)
            win_models = _agy_cached_records(win_cached)
            if win_models:
                agy_cache_file, cached_agy, cached_agy_models = win_cache_file, win_cached, win_models
                source = 'agy_windows_harness_cache'
    requested = agy_input_budget or _positive_int(os.environ.get('CAE_AGY_INPUT_BUDGET'))
    agy = {**common, 'available': bool(agy_binary), 'detected': bool(agy_binary), 'binary': agy_binary,
        'family': 'antigravity', **_agy_config(root), 'models': cached_agy_models, 'windows_interop': agy_interop,
        'model_catalog': _catalog_metadata(agy_cache_file, cached_agy, cached_agy_models, source, now),
        'execution_config_supported': not os.environ.get('CAE_AGY_HOME'),
        'configured_window_tokens': None, 'effective_compaction_tokens': None, 'safe_input_budget_tokens': requested or 120_000,
        'budget_source': 'explicit_planning_budget' if requested else 'policy_estimate_120000',
        'capability_source': 'local_settings_and_cache' if cached_agy_models else 'local_settings',
        'capacity_notice': '宿主压缩阈值未核实，规划预算不表示无压缩容量。'}
    if discover_models:
        if not agy['execution_config_supported']:
            raise HarnessError('CAE_AGY_HOME 仅用于离线配置探测，执行查询请使用当前 CLI 的真实用户配置')
        if not agy_binary:
            raise HarnessError('未发现 agy，无法查询模型', backends={'codex': codex, 'agy': agy})

        def _query_agy_models(bin_path, use_interop):
            if use_interop and shutil.which('powershell.exe'):
                cmd = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                       f'& {_ps_literal(_wsl_path(Path(bin_path)))} models']
                return run_process(cmd, Path.cwd(), timeout_sec=30)
            return run_process([bin_path, 'models'], Path.cwd(), timeout_sec=30)

        result = _query_agy_models(agy_binary, agy_interop)
        if result.returncode and not agy_interop and not os.environ.get('CAE_AGY_BINARY'):
            detail0 = (result.stderr or result.stdout)[-1000:]
            if 'sign in' in detail0.lower() or 'login' in detail0.lower():
                win_agy = _find_windows_agy_in_wsl()
                if win_agy and win_agy != agy_binary:
                    alt_res = _query_agy_models(win_agy, True)
                    if alt_res.returncode == 0:
                        result = alt_res
                        agy_binary = win_agy
                        agy_interop = True
                        root, _ = _agy_home(win_agy)
                        agy.update(_agy_config(root), binary=win_agy, windows_interop=True)
        if result.returncode:
            detail = (result.stderr or result.stdout)[-1000:]
            if 'sign in' in detail.lower() or 'login' in detail.lower():
                agy['authentication_status'] = 'unauthenticated'
            agy['verified_status'] = 'discovery_failed'
            raise HarnessError('agy models 查询失败', stderr=detail, backends={'codex': codex, 'agy': agy})
        parsed_models = _parse_agy_models(result.stdout)
        fetched_at = datetime.datetime.now(datetime.timezone.utc)
        snapshot = {'fetched_at': fetched_at.isoformat(), 'models': parsed_models}
        if parsed_models and refresh_model_cache:
            agy_cache_file = root / 'antigravity-cli' / 'cache' / 'cae_models_cache.json'
            try:
                agy_cache_file.parent.mkdir(parents=True, exist_ok=True)
                agy_cache_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            except OSError:
                pass
        agy.update(models=parsed_models, authentication_status='authenticated', model_discovery_output=result.stdout.strip(),
                   model_discovery_source='agy_models_cli', model_list_parsed=bool(parsed_models), verified_status='model_listing_only',
                   cached_model_catalog=agy['model_catalog'], capability_source='agy_models_cli',
                   model_catalog=_catalog_metadata(None, snapshot, parsed_models, 'agy_models_cli', fetched_at))
    result_map = {'codex': codex, 'agy': agy}
    for name in ('codex', 'agy'):
        prof = resolve_execution_profile(result_map, name + ':default', 'max', kind='code')
        deep_prof = resolve_execution_profile(result_map, name + ':default', 'max', kind='math')
        fast_prof = resolve_execution_profile(result_map, name + ':default', 'low', kind='review')
        result_map[name]['frontier_model'] = prof['model']
        result_map[name]['max_reasoning_effort'] = prof['reasoning_effort']
        result_map[name]['specialty_routing'] = {
            'engineering_workhorse': {'model': prof['model'], 'reasoning_effort': prof['reasoning_effort']},
            'deep_reasoning': {'model': deep_prof['model'], 'reasoning_effort': deep_prof['reasoning_effort']},
            'fast_lightweight': {'model': fast_prof['model'], 'reasoning_effort': fast_prof['reasoning_effort']},
        }
        cooldown = _load_active_quota_cooldown(name, result_map[name], cleanup_expired=False)
        result_map[name]['quota_status'] = 'exhausted_cached' if cooldown else 'unknown_until_called'
        result_map[name]['quota_cooldown'] = cooldown
    return result_map


def _is_quota_exhausted(text):
    return bool(text and QUOTA_ERROR_RE.search(text))


def _parse_quota_reset(text, now=None):
    now_ts = time.time() if now is None else float(now)
    match = re.search(
        r'(?:try again (?:at|after|in)|resets? (?:at|in))\s+([^\r\n]+?)(?:\.(?:\s|$)|$)',
        text or '',
        re.IGNORECASE,
    )
    hint = match.group(1).strip() if match else ''
    cleaned = re.sub(r'\b(\d{1,2})(?:st|nd|rd|th)\b', r'\1', hint, flags=re.IGNORECASE).strip()
    epoch = None
    if cleaned:
        for fmt in ('%b %d, %Y %I:%M %p', '%B %d, %Y %I:%M %p', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
            try:
                epoch = datetime.datetime.strptime(cleaned, fmt).timestamp()
                break
            except ValueError:
                continue
        if epoch is None:
            try:
                epoch = datetime.datetime.fromisoformat(cleaned.replace('Z', '+00:00')).timestamp()
            except ValueError:
                rel = re.fullmatch(r'(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*(?:(\d+)\s*s(?:ec)?)?', cleaned, re.IGNORECASE)
                if rel and any(rel.groups()):
                    hrs, mins, secs = (int(val or 0) for val in rel.groups())
                    epoch = now_ts + hrs * 3600 + mins * 60 + secs
    if epoch is not None and now_ts < epoch <= now_ts + 45 * 86400:
        until, source = epoch, 'parsed_reset_time'
    else:
        until, source = now_ts + 3600, 'default_ttl_3600s'
    return {'reset_hint': hint or None, 'cooldown_until_epoch': round(until, 3), 'cooldown_source': source}


def _quota_state_path(prefix, info):
    cfg_home = info.get('config_home') if isinstance(info, dict) else None
    if not cfg_home:
        return None
    base = Path(str(cfg_home))
    return (base / 'cache' / 'cae_quota_state.json') if prefix == 'codex' else (base / 'antigravity-cli' / 'cache' / 'cae_quota_state.json')


def _load_active_quota_cooldown(prefix, info, now=None, *, cleanup_expired=True):
    path = _quota_state_path(prefix, info)
    if not path or not path.is_file():
        return None
    data = _read_json(path)
    if not isinstance(data, dict) or not data.get('exhausted'):
        return None
    now_ts = time.time() if now is None else float(now)
    until = data.get('cooldown_until_epoch')
    if not isinstance(until, (int, float)) or now_ts >= float(until):
        if cleanup_expired:
            try:
                path.unlink()
            except OSError:
                pass
        return None
    return data


def _record_quota_exhausted(prefix, info, detail, now=None):
    now_ts = time.time() if now is None else float(now)
    clean_detail = re.sub(r'<Objs\b[\s\S]*', '', detail or '').strip()
    record = {
        'backend': prefix,
        'exhausted': True,
        'detected_at_epoch': round(now_ts, 3),
        **_parse_quota_reset(clean_detail, now_ts),
        'detail': clean_detail[-600:],
    }
    path = _quota_state_path(prefix, info)
    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        except OSError:
            pass
    with _EVENT_LOCK:
        _SESSION_QUOTA_BLOCKED[prefix] = record
    if isinstance(info, dict):
        info['quota_status'] = 'exhausted_cached'
        info['quota_cooldown'] = record
    return record


def _clear_quota_cooldown(prefix, info):
    with _EVENT_LOCK:
        _SESSION_QUOTA_BLOCKED.pop(prefix, None)
    path = _quota_state_path(prefix, info)
    if path and path.is_file():
        try:
            path.unlink()
        except OSError:
            pass
    if isinstance(info, dict):
        info['quota_status'] = 'unknown_until_called'
        info['quota_cooldown'] = None


def _quota_user_choices(prefix, cooldown):
    hint = cooldown.get('reset_hint') or '待冷却结束或补充额度后'
    return {
        'quota_backend': prefix,
        'quota_cooldown': cooldown,
        'user_confirmation_required': True,
        'user_choices': [
            f'1. 先不发起跨端调用：改由当前总控在本侧直接完成（{prefix} 预计恢复参考: {hint}）',
            f'2. 已充值或额度已恢复：经用户确认后附带 --retry-quota 重新发起跨端调用',
        ],
    }


def _enforce_preflight_quota(backends, specs, retry_quota=False):
    for spec in specs:
        if not spec:
            continue
        prefix, _ = _engine_parts(spec)
        info = backends.get(prefix, {})
        with _EVENT_LOCK:
            in_session = _SESSION_QUOTA_BLOCKED.get(prefix)
        cooldown = in_session or (None if retry_quota else _load_active_quota_cooldown(prefix, info))
        if cooldown:
            hint = cooldown.get('reset_hint') or '待额度恢复'
            raise HarnessError(
                f'检测到对侧后端 {prefix} 处于已知额度冷却期（预计恢复: {hint}），已在发起前自动拦截；请提示用户选择先不发起（由本侧直接完成）或充值后加 --retry-quota 重试',
                'quota_exhausted',
                preflight_blocked=True,
                **_quota_user_choices(prefix, cooldown),
            )


def resolve_caller(value):
    if value != 'auto':
        return value
    inherited = os.environ.get('CAE_HARNESS_CALLER', '')
    return inherited if inherited in ('codex', 'antigravity') else 'caller'


def _engine_parts(spec):
    prefix, _, model = spec.partition(':')
    if prefix not in ('codex', 'agy'):
        raise HarnessError('本协作机制仅支持 agy 和 codex 后端')
    if model == '1m':
        emit_event('deprecated_model_alias', engine=prefix, alias='1m', replacement='default')
        model = 'default'
    return prefix, model if model and model != 'default' else ''


def choose_engine(backends, caller, explicit=''):
    if explicit:
        name, _ = _engine_parts(explicit)
    elif caller in ('codex', 'antigravity'):
        name = 'agy' if caller == 'codex' else 'codex'
        explicit = name + ':default'
    else:
        raise HarnessError('请指定 --caller，或指定 --engine/--worker/--left')
    if not backends.get(name, {}).get('available'):
        raise HarnessError(f'未发现所选后端 {name}，请配置对应运行环境')
    return explicit


def estimate_tokens_from_bytes(num_bytes):
    return max(1, (num_bytes + 2) // 3) if num_bytes else 0


def _is_link(path):
    attributes = getattr(path.lstat(), 'st_file_attributes', 0)
    return path.is_symlink() or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def collect_target_files(target, skipped_files=None):
    target = target.resolve()
    skipped = skipped_files if skipped_files is not None else []
    candidates = [target] if target.is_file() else []
    if target.is_dir():
        for root, dirs, files in os.walk(target):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not d.startswith('.'))
            for name in list(dirs):
                directory = Path(root) / name
                if _is_link(directory):
                    skipped.append({'file': directory.relative_to(target).as_posix(), 'reason': 'directory_link'})
                    dirs.remove(name)
            candidates.extend(Path(root) / name for name in sorted(files)
                              if Path(name).suffix.lower() in SOURCE_EXTENSIONS or name in ('CMakeLists.txt', 'Makefile', 'makefile'))
    records = []
    for path in candidates:
        rel = path.name if target.is_file() else path.relative_to(target).as_posix()
        if _is_link(path):
            skipped.append({'file': rel, 'reason': 'symbolic_link'})
            continue
        try:
            size = path.stat().st_size
            with path.open('rb') as stream:
                stream.read(1)
        except OSError as exc:
            skipped.append({'file': rel, 'reason': 'unreadable', 'detail': str(exc)})
            continue
        if not size:
            skipped.append({'file': rel, 'reason': 'empty_file'})
            continue
        records.append({'path': path, 'rel_path': rel, 'bytes': size, 'tokens': estimate_tokens_from_bytes(size)})
    return records


def build_shards(file_records, max_shard_tokens):
    if max_shard_tokens <= 0:
        raise HarnessError('分片预算必须为正数')
    shards, files, tokens = [], [], 0
    for record in file_records:
        if record['tokens'] > max_shard_tokens:
            raise HarnessError(f"单文件超出分片预算: {record['rel_path']}")
        if files and tokens + record['tokens'] > max_shard_tokens:
            shards.append({'shard_id': len(shards) + 1, 'files': files, 'estimated_tokens': tokens})
            files, tokens = [], 0
        files.append(record['rel_path'])
        tokens += record['tokens']
    if files:
        shards.append({'shard_id': len(shards) + 1, 'files': files, 'estimated_tokens': tokens})
    return shards


def _budget_for_spec(backends, spec):
    name, model = _engine_parts(spec)
    info = backends[name]
    if not model or name != 'codex':
        return info['safe_input_budget_tokens']
    record = _resolve_explicit_codex_record(info.get('models', []), model)
    cap = info.get('configured_window_tokens') or _positive_int(record.get('context_window'))
    maximum = _positive_int(record.get('max_context_window'))
    if cap and maximum:
        cap = min(cap, maximum)
    compact = info.get('effective_compaction_tokens')
    if compact:
        cap = min(cap, compact) if cap else compact
    requested = info.get('requested_input_budget_tokens')
    budget = requested or (max(1, int(cap * 0.90)) if cap else 120_000)
    return min(budget, cap) if cap else budget


def select_audit_plan(total_tokens, file_records, backends, strategy='auto', dual_blind=True,
                      left_override='', right_override='', shard_tokens=120_000, allow_unconfigured=False, caller='caller'):
    requested_strategy = strategy
    if strategy == 'whole-1m':
        strategy = 'single-pass'
    if not left_override and caller == 'caller':
        available = [name for name in ('codex', 'agy') if backends.get(name, {}).get('available')]
        if not available:
            if allow_unconfigured:
                return {'mode': 'unconfigured', 'caller': caller, 'handoff_to_caller': True,
                        'total_files': len(file_records), 'total_estimated_tokens': total_tokens,
                        'left_engine': None, 'right_engine': None, 'left_units': [], 'right_units': [],
                        'skipped_files': [], 'notice': '未发现 agy 或 codex，当前只提供未就绪计划。'}
            raise HarnessError('未发现 agy 或 codex 后端')
        left_override = available[0] + ':default'
    left = choose_engine(backends, caller, left_override)
    right = None
    if dual_blind:
        name, _ = _engine_parts(left)
        right = right_override or ('agy:default' if name == 'codex' else 'codex:default')
        choose_engine(backends, caller, right)
        if left == right:
            raise HarnessError('双路审查需要两个不同引擎或显式模型')
    plan = {'mode': strategy, 'requested_strategy': requested_strategy, 'caller': caller, 'handoff_to_caller': True,
            'total_files': len(file_records), 'total_estimated_tokens': total_tokens,
            'dual_blind': bool(right), 'left_engine': left, 'right_engine': right, 'skipped_files': [],
            'coverage_notice': '清单是计划覆盖范围，实际读取完整性仍由回执与证据核实。',
            'independence_notice': '不同进程独立审查，模型家族及错误独立性尚未验证。'}
    for side, spec in (('left', left), ('right', right)):
        if not spec:
            plan[side + '_units'] = []
            continue
        cap = _budget_for_spec(backends, spec)
        unit_cap = cap if strategy != 'sharded' and total_tokens <= cap else min(cap, shard_tokens)
        usable = []
        for record in file_records:
            if record['tokens'] > unit_cap:
                plan['skipped_files'].append({'file': record['rel_path'], 'engine': spec, 'reason': 'over_input_budget',
                                             'estimated_tokens': record['tokens'], 'budget_tokens': unit_cap})
            else:
                usable.append(record)
        if strategy == 'single-pass' and (total_tokens > cap or len(usable) != len(file_records)):
            raise HarnessError(f'{spec} 输入计划超出单次预算，请选择 sharded 或调整有依据的预算')
        whole = strategy != 'sharded' and total_tokens <= cap
        units = build_shards(usable, unit_cap)
        for unit in units:
            unit['single_pass_planned'] = whole
        plan[side + '_units'] = units
    whole = all(len(plan[side + '_units']) <= 1 and all(u['single_pass_planned'] for u in plan[side + '_units']) for side in ('left', 'right'))
    plan['mode'] = 'single-pass' if whole else 'sharded'
    if plan['mode'] == 'sharded':
        plan['coverage_notice'] += ' 跨分片依赖未在同一个上下文内联合核查。'
    if requested_strategy == 'whole-1m':
        plan['deprecation_notice'] = 'whole-1m 兼容映射至 single-pass，不表示百万 token 或无压缩能力。'
    return plan


def _validate_schema(value, schema, location='result'):
    expected = schema.get('type')
    valid = {'object': isinstance(value, dict), 'array': isinstance(value, list), 'string': isinstance(value, str),
             'integer': isinstance(value, int) and not isinstance(value, bool)}
    if expected and not valid.get(expected, False):
        raise OutputParseError(f'{location} 类型不符合 {expected}')
    if 'enum' in schema and value not in schema['enum']:
        raise OutputParseError(f'{location} 取值不合法')
    if expected == 'integer' and value < schema.get('minimum', value):
        raise OutputParseError(f'{location} 超出下界')
    if expected == 'object':
        properties = schema.get('properties', {})
        if any(key not in value for key in schema.get('required', [])):
            raise OutputParseError(f'{location} 缺少必需字段')
        if schema.get('additionalProperties') is False and set(value) - set(properties):
            raise OutputParseError(f'{location} 含未定义字段')
        for key, item in value.items():
            if key in properties:
                _validate_schema(item, properties[key], location + '.' + key)
    if expected == 'array':
        for index, item in enumerate(value):
            _validate_schema(item, schema['items'], f'{location}[{index}]')


def _extract_json_object(raw_text, schema=None):
    schema = schema or AUDIT_JSON_SCHEMA
    raw = raw_text.strip()
    if not raw:
        raise OutputParseError('子任务输出为空')
    try:
        parsed = json.loads(raw)
    except ValueError:
        events = []
        try:
            events = [json.loads(line) for line in raw.splitlines() if line.strip()]
        except ValueError:
            fence = re.fullmatch(r'```(?:json)?\s*(\{[\s\S]*\})\s*```', raw)
            if not fence:
                raise OutputParseError('子任务输出不是合法 JSON')
            try:
                parsed = json.loads(fence.group(1))
            except ValueError as exc:
                raise OutputParseError('JSON 代码块无效') from exc
        if events:
            results = [item for item in events if isinstance(item, dict) and item.get('event') == 'result']
            if len(results) != 1:
                raise OutputParseError('输出流必须包含一个最终 result 回执')
            parsed = results[0]
    for _ in range(8):
        if not isinstance(parsed, dict):
            raise OutputParseError('最终回执必须为对象')
        finished_reason = str(parsed.get('finished_reason', '')).lower()
        if finished_reason == 'print_timeout':
            raise HarnessError('后端输出因超时截断', 'timeout', finished_reason=finished_reason)
        if finished_reason in ('interrupted', 'cancelled', 'canceled'):
            raise HarnessError('后端执行被中断', 'cancelled', finished_reason=finished_reason)
        if finished_reason == 'error':
            err_msg = str(parsed.get('error', ''))[:1000]
            err_status = 'quota_exhausted' if _is_quota_exhausted(err_msg) else 'failed'
            raise HarnessError('后端返回错误结束原因', err_status, finished_reason=finished_reason, error=err_msg)
        status = str(parsed.get('status', '')).lower()
        if status == 'timeout':
            raise HarnessError('后端返回超时回执', 'timeout', backend_status=status)
        if status in ('cancelled', 'canceled', 'interrupted'):
            raise HarnessError('后端返回取消回执', 'cancelled', backend_status=status)
        if parsed.get('is_error') or parsed.get('error') or status in ('error', 'failed', 'partial', 'incomplete'):
            err_msg = str(parsed.get('error', ''))[:1000]
            err_status = 'quota_exhausted' if _is_quota_exhausted(err_msg) else 'failed'
            raise HarnessError('后端返回错误回执', err_status, backend_status=status or 'error', error=err_msg)
        if all(key in parsed for key in schema['required']):
            _validate_schema(parsed, schema)
            if not parsed['summary'].strip():
                raise OutputParseError('summary 为空')
            if 'content' in schema['properties'] and not parsed['content'].strip():
                raise OutputParseError('任务正文 content 为空')
            for finding in parsed.get('findings', []):
                if finding['line_end'] < finding['line_start']:
                    raise OutputParseError('发现项的行号范围倒置')
            return parsed
        next_value = None
        for key in ('structured_output', 'result', 'response', 'output', 'text', 'content'):
            if key in parsed and isinstance(parsed[key], (dict, str)):
                next_value = parsed[key]
                break
        if isinstance(next_value, str):
            return _extract_json_object(next_value, schema)
        if next_value is None:
            break
        parsed = next_value
    raise OutputParseError('回执缺少符合指定结构的结果')


def _terminate_tree(proc, native_pid_file=None):
    errors = []
    if native_pid_file:
        deadline = time.monotonic() + 2
        while not native_pid_file.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        try:
            native_pid = int(native_pid_file.read_text(encoding='utf-8-sig').strip())
            if native_pid <= 0:
                raise ValueError('native PID invalid')
            cleanup = subprocess.run(['taskkill.exe', '/T', '/F', '/PID', str(native_pid)], capture_output=True, timeout=10)
            if cleanup.returncode:
                errors.append('Windows process tree termination returned nonzero')
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            errors.append('Windows process tree cleanup unverified: ' + str(exc))
    try:
        if os.name == 'nt':
            cleanup = subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True, timeout=10)
            if cleanup.returncode and proc.poll() is None:
                errors.append('Local process tree termination returned nonzero')
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append('Local process tree cleanup unverified: ' + str(exc))
    return errors


def run_process(command, cwd, input_text=None, timeout_sec=600, env=None, shell=False, native_pid_file=None):
    if _CANCELLED.is_set():
        raise HarnessError('任务已取消', 'cancelled')
    options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    proc = subprocess.Popen(command, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', errors='replace', env=env, shell=shell, **options)
    emit_event('process_started', pid=proc.pid)
    started = time.monotonic()
    first_input = input_text
    try:
        while True:
            if _CANCELLED.is_set():
                raise HarnessError('任务已取消', 'cancelled')
            remaining = timeout_sec - (time.monotonic() - started)
            if remaining <= 0:
                raise HarnessError('子进程超时', 'timeout', timeout_sec=timeout_sec)
            try:
                out, err = proc.communicate(input=first_input, timeout=min(remaining, 0.25))
                break
            except subprocess.TimeoutExpired:
                first_input = None
        if _CANCELLED.is_set():
            raise HarnessError('任务已取消', 'cancelled')
        emit_event('process_finished', pid=proc.pid, returncode=proc.returncode)
        return subprocess.CompletedProcess(command, proc.returncode, out, err)
    except BaseException as exc:
        cleanup_errors = _terminate_tree(proc, native_pid_file)
        if isinstance(exc, HarnessError) and cleanup_errors:
            exc.details['cleanup_errors'] = cleanup_errors
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise


def _cancel_signal(_signum, _frame):
    _CANCELLED.set()


def _ps_literal(text):
    return "'" + text.replace("'", "''") + "'"


MAX_INTEROP_STAGE_BYTES = 64 * 1024 * 1024


def _stage_wsl_readonly_workspace(target_dir, stage_root, rel_files=None):
    stage_dir = stage_root / 'workspace'
    stage_dir.mkdir(parents=True, exist_ok=True)
    if rel_files is None:
        records = collect_target_files(target_dir)
    else:
        records = []
        for rel in rel_files:
            src = target_dir / rel
            if _is_link(src) or not src.is_file():
                continue
            records.append({'path': src, 'rel_path': rel, 'bytes': src.stat().st_size})
    if sum(item['bytes'] for item in records) > MAX_INTEROP_STAGE_BYTES:
        raise HarnessError('WSL 纯 Linux 路径只读镜像超出 64MB 上限，请缩小 --target 范围或在 WSL 安装原生 codex')
    for item in records:
        dst = stage_dir / item['rel_path']
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item['path'], dst)
    return stage_dir


def _rewrite_staged_paths(parsed, stage_dir):
    prefixes = [str(stage_dir).replace('\\', '/').rstrip('/') + '/',
                str(stage_dir.resolve()).replace('\\', '/').rstrip('/') + '/']
    if _is_posix() and shutil.which('wslpath'):
        try:
            prefixes.append(_wsl_path(stage_dir, windows=True).replace('\\', '/').rstrip('/') + '/')
        except HarnessError:
            pass

    def _clean(raw_path):
        norm = raw_path.strip().replace('\\', '/')
        for prefix in prefixes:
            if norm.lower().startswith(prefix.lower()):
                return norm[len(prefix):]
        return raw_path

    if 'read_files' in parsed:
        parsed['read_files'] = [_clean(item) for item in parsed['read_files']]
    if 'unread_files' in parsed:
        parsed['unread_files'] = [{**item, 'file': _clean(item['file'])} for item in parsed['unread_files']]
    if 'findings' in parsed:
        parsed['findings'] = [{**item, 'file': _clean(item['file'])} for item in parsed['findings']]
    return parsed


def _interop_command(command, work_dir, temp_dir, input_text, codex_home=None):
    """监督进程写入本次真实 Windows PID，供 WSL 定向清理进程树。"""
    prompt_file = temp_dir / 'input.txt'
    pid_file = temp_dir / 'native.pid'
    prompt_file.write_text(input_text, encoding='utf-8')
    args = subprocess.list2cmdline(command[1:])
    env_lines = ["$info.EnvironmentVariables['CAE_HARNESS_DEPTH'] = '1'"]
    if codex_home:
        env_lines.insert(0, "$info.EnvironmentVariables['CODEX_HOME'] = " + _ps_literal(_wsl_path(Path(codex_home))))
    script = '\n'.join([
        "$ErrorActionPreference='Stop'",
        "$ProgressPreference='SilentlyContinue'",
        '[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)',
        '[IO.File]::WriteAllText(' + _ps_literal(_wsl_path(pid_file.with_suffix('.pending'))) + ', [string]$PID)',
        '[IO.File]::Move(' + _ps_literal(_wsl_path(pid_file.with_suffix('.pending'))) + ', ' + _ps_literal(_wsl_path(pid_file)) + ')',
        '$info = New-Object System.Diagnostics.ProcessStartInfo',
        '$info.FileName = ' + _ps_literal(_wsl_path(Path(command[0]))),
        '$info.Arguments = ' + _ps_literal(args),
        '$info.WorkingDirectory = ' + _ps_literal(_wsl_path(work_dir)),
        '$info.UseShellExecute = $false', '$info.CreateNoWindow = $true',
        '$info.RedirectStandardInput = $true', '$info.RedirectStandardOutput = $true', '$info.RedirectStandardError = $true',
        '$info.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)',
        '$info.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)',
        *env_lines,
        '$p = New-Object System.Diagnostics.Process', '$p.StartInfo = $info',
        '$started = $false', '$completed = $false', '$exitCode = 1', 'try {',
        '[void]$p.Start()', '$started = $true',
        '$output = $p.StandardOutput.ReadToEndAsync()', '$errors = $p.StandardError.ReadToEndAsync()',
        '$inputBytes = [IO.File]::ReadAllBytes(' + _ps_literal(_wsl_path(prompt_file)) + ')',
        '$p.StandardInput.BaseStream.Write($inputBytes, 0, $inputBytes.Length)', '$p.StandardInput.BaseStream.Flush()',
        '$p.StandardInput.Close()', '$p.WaitForExit()',
        '[Console]::Out.Write($output.Result)', '[Console]::Error.Write($errors.Result)',
        '$exitCode = $p.ExitCode', '$completed = $true', '} finally {',
        'if ($started -and -not $completed) { & taskkill.exe /T /F /PID $p.Id | Out-Null }',
        '$p.Dispose()', '}', 'exit $exitCode',
    ])
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    return ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded], pid_file


def invoke_engine(spec, target_dir, prompt, backends, schema, readonly=True, timeout_sec=600,
                  agent='readonly-collaborator', stage_files=None, effort=None, retry_quota=False):
    prefix, model = _engine_parts(spec)
    info = backends.get(prefix, {})
    if not info.get('available'):
        raise HarnessError(f'未发现所选后端 {prefix}')
    _enforce_preflight_quota(backends, (spec,), retry_quota)
    binary = info['binary']
    if prefix == 'agy' and not info.get('execution_config_supported', True):
        raise HarnessError('CAE_AGY_HOME 仅用于离线配置探测，执行请使用当前 CLI 的真实用户配置')
    if Path(binary).suffix.lower() in ('.cmd', '.bat'):
        raise HarnessError('请通过 CAE_CODEX_BINARY / CAE_AGY_BINARY 指定原生可执行文件')
    interop = info.get('windows_interop', False)
    if (prefix == 'agy' and not interop and _is_posix() and not os.environ.get('CAE_AGY_BINARY')
            and info.get('authentication_status') not in ('authenticated', 'interop_fallback')
            and Path(str(binary)).is_absolute() and Path(str(binary)).is_file()):
        auth_probe = run_process([binary, 'models'], Path.cwd(), timeout_sec=15)
        probe_err = ((auth_probe.stderr or '') + '\n' + (auth_probe.stdout or '')).lower()
        if auth_probe.returncode == 0:
            with _EVENT_LOCK:
                info['authentication_status'] = 'authenticated'
        elif any(cue in probe_err for cue in ('sign in', 'login', 'authentication required')):
            win_agy = _find_windows_agy_in_wsl()
            if win_agy and win_agy != binary:
                win_root, _ = _agy_home(win_agy)
                with _EVENT_LOCK:
                    info.update(binary=win_agy, windows_interop=True, config_home=str(win_root), authentication_status='interop_fallback')
                binary = win_agy
                interop = True
    temp_parent = None
    if interop:
        if not shutil.which('powershell.exe') or not shutil.which('taskkill.exe'):
            raise HarnessError('WSL 跨端调用需要 powershell.exe 与 taskkill.exe 进行进程监督')
        windows_temp = _windows_env('TEMP')
        temp_parent = _wsl_path(Path(windows_temp), windows=False)
    with tempfile.TemporaryDirectory(prefix='cae-harness-', dir=temp_parent) as temp:
        temp_path = Path(temp)
        schema_file = temp_path / 'schema.json'
        schema_file.write_text(json.dumps(schema, ensure_ascii=False), encoding='utf-8')
        output_file = temp_path / 'result.json'
        env = os.environ.copy()
        env['CAE_HARNESS_DEPTH'] = '1'
        if prefix == 'codex' and info.get('config_home') and not interop:
            env['CODEX_HOME'] = info['config_home']
        native_pid_file = None
        exec_dir = target_dir
        stage_dir = None
        exec_prompt = prompt
        translate = _wsl_path if interop else str
        win_target = translate(target_dir)
        if interop and win_target.startswith('\\\\'):
            if not readonly:
                cli_label = 'codex.exe' if prefix == 'codex' else 'agy.exe'
                native_hint = '安装原生 codex' if prefix == 'codex' else '登录原生 agy'
                raise HarnessError(f'WSL 纯 Linux 文件系统路径无法在 Windows {cli_label} 沙箱内直接写入；请在 WSL {native_hint}，或在 /mnt/<盘符> 工作区执行写入任务')
            stage_dir = _stage_wsl_readonly_workspace(target_dir, temp_path, stage_files)
            exec_dir = stage_dir
            win_target = translate(stage_dir)
            exec_prompt = prompt + '\n[跨端只读工作区说明] 当前目录已按原相对路径只读镜像目标文件，请直接在当前目录下按相对路径读取。'
        if prefix == 'codex':
            command = [binary, 'exec', '-s', 'read-only' if readonly else 'workspace-write',
                       '-c', 'approval_policy="never"', '--ephemeral',
                       '--skip-git-repo-check', '-C', win_target, '--output-schema', translate(schema_file), '-o', translate(output_file)]
            if model:
                command.extend(['-m', model])
            if effort:
                command.extend(['-c', f'model_reasoning_effort="{effort}"'])
            command.append('-')
            input_text = exec_prompt
            if interop:
                command, native_pid_file = _interop_command(command, exec_dir, temp_path, exec_prompt, info['config_home'])
                input_text = None
        else:
            command = [binary, '--input-format', 'stream-json', '--output-format', 'stream-json',
                       '--print-timeout', f'{timeout_sec}s', '--json-schema', translate(schema_file),
                       '--disable-slash-commands', '--sandbox']
            if model:
                command.extend(['--model', model])
            if effort and not _agy_slug_tier(model)[1]:
                command.extend(['--effort', effort])
            if readonly:
                command.extend(['--agent', agent])
            else:
                command.extend(['--mode', 'accept-edits', '--dangerously-skip-permissions'])
            input_text = json.dumps({'event': 'user', 'message': {'content': exec_prompt}}, ensure_ascii=False) + '\n'
            if interop:
                command, native_pid_file = _interop_command(command, exec_dir, temp_path, input_text, None)
                input_text = None
        result = run_process(command, exec_dir, input_text, timeout_sec, env=env, native_pid_file=native_pid_file)
        if result.returncode:
            combined_err = ((result.stderr or '') + '\n' + (result.stdout or '')).strip()
            if (prefix == 'agy' and not interop and _is_posix() and not os.environ.get('CAE_AGY_BINARY')
                    and any(cue in combined_err.lower() for cue in ('sign in', 'please login', 'authentication required'))):
                win_agy = _find_windows_agy_in_wsl()
                if win_agy and win_agy != binary:
                    win_root, _ = _agy_home(win_agy)
                    with _EVENT_LOCK:
                        info.update(binary=win_agy, windows_interop=True, config_home=str(win_root), authentication_status='interop_fallback')
                    return invoke_engine(spec, target_dir, prompt, backends, schema, readonly,
                                         timeout_sec, agent, stage_files, effort, retry_quota)
            if _is_quota_exhausted(combined_err):
                cooldown = _record_quota_exhausted(prefix, info, combined_err)
                raise HarnessError(
                    f'{prefix} 额度不足或已达用量上限，已终止调用并记录冷却状态；请提示用户选择先不发起（由本侧直接完成）或充值后加 --retry-quota 重试',
                    'quota_exhausted', preflight_blocked=False, returncode=result.returncode,
                    stderr=result.stderr[-2000:], **_quota_user_choices(prefix, cooldown),
                )
            raise HarnessError(f'{prefix} 执行失败，退出码 {result.returncode}', returncode=result.returncode, stderr=result.stderr[-2000:])
        raw = output_file.read_text(encoding='utf-8-sig') if prefix == 'codex' and output_file.exists() else result.stdout
        try:
            parsed = _extract_json_object(raw, schema)
        except HarnessError as exc:
            if exc.status == 'quota_exhausted':
                detail = str(exc.details.get('error') or raw)[-1000:]
                cooldown = _record_quota_exhausted(prefix, info, detail)
                raise HarnessError(
                    f'{prefix} 额度不足或已达用量上限，已终止调用并记录冷却状态；请提示用户选择先不发起（由本侧直接完成）或充值后加 --retry-quota 重试',
                    'quota_exhausted', preflight_blocked=False, **_quota_user_choices(prefix, cooldown),
                ) from exc
            raise
        _clear_quota_cooldown(prefix, info)
        return _rewrite_staged_paths(parsed, stage_dir) if stage_dir is not None else parsed


def invoke_readonly_engine(spec, target_dir, prompt, backends, timeout_sec=600, stage_files=None, effort=None, retry_quota=False):
    return invoke_engine(spec, target_dir, prompt, backends, AUDIT_JSON_SCHEMA, True, timeout_sec, 'readonly-auditor', stage_files, effort, retry_quota)


def invoke_edit_worker(spec, target_dir, prompt, backends, timeout_sec=600, effort=None, retry_quota=False):
    return json.dumps(invoke_engine(spec, target_dir, prompt, backends, TASK_JSON_SCHEMA, False, timeout_sec, 'readonly-collaborator', None, effort, retry_quota), ensure_ascii=False)


def snapshot_files(root):
    """对独立任务目录作事后核对，排除 Git 元数据，不撤销文件。"""
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != '.git')
        for name in list(dirs):
            link = Path(directory) / name
            if _is_link(link):
                result[link.relative_to(root).as_posix()] = 'symlink:' + os.readlink(link)
                dirs.remove(name)
        for name in files:
            path = Path(directory) / name
            if path == root / '.git':
                continue
            rel = path.relative_to(root).as_posix()
            if _is_link(path):
                result[rel] = 'symlink:' + os.readlink(path)
            else:
                try:
                    digest = hashlib.sha256()
                    with path.open('rb') as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b''):
                            digest.update(block)
                    result[rel] = digest.hexdigest()
                except OSError as exc:
                    raise HarnessError(f'无法核对文件: {rel}: {exc}') from exc
    return result


def _scope_patterns(patterns):
    result = []
    for pattern in patterns:
        normalized = pattern.replace('\\', '/')
        if not normalized or normalized.startswith('/') or ':' in normalized or '..' in normalized.split('/'):
            raise HarnessError('--write-scope 必须使用目标目录内的相对路径或通配符')
        result.append(normalized[2:] if normalized.startswith('./') else normalized)
    return result


def changed_files(before, after):
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def _changes_report(before, root, scopes, readonly):
    changes = changed_files(before, snapshot_files(root))
    violations = changes if readonly else [path for path in changes if scopes and not any(fnmatch.fnmatchcase(path, pattern) for pattern in scopes)]
    return {'changed_files': changes, 'scope_violations': violations, 'scope_check': 'post_execution_snapshot',
            'scope_notice': '需使用独立任务目录，快照无法区分并发写入来源，检查不自动回滚。'}


def _build_audit_prompt(unit, focus):
    files = '\n'.join('- ' + path for path in unit['files'])
    return ('在当前目录按指定范围只读审查，给出文件和真实行号。不要修改文件，严禁在目标工作目录外创建或改动任何文件或目录（含严禁 git worktree add），且不要另行调用本编排器。\n重点: '
            + (focus or '物理守恒、边界条件、数值稳定性及代码一致性') + '\n计划文件清单:\n' + files
            + '\n按 JSON Schema 返回发现，read_files 列出已读取文件，unread_files 逐文件说明未完成的原因。')


def _normalize_rel_file(file_str, target_dir):
    cleaned = file_str.strip().replace('\\', '/')
    if _is_posix() and re.match(r'^(?:[A-Za-z]:/|//)', cleaned) and shutil.which('wslpath'):
        try:
            cleaned = _wsl_path(cleaned, windows=False)
        except HarnessError:
            pass
    candidate = Path(cleaned)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(target_dir.resolve()).as_posix()
        except ValueError:
            return cleaned
    return re.sub(r'^\./+', '', cleaned)


def merge_blind_findings(left_findings, right_findings, left_engine, right_engine, target_dir, line_tolerance=8):
    """按相近行号组织候选发现，同一位置的意见仍需当前总控核实。"""
    ranks = {'critical': 4, 'high': 3, 'medium': 2, 'low': 1}
    left = [{**item, 'file': _normalize_rel_file(item['file'], target_dir)} for item in left_findings]
    right = [{**item, 'file': _normalize_rel_file(item['file'], target_dir)} for item in right_findings]
    matched = set()
    consensus, singles = [], []
    for item in left:
        hit = next((i for i, other in enumerate(right) if i not in matched and item['file'] == other['file']
                    and item['line_start'] - line_tolerance <= other['line_end']
                    and other['line_start'] - line_tolerance <= item['line_end']), None) if right_engine else None
        if hit is None:
            singles.append({**item, 'reported_by': [left_engine], 'independently_verified': False})
            continue
        matched.add(hit)
        other = right[hit]
        consensus.append({**item, 'line_start': min(item['line_start'], other['line_start']),
                          'line_end': max(item['line_end'], other['line_end']),
                          'severity': max((item['severity'], other['severity']), key=lambda level: ranks[level]),
                          'reported_by': [left_engine, right_engine], 'left_description': item['description'],
                          'right_description': other['description'], 'left_finding': item, 'right_finding': other,
                          'match_basis': 'location_overlap_only', 'independently_verified': False})
    singles.extend({**item, 'reported_by': [right_engine], 'independently_verified': False}
                   for i, item in enumerate(right) if i not in matched)
    key = lambda item: (-ranks[item['severity']], item['file'], item['line_start'])
    return {'consensus_findings': sorted(consensus, key=key), 'single_source_findings': sorted(singles, key=key)}


def run_physics_gate_loop(target_dir, task_desc, verify_cmd, worker_fn, verify_fn=None, max_rounds=3,
                          caller='caller', timeout_sec=600, write_scope=None):
    def default_verify(command, directory):
        result = run_process(command, directory, timeout_sec=timeout_sec, shell=True)
        return result.returncode, (result.stdout + '\n' + result.stderr).strip()

    verifier = verify_fn or default_verify
    scopes = _scope_patterns(write_scope or [])
    initial = snapshot_files(target_dir)
    worker_changes = set()
    verification_changes = set()
    violations = set()
    history = []
    status = 'verification_failed'
    last_error = ''
    for number in range(1, max_rounds + 1):
        prompt = (f"按授权任务修改当前目录，允许写入范围为 {scopes or '调用方授权的目标工作目录'}。"
                  f'严禁在目标工作目录外创建、修改或删除任何文件或目录（含严禁向目录外执行 git worktree add）。\n'
                  f'{task_desc}\n验收命令为 {verify_cmd}。不得通过放宽验收要求取得通过。'
                  f'Git 提交与推送由当前总控负责。不要调用本编排器或改变其他会话。\n上一轮反馈:\n{last_error[-3500:]}')
        entry = {'round': number, 'passed': False, 'verify_exit_code': None}
        emit_event('gate_round_started', round=number, caller=caller)
        before_worker = snapshot_files(target_dir)
        worker_ok = False
        try:
            entry['worker_summary'] = worker_fn(prompt, number)
            worker_ok = True
        except HarnessError as exc:
            status = exc.status
            entry.update(error=str(exc), status=exc.status, **exc.details)
            last_error = str(exc)
        except Exception as exc:
            status = 'failed'
            entry.update(error=str(exc), status='failed')
            last_error = str(exc)
        after_worker = snapshot_files(target_dir)
        round_changes = changed_files(before_worker, after_worker)
        worker_changes.update(round_changes)
        round_violations = [path for path in round_changes if scopes and
                            not any(fnmatch.fnmatchcase(path, pattern) for pattern in scopes)]
        violations.update(round_violations)
        entry['worker_changed_files'] = round_changes
        if round_violations:
            status = 'scope_violation'
            entry.update(status=status, scope_violations=round_violations)
            worker_ok = False
        if worker_ok:
            try:
                code, output = verifier(verify_cmd, target_dir)
                entry.update(verify_exit_code=code, verify_output_tail=output[-2000:], passed=code == 0)
                status = 'ok' if code == 0 else 'verification_failed'
                last_error = output
            except HarnessError as exc:
                status = exc.status
                entry.update(error=str(exc), status=exc.status, **exc.details)
                last_error = str(exc)
            except Exception as exc:
                status = 'failed'
                entry.update(error=str(exc), status='failed')
                last_error = str(exc)
            verify_changes = changed_files(after_worker, snapshot_files(target_dir))
            verification_changes.update(verify_changes)
            entry['verification_changed_files'] = verify_changes
        history.append(entry)
        if status in ('ok', 'cancelled', 'timeout', 'scope_violation', 'invalid_output', 'quota_exhausted'):
            break
    changes = changed_files(initial, snapshot_files(target_dir))
    extra_quota = {k: history[-1][k] for k in ('preflight_blocked', 'quota_backend', 'quota_cooldown', 'user_confirmation_required', 'user_choices')
                   if history and k in history[-1]} if status == 'quota_exhausted' else {}
    return {'status': status, 'gate_passed': status == 'ok', 'circuit_breaker_triggered': status != 'ok',
            'rounds_used': len(history), 'verify_cmd': verify_cmd, 'history': history,
            'caller': caller, 'handoff_to_caller': True, 'changed_files': changes,
            'worker_changed_files': sorted(worker_changes), 'verification_changed_files': sorted(verification_changes),
            'scope_violations': sorted(violations), 'scope_check': 'post_worker_snapshot',
            'scope_notice': '需使用独立任务目录，快照无法区分并发写入来源。验收命令产物单独记录，检查不自动回滚。',
            **extra_quota}


def _args_backends(args):
    return probe_backends(args.codex_input_budget, args.agy_input_budget, getattr(args, 'discover_models', False))


def _target(args):
    target = Path(args.target).expanduser().resolve()
    if not target.exists():
        raise HarnessError(f'目标不存在: {target}')
    return target, target if target.is_dir() else target.parent


def _git_head(root):
    try:
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return EXIT_CODES.get(payload.get('status', 'ok'), 1)


def cmd_probe(args):
    return _print({'status': 'ok', 'caller': resolve_caller(args.caller), 'backends': _args_backends(args)})


def cmd_assess(args):
    backends = probe_backends(args.codex_input_budget, args.agy_input_budget, args.discover_models,
                              refresh_model_cache=False)
    caller = resolve_caller(args.caller)
    local_backend = {'codex': 'codex', 'antigravity': 'agy'}.get(caller)
    scenarios = {kind: (kind, '') for kind in ('review', 'direction', 'polish', 'math', 'code', 'audit', 'gate')}
    scenarios['surface_review'] = ('review', '检查注释拼写')
    recommendations = {}
    for name, info in backends.items():
        info['caller_relation'] = ('local' if name == local_backend else 'counterpart') if local_backend else 'unspecified'
        info['program_origin'] = ('windows_interop' if info['windows_interop'] else 'native') if info['detected'] else None
        info['model_metadata_sources'] = {
            'context': 'codex_client_cache_context_fields' if name == 'codex' and any(
                model.get('context_window') or model.get('max_context_window') for model in info['models']) else None,
            'effort': ('codex_client_cache_reasoning_levels' if name == 'codex' and any(
                model.get('supported_efforts') for model in info['models']) else
                'agy_catalog_slug_suffix' if name == 'agy' and any(
                    _agy_slug_tier(model['slug'])[1] for model in info['models']) else None),
            'runtime_verified': False,
        }
        recommendations[name] = {}
        for scenario, (kind, prompt) in scenarios.items():
            tier, reason = select_task_tier(kind=kind, text=prompt)
            profile = resolve_execution_profile(backends, name + ':default', tier, prompt, kind=kind)
            recommendations[name][scenario] = {**profile, 'tier_reason': reason,
                                                'backend_detected': info['detected']}
    return _print({'status': 'ok', 'caller': caller, 'backends': backends,
                   'recommendations': recommendations,
                   'catalog_discovery_requested': args.discover_models,
                   'runtime_verified': False, 'controls_native_spawn_agent': False,
                   'notice': '推荐使用常规或短任务示例。目录时间用于评估采集时效，配置与目录声明仅供选路参考。'
                             '实际可用性、有效上下文、压缩与委派行为仍需执行验证。'})


def _ensure_live_agy_catalog(args, backends, specs):
    if getattr(args, 'dry_run', False):
        return
    needs_agy = any(spec and _engine_parts(spec)[0] == 'agy' for spec in specs)
    agy_info = backends.get('agy', {})
    binary = agy_info.get('binary')
    if (needs_agy and not agy_info.get('models') and agy_info.get('execution_config_supported', True)
            and binary and Path(str(binary)).is_file()):
        try:
            refreshed = probe_backends(args.codex_input_budget, args.agy_input_budget, discover_models=True)
            backends['agy'] = refreshed['agy']
        except HarnessError:
            pass


def _target_metrics(requested_target, text):
    prompt_tokens = estimate_tokens_from_bytes(len((text or '').encode('utf-8')))
    if requested_target.is_file():
        try:
            size = requested_target.stat().st_size
        except OSError:
            size = 0
        return 1, estimate_tokens_from_bytes(size) + prompt_tokens
    records = collect_target_files(requested_target)
    return max(1, len(records)), sum(item['tokens'] for item in records) + prompt_tokens


def cmd_audit(args):
    target, root = _target(args)
    caller = resolve_caller(args.caller)
    backends = _args_backends(args)
    skipped = []
    records = collect_target_files(target, skipped)
    if not records:
        raise HarnessError('目标范围没有可审查文件', skipped_files=skipped)
    total_tokens = sum(item['tokens'] for item in records)
    plan = select_audit_plan(total_tokens, records, backends, args.strategy,
                             not args.single, args.left, args.right, args.shard_tokens, args.dry_run, caller)
    plan['skipped_files'].extend(skipped)
    tier, tier_reason = select_task_tier(args.tier, 'audit', args.focus, len(records), total_tokens)
    _ensure_live_agy_catalog(args, backends, (plan.get('left_engine'), plan.get('right_engine')))
    profiles = {side: resolve_execution_profile(backends, plan[side + '_engine'], tier, args.focus, kind='audit', estimated_tokens=total_tokens)
                for side in ('left', 'right') if plan.get(side + '_engine')}
    plan['selected_tier'] = tier
    plan['tier_reason'] = tier_reason
    plan['execution_profiles'] = {side: {'model': prof['model'], 'reasoning_effort': prof['reasoning_effort'],
                                         'profile_source': prof['profile_source'],
                                         'effort_source': prof['effort_source'],
                                         'quota_status': backends.get(_engine_parts(plan[side + '_engine'])[0], {}).get('quota_status', 'unknown_until_called')}
                                  for side, prof in profiles.items()}
    if args.dry_run:
        return _print({'status': 'ok', 'dry_run': True, 'plan': plan})
    for profile in profiles.values():
        _require_execution_profile(profile)
    try:
        _enforce_preflight_quota(backends, (plan.get('left_engine'), plan.get('right_engine')), args.retry_quota)
    except HarnessError as exc:
        return _print({'status': exc.status, 'caller': caller, 'handoff_to_caller': True, 'selected_tier': tier,
                       'tier_reason': tier_reason, 'plan': plan, 'target': str(root), 'error': str(exc), **exc.details})
    base = _git_head(root)
    before = snapshot_files(root)
    tasks = [(side, plan[side + '_engine'], unit) for side in ('left', 'right') for unit in plan[side + '_units']]
    findings = {'left': [], 'right': []}
    errors, completed = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, max(1, len(tasks)))) as pool:
        futures = {pool.submit(invoke_readonly_engine, profiles[side]['spec'], root, _build_audit_prompt(unit, args.focus),
                               backends, args.timeout, unit['files'], profiles[side]['cli_effort'], args.retry_quota):
                   (side, engine, unit) for side, engine, unit in tasks}
        for future in concurrent.futures.as_completed(futures):
            side, engine, unit = futures[future]
            try:
                result = future.result()
                _validate_schema(result, AUDIT_JSON_SCHEMA)
                planned = set(unit['files'])
                read = [_normalize_rel_file(path, root) for path in result['read_files']]
                unread = [{**item, 'file': _normalize_rel_file(item['file'], root)} for item in result['unread_files']]
                unread_paths = {item['file'] for item in unread}
                if (set(read) | unread_paths) - planned or set(read) & unread_paths:
                    raise OutputParseError('读取覆盖回执与本分片文件范围不一致')
                for missing in sorted(planned - set(read) - unread_paths):
                    unread.append({'file': missing, 'reason': 'backend_did_not_report_reading'})
                findings[side].extend(result['findings'])
                completed.append({'engine': engine, 'model': profiles[side]['model'],
                                  'reasoning_effort': profiles[side]['reasoning_effort'], 'shard_id': unit['shard_id'],
                                  'planned_files': unit['files'], 'read_files': read, 'unread_files': unread,
                                  'status': 'partial' if unread else 'ok',
                                  'reading_coverage_verified': False, 'summary': result['summary']})
                emit_event('audit_unit_completed', engine=engine, shard_id=unit['shard_id'])
            except Exception as exc:
                errors.append({'engine': engine, 'shard_id': unit['shard_id'], 'status': getattr(exc, 'status', 'failed'),
                               'error': str(exc), **getattr(exc, 'details', {})})
    scope = _changes_report(before, root, [], True)
    quota_err = next((err for err in errors if err.get('status') == 'quota_exhausted'), None)
    if scope['scope_violations']:
        status = 'scope_violation'
    elif _CANCELLED.is_set():
        status = 'cancelled'
    elif not completed:
        statuses = {error['status'] for error in errors}
        status = 'quota_exhausted' if 'quota_exhausted' in statuses else (next(iter(statuses)) if len(statuses) == 1 else 'failed')
    else:
        status = 'partial' if errors or plan['skipped_files'] or any(unit['unread_files'] for unit in completed) else 'ok'
    quota_meta = {k: quota_err[k] for k in ('quota_backend', 'quota_cooldown', 'user_confirmation_required', 'user_choices')
                  if quota_err and k in quota_err}
    merged = merge_blind_findings(findings['left'], findings['right'], plan['left_engine'], plan['right_engine'], root)
    return _print({'status': status, 'caller': caller, 'handoff_to_caller': True, 'selected_tier': tier,
                   'tier_reason': tier_reason, 'plan': plan, 'target': str(root), 'base_commit': base,
                   'completed_units': completed, 'completed_count': sum(unit['status'] == 'ok' for unit in completed),
                   'received_count': len(completed), 'total_units': len(tasks), 'errors': errors,
                   'consensus_count': len(merged['consensus_findings']), 'single_source_count': len(merged['single_source_findings']),
                   **merged, **scope, **quota_meta, 'verification_notice': '当前总控需核实原始文件与证据后采纳发现。'})


def _task_text(args):
    text = Path(args.task_file).expanduser().resolve().read_text(encoding='utf-8-sig') if getattr(args, 'task_file', None) else args.task
    if not text or not text.strip():
        raise HarnessError('任务内容为空')
    return text


def cmd_gate(args):
    requested_target, root = _target(args)
    text = _task_text(args)
    backends = _args_backends(args)
    caller = resolve_caller(args.caller)
    engine = choose_engine(backends, caller, args.worker)
    scopes = _scope_patterns(args.write_scope)
    file_count, est_tokens = _target_metrics(requested_target, text)
    tier, tier_reason = select_task_tier(args.tier, 'gate', text, file_count, est_tokens)
    _ensure_live_agy_catalog(args, backends, (engine,))
    profile = resolve_execution_profile(backends, engine, tier, text, kind='gate', estimated_tokens=est_tokens)
    retry_profile = (resolve_execution_profile(backends, engine, 'max', text, kind='gate', estimated_tokens=est_tokens)
                     if args.tier == 'auto' and tier not in ('max', 'ultra') else profile)
    if args.dry_run:
        prefix, _ = _engine_parts(engine)
        return _print({'status': 'ok', 'dry_run': True, 'caller': caller, 'handoff_to_caller': True,
                       'worker_engine': engine, 'model': profile['model'], 'reasoning_effort': profile['reasoning_effort'],
                       'selected_tier': tier, 'tier_reason': tier_reason, 'profile_source': profile['profile_source'],
                       'effort_source': profile['effort_source'],
                       'quota_status': backends.get(prefix, {}).get('quota_status', 'unknown_until_called'),
                       'quota_cooldown': backends.get(prefix, {}).get('quota_cooldown'),
                       'work_dir': str(root), 'verify_cmd': args.verify_cmd, 'write_scope': scopes, 'max_rounds': args.max_rounds})
    _require_execution_profile(profile)
    try:
        _enforce_preflight_quota(backends, (engine,), args.retry_quota)
    except HarnessError as exc:
        return _print({'status': exc.status, 'caller': caller, 'handoff_to_caller': True, 'engine': engine,
                       'model': profile['model'], 'reasoning_effort': profile['reasoning_effort'],
                       'selected_tier': tier, 'tier_reason': tier_reason, 'target': str(root),
                       'requested_target': str(requested_target), 'error': str(exc), **exc.details})
    base = _git_head(root)

    def _gate_worker(prompt, number):
        active = retry_profile if number >= 2 else profile
        _require_execution_profile(active)
        return invoke_edit_worker(active['spec'], root, prompt, backends, args.timeout, active['cli_effort'], args.retry_quota)

    result = run_physics_gate_loop(root, f'任务目标 {requested_target}。\n' + text, args.verify_cmd,
                                   _gate_worker,
                                   max_rounds=args.max_rounds, caller=caller, timeout_sec=args.timeout, write_scope=scopes)
    final_prof = retry_profile if result.get('rounds_used', 1) >= 2 else profile
    result.update(engine=engine, model=final_prof['model'], reasoning_effort=final_prof['reasoning_effort'],
                  selected_tier=final_prof['tier'], tier_reason=('gate_retry_escalated_max' if final_prof is not profile else tier_reason),
                  profile_source=final_prof['profile_source'], effort_source=final_prof['effort_source'], target=str(root),
                  requested_target=str(requested_target), base_commit=base)
    return _print(result)


def cmd_task(args):
    requested_target, root = _target(args)
    text = _task_text(args)
    caller = resolve_caller(args.caller)
    backends = _args_backends(args)
    engine = choose_engine(backends, caller, args.engine)
    scopes = _scope_patterns(args.write_scope)
    readonly = args.kind != 'code'
    if readonly and (scopes or args.verify_cmd):
        raise HarnessError('只读任务不可声明写入范围或验收命令')
    base = _git_head(root)
    if args.base_commit:
        expected = _local_command(['git', '-C', str(root), 'rev-parse', args.base_commit + '^{commit}'])
        if base != expected:
            raise HarnessError('当前 HEAD 与 --base-commit 不一致')
    file_count, est_tokens = _target_metrics(requested_target, text)
    tier, tier_reason = select_task_tier(args.tier, args.kind, text, file_count, est_tokens)
    _ensure_live_agy_catalog(args, backends, (engine,))
    profile = resolve_execution_profile(backends, engine, tier, text, kind=args.kind, estimated_tokens=est_tokens)
    prefix, _ = _engine_parts(engine)
    payload = {'kind': args.kind, 'caller': caller, 'handoff_to_caller': True, 'engine': engine,
               'model': profile['model'], 'reasoning_effort': profile['reasoning_effort'],
               'selected_tier': tier, 'tier_reason': tier_reason, 'profile_source': profile['profile_source'],
               'effort_source': profile['effort_source'],
               'quota_status': backends.get(prefix, {}).get('quota_status', 'unknown_until_called'),
               'target': str(root), 'requested_target': str(requested_target), 'base_commit': base,
               'readonly': readonly, 'write_scope': scopes}
    if args.dry_run:
        return _print({'status': 'ok', 'dry_run': True, 'quota_cooldown': backends.get(prefix, {}).get('quota_cooldown'), **payload})
    _require_execution_profile(profile)
    try:
        _enforce_preflight_quota(backends, (engine,), args.retry_quota)
    except HarnessError as exc:
        return _print({'status': exc.status, **payload, 'result': {}, 'changed_files': [], 'scope_violations': [],
                       'error': str(exc), **exc.details})
    if args.kind == 'code' and args.verify_cmd:
        retry_profile = (resolve_execution_profile(backends, engine, 'max', text, kind='code', estimated_tokens=est_tokens)
                         if args.tier == 'auto' and tier not in ('max', 'ultra') else profile)

        def _task_worker(prompt, number):
            active = retry_profile if number >= 2 else profile
            _require_execution_profile(active)
            return invoke_edit_worker(active['spec'], root, prompt, backends, args.timeout,
                                      active['cli_effort'], args.retry_quota)

        result = run_physics_gate_loop(
            root, f'任务目标 {requested_target}。\n' + text, args.verify_cmd,
            _task_worker,
            max_rounds=args.max_rounds, caller=caller, timeout_sec=args.timeout, write_scope=scopes,
        )
        final_prof = retry_profile if result.get('rounds_used', 1) >= 2 else profile
        payload.update(
            model=final_prof['model'], reasoning_effort=final_prof['reasoning_effort'],
            selected_tier=final_prof['tier'],
            tier_reason=('gate_retry_escalated_max' if final_prof is not profile else tier_reason),
            profile_source=final_prof['profile_source'],
            effort_source=final_prof['effort_source'],
            quota_status=backends.get(prefix, {}).get('quota_status', payload['quota_status']),
        )
        return _print({**payload, **result})
    before = snapshot_files(root)
    rel_hint = f'（相对路径: {requested_target.relative_to(root).as_posix()}）' if requested_target != root else ''
    prompt = (f'任务类型 {args.kind}。当前总控 {caller}。工作目录为当前目录。任务目标 {requested_target}{rel_hint}。\n'
              + ('只读完成任务，正文通过回执返回。' if readonly else f"允许写入范围 {scopes or '调用方授权的目标工作目录'}。")
              + '严禁在目标工作目录外创建、修改或删除任何文件或目录（含严禁向目录外执行 git worktree add）。'
              + '不要调用本编排器，不要修改其他会话或全局配置。\n' + text
              + '\n按给定 JSON Schema 返回正文、证据及未验证事项。')
    result, error, status = {}, {}, 'ok'
    try:
        result = invoke_engine(profile['spec'], root, prompt, backends, TASK_JSON_SCHEMA,
                               readonly, args.timeout, 'readonly-collaborator', None, profile['cli_effort'], args.retry_quota)
    except Exception as exc:
        status = getattr(exc, 'status', 'failed')
        error = {'error': str(exc), **getattr(exc, 'details', {})}
    finally:
        scope = _changes_report(before, root, scopes, readonly)
    payload['quota_status'] = backends.get(prefix, {}).get('quota_status', payload['quota_status'])
    if scope['scope_violations']:
        status = 'scope_violation'
    return _print({'status': status, **payload, 'result': result, **error, **scope})


def _positive_arg(raw):
    value = _positive_int(raw)
    if value is None:
        raise argparse.ArgumentTypeError('必须为正整数')
    return value


def build_arg_parser():
    parser = argparse.ArgumentParser(description='Codex / Antigravity 双向任务与审计编排')
    sub = parser.add_subparsers(dest='subcommand', required=True)
    probe = sub.add_parser('probe', help='只读探测程序与配置，认证和执行能力分别验证')
    probe.add_argument('--discover-models', action='store_true', help='显式执行 agy models 查询并刷新本地模型目录缓存')
    assess = sub.add_parser('assess', help='只读评估程序、目录元数据和任务选路')
    assess.add_argument('--discover-models', action='store_true', help='显式执行 agy models 查询，结果仅用于本次评估')
    audit = sub.add_parser('audit', help='单路或双路文件审计')
    audit.add_argument('--target', required=True)
    audit.add_argument('--focus', default='')
    audit.add_argument('--strategy', choices=['auto', 'single-pass', 'whole-1m', 'sharded'], default='auto')
    audit.add_argument('--single', action='store_true')
    audit.add_argument('--left', default='', help='左路引擎与模型规格（如 codex:default、agy:default，支持精确 slug、显示名或模糊别名如 agy:carbon）')
    audit.add_argument('--right', default='', help='右路引擎与模型规格（如 agy:default、codex:default，支持精确 slug、显示名或模糊别名）')
    audit.add_argument('--shard-tokens', type=_positive_arg, default=120_000)
    gate = sub.add_parser('gate', help='修改后运行明确验收命令')
    gate.add_argument('--target', required=True)
    gate.add_argument('--worker', default='', help='执行引擎与模型规格（如 codex:default、agy:default，支持精确 slug、显示名或模糊别名）')
    gate.add_argument('--verify-cmd', required=True)
    task = sub.add_parser('task', help='通用审查、方向、润色、数学和代码任务')
    task.add_argument('--target', required=True)
    task.add_argument('--kind', required=True, choices=['review', 'direction', 'polish', 'math', 'code'])
    task.add_argument('--engine', default='', help='后端与模型规格（如 codex:default、agy:default，支持精确 slug、显示名或模糊别名如 agy:carbon）')
    task.add_argument('--base-commit')
    task.add_argument('--verify-cmd')
    for command in (gate, task):
        group = command.add_mutually_exclusive_group(required=True)
        group.add_argument('--task')
        group.add_argument('--task-file')
        command.add_argument('--write-scope', action='append', default=[])
        command.add_argument('--max-rounds', type=_positive_arg, default=3)
    for command in (probe, assess, audit, gate, task):
        command.add_argument('--caller', choices=['auto', 'codex', 'antigravity'], default='auto')
        command.add_argument('--codex-input-budget', type=_positive_arg)
        command.add_argument('--agy-input-budget', type=_positive_arg)
    for command in (audit, gate, task):
        command.add_argument('--tier', choices=list(TIER_CHOICES), default='auto',
                             help='挡位策略：auto 按任务专长选模并分级思考挡位（攻坚用单模型最高挡 max，常规用 high，短润色用 medium，表面检查用 low）')
        command.add_argument('--retry-quota', action='store_true',
                             help='用户确认已充值或额度恢复后，忽略本地额度冷却记录重试发起')
        command.add_argument('--timeout', type=_positive_arg, default=600)
        command.add_argument('--dry-run', action='store_true')
    return parser


def main():
    _SESSION_QUOTA_BLOCKED.clear()
    args = build_arg_parser().parse_args()
    if os.environ.get('CAE_HARNESS_DEPTH') and args.subcommand not in ('probe', 'assess'):
        sys.exit(_print({'status': 'failed', 'error': '子任务再次调用本编排器会形成递归派工，已拒绝'}))
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _cancel_signal)
    try:
        handler = {'probe': cmd_probe, 'assess': cmd_assess, 'audit': cmd_audit, 'gate': cmd_gate, 'task': cmd_task}[args.subcommand]
        code = handler(args)
    except HarnessError as exc:
        code = _print({'status': exc.status, 'error': str(exc), 'caller': resolve_caller(args.caller), **exc.details})
    except (OSError, ValueError) as exc:
        code = _print({'status': 'failed', 'error': str(exc), 'caller': resolve_caller(args.caller)})
    sys.exit(code)


if __name__ == '__main__':
    main()
