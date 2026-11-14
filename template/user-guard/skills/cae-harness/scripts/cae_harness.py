#!/usr/bin/env python3


"""Codex / Antigravity 双向任务、分片审计与可执行验收。"""


import argparse
import base64
import collections
import concurrent.futures
import datetime
import fnmatch
import hashlib
import json
import math
import ntpath
import os
import posixpath
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
try:
    import tomllib
except ImportError:  # Python 3.9 / 3.10
    tomllib = None


# ════════════════════════════════════════════════════════════════════════════
# ① 全局常量、异常与回执 JSON Schema
# ════════════════════════════════════════════════════════════════════════════


SOURCE_EXTENSIONS = {'.f90', '.f95', '.f03', '.f08', '.f', '.for', '.c', '.cpp', '.cc', '.cxx', '.cu', '.cuh',
                     '.h', '.hpp', '.hh', '.py', '.jl', '.m', '.rs', '.go', '.ts', '.js', '.cmake', '.toml',
                     '.yaml', '.yml', '.json', '.md', '.tex', '.bib', '.rst', '.txt', '.lean', '.inc',
                     '.sh', '.bash', '.ps1', '.cfg', '.ini'}


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


HIGH_COMPLEXITY_CUES = (
    '守恒', 'conservation', '通量', '通量重构', '黎曼', 'riemann', '雅可比', 'jacobian',
    '熵稳定', 'entropy stability', 'dgsem', 'weno', '高阶格式', '间断伽辽金',
    '收敛阶', 'order of convergence', '数值稳定性', '边界条件', '特征分解', '特征值',
    '数学公式', '公式推导', '控制方程', '偏微分方程', '本构', '线性化', '牛顿法', 'newton',
    '证明', 'prove', '定理', '引理', '推导', 'derive', '变分',
    '根因', 'root cause', '死锁', 'deadlock', '竞态', 'race condition', '并发', 'concurrency',
    '数值耗散', '激波',
)


TRIVIAL_SURFACE_CUES = ('错别字', 'typo', '拼写', '缩进', '空行', '注释微调', '文案微调', '格式微调')


ECONOMY_DOWNGRADE_CUES = (
    '降挡', '降档', '降一挡', '降一档', '适当降挡', '适当降档', '降低挡位', '降低档位',
    '省额度', '省点额度', '省些额度', '节省额度', '节约额度', '省着点额度',
    '省token', '省 token', '省点token', '省点 token', '省些token', '省些 token',
    '节省token', '节省 token', '节省点token', '节省点 token', '节省一点token', '节省一点 token',
    '节约token', '节约 token', '省算力', '节省算力', '省开销', '节省开销', '轻量快速',
    'save tokens', 'save token', 'save quota', 'lower tier', 'lower effort',
)


QUOTA_LOCK = threading.RLock()


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


_SESSION_QUOTA_BLOCKED = {}


_CODEX_TOML_KEYS = {'model', 'model_reasoning_effort', 'model_context_window', 'model_auto_compact_token_limit'}


_TOML_LINE_TOKEN = re.compile(r'''"""(?:\\[\s\S]|(?!"{3})[\s\S])*"""|'{3}[\s\S]*?'{3}|"(?:\\.|[^"\\\n])*"|'[^'\n]*'|#[^\n]*|[\[\]{}\n]''')


_TOML_INTEGER = re.compile(r'(?:[+-]?(?:0|[1-9](?:_?[0-9])*)|0x[0-9A-Fa-f](?:_?[0-9A-Fa-f])*|0o[0-7](?:_?[0-7])*|0b[01](?:_?[01])*)')


_CUE_BOUNDARIES = re.compile(r'[，。；、,;.!?！？\n]+|\b(?:but|however|instead)\b|但是|而是|而只', re.I)


_NEGATION = re.compile(
    r"(?:不(?:要|用|必|需|须|能|可|得|会|想|愿|再|应|打算|准备|允许|降|省|节|考虑|讨论|涉及|检查|核查|分析|推导|证明|处理|复核|验证)|"
    r"无需|无须|禁止|排除|避免|跳过|别|勿|莫)[^，。；、,;.!?！？\n]{0,12}$|"
    r"(?:^|(?<![要能好行成可会]))不\s*$|"
    r"\b(?:no|not|never|without|exclude|skip|avoid|don't|cannot|can't)\b(?:\s+[\w-]+){0,5}\s*$", re.I)


ROUTER_MODEL_TAGS = {'orchestrator', 'router', 'auto'}


FAST_MODEL_TAGS = {'lite', 'mini', 'fast', 'flash', 'hydrogen', 'luna'}


NON_AI_MODEL_STOPWORDS = {
    'turbulence', 'rans', 'les', 'dns', 'sst', 'kepsilon', 'komega', 'spalart', 'allmaras',
    'epsilon', 'navier', 'stokes', 'euler', 'boltzmann', 'bgk', 'muscl', 'tvd', 'godunov',
    'smagorinsky', 'vreman', 'courant', 'knudsen', 'reynolds', 'prandtl', 'mach',
    'weno', 'cweno', 'eno', 'dg', 'dgsem', 'fvm', 'fem', 'fdm', 'cfd', 'cae', 'eos',
    'riemann', 'roe', 'hll', 'hllc', 'hlle', 'ausm', 'rusanov', 'rk', 'bdf', 'cfl',
    'model', 'models', 'default', 'auto', 'cli', 'lite', 'lightweight',
    'codex', 'antigravity', 'agy', 'openai', 'google', 'gemini', 'gpt',
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


_AGY_AUTH_REQUIRED = re.compile(
    r'\b(?:please (?:sign in|log in|login)|not (?:signed|logged) in|authentication required|'
    r'login required|sign-in required|unauthenticated)\b', re.I)


_QUOTA_EVENT_NAME = re.compile(r'^[0-9a-f]{32}\.json$')


MAX_INTEROP_STAGE_BYTES = 64 * 1024 * 1024


STAGED_PROMPT_SUFFIX = '\n[跨端只读工作区说明] 当前目录已按原相对路径只读镜像目标文件，请直接在当前目录下按相对路径读取。'


class HarnessError(RuntimeError):
    def __init__(self, message, status='failed', **details):
        super().__init__(message)
        self.status = status
        self.details = details


class OutputParseError(HarnessError):
    def __init__(self, message):
        super().__init__(message, 'invalid_output')


# ════════════════════════════════════════════════════════════════════════════
# ② 基础配置、跨平台路径与 WSL 互操作工具
# ════════════════════════════════════════════════════════════════════════════


def emit_event(event, **fields):
    with _EVENT_LOCK:
        print(json.dumps({'event': event, **fields}, ensure_ascii=False), file=sys.stderr, flush=True)


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}


def _toml_simple_string(raw):
    if re.fullmatch(r"'[^'\r\n]*'", raw):
        return raw[1:-1]
    if not re.fullmatch(r'"(?:[^"\\\x00-\x1f]|\\(?:["\\btnfr]|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}))*"', raw):
        return None
    try:
        def unescape(match):
            value = match.group()
            return chr(int(value[2:], 16)) if value[1] == 'U' else json.loads('"' + value + '"')

        return re.sub(r'\\(?:U[0-9a-fA-F]{8}|u[0-9a-fA-F]{4}|.)', unescape, raw[1:-1])
    except (ValueError, OverflowError):
        return None


def _toml_scalar_subset(text):
    """3.9/3.10 fallback: read only the supported top-level scalar keys."""
    result, seen = {}, set()
    parts, pos, depth = [], 0, 0
    for token in _TOML_LINE_TOKEN.finditer(text + '\n'):
        raw = token.group()
        parts.append(text[pos:token.start()])
        pos = token.end()
        if raw.startswith('#'):
            continue
        if raw.startswith(('"""', "'''")):
            parts.append('<multiline_string>')
            continue
        if raw == '[' and not depth and not ''.join(parts).strip():
            break  # First table; following bare keys are not top level.
        if raw in ('[', '{'):
            depth += 1
        elif raw in (']', '}'):
            depth -= 1
        if depth < 0:
            return {}
        if raw != '\n' or depth:
            parts.append(raw)
            continue
        line = ''.join(parts)
        parts.clear()
        match = re.fullmatch(r'''\s*([A-Za-z0-9_-]+|"(?:\\.|[^"\\])*"|'[^']*')\s*=\s*(.*?)\s*''', line)
        if not match:
            continue
        key, value = match.groups()
        key = _toml_simple_string(key) if key.startswith(('"', "'")) else key
        if key not in _CODEX_TOML_KEYS:
            continue
        if key in seen:
            return {}
        seen.add(key)
        if _TOML_INTEGER.fullmatch(value):
            result[key] = int(value, 0) if value.startswith(('0x', '0o', '0b')) else int(value)
        else:
            parsed = _toml_simple_string(value)
            if parsed is not None:
                result[key] = parsed
    return result


def _top_level_toml(path):
    try:
        text = path.read_text(encoding='utf-8-sig')
        if tomllib is None:
            return _toml_scalar_subset(text)
        config = tomllib.loads(text)
        return {key: config[key] for key in _CODEX_TOML_KEYS if key in config}
    except (OSError, ValueError):
        return {}


def _positive_int(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _local_command(args, cwd=None):
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(f'本地查询无法完成: {args[0]}', stderr=str(exc)) from exc
    if proc.returncode:
        raise HarnessError(f'本地查询失败: {args[0]}')
    return proc.stdout.strip()


def _wsl_path(path, windows=True):
    return _local_command(['wslpath', '-w' if windows else '-u', str(path)])


def _windows_env(var_name):
    var_name = str(var_name)
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', var_name):
        raise HarnessError(f'Windows 环境变量名不合法: {var_name}')
    powershell_exe = shutil.which('powershell.exe')
    if powershell_exe:
        command = [powershell_exe, '-NoProfile', '-NonInteractive', '-Command',
                   f"[Environment]::GetEnvironmentVariable('{var_name}')"]
    else:
        cmd_exe = shutil.which('cmd.exe')
        if not cmd_exe:
            raise HarnessError('未发现 powershell.exe 或 cmd.exe，无法解析 Windows 环境变量')
        command = [cmd_exe, '/d', '/c', 'echo', f'%{var_name}%']
    parent = Path(command[0]).parent
    value = _local_command(command, cwd=str(parent) if parent.is_dir() else None)
    if not value or (not powershell_exe and value == f'%{var_name}%'):
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


def _is_link(path, *, st=None):
    st = path.lstat() if st is None else st
    attributes = getattr(st, 'st_file_attributes', 0) or 0
    return stat.S_ISLNK(st.st_mode) or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _ps_literal(text):
    return "'" + text.replace("'", "''") + "'"


def _stage_wsl_readonly_workspace(target_dir, stage_root, rel_files=None):
    """将纯 Linux 文件镜像到 Windows 临时盘；清单不能逃出原目标树。"""
    target_dir = target_dir.resolve()
    stage_dir = stage_root / 'workspace'
    stage_dir.mkdir(parents=True, exist_ok=True)
    if rel_files is None:
        records = collect_target_files(target_dir)
    else:
        records = []
        for rel in rel_files:
            rel = _scope_patterns([rel])[0]
            src = target_dir / rel
            try:
                src.resolve().relative_to(target_dir)
                # 叶文件正常也可能经过目录链接，逐级检查阻止镜像树外材料。
                ancestors = [src] + list(src.parents)[:len(src.relative_to(target_dir).parts) - 1]
                if any(_is_link(path) for path in ancestors) or not src.is_file():
                    raise HarnessError(f'只读镜像不接受文件或目录链接: {rel}')
                records.append({'path': src, 'rel_path': rel, 'bytes': src.stat().st_size})
            except (OSError, ValueError) as exc:
                raise HarnessError(f'无法在目标目录内镜像文件: {rel}') from exc
    if sum(item['bytes'] for item in records) > MAX_INTEROP_STAGE_BYTES:
        raise HarnessError('WSL 纯 Linux 路径只读镜像超出 64MB 上限，请缩小 --target 范围或在 WSL 安装原生 codex')
    windows_names = {}
    for item in records:
        parts = Path(item['rel_path']).parts
        for index, part in enumerate(parts, 1):
            if (re.search(r'[<>:"|?*\x00-\x1f]', part) or part.endswith((' ', '.'))
                    or re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I)):
                raise HarnessError(f'源文件名无法在 Windows 镜像中无损表示: {item["rel_path"]}')
            spelling = '/'.join(parts[:index])
            key = spelling.casefold()
            if key in windows_names and windows_names[key] != spelling:
                raise HarnessError(f'源路径在 Windows 镜像中发生大小写冲突: {windows_names[key]} / {spelling}')
            windows_names[key] = spelling
    for item in records:
        try:
            item['path'].resolve().relative_to(target_dir)
        except (OSError, ValueError) as exc:
            raise HarnessError(f'镜像源已离开目标目录: {item["rel_path"]}') from exc
        dst = stage_dir / item['rel_path']
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item['path'], dst)
    return stage_dir


def _rewrite_staged_paths(parsed, stage_dir):
    # Windows 可改变回执中的大小写；使用镜像里保留的原拼写还原 ext4 路径。
    original_paths = {}
    for directory, _dirs, files in os.walk(stage_dir):
        for name in files:
            relative = (Path(directory) / name).relative_to(stage_dir).as_posix()
            original_paths[relative.casefold()] = relative

    prefixes = [(str(stage_dir).replace('\\', '/').rstrip('/') + '/', os.name == 'nt'),
                (str(stage_dir.resolve()).replace('\\', '/').rstrip('/') + '/', os.name == 'nt')]
    if _is_posix() and shutil.which('wslpath'):
        try:
            prefixes.append((_wsl_path(stage_dir, windows=True).replace('\\', '/').rstrip('/') + '/', True))
        except HarnessError:
            pass

    def _clean(raw_path):
        norm = raw_path.strip().replace('\\', '/')
        for prefix, case_insensitive in prefixes:
            value = norm.casefold() if case_insensitive else norm
            base = prefix.casefold() if case_insensitive else prefix
            if value.startswith(base):
                relative = posixpath.normpath(norm[len(prefix):])
                return original_paths.get(relative.casefold(), relative)
        return original_paths.get(posixpath.normpath(norm).casefold(), raw_path)

    if 'read_files' in parsed:
        parsed['read_files'] = [_clean(item) for item in parsed['read_files']]
    if 'unread_files' in parsed:
        parsed['unread_files'] = [{**item, 'file': _clean(item['file'])} for item in parsed['unread_files']]
    if 'findings' in parsed:
        parsed['findings'] = [{**item, 'file': _clean(item['file'])} for item in parsed['findings']]
    return parsed


def _interop_command(command, work_dir, temp_dir, input_text, codex_home=None):
    """监督进程写入本次真实 Windows PID，供 WSL 定向清理进程树。"""
    pid_file = temp_dir / 'native.pid'
    if input_text is None:
        pipes = [
            '$output = $p.StandardOutput.BaseStream.CopyToAsync([Console]::OpenStandardOutput())',
            '$errors = $p.StandardError.BaseStream.CopyToAsync([Console]::OpenStandardError())',
            'while ($null -ne ($line = [Console]::ReadLine())) {',
            '$p.StandardInput.WriteLine($line)', '$p.StandardInput.Flush()', '}',
            '$p.StandardInput.Close()', '$p.WaitForExit()', '$output.Wait()', '$errors.Wait()',
        ]
    else:
        prompt_file = temp_dir / 'input.txt'
        prompt_file.write_text(input_text, encoding='utf-8')
        pipes = [
            '$output = $p.StandardOutput.ReadToEndAsync()', '$errors = $p.StandardError.ReadToEndAsync()',
            '$inputBytes = [IO.File]::ReadAllBytes(' + _ps_literal(_wsl_path(prompt_file)) + ')',
            '$p.StandardInput.BaseStream.Write($inputBytes, 0, $inputBytes.Length)', '$p.StandardInput.BaseStream.Flush()',
            '$p.StandardInput.Close()', '$p.WaitForExit()',
            '[Console]::Out.Write($output.Result)', '[Console]::Error.Write($errors.Result)',
        ]
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
        *pipes,
        '$exitCode = $p.ExitCode', '$completed = $true', '} finally {',
        'if ($started -and -not $completed) { & taskkill.exe /T /F /PID $p.Id | Out-Null }',
        '$p.Dispose()', '}', 'exit $exitCode',
    ])
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    return ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded], pid_file


# ════════════════════════════════════════════════════════════════════════════
# ③ 模型目录、语义线索、挡位策略与执行画像
# ════════════════════════════════════════════════════════════════════════════


def _tokens_lower(text):
    return set(re.split(r'[^a-z0-9]+', str(text or '').lower())) - {''}


def _alias_tokens(text):
    return set(re.findall(r'[a-z0-9]+(?:\.[a-z0-9]+)*', str(text or '').lower()))


def _model_records(cache):
    items = cache.get('models', []) if isinstance(cache, dict) else cache
    if not isinstance(items, list):
        return []
    fields = ('slug', 'display_name', 'description', 'priority', 'visibility', 'default_reasoning_level',
              'context_window', 'max_context_window', 'effective_context_window_percent',
              'provider', 'model_provider', 'category', 'routing_type', 'model_specialty', 'modelSpecialty', 'supported_in_api')
    records = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('slug'), str) or not item['slug'].strip():
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
            delegating = [e for e in efforts if e == 'ultra' or e in (item.get('delegating_efforts') or [])]
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


def _catalog_order(item):
    priority = item.get('priority')
    if type(priority) is not int or priority < 0:
        priority = 9999
    description = str(item.get('description') or '').strip().lower()
    return (bool(re.match(r'(?:older|previous|legacy)\b', description)), priority,
            str(item.get('slug') or '').lower())


def _is_agy_orchestrator(record):
    # Published Antigravity fixed composite: planning/review and execution
    # alternate within the same agent. It does not accept a reasoning tier.
    return 'orchestrator' in _tokens_lower(f"{record.get('slug', '')} {record.get('display_name', '')}")


def _model_category(prefix, record):
    slug = str(record.get('slug') or '').lower()
    label = str(record.get('display_name') or '')
    category = str(record.get('category') or record.get('routing_type') or '').lower()
    provider = str(record.get('provider') or record.get('model_provider') or '').lower()
    if prefix == 'agy' and _is_agy_orchestrator(record):
        return 'fixed_composite'
    tags = _tokens_lower(f'{slug} {label}')
    if tags & ROUTER_MODEL_TAGS or category in ('router', 'composite', 'hybrid', 'auto', 'fixed_composite'):
        return 'router_or_composite'
    if 'auto-review' in slug or record.get('visibility') == 'hide':
        return 'hidden_or_internal'
    if category in ('hosted', 'third_party', 'hosted_or_third_party', 'third-party'):
        return 'hosted_or_third_party'
    if prefix == 'agy' and slug.startswith('gemini-') and provider in ('', 'google', 'gemini'):
        return 'native'
    if prefix == 'codex' and provider in ('', 'openai') and (
            provider == 'openai' or re.match(r'^(?:gpt-[0-9]+(?:\.[0-9]+)?(?:-|$)|o[1-9]\d*(?:-|$))', slug)):
        return 'native'
    if provider or re.match(r'^(?:claude-|gpt-oss-|deepseek-|qwen-|mistral-|llama-)', slug):
        return 'hosted_or_third_party'
    return 'unknown'


def _model_specialty(record):
    specialty = str(record.get('model_specialty') or record.get('modelSpecialty') or '').lower()
    description = str(record.get('description') or '').lower()
    if specialty in ('fast', 'lightweight', 'speed') or _tokens_lower(record.get('slug')) & FAST_MODEL_TAGS or re.search(
            r'\b(?:fast and affordable|easier tasks|lightweight)\b', description):
        return 'fast'
    if specialty in ('deep', 'reasoning', 'deep_reasoning') or 'astra' in _tokens_lower(record.get('slug')) or re.search(
            r'\b(?:most demanding|frontier intelligence|deepest reasoning|hardest problems)\b', description):
        return 'deep'
    return 'engineering'


def _codex_versioned_line(slug):
    match = re.match(r'^gpt-(\d+(?:\.\d+)*)-([a-z][a-z0-9-]*)$', str(slug or '').strip().lower())
    if not match:
        return None, None
    return tuple(int(part) for part in match.group(1).split('.')), match.group(2)


def _unique_model_matches(query, models):
    by_slug = {item['slug']: item for item in models}
    if len(by_slug) > 1:
        choices = sorted(by_slug)
        raise HarnessError(f'模型别名 {query!r} 不唯一，请指定精确 slug', model_alias=query, matching_models=choices)
    return next(iter(by_slug.values()), {})


def _exact_model_record(models, query):
    value = query.lower()
    return _unique_model_matches(query, [item for item in models if item['slug'].lower() == value]) or _unique_model_matches(
        query, [item for item in models if str(item.get('display_name') or '').lower() == value])


def _resolve_explicit_codex_record(models, explicit_model):
    query = str(explicit_model or '').strip()
    if not query:
        return {}
    exact = _exact_model_record(models, query)
    if exact:
        return exact
    terms = _alias_tokens(query)
    matches = [
        item for item in models if terms and terms <= _alias_tokens(f"{item['slug']} {item.get('display_name', '')}")
        and _model_category('codex', item) != 'hidden_or_internal'
    ]
    if len({item['slug'] for item in matches}) > 1 and not any(re.search(r'\d', tok) for tok in terms):
        parsed = [(item, *_codex_versioned_line(item['slug'])) for item in matches]
        lines = {line for _item, ver, line in parsed if ver is not None and line is not None}
        if len(lines) == 1 and all(ver is not None for _item, ver, _line in parsed):
            return max(parsed, key=lambda row: (row[1], -_catalog_order(row[0])[1]))[0]
    return _unique_model_matches(query, matches)


def _agy_families(models):
    families = {}
    for item in models:
        if _model_category('agy', item) in ('fixed_composite', 'router_or_composite'):
            continue
        name, effort = _agy_slug_tier(item['slug'])
        if name and effort:
            family = families.setdefault(name, {})
            family[effort] = _unique_model_matches(name, [x for x in (family.get(effort), item) if x])
    return families


def _agy_family_order(name):
    generation = re.search(r'^gemini-(\d+(?:\.\d+)*)-', name)
    version = tuple(int(v) for v in generation.group(1).split('.')) if generation else ()
    tags = _tokens_lower(name)
    # Carbon is the published high-difficulty engineering / science model.
    priority = next((n for n, tag in enumerate(('carbon', 'argon', 'pro')) if tag in tags), 9)
    return tuple(-v for v in (version + (0, 0, 0))[:3]), priority, name


def _match_effort_level(supported, target_tier, delegating_efforts=None):
    """max 表示目录声明的最高非委派挡；仅显式 ultra 可以放行委派挡。"""
    valid = [item for item in supported if item in EFFORT_LADDER]
    if target_tier != 'ultra':
        blocked = set(delegating_efforts or ()) | {'ultra'}
        valid = [item for item in valid if item not in blocked]
    if not valid:
        return None
    if target_tier in ('max', 'ultra'):
        return max(valid, key=EFFORT_LADDER.index)
    target_rank = EFFORT_LADDER.index(target_tier)
    at_or_below = [item for item in valid if EFFORT_LADDER.index(item) <= target_rank]
    return max(at_or_below, key=EFFORT_LADDER.index) if at_or_below else min(valid, key=EFFORT_LADDER.index)


def _only_delegating_efforts(record, tier):
    efforts = record.get('supported_efforts') or []
    return bool(efforts and tier != 'ultra' and not _match_effort_level(efforts, tier, record.get('delegating_efforts')))


def _match_agy_family(family, tier):
    return family.get(_match_effort_level(family, tier), {})


def _resolve_explicit_agy_record(models, query, tier):
    exact = _exact_model_record(models, query)
    if exact:
        return exact, 'explicit_engine_model'
    terms = _alias_tokens(query)
    variant_terms = terms & set(EFFORT_LADDER)
    family_terms = terms - variant_terms
    wants_composite = bool(terms & ROUTER_MODEL_TAGS or '混动' in query)
    candidates = []
    if family_terms and not wants_composite:
        for family, variants in _agy_families(models).items():
            if family_terms <= _alias_tokens(family):
                if variant_terms:
                    candidates.extend(record for level, record in variants.items() if level in variant_terms)
                else:
                    record = _match_agy_family(variants, tier)
                    if record:
                        candidates.append(record)
                    elif variants:
                        # Expose a named ultra-only family as blocked, not unknown.
                        candidates.extend(variants.values())
    for item in models:
        composite = _model_category('agy', item) in ('fixed_composite', 'router_or_composite')
        if composite != wants_composite:
            continue
        if not composite and _agy_slug_tier(item['slug'])[1]:
            continue
        if (terms and terms <= _alias_tokens(f"{item['slug']} {item.get('display_name', '')}")) or (
                wants_composite and not terms and composite):
            candidates.append(item)
    return _unique_model_matches(query, candidates), 'explicit_fuzzy_model'


def _semantic_text(text):
    return re.sub(r'(?m)^\s*>[^\n]*|\x60{3}[\s\S]*?\x60{3}|\x60[^\x60\n]*\x60|'
                  r'“[^”]*”|‘[^’]*’|"[^"\n]*"|(?<![A-Za-z0-9_])\x27[^\x27\n]*\x27(?![A-Za-z0-9_])',
                  ' ', str(text or ''))


def _cue_is_negated(prefix):
    # “能不能降档”“要不省点”是在提议；不能因其中一个“不”反转意图。
    prefix = re.sub(r'(?<!不)(?:能不能|可不可以|要不|不妨|何不)', ' ', prefix)
    return bool(_NEGATION.search(prefix))


def _cue_pattern(cue):
    escaped = re.escape(cue)
    # 中英文混写中的空白不影响意图，例如“省  token”和“save   tokens”。
    escaped = escaped.replace(r'\ ', r'\s+')
    if cue.isascii():
        return r'(?<![A-Za-z0-9_])' + escaped + r'(?![A-Za-z0-9_])'
    return escaped


def _has_cue(text, cues, *, negated=False):
    if not cues:
        return False
    pattern = re.compile('|'.join(_cue_pattern(cue) for cue in cues), re.I)
    for clause in _CUE_BOUNDARIES.split(_semantic_text(text)):
        for match in pattern.finditer(clause):
            if _cue_is_negated(clause[:match.start()]) == negated:
                return True
    return False


def _has_negated_cue(text, cues):
    return _has_cue(text, cues, negated=True)


def _has_positive_cue(text, cues):
    return _has_cue(text, cues)


def _extract_prompt_model_hint(prompt_text):
    text = _semantic_text(prompt_text).strip()
    if not text:
        return ''
    stop_words = NON_AI_MODEL_STOPWORDS - ROUTER_MODEL_TAGS
    for pattern in PROMPT_MODEL_HINT_PATTERNS:
        for match in pattern.finditer(text):
            prefix = _CUE_BOUNDARIES.split(text[:match.start()])[-1]
            if _cue_is_negated(prefix):
                continue
            cand = re.sub(r'\s+', ' ', match.group(1)).strip()
            if not cand:
                continue
            tokens = [tok for tok in re.findall(r'[a-z0-9]+', cand.lower()) if tok]
            collapsed = re.sub(r'[^a-z0-9]+', '', cand.lower())
            # 通用名称与 CFD 格式名称应排除，完整 gpt/gemini 型号则保留。
            provider_qualifiers = {'gpt', 'gemini', 'codex', 'agy', 'openai', 'google'}
            if collapsed in stop_words or any(tok in stop_words - provider_qualifiers for tok in tokens):
                continue
            if collapsed in PHYSICAL_SUBSTANCE_TAGS:
                has_ai_qualifier = bool(re.search(r'(?:\bAI\b|\bagy\b|\bcodex\b|\bgemini\b|\borchestrator\b|[()（）])', match.group(0), re.IGNORECASE))
                tail_ctx = text[match.end():min(len(text), match.end() + 24)]
                head_ctx = text[max(0, match.start() - 16):match.start()]
                if not has_ai_qualifier and (
                    PHYSICS_MODEL_CONTEXT_RE.search(tail_ctx) or PHYSICS_MODEL_CONTEXT_RE.search(head_ctx)
                ):
                    continue
            if tokens or '混动' in cand:
                return cand
    return ''


def select_task_tier(requested_tier='auto', kind='review', text='', file_count=1, estimated_tokens=0):
    """默认满血；显式挡位、省流意图与非 ultra 环境默认值可覆盖自动策略。"""
    if requested_tier and requested_tier != 'auto':
        return requested_tier, 'explicit_tier_flag'
    sample = (text or '').strip()
    prose_requests = {
        'max': ('用最高挡', '用最高档', '选最高挡', '选最高档', '用最大挡', '用最大档', '使用 max 挡', 'use max effort'),
        'xhigh': ('用超高挡', '用超高档', '选超高挡', '选超高档', '使用 xhigh 挡', 'use xhigh effort'),
        'low': ('用低挡', '用低档', '选低挡', '选低档', '低挡快速', '低档快速', '降到低挡', '降到低档',
                '最低挡', '最低档', '使用 low 挡', 'use low effort'),
        'medium': ('用中挡', '用中档', '选中挡', '选中档', '降到中挡', '降到中档', '使用 medium 挡', 'use medium effort'),
        'high': ('用高挡', '用高档', '选高挡', '选高档', '降到高挡', '降到高档', '使用 high 挡', 'use high effort'),
    }
    for level, cues in prose_requests.items():
        if _has_positive_cue(sample, cues):
            return level, 'user_description_' + level
    env_tier = os.environ.get('CAE_DEFAULT_TIER', '').strip().lower()
    if env_tier in TIER_CHOICES and env_tier not in ('auto', 'ultra'):
        return env_tier, 'env_default_tier'
    if kind == 'polish' and not _has_positive_cue(sample, ('证明', '推导', '根因', 'prove', 'derive')):
        if file_count <= 3 and estimated_tokens <= 4_000 and len(sample) <= 2_500:
            return 'medium', 'complexity_lightweight_polish'
        return 'high', 'complexity_long_polish'
    if _has_positive_cue(sample, ECONOMY_DOWNGRADE_CUES):
        if (kind in ('review', 'code', 'audit')
                and (_has_positive_cue(sample, TRIVIAL_SURFACE_CUES)
                     or _has_positive_cue(sample, ('轻量快速', '快速扫', '简单扫', '快速看')))
                and not _has_positive_cue(sample, HIGH_COMPLEXITY_CUES)
                and not _has_positive_cue(sample, ('兼容', '回归', '正确性', '安全', '功能', '全量审计', 'verify', 'regression'))):
            return 'low', 'economy_trivial_surface_low'
        if (kind in ('math', 'direction') or _has_positive_cue(sample, HIGH_COMPLEXITY_CUES)) and not _has_positive_cue(
                sample, ('降挡', '降档', '降一挡', '降一档', '适当降挡', '适当降档', '降低挡位', '降低档位', 'lower tier', 'lower effort')):
            return 'max', f'task_specialty_{kind}_max' if kind in ('math', 'direction') else 'complexity_high_domain_max'
        return 'high', 'economy_adaptive_high'
    if kind in ('math', 'direction'):
        return 'max', f'task_specialty_{kind}_max'
    if _has_positive_cue(sample, HIGH_COMPLEXITY_CUES):
        return 'max', 'complexity_high_domain_max'
    return 'max', 'default_flagship_max'


def _execution_side(info):
    return info.get('execution_side') or (
        'windows' if info.get('windows_interop') or not _is_posix() else 'posix')


def _execution_environment(info):
    return {'side': _execution_side(info), 'windows_interop': bool(info.get('windows_interop')),
            'binary': info.get('binary'), 'config_home': info.get('config_home'),
            'authentication_status': info.get('authentication_status', 'not_checked'),
            'fallback_from': info.get('fallback_from')}


def _execution_budget(prefix, info, record):
    default = _positive_int(record.get('context_window'))
    maximum = _positive_int(record.get('max_context_window'))
    configured = _positive_int(info.get('configured_window_tokens'))
    compact = _positive_int(info.get('effective_compaction_tokens'))
    window = maximum or default
    percent = _positive_int(record.get('effective_context_window_percent'))
    if percent is not None and percent > 100:
        percent = None
    effective_window = window * percent // 100 if window is not None and percent is not None else None
    model_limit = effective_window if effective_window is not None else window
    requested = _positive_int(info.get('requested_input_budget_tokens'))
    policy_cap = 120_000
    cap = min((v for v in (model_limit, compact) if v is not None), default=None)
    if cap is None:
        budget = requested or policy_cap
        source = 'explicit_unverified_planning_budget' if requested else 'conservative_estimate_without_model_window'
    elif requested:
        budget, source = min(requested, cap), 'explicit_planning_budget_capped'
    else:
        budget = min(cap, max(1, cap * 9 // 10))
        if not window:
            budget = min(budget, policy_cap)
        source = ('policy_estimate_90pct_of_planning_cap' if window else
                  'conservative_estimate_capped_by_configured_compaction')
    return {
        'model_default_window_tokens': default, 'model_declared_max_window_tokens': maximum,
        'frontend_configured_window_tokens': configured, 'configured_compaction_tokens': compact,
        'selected_window_tokens': window, 'cli_context_window_tokens': window if prefix == 'codex' else None,
        'window_source': ('model_declared_max' if maximum else 'model_declared_default' if default else 'unknown'),
        'declared_effective_context_percent': percent,
        'catalog_effective_window_tokens': effective_window,
        'effective_window_source': 'catalog_percent_of_selected_window' if effective_window is not None else None,
        'planning_cap_tokens': cap, 'requested_input_budget_tokens': requested,
        'input_budget_tokens': budget, 'policy_reserve_tokens': cap - budget if cap is not None else None,
        'budget_source': source,
        'capacity_verified': False,
    }


def _profile(prefix, info, spec, slug, record, tier, source, effort=None, cli_effort=None,
             effort_source='unknown', error=None):
    catalog = info.get('model_catalog') or {}
    capacity = _execution_budget(prefix, info, record)
    if prefix == 'codex' and not capacity['selected_window_tokens'] and capacity['frontend_configured_window_tokens']:
        error = error or ('该子模型的默认及最大窗口均无目录声明，无法排除宿主全局窗口覆盖；'
                          '请刷新目录或选择有窗口声明的模型。')
    declared = effort if effort_source in ('catalog_declared', 'catalog_variant') else None
    if effort_source == 'fixed_configuration':
        adjustment = 'fixed_configuration_ignores_requested_tier'
    elif declared is None:
        adjustment = 'unsupported_tier_not_verified' if cli_effort == tier else 'requested_tier_not_verified'
    elif effort != tier:
        direction = 'lower' if EFFORT_LADDER.index(effort) < EFFORT_LADDER.index(tier) else 'higher'
        adjustment = f'{direction}_catalog_effort_or_model_variant'
    else:
        adjustment = None
    return {
        'spec': f'{prefix}:{slug}' if slug else f'{prefix}:default',
        'requested_engine': spec, 'requested_model': _engine_parts(spec)[1] or None,
        'model': slug or None, 'model_category': _model_category(prefix, record) if record else 'unknown',
        'reasoning_effort': effort, 'cli_effort': cli_effort, 'declared_effort': declared,
        'effective_effort': None, 'effort_verified': False,
        'tier': tier, 'selected_tier': tier, 'profile_source': source, 'effort_source': effort_source,
        'tier_adjustment': adjustment, 'execution_blocked_reason': error,
        'capacity': capacity, 'model_catalog': catalog,
        'model_listing': info.get('model_listing'),
        'catalog_usage': ('listing_declared' if catalog.get('source') == 'agy_models_cli' else
                          'unverified_client_cache' if catalog.get('model_count') else 'unverified_or_missing'),
        'execution_environment': _execution_environment(info),
    }


def _require_execution_profile(profile):
    error = profile.get('execution_blocked_reason')
    if error:
        raise HarnessError(error, execution_profile=profile)


def _codex_profile(info, spec, record, slug, tier, source):
    effort = _match_effort_level(record.get('supported_efforts', []), tier, record.get('delegating_efforts'))
    if _only_delegating_efforts(record, tier):
        return _profile('codex', info, spec, slug, record, tier, source, effort_source='no_non_delegating_effort',
                        error='目录未声明可用的非委派挡位，请指定其他模型或显式选择 --tier ultra。')
    if effort:
        return _profile('codex', info, spec, slug, record, tier, source, effort, effort, 'catalog_declared')
    # Never inherit an interactive delegated setting. An unlisted named model
    # may receive a requested tier but the receipt cannot claim it is supported.
    effort = tier if tier != 'max' else 'high'
    return _profile('codex', info, spec, slug, record, tier, source, effort, effort, 'unverified_request')


def _agy_profile(info, spec, record, slug, tier, source):
    if _is_agy_orchestrator(record or {'slug': slug}):
        profile = _profile('agy', info, spec, slug, record, tier, source, effort_source='fixed_configuration')
        profile.update(model_category='fixed_composite',
                       fixed_configuration='same_agent_planning_review_and_execution',
                       effort_option_supported=False)
        return profile
    _, variant = _agy_slug_tier(slug)
    error = '模型变体会启用自动委派；请显式选择 --tier ultra 或其他模型。' if variant == 'ultra' and tier != 'ultra' else None
    return _profile('agy', info, spec, slug, record, tier, source, variant or None, None,
                    'catalog_variant' if record and variant else 'unverified_request' if variant else 'unknown', error)


def _select_codex_frontier_record(models):
    native = [record for record in models if _model_category('codex', record) == 'native']
    return min(native, key=_catalog_order) if native else {}


def _select_codex_specialty_record(info, kind, tier, prompt_text, estimated_tokens):
    models = [item for item in info.get('models', []) if _model_category('codex', item) == 'native']
    eligible = sorted((item for item in models if not _only_delegating_efforts(item, tier)), key=_catalog_order)
    # 同一产品线只自动采用最新一代；目录优先级仍用于不同产品线之间的选择。
    latest = {}
    for item in eligible:
        version, line = _codex_versioned_line(item['slug'])
        if version is not None:
            latest[line] = max(latest.get(line, ()), version)
    current = []
    for item in eligible:
        version, line = _codex_versioned_line(item['slug'])
        if version is None or version == latest[line]:
            current.append(item)
    eligible = current
    if not eligible:
        return {}, 'no_non_delegating_effort' if models else 'no_native_catalog_candidate'

    def budget(record):
        return _execution_budget('codex', info, record)['input_budget_tokens']

    if tier == 'low' and not _has_negated_cue(prompt_text, ('轻量', '极速', 'lite', 'mini', 'fast', 'flash', 'luna')):
        fast = [item for item in eligible if _model_specialty(item) == 'fast' and estimated_tokens <= budget(item)]
        if fast:
            return fast[0], 'specialty_fast_lightweight'
    if (kind in ('math', 'direction') or (kind == 'review' and tier != 'low'
                                         and _has_positive_cue(prompt_text, HIGH_COMPLEXITY_CUES))):
        deep = [item for item in eligible if _model_specialty(item) == 'deep' and estimated_tokens <= budget(item)]
        if deep:
            return deep[0], 'specialty_deep_reasoning'
    # 满血模式不能因主力目录缺失而静默切到轻量模型。
    non_lightweight = [item for item in eligible if _model_specialty(item) != 'fast']
    workhorse = [item for item in non_lightweight if _model_specialty(item) == 'engineering'] or non_lightweight
    if not workhorse:
        return {}, 'no_native_catalog_candidate'
    fitting = [item for item in workhorse if estimated_tokens <= budget(item)]
    if not fitting and kind in ('audit', 'code', 'gate'):
        return max(workhorse, key=budget), 'largest_engineering_budget'
    return (fitting or workhorse)[0], 'frontier_catalog'


def resolve_execution_profile(backends, spec, tier='max', prompt_text='', kind='review', estimated_tokens=0):
    prefix, explicit_model = _engine_parts(spec)
    info = backends.get(prefix, {})
    models = info.get('models', [])
    hint = _extract_prompt_model_hint(prompt_text) if not explicit_model and models else ''
    if prefix == 'codex':
        if explicit_model or hint:
            try:
                record = _resolve_explicit_codex_record(models, explicit_model or hint)
            except HarnessError:
                if explicit_model:
                    raise
                record, hint = {}, ''
            if record or explicit_model:
                slug = record.get('slug') or explicit_model
                source = ('prompt_fuzzy_model' if hint else 'explicit_engine_model' if slug.lower() == explicit_model.lower()
                          else 'explicit_fuzzy_model')
                return _codex_profile(info, spec, record, slug, tier, source)
        record, source = _select_codex_specialty_record(info, kind, tier, prompt_text, estimated_tokens)
        if record:
            return _codex_profile(info, spec, record, record['slug'], tier, source)
    else:
        if explicit_model or hint:
            try:
                record, source = _resolve_explicit_agy_record(models, explicit_model or hint, tier)
            except HarnessError:
                if explicit_model:
                    raise
                record, source, hint = {}, '', ''
            if record or explicit_model:
                return _agy_profile(info, spec, record, record.get('slug') or explicit_model, tier,
                                    'prompt_fuzzy_model' if hint else source if record else 'explicit_engine_model')
        native = [item for item in models if _model_category('agy', item) == 'native']
        families = _agy_families(native)
        eligible = sorted((name for name in families if _match_agy_family(families[name], tier)), key=_agy_family_order)
        if eligible:
            is_fast = lambda name: bool(_tokens_lower(name) & FAST_MODEL_TAGS)
            normal = [name for name in eligible if not is_fast(name)]
            forbid_lightweight = _has_negated_cue(prompt_text, ('轻量', '极速', 'lite', 'mini', 'fast', 'flash', 'hydrogen', '氢'))
            if not normal and (tier != 'low' or forbid_lightweight):
                return _profile(prefix, info, spec, None, {}, tier, 'no_native_catalog_candidate',
                                error='未找到原生旗舰模型，请刷新目录或显式指定模型。')
            normal = normal or eligible
            chosen, source = normal[0], 'frontier_catalog'
            if tier == 'low' and not forbid_lightweight:
                fast = [name for name in eligible if is_fast(name)]
                if fast:
                    chosen, source = fast[0], 'specialty_fast_lightweight'
            elif kind in ('math', 'direction') or (kind == 'review' and tier != 'low'
                                                  and _has_positive_cue(prompt_text, HIGH_COMPLEXITY_CUES)):
                top_gen = _agy_family_order(normal[0])[0]
                top_normal = [name for name in normal if _agy_family_order(name)[0] == top_gen]
                chosen = max(top_normal, key=lambda name: EFFORT_LADDER.index(
                    _agy_slug_tier(_match_agy_family(families[name], tier)['slug'])[1]))
                source = 'specialty_deep_reasoning'
            record = _match_agy_family(families[chosen], tier)
            return _agy_profile(info, spec, record, record['slug'], tier, source)
        source = 'no_non_delegating_effort' if families else 'no_native_catalog_candidate'
    return _profile(prefix, info, spec, None, {}, tier, source, effort_source=source,
                    error='未找到符合自动选路规则的原生目录模型，请刷新目录或显式指定后端模型。')


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


def _read_codex_catalog(home, now):
    path = Path(home) / 'models_cache.json'
    cache = _read_json(path)
    models = _model_records(cache)
    catalog = _catalog_metadata(path, cache, models, 'codex_client_cache', now)
    cache_version = cache.get('client_version') if isinstance(cache, dict) else None
    if isinstance(cache_version, str):
        catalog['client_version_at_collection'] = cache_version
    return models, catalog


def _current_catalog(info):
    catalog = info.get('model_catalog') or {}
    return bool(info.get('models') and catalog.get('model_count') and catalog.get('fetched_at')
                and catalog.get('freshness') == 'within_assessment_window')


def _refresh_codex_listing(info, batch):
    _enforce_preflight_quota({'codex': info}, ('codex:default',), batch=batch)
    try:
        listing = _query_codex_listing(info)
    except HarnessError as exc:
        detail = str(exc.details.get('stderr') or exc)
        if _is_quota_exhausted(detail):
            cooldown = _record_quota_exhausted('codex', info, detail, batch=batch)
            raise HarnessError('Codex 模型目录查询遇到额度限制，停止派工', 'quota_exhausted',
                               preflight_blocked=False, execution_environment=_execution_environment(info),
                               **_quota_user_choices('codex', cooldown)) from exc
        raise
    fetched = datetime.datetime.now(datetime.timezone.utc)
    models, catalog = _read_codex_catalog(info['config_home'], fetched)
    if catalog['freshness'] != 'within_assessment_window':
        raise HarnessError('Codex 模型列表已返回，但同侧客户端缓存缺少有效的采集时间或模型记录，停止派工。',
                           model_catalog=catalog, execution_environment=_execution_environment(info))
    cached_by_slug = {record['slug']: record for record in models}
    listed_models = []
    for slug, row in listing.items():
        cached = cached_by_slug.get(slug, {})
        # The app-server list does not expose context windows. Join these
        # fields only from the same-side cache and on the exact model slug.
        available = {'slug': slug, 'visibility': 'list'}
        for upstream, field in (('displayName', 'display_name'), ('description', 'description'),
                                ('defaultReasoningEffort', 'default_reasoning_level'),
                                ('supportedReasoningEfforts', 'supported_reasoning_levels'),
                                ('modelSpecialty', 'model_specialty')):
            if row.get(upstream) is not None:
                available[field] = row[upstream]
        normalized = _model_records({'models': [available]})[0]
        listed_models.append({**cached, **normalized, 'context_cache_slug_matched': bool(cached)})
    info.update(models=listed_models, model_catalog=catalog,
                model_listing={'source': 'codex_app_server_model_list', 'fetched_at': fetched.isoformat(),
                               'model_count': len(listed_models),
                               'context_fields': 'codex_client_cache_exact_slug_only'},
                verified_status='model_listing_only', capability_source='model_list_and_same_side_cache')


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
    if not isinstance(models, list):
        return []
    return [item for item in models if isinstance(item, dict) and isinstance(item.get('slug'), str)
            and item['slug'].strip()]


def _agy_info(binary, requested, now=None):
    root, interop = _agy_home(binary)
    cache_file = root / 'antigravity-cli' / 'cache' / 'cae_models_cache.json'
    data = _read_json(cache_file)
    models = _agy_cached_records(data)
    return {'available': bool(binary), 'detected': bool(binary), 'binary': binary, 'family': 'antigravity',
            **_agy_config(root), 'models': models, 'windows_interop': interop,
            'verified_status': 'unverified', 'authentication_status': 'not_checked',
            'model_catalog': _catalog_metadata(cache_file, data, models, 'agy_harness_cache',
                                               now or datetime.datetime.now(datetime.timezone.utc)),
            'execution_config_supported': not os.environ.get('CAE_AGY_HOME'),
            'configured_window_tokens': None, 'effective_compaction_tokens': None,
            'requested_input_budget_tokens': requested, 'safe_input_budget_tokens': requested or 120_000,
            'budget_source': 'explicit_planning_budget' if requested else 'policy_estimate_120000',
            'capability_source': 'local_settings_and_cache' if models else 'local_settings',
            'capacity_notice': '宿主压缩阈值未核实，规划预算不表示无压缩容量。'}


def _can_fallback_agy(info):
    return _is_posix() and not info.get('windows_interop') and not os.environ.get('CAE_AGY_BINARY')


def _fallback_agy_info(info):
    if not _can_fallback_agy(info):
        return None
    binary = _find_windows_agy_in_wsl()
    if not binary or binary == info.get('binary'):
        return None
    fallback = _agy_info(binary, info.get('requested_input_budget_tokens'))
    fallback['fallback_from'] = _execution_environment(info)
    fallback['fallback_reason'] = 'native_authentication_required'
    return fallback


def _run_agy_listing(info):
    binary = info['binary']
    command = [binary, 'models']
    if not info.get('windows_interop'):
        return run_process(command, Path.cwd(), timeout_sec=30)
    if not shutil.which('powershell.exe') or not shutil.which('taskkill.exe'):
        raise HarnessError('WSL 跨端查询需要 powershell.exe 与 taskkill.exe 进行进程监督')
    windows_temp = _wsl_path(Path(_windows_env('TEMP')), windows=False)
    with tempfile.TemporaryDirectory(prefix='cae-harness-', dir=windows_temp) as temp:
        path = Path(temp)
        command, pid_file = _interop_command(command, path, path, '', None)
        return run_process(command, path, timeout_sec=30, native_pid_file=pid_file)


def _refresh_agy_listing(info, batch, *, refresh_model_cache, record_quota=True):
    if not info.get('execution_config_supported', True):
        raise HarnessError('CAE_AGY_HOME 仅用于离线配置探测，执行查询请使用当前 CLI 的真实用户配置')
    if not info.get('binary'):
        raise HarnessError('未发现 agy，无法查询模型')
    first = info
    while True:
        _enforce_preflight_quota({'agy': info}, ('agy:default',), batch=batch,
                                 cleanup_expired=refresh_model_cache)
        result = _run_agy_listing(info)
        detail = ((result.stderr or '') + '\n' + (result.stdout or '')).strip()
        models = _parse_agy_models(result.stdout) if result.returncode == 0 else []
        if not models and _is_quota_exhausted(detail):
            if record_quota:
                cooldown = _record_quota_exhausted('agy', info, detail, batch=batch)
            else:
                cooldown = {'backend': 'agy', 'execution_side': _execution_side(info), 'exhausted': True,
                            **_parse_quota_reset(detail)}
            raise HarnessError('agy models 查询遇到额度限制，停止派工', 'quota_exhausted',
                               preflight_blocked=False, execution_environment=_execution_environment(info),
                               **_quota_user_choices('agy', cooldown))
        if result.returncode == 0:
            if not models:
                raise HarnessError('agy models 未返回可解析的目录，停止派工',
                                   execution_environment=_execution_environment(info))
            break
        if _AGY_AUTH_REQUIRED.search(detail):
            info['authentication_status'] = 'unauthenticated'
            alternate = _fallback_agy_info(info) if info is first else None
            if alternate:
                info = alternate
                continue
        raise HarnessError('agy models 查询失败，停止派工', returncode=result.returncode, stderr=detail[-1000:],
                           execution_environment=_execution_environment(info))
    fetched_at = datetime.datetime.now(datetime.timezone.utc)
    snapshot = {'source': 'agy_models_cli', 'fetched_at': fetched_at.isoformat(), 'models': models}
    if refresh_model_cache:
        cache_file = Path(info['config_home']) / 'antigravity-cli' / 'cache' / 'cae_models_cache.json'
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        except OSError as exc:
            info['model_cache_write_error'] = str(exc)
    info.update(models=models, authentication_status='authenticated', model_discovery_source='agy_models_cli',
                model_list_parsed=True, verified_status='model_listing_only', cached_model_catalog=info.get('model_catalog'),
                capability_source='agy_models_cli',
                model_catalog=_catalog_metadata(None, snapshot, models, 'agy_models_cli', fetched_at))
    return info


def probe_backends(codex_input_budget=None, agy_input_budget=None, discover_models=False, *, refresh_model_cache=True):
    """普通探测只读文件，程序发现和缓存均不代表认证或线上容量验证。"""
    now = datetime.datetime.now(datetime.timezone.utc)
    binary = _find_binary('codex')
    home, interop = _codex_home(binary)
    config_file = home / 'config.toml'
    config = _top_level_toml(config_file)
    models, catalog = _read_codex_catalog(home, now)
    interactive_model = config.get('model')
    if interactive_model:
        selected = next((item for item in models if item['slug'] == interactive_model), {})
    else:
        selected = _select_codex_frontier_record(models)
    configured = _positive_int(config.get('model_context_window'))
    cached_default = _positive_int(selected.get('context_window'))
    cached_max = _positive_int(selected.get('max_context_window'))
    inferred = configured or cached_default
    if inferred and cached_max:
        inferred = min(inferred, cached_max)
    compact = _positive_int(config.get('model_auto_compact_token_limit'))
    requested = codex_input_budget or _positive_int(os.environ.get('CAE_CODEX_INPUT_BUDGET'))
    codex = {'verified_status': 'unverified', 'authentication_status': 'not_checked',
        'available': bool(binary), 'detected': bool(binary), 'binary': binary,
        'family': 'codex', 'model': interactive_model, 'reasoning_effort': config.get('model_reasoning_effort'),
        'models': models, 'config_home': str(home),
        'config_files': {'cli': {'path': str(config_file), 'exists': config_file.is_file()}},
        'configured_model_source': str(config_file) if interactive_model else None, 'model_catalog': catalog,
        'windows_interop': interop, 'configured_window_tokens': configured, 'cached_default_window_tokens': cached_default,
        'cached_max_window_tokens': cached_max, 'inferred_window_tokens': inferred, 'effective_compaction_tokens': compact,
        'context_metadata_model': selected.get('slug'),
        'cached_effective_context_percent': selected.get('effective_context_window_percent'),
        'requested_input_budget_tokens': requested, 'capability_source': 'local_config_and_cache',
        'capacity_notice': '配置和缓存供预算估算，实际模型权限、压缩阈值及完整读取仍需执行验证。'}
    requested = agy_input_budget or _positive_int(os.environ.get('CAE_AGY_INPUT_BUDGET'))
    agy = _agy_info(_find_binary('agy'), requested, now)
    if discover_models:
        agy = _refresh_agy_listing(agy, _QuotaBatch(), refresh_model_cache=refresh_model_cache,
                                   record_quota=refresh_model_cache)
    result_map = {'codex': codex, 'agy': agy}
    for name, info in result_map.items():
        info['execution_side'] = _execution_side(info)
        prof = resolve_execution_profile(result_map, name + ':default', 'max', kind='code')
        deep_prof = resolve_execution_profile(result_map, name + ':default', 'max', kind='math')
        fast_prof = resolve_execution_profile(result_map, name + ':default', 'low', kind='review')
        info['frontier_model'] = prof['model']
        info['max_reasoning_effort'] = prof['reasoning_effort']
        info['auto_selectable'] = bool(info['detected'] and not prof['execution_blocked_reason'])
        info['specialty_routing'] = {
            'engineering_workhorse': {'model': prof['model'], 'reasoning_effort': prof['reasoning_effort']},
            'deep_reasoning': {'model': deep_prof['model'], 'reasoning_effort': deep_prof['reasoning_effort']},
            'fast_lightweight': {'model': fast_prof['model'], 'reasoning_effort': fast_prof['reasoning_effort']},
        }
        info['budget_model'] = prof['model']
        info['safe_input_budget_tokens'] = prof['capacity']['input_budget_tokens']
        info['budget_source'] = prof['capacity']['budget_source']
        cooldown = _load_active_quota_cooldown(name, info, cleanup_expired=refresh_model_cache)
        info['quota_status'] = 'exhausted_cached' if cooldown else 'unknown_until_called'
        info['quota_cooldown'] = cooldown
    return result_map


def _capability_matrix(prefix, info):
    models = info['models']
    families = _agy_families(models) if prefix == 'agy' else {}
    rows = []
    for record in sorted(models, key=_catalog_order):
        slug = record['slug']
        category = _model_category(prefix, record)
        if prefix == 'agy':
            family, variant = _agy_slug_tier(slug)
            efforts = sorted(families.get(family, {}), key=EFFORT_LADDER.index) if family else []
        else:
            family, variant = None, None
            efforts = list(record.get('supported_efforts') or [])
        rows.append({
            'model': slug, 'display_name': record.get('display_name'), 'category': category,
            'auto_candidate': category == 'native',
            'family': family or None, 'variant_effort': variant or None,
            'declared_efforts': efforts, 'declared_default_effort': record.get('default_reasoning_level'),
            'effort_source': ('fixed_configuration' if category == 'fixed_composite' else
                              'agy_catalog_slug_suffix' if variant else
                              'codex_client_cache_reasoning_levels' if efforts else 'unknown'),
            'fixed_configuration': 'same_agent_planning_review_and_execution' if category == 'fixed_composite' else None,
            'capacity': _execution_budget(prefix, info, record),
            'catalog_source': info['model_catalog'].get('source'),
            'catalog_fetched_at': info['model_catalog'].get('fetched_at'),
            'catalog_freshness': info['model_catalog'].get('freshness'),
            'runtime_verified': False,
        })
    return rows


# ════════════════════════════════════════════════════════════════════════════
# ④ 额度冷却持久化、熔断门禁与回退资格
# ════════════════════════════════════════════════════════════════════════════


class _QuotaBatch:
    def __init__(self, retry=False):
        self.retry = retry
        self.initial_cooldown = {}
        self.blocked = {}


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
            except (ValueError, OverflowError, OSError):
                continue
        if epoch is None:
            try:
                epoch = datetime.datetime.fromisoformat(cleaned.replace('Z', '+00:00')).timestamp()
            except (ValueError, OverflowError, OSError):
                rel = re.fullmatch(r'(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*(?:(\d+)\s*s(?:ec)?)?', cleaned, re.IGNORECASE)
                if rel and any(rel.groups()):
                    try:
                        hrs, mins, secs = (int(val or 0) for val in rel.groups())
                        epoch = now_ts + hrs * 3600 + mins * 60 + secs
                    except (ValueError, OverflowError):
                        epoch = None
    if epoch is not None and now_ts < epoch <= now_ts + 45 * 86400:
        until, source = epoch, 'parsed_reset_time'
    else:
        until, source = now_ts + 3600, 'default_ttl_3600s'
    return {'reset_hint': hint or None, 'cooldown_until_epoch': round(until, 3), 'cooldown_source': source}


def _quota_scope(prefix, info):
    home = info.get('config_home')
    side = _execution_side(info)
    scope = str(Path(home).resolve()) if home else str(info.get('binary') or '')
    if side == 'windows':
        scope = ntpath.normpath(scope).replace('\\', '/').casefold()
    return prefix, side, scope


def _quota_state_path(prefix, info, *, legacy=False):
    cfg_home = info.get('config_home')
    if not cfg_home:
        return None
    base = Path(cfg_home)
    if prefix == 'agy':
        base /= 'antigravity-cli'
    filename = 'cae_quota_state.json' if legacy else f'cae_quota_state_{_execution_side(info)}.json'
    return base / 'cache' / filename


def _quota_events_dir(state_path):
    return state_path.with_suffix('.events')


def _quota_ack_name(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest() + '.json'


def _quota_identity(record):
    if not record:
        return None
    return _quota_ack_name(record)


def _publish_quota_record(path, record):
    # Each failure owns a unique path. Publish it atomically so a reader
    # cannot mistake partially written JSON for an absent cooldown.
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name('.' + uuid.uuid4().hex + '.pending')
    try:
        pending.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def _read_quota_cooldowns(prefix, info, now=None, *, cleanup_expired=False):
    now_ts = time.time() if now is None else float(now)
    state_path = _quota_state_path(prefix, info)
    if state_path is None:
        return []
    found = []
    events = _quota_events_dir(state_path)
    try:
        entries = list(events.iterdir())
    except OSError:
        entries = []
    for path in entries + [state_path, _quota_state_path(prefix, info, legacy=True)]:
        immutable = path.parent == events
        if immutable and not _QUOTA_EVENT_NAME.fullmatch(path.name):
            continue
        data = _read_json(path)
        if not isinstance(data, dict) or not data.get('exhausted') or data.get('backend', prefix) != prefix:
            continue
        if data.get('execution_side', _execution_side(info)) != _execution_side(info):
            continue
        until = data.get('cooldown_until_epoch')
        if (type(until) not in (int, float) or (type(until) is float and not math.isfinite(until))
                or not now_ts < until):
            if cleanup_expired and immutable:
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        # 旧版写固定路径。按记录全文的摘要确认成功，只确认本次见过的值，
        # 不删除可能已被另一进程替换为新失败记录的固定路径。
        if not immutable and (events / 'acknowledged' / _quota_ack_name(data)).is_file():
            continue
        found.append((path, data))
    if cleanup_expired:
        try:
            for ack in (events / 'acknowledged').iterdir():
                acknowledged = _read_json(ack)
                until = acknowledged.get('cooldown_until_epoch') if isinstance(acknowledged, dict) else None
                if type(until) in (int, float) and now_ts >= until:
                    ack.unlink(missing_ok=True)
        except OSError:
            pass
    return found


def _load_active_quota_cooldown(prefix, info, now=None, *, cleanup_expired=True):
    with QUOTA_LOCK:
        found = _read_quota_cooldowns(prefix, info, now, cleanup_expired=cleanup_expired)
        return max((data for _path, data in found), key=lambda data: data['cooldown_until_epoch'], default=None)


def _active_quota_record(prefix, info, files=None):
    with QUOTA_LOCK:
        key = _quota_scope(prefix, info)
        in_session = _SESSION_QUOTA_BLOCKED.get(key)
        if in_session and time.time() >= in_session['cooldown_until_epoch']:
            _SESSION_QUOTA_BLOCKED.pop(key, None)
            in_session = None
        if files is None:
            files = _read_quota_cooldowns(prefix, info)
        records = [record for _path, record in files]
        if in_session:
            records.append(in_session)
        return max(records, key=lambda record: record['cooldown_until_epoch'], default=None)


def _record_quota_exhausted(prefix, info, detail, now=None, *, batch=None):
    now_ts = time.time() if now is None else float(now)
    clean_detail = re.sub(r'<Objs\b[\s\S]*', '', detail or '').strip()
    key = _quota_scope(prefix, info)
    record = {'backend': prefix, 'execution_side': key[1], 'exhausted': True,
              'event_id': uuid.uuid4().hex, 'detected_at_epoch': round(now_ts, 3),
              **_parse_quota_reset(clean_detail, now_ts), 'detail': clean_detail[-600:]}
    state_path = _quota_state_path(prefix, info)
    with QUOTA_LOCK:
        _SESSION_QUOTA_BLOCKED[key] = record
        if batch is not None:
            batch.blocked[key] = record
        info.update(quota_status='exhausted_cached', quota_cooldown=record)
        if state_path:
            path = _quota_events_dir(state_path) / (record['event_id'] + '.json')
            try:
                _publish_quota_record(path, record)
            except OSError:
                info['quota_cache_write_failed'] = True
    return record


def _mark_quota_success(prefix, info, batch):
    # A successful request already in flight cannot undo a failure that
    # happened in this batch, or a newer persisted failure from another batch.
    if not batch.retry:
        return
    key = _quota_scope(prefix, info)
    with QUOTA_LOCK:
        initial = batch.initial_cooldown.get(key, set())
        in_session = _SESSION_QUOTA_BLOCKED.get(key)
        if key in batch.blocked or (in_session and _quota_identity(in_session) not in initial):
            return
        files = _read_quota_cooldowns(prefix, info, cleanup_expired=True)
        if any(_quota_identity(record) not in initial for _path, record in files):
            return
        _SESSION_QUOTA_BLOCKED.pop(key, None)
        state_path = _quota_state_path(prefix, info)
        for path, record in files:
            events = _quota_events_dir(state_path)
            try:
                if path.parent == events:
                    path.unlink(missing_ok=True)
                else:
                    _publish_quota_record(events / 'acknowledged' / _quota_ack_name(record),
                                          {'cooldown_until_epoch': record['cooldown_until_epoch']})
            except OSError:
                pass
        remaining = _active_quota_record(prefix, info)
        info.update(quota_status='exhausted_cached' if remaining else 'unknown_until_called', quota_cooldown=remaining)


def _quota_user_choices(prefix, cooldown):
    hint = cooldown.get('reset_hint') or '待冷却结束或补充额度后'
    return {
        'quota_backend': prefix, 'quota_execution_side': cooldown.get('execution_side'),
        'quota_cooldown': cooldown, 'quota_fuse_triggered': True, 'user_confirmation_required': True,
        'user_choices': [
            f'1. 先不发起跨端调用：改由当前总控在本侧直接完成（{prefix} 预计恢复参考: {hint}）',
            f'2. 已充值或额度已恢复：经用户确认后附带 --retry-quota 重新发起跨端调用',
        ],
    }


def _enforce_preflight_quota(backends, specs, retry_quota=False, *, batch=None, cleanup_expired=True):
    batch = batch or _QuotaBatch(retry_quota)
    for spec in specs:
        if not spec:
            continue
        prefix, _ = _engine_parts(spec)
        info = backends.get(prefix, {})
        key = _quota_scope(prefix, info)
        with QUOTA_LOCK:
            files = _read_quota_cooldowns(prefix, info, cleanup_expired=cleanup_expired)
            active = _active_quota_record(prefix, info, files)
            if batch.retry and key not in batch.initial_cooldown:
                persisted = {_quota_identity(record) for _path, record in files}
                batch.initial_cooldown[key] = persisted | ({_quota_identity(active)} if active else set())
            allowed = batch.initial_cooldown.get(key, set())
            unacknowledged = [record for _path, record in files if _quota_identity(record) not in allowed]
            if active and _quota_identity(active) not in allowed:
                unacknowledged.append(active)
            cooldown = batch.blocked.get(key) or max(unacknowledged, key=lambda record: record['cooldown_until_epoch'], default=None)
            if cooldown:
                info.update(quota_status='exhausted_cached', quota_cooldown=cooldown)
        if cooldown:
            hint = cooldown.get('reset_hint') or '待额度恢复'
            raise HarnessError(f'{prefix} 处于已知额度冷却期（预计恢复: {hint}），已在发起前拦截',
                               'quota_exhausted', preflight_blocked=True,
                               execution_environment=_execution_environment(info),
                               **_quota_user_choices(prefix, cooldown))


def _local_fallback_engine(caller, engine, backends):
    fallback = {'antigravity': 'agy:default', 'codex': 'codex:default'}.get(caller)
    if not fallback:
        return None
    cur_prefix, _ = _engine_parts(engine)
    fb_prefix, _ = _engine_parts(fallback)
    if cur_prefix == fb_prefix or not backends.get(fb_prefix, {}).get('available'):
        return None
    return fallback


def _quota_fallback_record(from_engine, to_engine, details):
    details = details or {}
    return {
        'from_engine': from_engine,
        'to_engine': to_engine,
        'reason': 'quota_exhausted',
        'preflight_blocked': bool(details.get('preflight_blocked')),
        'quota_cooldown': details.get('quota_cooldown'),
    }


def _quota_can_replay(details, invocation):
    return (bool(details.get('preflight_blocked')) or invocation.get('startup_only') is True
            or not invocation.get('invocation_started', invocation.get('launch_attempted', False)))


# ════════════════════════════════════════════════════════════════════════════
# ⑤ 进程树监督、JSON 流解析、App-Server RPC 与无头执行
# ════════════════════════════════════════════════════════════════════════════


class _WindowsJob:
    """为本次子进程单独持有 Job；挂起创建、先纳管后恢复，封住派生竞态。"""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                        ('th32ThreadID', wintypes.DWORD), ('th32OwnerProcessID', wintypes.DWORD),
                        ('tpBasePri', wintypes.LONG), ('tpDeltaPri', wintypes.LONG), ('dwFlags', wintypes.DWORD)]

        self.ctypes = ctypes
        self.entry_type = ThreadEntry
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = (
            ('CreateJobObjectW', wintypes.HANDLE, (ctypes.c_void_p, wintypes.LPCWSTR)),
            ('AssignProcessToJobObject', wintypes.BOOL, (wintypes.HANDLE, wintypes.HANDLE)),
            ('TerminateJobObject', wintypes.BOOL, (wintypes.HANDLE, wintypes.UINT)),
            ('CloseHandle', wintypes.BOOL, (wintypes.HANDLE,)),
            ('CreateToolhelp32Snapshot', wintypes.HANDLE, (wintypes.DWORD, wintypes.DWORD)),
            ('Thread32First', wintypes.BOOL, (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))),
            ('Thread32Next', wintypes.BOOL, (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))),
            ('OpenThread', wintypes.HANDLE, (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)),
            ('ResumeThread', wintypes.DWORD, (wintypes.HANDLE,)),
        )
        for name, result, arguments in signatures:
            function = getattr(self.api, name)
            function.restype, function.argtypes = result, arguments
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def attach(self, proc):
        # Popen 保留的句柄具有创建时的权限，受限令牌重新 OpenProcess 可能被拒绝。
        if not self.api.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        snapshot = self.api.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == self.ctypes.c_void_p(-1).value:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        try:
            entry = self.entry_type()
            entry.dwSize = self.ctypes.sizeof(entry)
            available = self.api.Thread32First(snapshot, self.ctypes.byref(entry))
            while available:
                if entry.th32OwnerProcessID == proc.pid:
                    thread = self.api.OpenThread(0x0002, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise self.ctypes.WinError(self.ctypes.get_last_error())
                    try:
                        if self.api.ResumeThread(thread) == 0xFFFFFFFF:
                            raise self.ctypes.WinError(self.ctypes.get_last_error())
                    finally:
                        self.api.CloseHandle(thread)
                    return
                available = self.api.Thread32Next(snapshot, self.ctypes.byref(entry))
            raise OSError('未找到挂起子进程的主线程')
        finally:
            self.api.CloseHandle(snapshot)

    def terminate(self):
        if self.handle and not self.api.TerminateJobObject(self.handle, 1):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _process_options():
    if os.name == 'nt':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | 0x00000004}
    return {'start_new_session': True}


def _note_cleanup_errors(failure, errors):
    if isinstance(failure, HarnessError):
        failure.details.setdefault('cleanup_errors', []).extend(errors)
    elif hasattr(failure, 'add_note'):
        failure.add_note('资源回收未验证: ' + '; '.join(errors))
    else:
        failure.cleanup_errors = list(errors)


def _close_process_streams(proc):
    errors = []
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None and not stream.closed:
            try:
                stream.close()
            except (OSError, ValueError) as exc:
                errors.append('Pipe close failed: ' + str(exc))
    if os.name == 'nt' and proc.returncode is not None:
        proc._handle.Close()
    return errors


def _start_process(command, cwd=None, env=None, shell=False):
    job = _WindowsJob() if os.name == 'nt' else None
    proc = None
    try:
        proc = subprocess.Popen(command, cwd=str(cwd) if cwd is not None else None, env=env, shell=shell,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8', errors='replace', **_process_options())
        if job is not None:
            proc._cae_job = job
            job.attach(proc)
        return proc
    except BaseException as exc:
        # 即使事件输出、入 Job 或恢复线程失败，也不能遗留一个挂起进程。
        cleanup_errors = []
        try:
            if proc is not None:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
                finally:
                    cleanup_errors.extend(_close_process_streams(proc))
        finally:
            if job is not None:
                job.close()
        if cleanup_errors:
            if isinstance(exc, Exception):
                raise HarnessError('子进程启动失败且资源回收未完成', stderr=str(exc), cleanup_errors=cleanup_errors) from exc
            _note_cleanup_errors(exc, cleanup_errors)
        raise


def _terminate_tree(proc, native_pid_file=None):
    errors = []
    if native_pid_file and proc.poll() is None and not getattr(proc, '_cae_native_tree_terminated', False):
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
            else:
                proc._cae_native_tree_terminated = True
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            errors.append('Windows process tree cleanup unverified: ' + str(exc))
    try:
        if getattr(proc, '_cae_job', None) is not None:
            proc._cae_job.terminate()
        elif os.name == 'nt':
            cleanup = subprocess.run(['taskkill.exe', '/T', '/F', '/PID', str(proc.pid)], capture_output=True,
                                     timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            if cleanup.returncode and proc.poll() is None:
                errors.append('Local process tree termination returned nonzero')
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append('Local process tree cleanup unverified: ' + str(exc))
    return errors


def _reap_process(proc, native_pid_file=None):
    """先清本次进程树，再 wait 回收直接子进程；最后一次 kill 后也必须 wait。"""
    errors = _terminate_tree(proc, native_pid_file)
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append('Child process reaping unverified: ' + str(exc))
    finally:
        job = getattr(proc, '_cae_job', None)
        if job is not None:
            job.close()
    return errors


def run_process(command, cwd, input_text=None, timeout_sec=600, env=None, shell=False, native_pid_file=None):
    if _CANCELLED.is_set():
        raise HarnessError('任务已取消', 'cancelled')
    deadline = time.monotonic() + timeout_sec
    proc = _start_process(command, cwd, env, shell)
    completed = queue.Queue(maxsize=1)
    failure = None

    def communicate():
        # Windows 的 communicate(timeout=...) 仍会同步写 stdin；由独立线程承受管道背压。
        try:
            completed.put((proc.communicate(input=input_text), None))
        except (OSError, ValueError) as exc:
            completed.put((None, exc))

    communicator = threading.Thread(target=communicate, name='cae-process-io', daemon=True)
    try:
        emit_event('process_started', pid=proc.pid)
        communicator.start()
        while True:
            if _CANCELLED.is_set():
                raise HarnessError('任务已取消', 'cancelled')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarnessError('子进程超时', 'timeout', timeout_sec=timeout_sec)
            try:
                output, communication_error = completed.get(timeout=min(remaining, 0.05))
                if communication_error is not None:
                    raise communication_error
                out, err = output
                break
            except queue.Empty:
                if proc.poll() is not None:
                    # 父进程退出不等于管道 EOF，后代可能仍持有写端。
                    cleanup_errors = _terminate_tree(proc, native_pid_file)
                    if cleanup_errors:
                        raise HarnessError('父进程退出后无法核实后代清理', cleanup_errors=cleanup_errors)
        if _CANCELLED.is_set():
            raise HarnessError('任务已取消', 'cancelled')
        emit_event('process_finished', pid=proc.pid, returncode=proc.returncode)
        return subprocess.CompletedProcess(command, proc.returncode, out, err)
    except BaseException as exc:
        failure = exc
        if isinstance(exc, HarnessError):
            exc.details['process_started'] = True
        raise
    finally:
        cleanup_errors = _reap_process(proc, native_pid_file)
        if communicator.ident is not None:
            communicator.join(timeout=5)
        if communicator.is_alive():
            cleanup_errors.append('Child pipe reader/writer did not stop')
        else:
            cleanup_errors.extend(_close_process_streams(proc))
        if cleanup_errors:
            if failure is not None:
                if isinstance(failure, Exception) and not isinstance(failure, HarnessError):
                    raise HarnessError(str(failure), cleanup_errors=cleanup_errors) from failure
                _note_cleanup_errors(failure, cleanup_errors)
            elif failure is None:
                raise HarnessError('子进程资源回收未完成', cleanup_errors=cleanup_errors)


def _cancel_signal(_signum, _frame):
    _CANCELLED.set()


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


def _is_agy_print_timeout_text(text):
    sample = str(text or '')
    low = sample.lower()
    if 'print timeout after' in low:
        return True
    if re.search(r'"(?:finished_reason|reason)"\s*:\s*"print_timeout"', sample):
        return True
    return False


def _parse_agy_error_line(line):
    stripped = line.strip()
    if not stripped.startswith('AGY_ERROR:'):
        return None
    body = stripped[len('AGY_ERROR:'):].strip()
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, RecursionError):
        return None


def _check_output_status(parsed):
    """只解释协议错误，不把工具读出的额度说明当作后端额度错误。"""
    finished_reason = str(parsed.get('finished_reason', '')).lower()
    if finished_reason == 'print_timeout':
        raise HarnessError('后端输出因超时截断', 'timeout', finished_reason=finished_reason)
    if finished_reason in ('interrupted', 'cancelled', 'canceled'):
        raise HarnessError('后端执行被中断', 'cancelled', finished_reason=finished_reason)
    status = str(parsed.get('status', '')).lower()
    if status == 'timeout':
        raise HarnessError('后端返回超时回执', 'timeout', backend_status=status)
    if status in ('cancelled', 'canceled', 'interrupted'):
        raise HarnessError('后端返回取消回执', 'cancelled', backend_status=status)
    event_error = parsed.get('event', parsed.get('type')) == 'error'
    if (event_error or finished_reason == 'error' or parsed.get('is_error') or parsed.get('error')
            or status in ('error', 'failed', 'partial', 'incomplete', 'invalid', 'waiting', 'running')):
        error = str(parsed.get('error') or parsed.get('message') or '')[-2000:]
        error_status = 'quota_exhausted' if _is_quota_exhausted(error) else 'failed'
        raise HarnessError('后端返回错误回执', error_status, backend_status=status or 'error',
                           finished_reason=finished_reason, error=error)


def _decode_output_json(raw_text):
    raw = raw_text.strip().lstrip('\ufeff')
    if not raw:
        raise OutputParseError('子任务输出为空')
    try:
        return json.loads(raw)
    except RecursionError as exc:
        raise OutputParseError('JSON 嵌套过深') from exc
    except ValueError as exc:
        fence = re.fullmatch(r'```(?:json)?\s*(\{[\s\S]*\})\s*```', raw)
        try:
            if fence:
                return json.loads(fence.group(1))
            events = []
            agy_errors = []
            saw_timeout_diag = False
            for line in raw.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                agy_err = _parse_agy_error_line(stripped)
                if agy_err is not None:
                    agy_errors.append(agy_err)
                    continue
                if re.match(r'^(?:\[agy\]\s+print timeout after\b|error:\s+print timeout after\b)', stripped, re.I):
                    saw_timeout_diag = True
                    continue
                events.append(json.loads(stripped))
        except (ValueError, RecursionError) as decode_error:
            raise OutputParseError('子任务输出不是合法 JSON') from decode_error
        for agy_err in agy_errors:
            reason = str(agy_err.get('reason') or '').lower()
            short_err = str(agy_err.get('short_error') or agy_err.get('message') or '')
            if reason == 'print_timeout' or 'print timeout after' in short_err.lower():
                raise HarnessError('后端输出因超时截断', 'timeout', finished_reason='print_timeout')
            if _is_quota_exhausted(short_err):
                raise HarnessError('后端返回错误回执', 'quota_exhausted', error=short_err[-2000:])
        if saw_timeout_diag:
            raise HarnessError('后端输出因超时截断', 'timeout', finished_reason='print_timeout')
        if agy_errors:
            short_err = str(agy_errors[-1].get('short_error') or agy_errors[-1])[-2000:]
            raise HarnessError('后端返回错误回执', 'failed', error=short_err)
        results = []
        for event in events:
            if not isinstance(event, dict):
                raise OutputParseError('输出流事件必须为对象') from exc
            _check_output_status(event)
            if event.get('event') == 'result':
                results.append(event)
        if len(results) != 1:
            raise OutputParseError('输出流必须包含一个最终 result 回执') from exc
        return results[0]


def _extract_json_object(raw_text, schema=None):
    schema = schema or AUDIT_JSON_SCHEMA
    parsed = _decode_output_json(raw_text)
    # 字典和字符串包装共用深度上限，字符串解包不能递归重置计数。
    for _ in range(8):
        if not isinstance(parsed, dict):
            raise OutputParseError('最终回执必须为对象')
        _check_output_status(parsed)
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
            next_value = _decode_output_json(next_value)
        if next_value is None:
            break
        parsed = next_value
    raise OutputParseError('回执缺少符合指定结构的结果')


def _read_rpc_lines(stream, inbox, stopped=None):
    def publish(line):
        while stopped is None or not stopped.is_set():
            try:
                inbox.put(line, timeout=0.05)
                return
            except queue.Full:
                continue

    try:
        for line in stream:
            if stopped is not None and stopped.is_set():
                break
            publish(line)
    except (OSError, ValueError):
        pass
    finally:
        publish(None)


def _query_codex_stdio(command, cwd, env, native_pid_file, timeout_sec):
    if _CANCELLED.is_set():
        raise HarnessError('任务已取消', 'cancelled')
    deadline = time.monotonic() + timeout_sec
    try:
        proc = _start_process(command, cwd, env)
    except OSError as exc:
        raise HarnessError('无法启动 Codex 模型目录查询', stderr=str(exc)) from exc
    inbox = queue.Queue(maxsize=128)
    outbox = queue.Queue()
    writer_errors = queue.Queue(maxsize=1)
    stopped = threading.Event()
    errors = collections.deque(maxlen=8)
    stdout_reader = threading.Thread(target=_read_rpc_lines, args=(proc.stdout, inbox, stopped),
                                     name='cae-rpc-stdout', daemon=True)

    def drain_stderr():
        try:
            for line in proc.stderr:
                errors.append(line[-2000:])
        except (OSError, ValueError):
            pass

    stderr_reader = threading.Thread(target=drain_stderr, name='cae-rpc-stderr', daemon=True)

    def write_requests():
        try:
            while True:
                message = outbox.get()
                if message is None:
                    break
                proc.stdin.write(message)
                proc.stdin.flush()
        except (OSError, ValueError) as exc:
            writer_errors.put(exc)
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    stdin_writer = threading.Thread(target=write_requests, name='cae-rpc-stdin', daemon=True)
    failure = None

    def send(message):
        outbox.put(json.dumps(message) + '\n')

    def rpc(number, method, params):
        send({'id': number, 'method': method, 'params': params})
        while True:
            if _CANCELLED.is_set():
                raise HarnessError('任务已取消', 'cancelled')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarnessError('Codex 模型目录查询超时', 'timeout')
            try:
                write_error = writer_errors.get_nowait()
            except queue.Empty:
                pass
            else:
                raise HarnessError('Codex 模型目录通信失败', stderr=str(write_error)) from write_error
            try:
                line = inbox.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            if line is None:
                raise HarnessError('Codex app-server 在返回模型目录前退出')
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get('id') != number or 'method' in event:
                continue
            if event.get('error'):
                raise HarnessError(f'Codex {method} 返回错误', stderr=str(event['error'])[-2000:])
            if not isinstance(event.get('result'), dict):
                raise HarnessError(f'Codex {method} 回执结构无效')
            return event['result']

    try:
        stdout_reader.start()
        stderr_reader.start()
        stdin_writer.start()
        rpc(1, 'initialize', {'clientInfo': {'name': 'cae_harness', 'version': '1.0'},
                              'capabilities': {'experimentalApi': True}})
        send({'method': 'initialized'})
        listing = {}
        params = {'includeHidden': False, 'limit': 100}
        cursors = set()
        for number in range(2, 12):
            response = rpc(number, 'model/list', params)
            data = response.get('data')
            if not isinstance(data, list):
                raise HarnessError('Codex 模型列表结构无效')
            for model in data:
                slug = model.get('model') if isinstance(model, dict) else None
                if isinstance(slug, str) and slug and not model.get('hidden'):
                    listing[slug] = model
            cursor = response.get('nextCursor')
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise HarnessError('Codex 模型列表分页游标无效')
            cursors.add(cursor)
            params['cursor'] = cursor
        else:
            raise HarnessError('Codex 模型目录超出查询页数上限')
        if not listing:
            raise HarnessError('Codex 模型目录为空，停止派工')
        return listing
    except HarnessError as exc:
        failure = exc
        raise
    except OSError as exc:
        failure = HarnessError('Codex 模型目录通信失败', stderr=str(exc))
        raise failure from exc
    except BaseException as exc:
        failure = exc
        raise
    finally:
        # 写线程独占 stdin；控制线程不能 close 一个正被阻塞写入的文本管道。
        outbox.put(None)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        # 正常退出也回收后代，避免后代继承管道让读取线程永久等 EOF。
        stopped.set()
        cleanup_errors = _reap_process(proc, native_pid_file)
        for reader in (stdout_reader, stderr_reader, stdin_writer):
            if reader.ident is not None:
                reader.join(timeout=5)
            if reader.is_alive():
                cleanup_errors.append(reader.name + ' did not stop')
        if not any(reader.is_alive() for reader in (stdout_reader, stderr_reader, stdin_writer)):
            cleanup_errors.extend(_close_process_streams(proc))
        if isinstance(failure, HarnessError) and errors:
            failure.details['stderr'] = (str(failure.details.get('stderr') or '') + '\n' + ''.join(errors))[-3000:]
        if cleanup_errors:
            if failure is not None:
                if isinstance(failure, Exception) and not isinstance(failure, HarnessError):
                    raise HarnessError(str(failure), cleanup_errors=cleanup_errors) from failure
                _note_cleanup_errors(failure, cleanup_errors)
            elif sys.exc_info()[0] is None:
                raise HarnessError('Codex 模型查询资源回收未完成', cleanup_errors=cleanup_errors)


def _query_codex_listing(info, timeout_sec=30):
    if _CANCELLED.is_set():
        raise HarnessError('任务已取消', 'cancelled')
    home = info.get('config_home')
    binary = info.get('binary')
    if not home or not binary:
        raise HarnessError('未发现 Codex 或其配置目录，无法刷新模型列表')
    command = [binary, 'app-server', '--stdio']
    if info.get('windows_interop'):
        if not shutil.which('powershell.exe') or not shutil.which('taskkill.exe'):
            raise HarnessError('WSL 跨端查询需要 powershell.exe 与 taskkill.exe 进行进程监督')
        windows_temp = _wsl_path(Path(_windows_env('TEMP')), windows=False)
        with tempfile.TemporaryDirectory(prefix='cae-harness-', dir=windows_temp) as temp:
            cwd = Path(temp)
            command, pid_file = _interop_command(command, cwd, cwd, None, home)
            return _query_codex_stdio(command, cwd, None, pid_file, timeout_sec)
    env = os.environ.copy()
    env.update(CODEX_HOME=home, CAE_HARNESS_DEPTH='1')
    return _query_codex_stdio(command, None, env, None, timeout_sec)


def _quota_startup_only(stdout):
    """只有空输出或明确启动/错误事件才证明尚未执行任务；未知事件按已执行处理。"""
    for line in (stdout or '').splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return False
        if not isinstance(event, dict):
            return False
        kind = event.get('event', event.get('type'))
        if kind in ('init', 'thread.started', 'turn.started', 'turn.failed', 'error'):
            continue
        if kind == 'system' and event.get('subtype') == 'init':
            continue
        if kind == 'result':
            result = event.get('result', event)
            if (isinstance(result, dict) and type(result.get('num_turns')) is int and result['num_turns'] == 0
                    and not any(result.get(key) for key in ('structured_output', 'response', 'output', 'content', 'text'))):
                continue
        return False
    return True


def _backend_diagnostics(result, *, structured_only=False):
    parts = [] if structured_only else [result.stderr or '']

    def error_details(event, depth=0):
        details = {key: event[key] for key in (
            'event', 'type', 'status', 'finished_reason', 'is_error', 'error', 'message',
            'code', 'reason', 'short_error', 'detail', 'details') if key in event}
        is_error = (event.get('is_error') or event.get('error')
                    or str(event.get('status', '')).lower() in (
                        'error', 'failed', 'partial', 'incomplete', 'invalid', 'waiting', 'running',
                        'timeout', 'cancelled', 'canceled', 'interrupted')
                    or str(event.get('finished_reason', '')).lower() in (
                        'error', 'print_timeout', 'interrupted', 'cancelled', 'canceled'))
        nested = event.get('result')
        if isinstance(nested, dict) and depth < 8:
            nested_details, nested_error = error_details(nested, depth + 1)
            if nested_details:
                details['result'] = nested_details
            is_error = is_error or nested_error
        return details, is_error

    for line in (result.stdout or '').splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            if not structured_only:
                parts.append(line)
            continue
        if isinstance(event, dict):
            kind = event.get('event', event.get('type'))
            if kind in ('error', 'result', 'turn.failed'):
                details, is_error = error_details(event)
                if kind != 'result' or is_error:
                    parts.append(json.dumps(details, ensure_ascii=False))
    return '\n'.join(part for part in parts if part).strip()


def invoke_engine(spec, target_dir, prompt, backends, schema, readonly=True, timeout_sec=600,
                  agent='readonly-collaborator', stage_files=None, effort=None, retry_quota=False,
                  *, profile=None, batch=None, receipt=None):
    batch = batch or _QuotaBatch(retry_quota)
    prefix, _ = _engine_parts(spec)
    info = backends.get(prefix, {})
    if not info.get('available'):
        raise HarnessError(f'未发现所选后端 {prefix}')
    if profile is None:
        _prepare_execution(backends, (spec,), batch)
        info = backends[prefix]
        profile = resolve_execution_profile(backends, spec, effort or 'max')
    _require_execution_profile(profile)
    _, model = _engine_parts(profile['spec'])
    effort = profile['cli_effort']
    if receipt is None:
        receipt = {}
    receipt.update(requested_engine=profile['requested_engine'], execution_environment=_execution_environment(info),
                   model_requested_to_cli=model or None, actual_model=None, effective_effort=None,
                   declared_effort=profile['declared_effort'], launch_attempted=False, invocation_started=False)
    _enforce_preflight_quota(backends, (spec,), batch=batch)
    binary = info['binary']
    if prefix == 'agy' and not info.get('execution_config_supported', True):
        raise HarnessError('CAE_AGY_HOME 仅用于离线配置探测，执行请使用当前 CLI 的真实用户配置')
    if Path(binary).suffix.lower() in ('.cmd', '.bat'):
        raise HarnessError('请通过 CAE_CODEX_BINARY / CAE_AGY_BINARY 指定原生可执行文件')
    interop = info.get('windows_interop', False)
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
            exec_prompt = prompt + STAGED_PROMPT_SUFFIX
        if prefix == 'codex':
            command = [binary, 'exec', '-s', 'read-only' if readonly else 'workspace-write',
                       '-c', 'approval_policy="never"', '--ephemeral',
                       '--skip-git-repo-check', '-C', win_target, '--output-schema', translate(schema_file), '-o', translate(output_file)]
            if model:
                command.extend(['-m', model])
            if effort:
                command.extend(['-c', f'model_reasoning_effort="{effort}"'])
            context_window = profile['capacity']['cli_context_window_tokens']
            if context_window:
                command.extend(['-c', f'model_context_window={context_window}'])
            command.append('-')
            input_text = exec_prompt
        else:
            command = [binary, '--input-format', 'stream-json', '--output-format', 'stream-json',
                       '--print-timeout', f'{timeout_sec}s', '--json-schema', translate(schema_file),
                       '--disable-slash-commands', '--sandbox']
            if model:
                command.extend(['--model', model])
            if effort and profile.get('effort_option_supported') is not False and not _agy_slug_tier(model)[1]:
                command.extend(['--effort', effort])
            if readonly:
                command.extend(['--agent', agent])
            else:
                command.extend(['--mode', 'accept-edits', '--dangerously-skip-permissions'])
            input_text = json.dumps({'event': 'user', 'message': {'content': exec_prompt}}, ensure_ascii=False) + '\n'
        receipt.update(argv=list(command), work_dir=str(exec_dir), staged_readonly_workspace=stage_dir is not None,
                       command_transport='windows_interop' if interop else 'native')
        if interop:
            command, native_pid_file = _interop_command(command, exec_dir, temp_path, input_text,
                                                        info.get('config_home') if prefix == 'codex' else None)
            input_text = None
        # Queued units must recheck after staging. A success already in flight
        # cannot clear a failure recorded by another unit in this same batch.
        _enforce_preflight_quota(backends, (spec,), batch=batch)
        receipt['launch_attempted'] = True
        proc_timeout = timeout_sec + 15 if prefix == 'agy' else timeout_sec
        try:
            result = run_process(command, exec_dir, input_text, proc_timeout, env=env, native_pid_file=native_pid_file)
        except HarnessError as exc:
            receipt['invocation_started'] = bool(exc.details.get('process_started'))
            raise
        receipt['invocation_started'] = True
        receipt['returncode'] = result.returncode
        startup_stderr = all(not line.strip() or _is_quota_exhausted(line)
                             for line in (result.stderr or '').splitlines())
        receipt['startup_only'] = (_quota_startup_only(result.stdout) and startup_stderr
                                   and not (output_file.exists() and output_file.stat().st_size))
        if prefix == 'agy' and result.returncode == 0:
            # 仅解释 stderr 中明确的终止诊断；普通警告不参与失败分类。
            diagnostics = [line.strip() for line in (result.stderr or '').splitlines()
                           if _parse_agy_error_line(line) is not None or re.match(
                               r'^(?:\[agy\]\s+print timeout after\b|error:\s+print timeout after\b)', line.strip(), re.I)]
            if diagnostics:
                detail = '\n'.join(diagnostics)
                if _is_agy_print_timeout_text(detail):
                    raise HarnessError('agy 执行触发 --print-timeout 超时', 'timeout',
                                       returncode=0, finished_reason='print_timeout', stderr=detail[-2000:])
                if _is_quota_exhausted(detail):
                    cooldown = _record_quota_exhausted(prefix, info, detail, batch=batch)
                    raise HarnessError('agy 额度不足或已达用量上限，已停止并记录冷却状态',
                                       'quota_exhausted', preflight_blocked=False, returncode=0,
                                       stderr=detail[-2000:], **_quota_user_choices(prefix, cooldown))
                raise HarnessError('agy 返回终止错误诊断', 'failed', returncode=0, stderr=detail[-2000:])
        if result.returncode:
            detail = _backend_diagnostics(result)
            structured = _backend_diagnostics(result, structured_only=True)
            if _is_quota_exhausted(structured or detail):
                cooldown = _record_quota_exhausted(prefix, info, detail, batch=batch)
                raise HarnessError(f'{prefix} 额度不足或已达用量上限，已停止并记录冷却状态',
                                   'quota_exhausted', preflight_blocked=False, returncode=result.returncode,
                                   stderr=(result.stderr or '')[-2000:], **_quota_user_choices(prefix, cooldown))
            if prefix == 'agy' and _is_agy_print_timeout_text(detail):
                raise HarnessError('agy 执行触发 --print-timeout 超时', 'timeout',
                                   returncode=result.returncode, finished_reason='print_timeout',
                                   stderr=(result.stderr or '')[-2000:])
            # A worker may already have written files. Authentication fallback
            # is allowed only in model listing before planning and execution.
            raise HarnessError(f'{prefix} 执行失败，退出码 {result.returncode}', returncode=result.returncode,
                               stderr=(result.stderr or '')[-2000:])
        raw = output_file.read_text(encoding='utf-8-sig') if prefix == 'codex' and output_file.exists() else result.stdout
        try:
            parsed = _extract_json_object(raw, schema)
        except HarnessError as exc:
            if exc.status == 'quota_exhausted':
                detail = str(exc.details.get('error') or raw)[-1000:]
                cooldown = _record_quota_exhausted(prefix, info, detail, batch=batch)
                raise HarnessError(f'{prefix} 额度不足或已达用量上限，已停止并记录冷却状态',
                                   'quota_exhausted', preflight_blocked=False,
                                   **_quota_user_choices(prefix, cooldown)) from exc
            raise
        _mark_quota_success(prefix, info, batch)
        if prefix == 'agy':
            for line in (result.stdout or '').splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                init = event.get('init') if isinstance(event, dict) and event.get('event') == 'init' else None
                if isinstance(init, dict) and isinstance(init.get('model'), str):
                    receipt['actual_model'] = init['model']
                    receipt['actual_model_source'] = 'agy_init_event'
                    break
        return _rewrite_staged_paths(parsed, stage_dir) if stage_dir is not None else parsed


def invoke_readonly_engine(spec, target_dir, prompt, backends, timeout_sec=600, stage_files=None, effort=None, retry_quota=False,
                           *, profile=None, batch=None, receipt=None):
    return invoke_engine(spec, target_dir, prompt, backends, AUDIT_JSON_SCHEMA, True, timeout_sec,
                         'readonly-auditor', stage_files, effort, retry_quota, profile=profile, batch=batch, receipt=receipt)


def invoke_edit_worker(spec, target_dir, prompt, backends, timeout_sec=600, effort=None, retry_quota=False,
                       *, profile=None, batch=None, receipt=None):
    result = invoke_engine(spec, target_dir, prompt, backends, TASK_JSON_SCHEMA, False, timeout_sec,
                           'readonly-collaborator', None, effort, retry_quota, profile=profile, batch=batch, receipt=receipt)
    return json.dumps(result, ensure_ascii=False)


# ════════════════════════════════════════════════════════════════════════════
# ⑥ 工作区快照、分片审计规划与双盲归并
# ════════════════════════════════════════════════════════════════════════════


def snapshot_files(root):
    """对独立任务目录作事后核对，排除 Git 元数据，不撤销文件。"""
    root = Path(root)

    def traversal_failed(exc):
        directory = Path(exc.filename) if exc.filename else root
        try:
            location = directory.relative_to(root).as_posix()
        except ValueError:
            location = str(directory)
        raise HarnessError(f'无法遍历目录 {location}: {exc}', snapshot_path=location) from exc

    try:
        result = {'.': 'directory:' + str(stat.S_IMODE(root.stat().st_mode))}
    except OSError as exc:
        raise HarnessError(f'无法核对目标目录: {exc}', snapshot_path='.') from exc
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=traversal_failed):
        dirs[:] = sorted(d for d in dirs if d != '.git')
        for name in list(dirs):
            link = Path(directory) / name
            rel = link.relative_to(root).as_posix()
            try:
                if _is_link(link):
                    result[rel] = 'symlink:' + os.readlink(link)
                    dirs.remove(name)
                else:
                    result[rel + '/'] = 'directory:' + str(stat.S_IMODE(link.stat().st_mode))
            except OSError as exc:
                raise HarnessError(f'无法核对目录 {rel}: {exc}', snapshot_path=rel) from exc
        for name in files:
            path = Path(directory) / name
            if path == root / '.git':
                continue
            rel = path.relative_to(root).as_posix()
            try:
                st = path.lstat()
                if _is_link(path, st=st):
                    result[rel] = 'symlink:' + os.readlink(path)
                elif not stat.S_ISREG(st.st_mode):
                    raise HarnessError(f'工作区包含非常规特殊文件，无法安全快照: {rel}', snapshot_path=rel)
                else:
                    digest = hashlib.sha256()
                    with path.open('rb') as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b''):
                            digest.update(block)
                    result[rel] = digest.hexdigest() + ':' + str(stat.S_IMODE(st.st_mode))
            except OSError as exc:
                raise HarnessError(f'无法核对文件 {rel}: {exc}', snapshot_path=rel) from exc
    return result


def _scope_patterns(patterns):
    result = []
    for pattern in patterns:
        normalized = pattern.replace('\\', '/')
        if not normalized or normalized.startswith('/') or ':' in normalized or '..' in normalized.split('/'):
            raise HarnessError('--write-scope 必须使用目标目录内的相对路径或通配符')
        normalized = posixpath.normpath(normalized)
        if normalized == '.':
            raise HarnessError('--write-scope 不可为空路径')
        result.append(normalized)
    return result


def _scope_matches(path, scopes):
    def key(value):
        return value.casefold() if os.name == 'nt' else value

    return any(fnmatch.fnmatchcase(key(path), key(pattern)) for pattern in scopes)


def changed_files(before, after):
    changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
    # 文件变化已能代表其新建/删除的父目录；单独保留空目录或目录权限变化。
    return sorted(path for path in changed if not path.endswith('/')
                  or (path in before and path in after)
                  or not any(other != path and other.startswith(path) for other in changed))


def _changes_report(before, root, scopes, readonly):
    changes = changed_files(before, snapshot_files(root))
    violations = changes if readonly else [path for path in changes if scopes and not _scope_matches(path, scopes)]
    return {'changed_files': changes, 'scope_violations': violations, 'scope_check': 'post_execution_snapshot',
            'scope_notice': '需使用独立任务目录，快照无法区分并发写入来源，检查不自动回滚。'}


def _safe_changes_report(before, root, scopes, readonly):
    try:
        report = _changes_report(before, root, scopes, readonly)
        report['scope_verified'] = True
        return report
    except (HarnessError, OSError) as exc:
        return {
            'changed_files': None,
            'scope_violations': [],
            'scope_check': 'unverified',
            'scope_verified': False,
            'scope_error': str(exc),
            'scope_error_path': getattr(exc, 'details', {}).get('snapshot_path'),
        }


def _workspace_unchanged(before, root):
    if before is None:
        return False
    scope = _safe_changes_report(before, root, [], True)
    return scope['scope_verified'] and not scope['changed_files']


def estimate_tokens_from_bytes(num_bytes):
    return max(1, (num_bytes + 2) // 3) if num_bytes else 0


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
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode):
                skipped.append({'file': rel, 'reason': 'special_file'})
                continue
            size = st.st_size
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


def _normalize_rel_file(file_str, target_dir):
    cleaned = file_str.strip().replace('\\', '/')
    if _is_posix() and re.match(r'^(?:[A-Za-z]:/|//)', cleaned) and shutil.which('wslpath'):
        try:
            cleaned = _wsl_path(cleaned, windows=False)
        except HarnessError:
            pass
    candidate = Path(cleaned)
    # 无法映射的 Windows 绝对路径必须原样交给范围核对，不能伪装成相对路径。
    if not candidate.is_absolute() and ntpath.isabs(cleaned):
        return cleaned
    try:
        root = target_dir.resolve()
        absolute = candidate if candidate.is_absolute() else root / candidate
        return absolute.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return posixpath.normpath(cleaned)


def _build_audit_prompt(unit, focus):
    files = '\n'.join('- ' + path for path in unit['files'])
    return ('在当前目录按指定范围只读审查，给出文件和真实行号。不要修改文件，严禁在目标工作目录外创建或改动任何文件或目录（含严禁 git worktree add），且不要另行调用本编排器。\n重点: '
            + (focus or '物理守恒、边界条件、数值稳定性及代码一致性') + '\n计划文件清单:\n' + files
            + '\n按 JSON Schema 返回发现，read_files 列出已读取文件，unread_files 逐文件说明未完成的原因。')


def _audit_estimate(files, file_tokens, focus, interop=False):
    prompt = _build_audit_prompt({'files': files}, focus)
    schema_bytes = len(json.dumps(AUDIT_JSON_SCHEMA, ensure_ascii=False).encode('utf-8'))
    interop_bytes = len(STAGED_PROMPT_SUFFIX.encode('utf-8')) if interop else 0
    overhead = estimate_tokens_from_bytes(len(prompt.encode('utf-8')) + schema_bytes + interop_bytes)
    return {'estimated_file_tokens': file_tokens, 'estimated_prompt_tokens': overhead,
            'estimated_tokens': file_tokens + overhead}


def build_shards(file_records, max_shard_tokens, *, input_budget=None, focus='', interop=False):
    if max_shard_tokens <= 0:
        raise HarnessError('分片预算必须为正数')
    budget = input_budget if input_budget is not None else max_shard_tokens
    soft = min(max_shard_tokens, budget)
    shards, files, file_tokens = [], [], 0
    base_bytes = len(_build_audit_prompt({'files': []}, focus).encode('utf-8'))
    base_bytes += len(json.dumps(AUDIT_JSON_SCHEMA, ensure_ascii=False).encode('utf-8'))
    if interop:
        base_bytes += len(STAGED_PROMPT_SUFFIX.encode('utf-8'))
    path_bytes = 0
    for record in file_records:
        name = record['rel_path']
        size = len(('- ' + name).encode('utf-8'))
        if record['tokens'] + estimate_tokens_from_bytes(base_bytes + size) > budget:
            raise HarnessError(f'单文件与审查提示词超出输入预算: {name}')
        combined = file_tokens + record['tokens'] + estimate_tokens_from_bytes(base_bytes + path_bytes + size + bool(files))
        if files and combined > soft:
            shards.append({'shard_id': len(shards) + 1, 'files': files, **_audit_estimate(files, file_tokens, focus, interop)})
            files, file_tokens, path_bytes = [], 0, 0
        path_bytes += size + bool(files)
        files.append(name)
        file_tokens += record['tokens']
    if files:
        shards.append({'shard_id': len(shards) + 1, 'files': files, **_audit_estimate(files, file_tokens, focus, interop)})
    return shards


def _budget_for_spec(backends, spec):
    return resolve_execution_profile(backends, spec, kind='audit')['capacity']['input_budget_tokens']


def _audit_engines(backends, dual_blind, left_override, right_override, allow_unconfigured, caller):
    if not left_override and caller == 'caller':
        available = [name for name in ('codex', 'agy') if backends.get(name, {}).get('available')]
        if not available:
            if allow_unconfigured:
                return None, None
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
    return left, right


def select_audit_plan(total_tokens, file_records, backends, strategy='auto', dual_blind=True,
                      left_override='', right_override='', shard_tokens=120_000, allow_unconfigured=False, caller='caller',
                      *, focus='', profiles=None, engines=None):
    requested_strategy = strategy
    if strategy == 'whole-1m':
        strategy = 'single-pass'
    left, right = engines if engines is not None else _audit_engines(
        backends, dual_blind, left_override, right_override, allow_unconfigured, caller)
    if not left:
        return {'mode': 'unconfigured', 'caller': caller, 'handoff_to_caller': True,
                'total_files': len(file_records), 'total_estimated_tokens': total_tokens,
                'left_engine': None, 'right_engine': None, 'left_units': [], 'right_units': [],
                'skipped_files': [], 'execution_profiles': {}, 'notice': '未发现 agy 或 codex，当前只提供未就绪计划。'}
    if profiles is None:
        tier, _ = select_task_tier(kind='audit', text=focus)
        paths = [record['rel_path'] for record in file_records]
        estimated = _audit_estimate(paths, total_tokens, focus)['estimated_tokens']
        profiles = {}
        for side, spec in (('left', left), ('right', right)):
            if spec:
                profiles[side] = resolve_execution_profile(backends, spec, tier, focus, 'audit', estimated)
    if right and profiles['left']['model'] and profiles['left']['spec'] == profiles['right']['spec']:
        raise HarnessError('双路选路最终指向同一个模型；请为其中一路指定其他模型。')
    plan = {'mode': strategy, 'requested_strategy': requested_strategy, 'caller': caller, 'handoff_to_caller': True,
            'total_files': len(file_records), 'total_estimated_tokens': total_tokens,
            'dual_blind': bool(right), 'left_engine': left, 'right_engine': right, 'skipped_files': [],
            'execution_profiles': profiles, 'soft_shard_target_tokens': shard_tokens,
            'coverage_notice': '清单是计划覆盖范围，实际读取完整性仍由回执与证据核实。',
            'budget_notice': '按每 3 字节约 1 token 估算文件、审查提示词、清单与 JSON Schema，另保留策略余量；不表示分词器实测容量。',
            'independence_notice': '不同进程独立审查，模型家族及错误独立性尚未验证。'}
    all_files = [record['rel_path'] for record in file_records]
    whole = strategy != 'sharded'
    for side, spec in (('left', left), ('right', right)):
        units_key = side + '_units'
        plan[units_key] = []
        if not spec:
            continue
        profile = profiles[side]
        cap = profile['capacity']['input_budget_tokens']
        interop = profile['execution_environment']['windows_interop']
        all_estimate = _audit_estimate(all_files, total_tokens, focus, interop)
        if strategy != 'sharded' and all_estimate['estimated_tokens'] <= cap:
            plan[units_key] = [{'shard_id': 1, 'files': all_files, **all_estimate, 'single_pass_planned': True}]
            continue
        whole = False
        if strategy == 'single-pass':
            raise HarnessError(f'{spec} 文件与提示词超出单次预算，请选择 sharded 或调整有依据的预算。',
                               execution_profile=profile, **all_estimate)
        usable = []
        for record in file_records:
            estimate = _audit_estimate([record['rel_path']], record['tokens'], focus, interop)
            if estimate['estimated_tokens'] > cap:
                plan['skipped_files'].append({'file': record['rel_path'], 'engine': spec, 'side': side,
                                             'reason': 'over_input_budget', **estimate, 'budget_tokens': cap})
            else:
                usable.append(record)
        plan[units_key] = [{**unit, 'single_pass_planned': False,
                            'exceeds_soft_shard_target': unit['estimated_tokens'] > shard_tokens}
                           for unit in build_shards(usable, shard_tokens, input_budget=cap, focus=focus, interop=interop)]
    plan['mode'] = 'single-pass' if whole else 'sharded'
    if not whole:
        plan['coverage_notice'] += ' 跨分片依赖未在同一个上下文内联合核查。'
    if requested_strategy == 'whole-1m':
        plan['deprecation_notice'] = 'whole-1m 兼容映射至 single-pass，不表示百万 token 或无压缩能力。'
    return plan


def merge_blind_findings(left_findings, right_findings, left_engine, right_engine, target_dir, line_tolerance=8):
    """按相近行号组织候选发现，同一位置的意见仍需当前总控核实。"""
    ranks = {'critical': 4, 'high': 3, 'medium': 2, 'low': 1}
    left = [{**item, 'file': _normalize_rel_file(item['file'], target_dir)} for item in left_findings]
    right = [{**item, 'file': _normalize_rel_file(item['file'], target_dir)} for item in right_findings]
    matched = set()
    consensus, singles = [], []
    for item in left:
        hit = None
        if right_engine:
            for index, other in enumerate(right):
                if (index not in matched and item['file'] == other['file']
                        and item['line_start'] - line_tolerance <= other['line_end']
                        and other['line_start'] - line_tolerance <= item['line_end']):
                    hit = index
                    break
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


# ════════════════════════════════════════════════════════════════════════════
# ⑦ 物理门禁循环与五大 CLI 子命令入口
# ════════════════════════════════════════════════════════════════════════════


def run_physics_gate_loop(target_dir, task_desc, verify_cmd, worker_fn, verify_fn=None, max_rounds=3,
                          caller='caller', timeout_sec=600, write_scope=None):
    if not str(verify_cmd).strip():
        raise HarnessError('--verify-cmd 不能为空白命令')

    def default_verify(command, directory):
        result = run_process(command, directory, timeout_sec=timeout_sec, shell=True)
        return result.returncode, (result.stdout + '\n' + result.stderr).strip()

    verifier = verify_fn or default_verify
    scopes = _scope_patterns(write_scope or [])
    worker_changes = set()
    verification_changes = set()
    violations = set()
    history = []
    status = 'verification_failed'
    last_error = ''
    scope_failure = {}

    def checked_snapshot(phase, round_number=None):
        try:
            return snapshot_files(target_dir)
        except (HarnessError, OSError) as exc:
            scope_failure.update(scope_error_phase=phase, scope_error=str(exc))
            if round_number is not None:
                scope_failure['scope_error_round'] = round_number
            path = getattr(exc, 'details', {}).get('snapshot_path') or getattr(exc, 'filename', None)
            if path is not None:
                scope_failure['scope_error_path'] = str(path)
            return None

    initial = checked_snapshot('preflight')
    rounds = range(1, max_rounds + 1) if initial is not None else ()
    for number in rounds:
        before_worker = initial if number == 1 else checked_snapshot('pre_worker', number)
        if before_worker is None:
            break
        prompt = (f"按授权任务修改当前目录，允许写入范围为 {scopes or '调用方授权的目标工作目录'}。"
                  f'严禁在目标工作目录外创建、修改或删除任何文件或目录（含严禁向目录外执行 git worktree add）。\n'
                  f'{task_desc}\n验收命令为 {verify_cmd}。不得通过放宽验收要求取得通过。'
                  f'Git 提交与推送由当前总控负责。不要调用本编排器或改变其他会话。\n上一轮反馈:\n{last_error[-3500:]}')
        entry = {'round': number, 'passed': False, 'verify_exit_code': None, 'verification_executed': False}
        emit_event('gate_round_started', round=number, caller=caller)
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
        after_worker = checked_snapshot('post_worker', number)
        if after_worker is None:
            entry.update(scope_verified=False, scope_status='failed', **scope_failure)
            history.append(entry)
            break
        round_changes = changed_files(before_worker, after_worker)
        worker_changes.update(round_changes)
        round_violations = [path for path in round_changes if scopes and not _scope_matches(path, scopes)]
        violations.update(round_violations)
        entry['worker_changed_files'] = round_changes
        if round_violations:
            status = 'scope_violation'
            entry.update(status=status, scope_violations=round_violations)
            worker_ok = False
        if worker_ok:
            try:
                entry['verification_executed'] = True
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
            after_verify = checked_snapshot('post_verify', number)
            if after_verify is None:
                entry.update(passed=False, scope_verified=False, scope_status='failed', **scope_failure)
                history.append(entry)
                break
            verify_changes = changed_files(after_worker, after_verify)
            verification_changes.update(verify_changes)
            entry['verification_changed_files'] = verify_changes
        history.append(entry)
        if status in ('ok', 'cancelled', 'timeout', 'scope_violation', 'invalid_output', 'quota_exhausted'):
            break
    final = checked_snapshot('final') if not scope_failure else None
    changes = changed_files(initial, final) if final is not None else None
    extra_quota = {k: history[-1][k] for k in ('preflight_blocked', 'quota_backend', 'quota_execution_side', 'quota_cooldown',
                                             'quota_fuse_triggered', 'user_confirmation_required', 'user_choices')
                   if history and k in history[-1]} if status == 'quota_exhausted' else {}
    scope_error = {'error': scope_failure['scope_error'], **scope_failure} if scope_failure else {}
    if scope_failure and status not in ('quota_exhausted', 'cancelled', 'timeout', 'scope_violation'):
        status = 'failed'
    return {'status': status, 'gate_passed': status == 'ok', 'circuit_breaker_triggered': status != 'ok',
            'rounds_used': len(history), 'verify_cmd': verify_cmd, 'history': history,
            'verification_executed': any(item.get('verification_executed') for item in history),
            'caller': caller, 'handoff_to_caller': True, 'changed_files': changes,
            'worker_changed_files': sorted(worker_changes), 'verification_changed_files': sorted(verification_changes),
            'scope_violations': sorted(violations), 'scope_check': 'unverified' if scope_failure else 'post_worker_snapshot',
            'scope_verified': not scope_failure,
            'scope_notice': '需使用独立任务目录，快照无法区分并发写入来源。验收命令产物单独记录，检查不自动回滚。',
            **extra_quota, **scope_error}


def _args_backends(args):
    return probe_backends(args.codex_input_budget, args.agy_input_budget, getattr(args, 'discover_models', False))


def _target(args):
    target = Path(args.target).expanduser().resolve()
    if not target.exists():
        raise HarnessError(f'目标不存在: {target}')
    return target, target if target.is_dir() else target.parent


def _task_text(args):
    text = Path(args.task_file).expanduser().resolve().read_text(encoding='utf-8-sig') if getattr(args, 'task_file', None) else args.task
    if not text or not text.strip():
        raise HarnessError('任务内容为空')
    return text


def _git_head(root):
    try:
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


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


def _prepare_execution(backends, specs, batch, *, dry_run=False):
    if dry_run:
        return
    # Check all selected sides before sending even a model-listing request.
    _enforce_preflight_quota(backends, specs, batch=batch)
    codex_spec = next((spec for spec in specs if spec and _engine_parts(spec)[0] == 'codex'), None)
    if codex_spec and not _current_catalog(backends['codex']):
        try:
            _refresh_codex_listing(backends['codex'], batch)
        except HarnessError as exc:
            exc.details.setdefault('requested_engine', codex_spec)
            exc.details.setdefault('execution_environment', _execution_environment(backends['codex']))
            raise
    agy_spec = next((spec for spec in specs if spec and _engine_parts(spec)[0] == 'agy'), None)
    if not agy_spec:
        return
    info = backends.get('agy', {})
    if not info.get('execution_config_supported', True):
        raise HarnessError('CAE_AGY_HOME 仅用于离线配置探测，执行请使用当前 CLI 的真实用户配置')
    authenticated = info.get('authentication_status') == 'authenticated'
    needs_side_check = _can_fallback_agy(info) and not authenticated and bool(_find_windows_agy_in_wsl())
    if needs_side_check or not _current_catalog(info):
        try:
            backends['agy'] = _refresh_agy_listing(info, batch, refresh_model_cache=True)
        except HarnessError as exc:
            exc.details.setdefault('requested_engine', agy_spec)
            raise
    _enforce_preflight_quota(backends, specs, batch=batch)


def _planned_profile(args, backends, engine, tier, reason, text, kind, estimated_tokens):
    profile = resolve_execution_profile(backends, engine, tier, text, kind, estimated_tokens)
    prefix, _ = _engine_parts(engine)
    profile.update(requested_tier=args.tier, tier_reason=reason,
                   quota_status=backends.get(prefix, {}).get('quota_status', 'unknown_until_called'))
    if (args.tier != 'auto' and prefix == 'agy' and profile['effort_source'] != 'fixed_configuration'
            and profile['declared_effort'] is None and profile['cli_effort'] is None):
        profile['tier_adjustment'] = 'requested_tier_not_applied_no_catalog_declaration'
    return profile


def _profile_payload(profile):
    return {key: profile[key] for key in ('requested_engine', 'requested_tier', 'model', 'reasoning_effort',
                                          'declared_effort', 'effective_effort', 'selected_tier', 'tier_reason',
                                          'profile_source', 'effort_source', 'quota_status')} | {'execution_profile': profile}


def _run_cli_gate(args, root, requested_target, text, caller, engine, backends, scopes, profile, kind, estimated_tokens, batch):
    receipts = []
    current = profile
    prefix, _ = _engine_parts(engine)
    retry_on_failure = (args.tier == 'auto' and profile['tier'] not in ('max', 'ultra')
                        and not profile['tier_reason'].startswith('user_description_'))

    def variant_key(selected):
        return (selected['spec'], selected['cli_effort'], selected['capacity']['cli_context_window_tokens'])

    def worker(prompt, number):
        nonlocal current
        previous = current
        if number == 2 and retry_on_failure:
            candidate = _planned_profile(args, backends, engine, 'max', 'gate_retry_escalated_max', text, kind, estimated_tokens)
            old = previous.get('declared_effort')
            new = candidate.get('declared_effort')
            if (not candidate.get('execution_blocked_reason') and variant_key(candidate) != variant_key(previous)
                    and new in EFFORT_LADDER and (old is None or EFFORT_LADDER.index(new) > EFFORT_LADDER.index(old))):
                current = candidate
        invocation = {}
        entry = {'execution_profile': current, 'invocation': invocation, 'variant_changed': variant_key(current) != variant_key(previous)}
        receipts.append(entry)
        return invoke_edit_worker(current['spec'], root, prompt, backends, args.timeout, current['cli_effort'], args.retry_quota,
                                  profile=current, batch=batch, receipt=invocation)

    result = run_physics_gate_loop(root, f'任务目标 {requested_target}。\n' + text, args.verify_cmd, worker,
                                   max_rounds=args.max_rounds, caller=caller, timeout_sec=args.timeout, write_scope=scopes)
    for entry, receipt in zip(result['history'], receipts):
        entry.update(receipt)
        entry['escalated'] = receipt['variant_changed'] and bool(receipt['invocation'].get('invocation_started'))
    last = next((entry['execution_profile'] for entry in reversed(result['history'])
                 if entry.get('invocation', {}).get('invocation_started')), profile)
    result.update(_profile_payload(last))
    result['quota_status'] = backends.get(prefix, {}).get('quota_status', last['quota_status'])
    return result


def _gate_can_replay(result, before, root):
    """第二轮、验收已执行、出现过改动或快照无法核对时，均不可自动重放。"""
    if (before is None or result.get('scope_verified') is not True
            or result.get('changed_files') != [] or result.get('worker_changed_files')
            or result.get('verification_changed_files') or result.get('rounds_used') != 1):
        return False
    entry = result['history'][0]
    if (entry.get('verification_executed') or entry.get('verify_exit_code') is not None
            or not _quota_can_replay(entry, entry.get('invocation', {}))):
        return False
    scope = _safe_changes_report(before, root, [], True)
    return scope['scope_verified'] and not scope['changed_files']


def _run_gate_with_fallback(args, root, requested_target, text, caller, engine, backends,
                            scopes, profile, kind, estimated_tokens, batch, meta, before):
    result = _run_cli_gate(args, root, requested_target, text, caller, engine, backends,
                           scopes, profile, kind, estimated_tokens, batch)
    if (result.get('status') != 'quota_exhausted' or not getattr(args, 'fallback_on_quota', False)
            or 'quota_fallback' in meta or not _gate_can_replay(result, before, root)):
        return result
    fallback = _local_fallback_engine(caller, engine, backends)
    if not fallback:
        return result
    meta['quota_fallback'] = _quota_fallback_record(engine, fallback, result)
    try:
        _prepare_execution(backends, (fallback,), batch)
        if not _workspace_unchanged(before, root):
            meta.pop('quota_fallback')
            return result
        selected = _planned_profile(args, backends, fallback, profile['tier'], profile['tier_reason'],
                                     text, kind, estimated_tokens)
        _require_execution_profile(selected)
    except HarnessError as exc:
        # 本侧准备失败时保留原额度回执与门禁历史，便于总控复核两次尝试。
        return {**result, 'status': exc.status, 'error': str(exc), **exc.details}
    meta.update(engine=fallback)
    if 'worker_engine' in meta:
        meta['worker_engine'] = fallback
    meta.update(_profile_payload(selected))
    return _run_cli_gate(args, root, requested_target, text, caller, fallback, backends,
                         scopes, selected, kind, estimated_tokens, batch)


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
    scenarios['economy_review'] = ('review', '省额度做常规代码审查')
    scenarios['economy_surface_review'] = ('review', '省额度检查注释拼写')
    recommendations = {}
    for name, info in backends.items():
        info['caller_relation'] = ('local' if name == local_backend else 'counterpart') if local_backend else 'unspecified'
        info['program_origin'] = ('windows_interop' if info['windows_interop'] else 'native') if info['detected'] else None
        info['execution_environment'] = _execution_environment(info)
        info['capability_matrix'] = _capability_matrix(name, info)
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
            recommendations[name][scenario] = {**profile, 'requested_tier': 'auto', 'tier_reason': reason,
                                                'backend_detected': info['detected']}
    return _print({'status': 'ok', 'caller': caller, 'backends': backends,
                   'recommendations': recommendations,
                   'catalog_discovery_requested': args.discover_models,
                   'runtime_verified': False, 'controls_native_spawn_agent': False,
                   'notice': '能力矩阵和选路基于目录及策略。实际可用性、性能、有效上下文、压缩和委派行为仍需执行验证。'})


def cmd_audit(args):
    target, root = _target(args)
    caller = resolve_caller(args.caller)
    backends = _args_backends(args)
    skipped = []
    records = collect_target_files(target, skipped)
    if not records:
        raise HarnessError('目标范围没有可审查文件', skipped_files=skipped)
    total_tokens = sum(item['tokens'] for item in records)
    engines = _audit_engines(backends, not args.single, args.left, args.right, args.dry_run, caller)
    tier, tier_reason = select_task_tier(args.tier, 'audit', args.focus, len(records), total_tokens)
    meta = {'caller': caller, 'handoff_to_caller': True, 'requested_tier': args.tier,
            'selected_tier': tier, 'tier_reason': tier_reason, 'target': str(root)}
    batch = _QuotaBatch(args.retry_quota)
    fallback_before = snapshot_files(root) if getattr(args, 'fallback_on_quota', False) and not args.dry_run else None
    try:
        _prepare_execution(backends, engines, batch, dry_run=args.dry_run)
    except HarnessError as exc:
        q_backend = exc.details.get('quota_backend') or ''
        fb_engine = _local_fallback_engine(caller, f'{q_backend}:default', backends) if (
            exc.status == 'quota_exhausted' and q_backend and getattr(args, 'fallback_on_quota', False)
            and _workspace_unchanged(fallback_before, root)
        ) else None
        if not fb_engine:
            return _print({'status': exc.status, **meta, 'error': str(exc), 'requested_engines': list(engines), **exc.details})
        tripped_spec = next((e for e in engines if e and _engine_parts(e)[0] == q_backend), f'{q_backend}:default')
        meta['quota_fallback'] = _quota_fallback_record(tripped_spec, fb_engine, exc.details)
        kept = [e for e in engines if e and _engine_parts(e)[0] != q_backend]
        engines = (kept[0] if kept else fb_engine, None)
        try:
            _prepare_execution(backends, engines, batch, dry_run=args.dry_run)
            if not _workspace_unchanged(fallback_before, root):
                raise HarnessError('回退预检后工作区发生变化或无法核对，已停止执行')
        except HarnessError as fb_exc:
            return _print({'status': fb_exc.status, **meta, 'error': str(fb_exc), 'requested_engines': list(engines), **fb_exc.details})
    profiles = {}
    for side, engine in zip(('left', 'right'), engines):
        if engine:
            prefix, _ = _engine_parts(engine)
            estimate = _audit_estimate([record['rel_path'] for record in records], total_tokens, args.focus,
                                       backends[prefix].get('windows_interop', False))
            profiles[side] = _planned_profile(args, backends, engine, tier, tier_reason, args.focus, 'audit', estimate['estimated_tokens'])
    plan = select_audit_plan(total_tokens, records, backends, args.strategy, not args.single, args.left, args.right,
                             args.shard_tokens, args.dry_run, caller, focus=args.focus, profiles=profiles, engines=engines)
    plan['skipped_files'].extend(skipped)
    plan.update(requested_tier=args.tier, selected_tier=tier, tier_reason=tier_reason)
    if args.dry_run:
        return _print({'status': 'ok', 'dry_run': True, 'plan': plan})
    for profile in profiles.values():
        _require_execution_profile(profile)
    base = _git_head(root)
    before = snapshot_files(root)
    tasks = [(side, unit) for side in ('left', 'right') for unit in plan[side + '_units']]
    findings = {'left': [], 'right': []}
    errors, completed = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, max(1, len(tasks)))) as pool:
        futures = {}
        for side, unit in tasks:
            profile = profiles[side]
            receipt = {}
            future = pool.submit(invoke_readonly_engine, profile['spec'], root, _build_audit_prompt(unit, args.focus),
                                 backends, args.timeout, unit['files'], profile['cli_effort'], args.retry_quota,
                                 profile=profile, batch=batch, receipt=receipt)
            futures[future] = side, unit, receipt
        for future in concurrent.futures.as_completed(futures):
            side, unit, receipt = futures[future]
            profile = profiles[side]
            engine = plan[side + '_engine']
            common = {'side': side, 'engine': engine, 'model': profile['model'],
                      'reasoning_effort': profile['reasoning_effort'], 'shard_id': unit['shard_id'],
                      'planned_files': unit['files'], 'invocation': receipt}
            try:
                result = future.result()
                _validate_schema(result, AUDIT_JSON_SCHEMA)
                planned = set(unit['files'])
                read = [_normalize_rel_file(path, root) for path in result['read_files']]
                read_paths = set(read)
                unread = [{**item, 'file': _normalize_rel_file(item['file'], root)} for item in result['unread_files']]
                unread_paths = {item['file'] for item in unread}
                if (read_paths | unread_paths) - planned or read_paths & unread_paths:
                    raise OutputParseError('读取覆盖回执与本分片文件范围不一致')
                if len(read_paths) != len(read) or len(unread_paths) != len(unread):
                    raise OutputParseError('读取覆盖回执包含重复路径')
                normalized = [{**item, 'file': _normalize_rel_file(item['file'], root)} for item in result['findings']]
                if {item['file'] for item in normalized} - planned:
                    raise OutputParseError('发现项引用了分片清单之外的文件')
                if any(item['file'] not in read_paths for item in normalized):
                    raise OutputParseError('发现项没有相应的自报已读文件')
                for missing in sorted(planned - read_paths - unread_paths):
                    unread.append({'file': missing, 'reason': 'backend_did_not_report_reading'})
                findings[side].extend(normalized)
                completed.append({**common, 'read_files': read, 'unread_files': unread,
                                  'status': 'partial' if unread else 'ok',
                                  'reading_coverage_verified': False, 'summary': result['summary']})
                emit_event('audit_unit_completed', engine=engine, shard_id=unit['shard_id'])
            except Exception as exc:
                errors.append({**common, 'status': getattr(exc, 'status', 'failed'),
                               'error': str(exc), **getattr(exc, 'details', {})})
    scope = _safe_changes_report(before, root, [], True)
    quota_err = next((err for err in errors if err.get('status') == 'quota_exhausted'), None)
    if scope['scope_violations']:
        status = 'scope_violation'
    elif _CANCELLED.is_set():
        status = 'cancelled'
    elif not tasks:
        status = 'partial'
    elif not completed:
        statuses = {error['status'] for error in errors}
        status = 'quota_exhausted' if 'quota_exhausted' in statuses else (next(iter(statuses)) if len(statuses) == 1 else 'failed')
    else:
        status = 'partial' if errors or plan['skipped_files'] or any(unit['unread_files'] for unit in completed) else 'ok'
    scope_error = {}
    if not scope['scope_verified']:
        if status not in ('quota_exhausted', 'cancelled', 'scope_violation'):
            status = 'failed'
        scope_error['error'] = scope['scope_error']
    quota_meta = {k: quota_err[k] for k in ('quota_backend', 'quota_execution_side', 'quota_cooldown',
                                          'quota_fuse_triggered', 'user_confirmation_required', 'user_choices')
                  if quota_err and k in quota_err}
    completed.sort(key=lambda unit: (unit['side'], unit['shard_id']))
    errors.sort(key=lambda unit: (unit['side'], unit['shard_id']))
    merged = merge_blind_findings(findings['left'], findings['right'], plan['left_engine'], plan['right_engine'], root)
    return _print({'status': status, **meta, 'plan': plan, 'base_commit': base,
                   'completed_units': completed, 'completed_count': sum(unit['status'] == 'ok' for unit in completed),
                   'received_count': len(completed), 'total_units': len(tasks), 'errors': errors,
                   'reading_coverage_verified': False,
                   'consensus_count': len(merged['consensus_findings']), 'single_source_count': len(merged['single_source_findings']),
                   **merged, **scope, **scope_error, **quota_meta,
                   'verification_notice': '当前总控需核实原始文件与证据后采纳发现。'})


def cmd_gate(args):
    if not str(args.verify_cmd).strip():
        raise HarnessError('--verify-cmd 不能为空白命令')
    requested_target, root = _target(args)
    text = _task_text(args)
    backends = _args_backends(args)
    caller = resolve_caller(args.caller)
    engine = choose_engine(backends, caller, args.worker)
    scopes = _scope_patterns(args.write_scope)
    if requested_target.is_file() and not scopes:
        scopes = [requested_target.relative_to(root).as_posix()]
    file_count, est_tokens = _target_metrics(requested_target, text)
    tier, tier_reason = select_task_tier(args.tier, 'gate', text, file_count, est_tokens)
    batch = _QuotaBatch(args.retry_quota)
    meta = {'caller': caller, 'handoff_to_caller': True, 'engine': engine, 'worker_engine': engine,
            'requested_tier': args.tier, 'selected_tier': tier, 'tier_reason': tier_reason,
            'target': str(root), 'requested_target': str(requested_target), 'verification_executed': False}
    fallback_before = snapshot_files(root) if getattr(args, 'fallback_on_quota', False) and not args.dry_run else None
    try:
        _prepare_execution(backends, (engine,), batch, dry_run=args.dry_run)
    except HarnessError as exc:
        fb_engine = _local_fallback_engine(caller, engine, backends) if (
            exc.status == 'quota_exhausted' and getattr(args, 'fallback_on_quota', False)
            and _workspace_unchanged(fallback_before, root)
        ) else None
        if not fb_engine:
            return _print({'status': exc.status, **meta, 'error': str(exc), **exc.details})
        meta['quota_fallback'] = _quota_fallback_record(engine, fb_engine, exc.details)
        engine = fb_engine
        meta.update(engine=engine, worker_engine=engine)
        try:
            _prepare_execution(backends, (engine,), batch, dry_run=args.dry_run)
            if not _workspace_unchanged(fallback_before, root):
                raise HarnessError('回退预检后工作区发生变化或无法核对，已停止执行')
        except HarnessError as fb_exc:
            return _print({'status': fb_exc.status, **meta, 'error': str(fb_exc), **fb_exc.details})
    profile = _planned_profile(args, backends, engine, tier, tier_reason, text, 'gate', est_tokens)
    if args.dry_run:
        prefix, _ = _engine_parts(engine)
        return _print({'status': 'ok', 'dry_run': True, **meta, **_profile_payload(profile),
                       'quota_cooldown': backends.get(prefix, {}).get('quota_cooldown'),
                       'work_dir': str(root), 'verify_cmd': args.verify_cmd, 'write_scope': scopes, 'max_rounds': args.max_rounds})
    _require_execution_profile(profile)
    base = _git_head(root)
    try:
        result = _run_gate_with_fallback(args, root, requested_target, text, caller, engine, backends,
                                         scopes, profile, 'gate', est_tokens, batch, meta, fallback_before)
    except HarnessError as exc:
        return _print({'status': exc.status, **meta, 'base_commit': base, 'error': str(exc), **exc.details})
    return _print({**meta, **result, 'base_commit': base})


def cmd_task(args):
    if args.kind == 'code' and args.verify_cmd is not None and not str(args.verify_cmd).strip():
        raise HarnessError('--verify-cmd 不能为空白命令')
    requested_target, root = _target(args)
    text = _task_text(args)
    caller = resolve_caller(args.caller)
    backends = _args_backends(args)
    engine = choose_engine(backends, caller, args.engine)
    scopes = _scope_patterns(args.write_scope)
    readonly = args.kind != 'code'
    if readonly and (scopes or args.verify_cmd):
        raise HarnessError('只读任务不可声明写入范围或验收命令')
    rel_single_file = requested_target.relative_to(root).as_posix() if requested_target.is_file() else None
    if not readonly and rel_single_file and not scopes:
        scopes = [rel_single_file]
    base = _git_head(root)
    if args.base_commit:
        expected = _local_command(['git', '-C', str(root), 'rev-parse', args.base_commit + '^{commit}'])
        if base != expected:
            raise HarnessError('当前 HEAD 与 --base-commit 不一致')
    file_count, est_tokens = _target_metrics(requested_target, text)
    tier, tier_reason = select_task_tier(args.tier, args.kind, text, file_count, est_tokens)
    batch = _QuotaBatch(args.retry_quota)
    payload = {'kind': args.kind, 'caller': caller, 'handoff_to_caller': True, 'engine': engine,
               'requested_tier': args.tier, 'selected_tier': tier, 'tier_reason': tier_reason,
               'target': str(root), 'requested_target': str(requested_target), 'base_commit': base,
               'readonly': readonly, 'write_scope': scopes}
    if not readonly:
        payload['verification_executed'] = False
    fallback_before = snapshot_files(root) if getattr(args, 'fallback_on_quota', False) and not args.dry_run else None
    try:
        _prepare_execution(backends, (engine,), batch, dry_run=args.dry_run)
    except HarnessError as exc:
        fb_engine = _local_fallback_engine(caller, engine, backends) if (
            exc.status == 'quota_exhausted' and getattr(args, 'fallback_on_quota', False)
            and _workspace_unchanged(fallback_before, root)
        ) else None
        if not fb_engine:
            return _print({'status': exc.status, **payload, 'result': {}, 'changed_files': [], 'scope_violations': [],
                           'error': str(exc), **exc.details})
        payload['quota_fallback'] = _quota_fallback_record(engine, fb_engine, exc.details)
        engine = fb_engine
        payload['engine'] = engine
        try:
            _prepare_execution(backends, (engine,), batch, dry_run=args.dry_run)
            if not _workspace_unchanged(fallback_before, root):
                raise HarnessError('回退预检后工作区发生变化或无法核对，已停止执行')
        except HarnessError as fb_exc:
            return _print({'status': fb_exc.status, **payload, 'result': {}, 'changed_files': [], 'scope_violations': [],
                           'error': str(fb_exc), **fb_exc.details})
    profile = _planned_profile(args, backends, engine, tier, tier_reason, text, args.kind, est_tokens)
    prefix, _ = _engine_parts(engine)
    payload.update(_profile_payload(profile))
    if args.dry_run:
        return _print({'status': 'ok', 'dry_run': True, 'quota_cooldown': backends.get(prefix, {}).get('quota_cooldown'), **payload})
    _require_execution_profile(profile)
    if args.kind == 'code' and args.verify_cmd:
        try:
            result = _run_gate_with_fallback(args, root, requested_target, text, caller, engine, backends,
                                             scopes, profile, args.kind, est_tokens, batch, payload, fallback_before)
        except HarnessError as exc:
            return _print({'status': exc.status, **payload, 'result': {}, 'changed_files': [],
                           'scope_violations': [], 'error': str(exc), **exc.details})
        return _print({**payload, **result})
    if not readonly:
        payload['verification_notice'] = '未提供 --verify-cmd，本次代码修改未经宿主机命令实测验证，请总控运行验证后再验收。'
    before = snapshot_files(root)
    rel_hint = f'（相对路径: {rel_single_file}）' if rel_single_file else ''
    stage_files = [rel_single_file] if (readonly and rel_single_file) else None
    prompt = (f'任务类型 {args.kind}。当前总控 {caller}。工作目录为当前目录。任务目标 {requested_target}{rel_hint}。\n'
              + ('只读完成任务，正文通过回执返回。' if readonly else f"允许写入范围 {scopes or '调用方授权的目标工作目录'}。")
              + '严禁在目标工作目录外创建、修改或删除任何文件或目录（含严禁向目录外执行 git worktree add）。'
              + '不要调用本编排器，不要修改其他会话或全局配置。\n' + text
              + '\n按给定 JSON Schema 返回正文、证据及未验证事项。')
    result, error, status, invocation = {}, {}, 'ok', {}
    try:
        try:
            result = invoke_engine(profile['spec'], root, prompt, backends, TASK_JSON_SCHEMA,
                                   readonly, args.timeout, 'readonly-collaborator', stage_files, profile['cli_effort'], args.retry_quota,
                                   profile=profile, batch=batch, receipt=invocation)
        except HarnessError as exc:
            fb_engine = _local_fallback_engine(caller, engine, backends) if (
                getattr(exc, 'status', None) == 'quota_exhausted'
                and getattr(args, 'fallback_on_quota', False)
                and 'quota_fallback' not in payload
                and _quota_can_replay(exc.details, invocation)
                and _workspace_unchanged(before, root)
                and _workspace_unchanged(fallback_before, root)
            ) else None
            if not fb_engine:
                raise
            payload['quota_fallback'] = _quota_fallback_record(engine, fb_engine, exc.details)
            _prepare_execution(backends, (fb_engine,), batch, dry_run=False)
            if not _workspace_unchanged(before, root):
                payload.pop('quota_fallback')
                raise
            fb_profile = _planned_profile(args, backends, fb_engine, tier, tier_reason, text, args.kind, est_tokens)
            _require_execution_profile(fb_profile)
            engine = fb_engine
            prefix, _ = _engine_parts(engine)
            payload['engine'] = engine
            payload.update(_profile_payload(fb_profile))
            invocation = {}
            result = invoke_engine(fb_profile['spec'], root, prompt, backends, TASK_JSON_SCHEMA,
                                   readonly, args.timeout, 'readonly-collaborator', stage_files, fb_profile['cli_effort'], args.retry_quota,
                                   profile=fb_profile, batch=batch, receipt=invocation)
    except Exception as exc:
        status = getattr(exc, 'status', 'failed')
        error = {'error': str(exc), **getattr(exc, 'details', {})}
    finally:
        scope = _safe_changes_report(before, root, scopes, readonly)
    payload['quota_status'] = backends.get(prefix, {}).get('quota_status', payload['quota_status'])
    if not scope.get('scope_verified', True) and status == 'ok':
        status = 'failed'
        error.setdefault('error', scope.get('scope_error') or '工作区快照核对失败')
    elif scope['scope_violations']:
        status = 'scope_violation'
    return _print({'status': status, **payload, 'invocation': invocation, 'result': result, **error, **scope})


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
                             help='挡位策略：auto 默认启用旗舰专长模型与单模型最高挡 max（短润色用 medium、长润色用 high）；仅当用户显式或模糊要求降挡／省 token 时切换分级自适应（常规 high、表面检查轻量模型 + low）')
        command.add_argument('--retry-quota', action='store_true',
                             help='用户确认已充值或额度恢复后，忽略本地额度冷却记录重试发起')
        command.add_argument('--fallback-on-quota', action='store_true',
                             help='跨端目标触发额度熔断且未修改工作区时，自动切回发起方本侧合适的模型与挡位执行')
        command.add_argument('--timeout', type=_positive_arg, default=600)
        command.add_argument('--dry-run', action='store_true')
    return parser


def main():
    # 作为库被测试导入时不改宿主标准流；编码与取消状态只在 CLI 入口初始化。
    for name in ('stdin', 'stdout', 'stderr'):
        stream = getattr(sys, name, None)
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    _CANCELLED.clear()
    with QUOTA_LOCK:
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
