#!/usr/bin/env python3
"""chats -- free-form conversations that can call the local tools (v0.20.0).

One chat is one JSON file holding an OpenAI message list (user/assistant/tool).
The system prompt is rebuilt on every run and never stored, so editing it also
changes how old chats are replayed.

POST /chat must answer before the model does: the mod's socket read timeout is
16 s, so the user line is appended, the run is marked 'running', and a daemon
thread continues the tool loop.  GET /chat is what the mod polls meanwhile.
"""
from __future__ import annotations

import difflib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import jobqueue

#: a single user line longer than this is refused before it reaches the model
MAX_TEXT = 8000
#: model calls per turn (one round = one request; tool calls make a round busy)
MAX_ROUNDS = 6
#: one tool result longer than this is cut before it enters the conversation
MAX_TOOL_OUTPUT = 4000
#: how much of the arguments / result a last['tools'] row keeps
TOOL_ARGS_SUMMARY = 120
TOOL_SUMMARY = 80
#: a game_lookup detail is JSON, but the model does not need megabytes of it
DETAIL_MAX = 1200
#: hard ceiling for one whole turn, however the per-call timeouts add up
OVERALL_TIMEOUT = 180.0

# v0.22.0（档3）：模组每一轮把工作区快照一起发过来，侧车只做只读工具 —— 写代码仍由玩家在窗口里点。
# 快照全量落进对话文件是为了「模型中途再查一次」也能查到，但必须夹住体积，不然一段对话就几 MB。
WS_MAX = 40             # 一段对话最多记多少个工作区
WS_PROCS_MAX = 40       # 每个工作区最多记多少台处理器
WS_CODE_MAX = 6000      # 每台处理器最多留多少字符代码
WS_NOTE_MAX = 500       # 备注最多留多少字符

# v0.23.0：玩家/服务器快照同样是模组每一轮送来的只读副本 —— 形状由模组定，侧车只负责夹体积。
# 蓝图与控制台命令走「意图队列」：模型只登记，真正写进游戏仍然要玩家在窗口里点确认。
SNAP_KEYS_MAX = 60      # 一个快照最多留多少个键
SNAP_LIST_MAX = 40      # 快照里一个列表最多留多少项
SNAP_TEXT_MAX = 200     # 快照里一个字符串最多留多少字符
SNAP_DEPTH = 3          # 快照最多往下几层
INTENTS_MAX = 4         # 每轮最多登记多少条意图
BP_NAME_MAX = 80        # 蓝图名字最多留多少字符
BP_DESC_MAX = 200       # 蓝图备注最多留多少字符
BP_CODE_MAX = 20000     # 一条蓝图代码（base64）最多留多少字符
# v0.23.1：F8 控制台是 JavaScript 控制台（ConsoleFragment 把输入直接丢给 Rhino 执行），
# 所以这里留的是「一段 JS 代码」而不是「一条聊天命令」，多行要原样留住。
CONSOLE_CODE_MAX = 2000  # 一段控制台 JS 代码最多留多少字符（多行一起算）
CONSOLE_API_MAX = 40     # console_help 最多列多少个 api.* 函数名
CONSOLE_INJECTED = ('unit', 'player', 'team', 'core', 'items', 'build',
                    'cursor', 'cursorUnit')  # ConsoleFragment 每次输入预置的变量

#: copied from jobqueue.Store: a write can lose a race on Windows and is retried
SAVE_TRIES = 6
SAVE_WAIT = 0.03
_WIN_CONTENDED = (5, 32, 33)

DATA_DIR = os.path.dirname(os.path.abspath(__file__))

SYSTEM_PROMPT = (
    '你是 Mindustry 游戏内的 mlog 助手，玩家就在游戏里和你说话。\n'
    '- 玩家问某段逻辑哪里有问题时，先调用 mlog_check 检查，再根据结果回答；不要凭空猜。\n'
    '- 需要方块、物品、单位、状态、传感器、指令、模板、教程条目这类游戏数据时，'
    '调用 game_lookup，不要编造名字。\n'
    '- 玩家想要一份能直接用的代码时，可以先用 mlog_template 起个头，再按需求改。\n'
    '- 回答用简体中文，简短直接；代码放在三反引号里。不要长篇大论。\n'
    '- 玩家提到「工作区」时，用 ws_list / ws_get / ws_search 去看他存在工作区里的东西；'
    '那是模组每轮发过来的只读快照，不要编工作区里有什么。\n'
    '- 工作区里的代码要改，就在回答里写清「哪个工作区、哪台处理器（坐标）、改成什么」，'
    '让玩家自己贴进去；你没有写入权限，不要说「我已经改好了」。\n'
    '- 蓝图（bp_add）和控制台代码（console_run）只是「登记意图」：登记完还要玩家在窗口里点'
    '确认才会真正写进游戏或执行，别说「已经写好了」「已经执行了」。\n'
    '- console_run 登记的是游戏内 F8 打开的那个 JavaScript 控制台（Rhino）里要执行的 JS 代码，'
    '不是服务器聊天命令，不要写 /xxx 这种。\n'
    '- 要用控制台之前先调 console_help，看这台游戏里预置了哪些变量、有哪些 api.* 函数；'
    '不要编不存在的函数或字段。\n'
    '- 控制台代码一段一次，可以多行；不要写死循环、删文件这类破坏性代码。\n'
    '- 玩家和服务器数据（player_info / server_info）是模组这一轮发来的只读快照；'
    '没有就直说没看到，不要编位置、血量、人数。\n'
    '- 只调用上面列出的工具，不要假装还有别的工具。'
)

TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'mlog_check',
            'description': '对一段 mlog 代码做静态检查，返回语句数、ERR/WARN 数和逐条问题',
            'parameters': {
                'type': 'object',
                'properties': {
                    'code': {'type': 'string', 'description': '要检查的 mlog 代码，一行一条语句'},
                },
                'required': ['code'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'mlog_value',
            'description': '解析一段 mlog 并做抽象求值，返回每个变量的可能取值',
            'parameters': {
                'type': 'object',
                'properties': {
                    'program': {'type': 'string', 'description': 'mlog 代码文本'},
                },
                'required': ['program'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'mlog_pack',
            'description': '把 mlog 代码打包成可粘贴的游戏蓝图字符串，并给出压缩统计',
            'parameters': {
                'type': 'object',
                'properties': {
                    'program': {'type': 'string', 'description': 'mlog 代码文本'},
                },
                'required': ['program'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'game_lookup',
            'description': '查询游戏数据：方块、物品、单位、状态、传感器、指令、模板、教程条目。'
                           'name 为空时列出该类名字，给了 name 时返回该条详情',
            'parameters': {
                'type': 'object',
                'properties': {
                    'kind': {
                        'type': 'string',
                        'enum': ['block', 'item', 'unit', 'status', 'sensor',
                                 'instr', 'op', 'template', 'cmd'],
                        'description': '数据类别',
                    },
                    'name': {'type': 'string', 'description': '具体名字，可省略'},
                },
                'required': ['kind'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'mlog_template',
            'description': '用一个内置模板生成 mlog 代码（patrol/item-threshold/radar-fire/'
                           'memory-table/timer-flush/counter）',
            'parameters': {
                'type': 'object',
                'properties': {
                    'template': {'type': 'string', 'description': '模板 id'},
                    'params': {'type': 'object', 'description': '模板参数'},
                },
                'required': ['template'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'ws_list',
            'description': '列出玩家当前工作区（id、名字、状态、地图、处理器数、备注开头）；'
                           '不确定用哪个工作区时先调这个',
            'parameters': {
                'type': 'object',
                'properties': {},
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'ws_get',
            'description': '取一个工作区的全部处理器与它们的代码（可以给 id、名字或名字的一部分）',
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string', 'description': '工作区 id 或名字；省略时取第一个'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'ws_search',
            'description': '在工作区的名字、备注和处理器代码里搜一段文字，返回命中的行',
            'parameters': {
                'type': 'object',
                'properties': {
                    'q': {'type': 'string', 'description': '要找的文字'},
                },
                'required': ['q'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'player_info',
            'description': '看玩家这一轮发过来的只读快照（名字、位置、血量、队伍、手里/脚下的东西等）',
            'parameters': {
                'type': 'object',
                'properties': {},
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'server_info',
            'description': '看服务器这一轮发过来的只读快照（地图、波次、人数、tps、规则等）',
            'parameters': {
                'type': 'object',
                'properties': {},
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'bp_add',
            'description': '登记一条蓝图，等玩家确认后写进游戏蓝图库；不给写入权限，只登记意图',
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string', 'description': '蓝图名字'},
                    'code': {'type': 'string',
                             'description': '蓝图 base64 串（最多 20000 字符），超了会被截断'},
                    'desc': {'type': 'string', 'description': '备注，可省略'},
                },
                'required': ['name', 'code'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'console_run',
            'description': '登记一段要在游戏内 F8 控制台（JavaScript 控制台）里执行的代码，'
                           '等玩家确认后才执行',
            'parameters': {
                'type': 'object',
                'properties': {
                    'command': {'type': 'string',
                                'description': '控制台 JS 代码，可多行，最多 2000 字符'},
                },
                'required': ['command'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'console_help',
            'description': '查看这台游戏里 F8 控制台能用的东西（注入变量、常用 Vars/Core '
                           '入口、本模组注册的 api.* 函数）',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
]


class NotFound(Exception):
    """A chat id that is not stored (the HTTP layer turns this into 404)."""


class _MissingData(Exception):
    def __init__(self, filename):
        Exception.__init__(self, filename)
        self.filename = filename


class _HTTPFailure(Exception):
    def __init__(self, code, body):
        Exception.__init__(self, 'HTTP %d' % code)
        self.code = code
        self.body = body or ''


class _ModelError(Exception):
    """A failure already phrased for the player."""


_DATA = {}
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_DEFAULT = urllib.request.build_opener()


def _data(name):
    """Load one bundled JSON table once; None means "missing or broken"."""
    if name in _DATA:
        return _DATA[name]
    try:
        with open(os.path.join(DATA_DIR, name), encoding='utf-8') as fh:
            obj = json.load(fh)
    except Exception:
        obj = None
    _DATA[name] = obj
    return obj


def _json(name):
    obj = _data(name)
    if obj is None:
        raise _MissingData(name)
    return obj


def _opener(url):
    """Loopback must bypass the machine-wide proxy; a real endpoint must not."""
    host = (urllib.parse.urlparse(url).hostname or '').lower()
    if host == 'localhost' or host == '::1' or host.startswith('127.'):
        return _NO_PROXY
    return _DEFAULT


def _now():
    return int(time.time() * 1000)


def _title(text):
    one = ' '.join(str(text or '').split())
    return one[:20] if one else '新对话'


def _valid_id(cid):
    return bool(cid) and len(cid) <= 64 and all(
        ch.isalnum() or ch in '_-' for ch in cid)


def default_dir():
    return os.environ.get('MLOG_CHATS_DIR') or os.path.join(jobqueue.default_dir(),
                                                            'chats')


class Store:
    """One JSON file per chat; every write is tmp + os.replace (see jobqueue.Store)."""

    def __init__(self, path=None):
        self.path = path or default_dir()
        os.makedirs(self.path, exist_ok=True)
        self.lock = threading.RLock()

    def file(self, cid):
        return os.path.join(self.path, cid + '.json')

    def save(self, chat):
        body = json.dumps(chat, ensure_ascii=False, indent=1, sort_keys=True)
        dest = self.file(chat['id'])
        tmp = '%s.%d.%d.tmp' % (dest, os.getpid(), threading.get_ident())
        last = None
        with self.lock:
            for n in range(SAVE_TRIES):
                try:
                    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
                        fh.write(body)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, dest)
                    return dest
                except OSError as exc:
                    if not _contended(exc):
                        raise
                    last = exc
                    time.sleep(SAVE_WAIT * (n + 1))
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise last

    def load(self, cid):
        path = self.file(cid)
        if not os.path.isfile(path):
            return None
        with self.lock:
            with open(path, encoding='utf-8') as fh:
                chat = json.load(fh)
        if not isinstance(chat, dict):
            return None
        # a sidecar that died mid-run left 'running' on disk; nobody owns it now
        if chat.get('state') == 'running' and chat.get('id') not in _RUNNING:
            chat['state'] = 'idle'
        return chat

    def ids(self):
        return sorted(n[:-5] for n in os.listdir(self.path) if n.endswith('.json'))

    def all(self):
        out = []
        for cid in self.ids():
            try:
                chat = self.load(cid)
            except Exception:
                continue
            if isinstance(chat, dict) and chat.get('id'):
                out.append(chat)
        return out

    def delete(self, cid):
        path = self.file(cid)
        if not os.path.isfile(path):
            return False
        os.remove(path)
        return True


def _contended(exc):
    return isinstance(exc, PermissionError) or getattr(exc, 'winerror', None) in _WIN_CONTENDED


_STORE = None
_STORE_LOCK = threading.RLock()
_RUNNING = set()
_RUN_LOCK = threading.RLock()
_GONE = set()

# v0.22.0：工作区快照跟着「这一轮的对话」走 —— 每段对话在自己的线程里跑，用 thread-local 传进去，
# 不动 _run_tool 的签名（那边是一堆按名字分派的纯函数，加参数会把所有工具都牵连一遍）。
_CTX = threading.local()


def store(path=None):
    """The process-wide store; main() may point it at --chats-dir once."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None or path is not None:
            _STORE = Store(path)
        return _STORE


def running():
    with _RUN_LOCK:
        return len(_RUNNING)


def _logicd():
    """The sidecar runs logicd.py as __main__, so a plain `import logicd` would load
    a second copy with its own LLM_STATE and TEMPLATES. Prefer the loaded module."""
    import sys
    mod = sys.modules.get('logicd')
    if mod is not None and hasattr(mod, 'LLM_STATE'):
        return mod
    main = sys.modules.get('__main__')
    if main is not None and hasattr(main, 'LLM_STATE') and hasattr(main, 'TEMPLATES'):
        return main
    import logicd
    return logicd


# ---------------------------------------------------------------- HTTP views

def _brief(chat):
    msgs = chat.get('messages') or []
    if not isinstance(msgs, list):
        msgs = []
    return {
        'id': chat.get('id'),
        'title': chat.get('title') or '新对话',
        'state': 'running' if chat.get('state') == 'running' else 'idle',
        'created': chat.get('created') or 0,
        'updated': chat.get('updated') or 0,
        # the documented brief uses the count under 'messages'; msgs is the short alias
        'messages': len(msgs),
        'msgs': len(msgs),
        'turns': sum(1 for m in msgs if m.get('role') == 'user'),
        'tools': sum(1 for m in msgs if m.get('role') == 'tool'),
        'model': chat.get('model') or '',
        'tools_supported': chat.get('tools_supported'),
        'error': chat.get('error') or '',
        'ws': len(chat.get('workspaces') or []),
        # v0.23.0：列表只看条数；快照有没有用布尔表示，别把整份玩家数据塞进列表窗口
        'intents': len(chat.get('intents') or []),
        'has_player': bool(chat.get('player')),
        'has_server': bool(chat.get('server')),
        'has_console': bool(chat.get('console')),
    }


def _full(chat):
    msgs = chat.get('messages') or []
    if not isinstance(msgs, list):
        msgs = []
    last = chat.get('last')
    reply = ''
    if isinstance(last, dict) and isinstance(last.get('reply'), str):
        reply = last['reply']
    else:
        for m in reversed(msgs):
            if m.get('role') == 'assistant' and isinstance(m.get('content'), str):
                reply = m['content']
                break
    tools = []
    rounds = 0
    if isinstance(last, dict):
        tools = last.get('tools') or []
        rounds = last.get('rounds') or 0
    return {
        'id': chat.get('id'),
        'title': chat.get('title') or '新对话',
        'created': chat.get('created') or 0,
        'updated': chat.get('updated') or 0,
        'state': 'running' if chat.get('state') == 'running' else 'idle',
        'error': chat.get('error') or '',
        'model': chat.get('model') or '',
        'tools_supported': chat.get('tools_supported'),
        'messages': msgs,
        'last': last if isinstance(last, dict) else None,
        'reply': reply,
        'tools': tools,
        'rounds': rounds,
        'ws': len(chat.get('workspaces') or []),
        'ws_at': chat.get('ws_at') or 0,
        'ws_map': chat.get('ws_map') or '',
        'intents': chat.get('intents') or [],
        'player': chat.get('player') or {},
        'server': chat.get('server') or {},
        'console': chat.get('console') or {},
    }


def list_chats(limit=50, want_all=False):
    chats = store().all()
    chats.sort(key=lambda c: c.get('updated') or 0, reverse=True)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    if limit < 1:
        limit = 50
    if want_all:
        limit = min(max(limit, 200), 200)
    limit = min(limit, 200)
    chats = chats[:limit]
    return {'ok': True, 'count': len(chats), 'chats': [_brief(c) for c in chats]}


def get_chat(cid):
    if not cid:
        raise ValueError('GET /chat 需要 ?id=')
    cid = str(cid).strip()
    if not _valid_id(cid):
        raise NotFound('没有这个对话：%s' % cid)
    chat = store().load(cid)
    if chat is None:
        raise NotFound('没有这个对话：%s' % cid)
    return {'ok': True, 'chat': _full(chat)}


def delete_chat(cid):
    if not cid:
        raise ValueError('DELETE /chat 需要 ?id=')
    cid = str(cid).strip()
    if not _valid_id(cid):
        raise NotFound('没有这个对话：%s' % cid)
    s = store()
    chat = s.load(cid)
    if chat is None:
        raise NotFound('没有这个对话：%s' % cid)
    _GONE.add(cid)
    try:
        s.delete(cid)
    finally:
        with _RUN_LOCK:
            _RUNNING.discard(cid)
    return {'ok': True, 'id': cid, 'deleted': True}


def start(req):
    """Append one user line, mark the chat running, and return before the model."""
    LD = _logicd()

    state = LD.LLM_STATE
    if state.get('kind') != 'llm' or not str(state.get('url') or '').strip():
        raise ValueError('模型还没配置：先在设置里填地址与模型')

    text = req.get('text')
    if not isinstance(text, str):
        raise ValueError('text 必须是一段文字')
    text = text.strip()
    if not text:
        raise ValueError('这句话是空的，先说点什么再发给我')
    if len(text) > MAX_TEXT:
        raise ValueError('这句话太长了（最多 %d 字符）' % MAX_TEXT)

    raw_id = req.get('id')
    if raw_id is not None and not isinstance(raw_id, str):
        raise ValueError('id 必须是字符串')
    cid = (raw_id or '').strip() or None

    s = store()
    with _RUN_LOCK:
        if cid and cid in _RUNNING:
            raise ValueError('busy')
        now = _now()
        if cid:
            if not _valid_id(cid):
                raise ValueError('没有这个对话：%s' % cid)
            chat = s.load(cid)
            if chat is None:
                raise ValueError('没有这个对话：%s' % cid)
        else:
            cid = uuid.uuid4().hex[:8]
            while s.load(cid) is not None:
                cid = uuid.uuid4().hex[:8]
            chat = {'id': cid, 'title': '新对话', 'created': now, 'updated': now,
                    'state': 'idle', 'error': '', 'messages': [],
                    'tools_supported': None, 'model': '', 'last': None}
        had_user = any(m.get('role') == 'user'
                       for m in chat.get('messages') or [])
        if not had_user:
            chat['title'] = _title(text)
        chat.setdefault('messages', [])
        chat['messages'].append({'role': 'user', 'content': text})
        chat['state'] = 'running'
        chat['error'] = ''
        chat['model'] = str(state.get('model') or '')
        # v0.22.0：模组每轮把当前工作区快照一并送过来；只读，落盘前先夹体积。
        if 'workspaces' in req:
            chat['workspaces'] = _ws_norm(req.get('workspaces'))
            chat['ws_at'] = now
            chat['ws_map'] = _cut(req.get('map') or '', 120)
        # v0.23.0：玩家/服务器快照同样只读，跟着这一轮走（这轮没发就沿用上一份，跟工作区一样），
        # 意图队列每轮清零 —— 玩家确认过的上一轮不该再被重放一遍。
        if 'player' in req:
            chat['player'] = _norm_snapshot(req.get('player'))
        if 'server' in req:
            chat['server'] = _norm_snapshot(req.get('server'))
        # v0.23.1：本局 F8 控制台能用的对象清单（{version, vars, api}）也是只读快照
        if 'console' in req:
            chat['console'] = _norm_console(req.get('console'))
        chat['intents'] = []
        chat['updated'] = now
        _RUNNING.add(cid)
        try:
            s.save(chat)
        except Exception:
            _RUNNING.discard(cid)
            raise
    th = threading.Thread(target=_run, args=(cid,), name='chat-%s' % cid)
    th.daemon = True
    th.start()
    return {'ok': True, 'id': cid, 'title': chat['title'], 'state': 'running',
            'created': chat['created'], 'updated': chat['updated'],
            'messages': len(chat['messages']), 'msgs': len(chat['messages'])}


# ---------------------------------------------------------------- background run

def _run(cid):
    s = store()
    chat = None
    try:
        chat = s.load(cid)
        if chat is None:
            return
        _run_loop(chat)
    except _ModelError as exc:
        if chat is not None:
            chat['error'] = str(exc)
    except Exception as exc:
        if chat is not None:
            chat['error'] = '出错了：%s: %s' % (type(exc).__name__, exc)
    finally:
        if chat is not None:
            chat['state'] = 'idle'
            chat['updated'] = _now()
            if cid not in _GONE:
                try:
                    s.save(chat)
                except Exception:
                    pass
        with _RUN_LOCK:
            _RUNNING.discard(cid)
        _GONE.discard(cid)


def _save(chat):
    if chat.get('id') not in _GONE:
        store().save(chat)


def _one_line(text):
    return ' '.join(str(text or '').split())


def _clip(text):
    text = str(text or '')
    if len(text) > MAX_TOOL_OUTPUT:
        return text[:MAX_TOOL_OUTPUT] + '…（已截断）'
    return text


def _cut(text, limit):
    """Clip to `limit` characters with a visible tail; never raises on junk input."""
    text = str(text if text is not None else '')
    if limit <= 0:
        return ''
    if len(text) <= limit:
        return text
    return text[:limit] + '…（截断）'


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ws_norm(raw):
    """Normalize the workspace snapshot the mod posts with each turn (v0.22.0).

    The mod owns the real files; this is a read-only copy so the model can answer
    "我第三个工作区那段代码有什么问题" without the player pasting anything. Every
    field is optional and everything is clipped: a hostile or broken payload must
    not be able to blow up the chat file.
    """
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        wid = _cut(str(item.get('id') or '').strip(), 40)
        if not wid:
            continue
        procs = []
        rows = item.get('procs')
        if isinstance(rows, list):
            for row in rows[:WS_PROCS_MAX]:
                if not isinstance(row, dict):
                    continue
                code = row.get('code')
                procs.append({'x': _int(row.get('x')),
                              'y': _int(row.get('y')),
                              'block': _cut(row.get('block') or '', 60),
                              'desc': _cut(row.get('desc') or '', 200),
                              'code': _cut(code if isinstance(code, str) else '', WS_CODE_MAX)})
        jobs = []
        jl = item.get('jobs')
        if isinstance(jl, list):
            for job in jl[:12]:
                if not isinstance(job, dict):
                    continue
                jobs.append({'id': _cut(job.get('id') or '', 40),
                             'state': _cut(job.get('state') or '', 20)})
        out.append({'id': wid,
                    'name': _cut(item.get('name') or '', 120),
                    'state': 'archived' if str(item.get('state') or '') == 'archived' else 'active',
                    'map': _cut(item.get('map') or '', 120),
                    'notes': _cut(item.get('notes') or '', WS_NOTE_MAX),
                    'updated': _int(item.get('updated')),
                    'procs': procs, 'jobs': jobs})
        if len(out) >= WS_MAX:
            break
    return out


def _norm_snapshot(raw):
    """Clip one player/server snapshot the mod posted with this turn (v0.23.0).

    The mod owns the real objects and decides the shape; the sidecar only ever
    renders this copy, so nothing in it is trusted: unknown value kinds become
    text, strings and lists are cut, and the depth is bounded.  A broken or
    hostile payload must not be able to blow up the chat file.
    """
    if not isinstance(raw, dict):
        return {}
    return _snap_value(raw, 0)


def _snap_value(value, depth):
    if isinstance(value, dict):
        if depth >= SNAP_DEPTH:
            return _cut(json.dumps(value, ensure_ascii=False, sort_keys=True), SNAP_TEXT_MAX)
        out = {}
        for i, (key, val) in enumerate(value.items()):
            if i >= SNAP_KEYS_MAX:
                break
            out[_cut(str(key), 40)] = _snap_value(val, depth + 1)
        return out
    if isinstance(value, list):
        if depth >= SNAP_DEPTH:
            return _cut(json.dumps(value, ensure_ascii=False), SNAP_TEXT_MAX)
        return [_snap_value(item, depth + 1) for item in value[:SNAP_LIST_MAX]]
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if value is None:
        return None
    return _cut(value, SNAP_TEXT_MAX)


def _run_loop(chat):
    # v0.22.0：把这一轮的工作区快照挂到本线程，工具实现自己来取。
    _CTX.ws = [w for w in (chat.get('workspaces') or []) if isinstance(w, dict)]
    _CTX.map = str(chat.get('ws_map') or '')
    # v0.23.0：快照只读、意图队列可写；工具直接往 chat['intents'] 这个列表里追加，
    # 轮末 _save 落盘，模组轮询 GET /chat 时就能看到要玩家确认的东西。
    _CTX.player = chat['player'] if isinstance(chat.get('player'), dict) else {}
    _CTX.server = chat['server'] if isinstance(chat.get('server'), dict) else {}
    _CTX.console = chat['console'] if isinstance(chat.get('console'), dict) else {}
    if not isinstance(chat.get('intents'), list):
        chat['intents'] = []
    _CTX.intents = chat['intents']
    msgs = [{'role': 'system', 'content': SYSTEM_PROMPT}]
    for m in chat.get('messages') or []:
        msgs.append(dict(m))
    text = ''
    for m in reversed(chat.get('messages') or []):
        if m.get('role') == 'user':
            text = str(m.get('content') or '')
            break

    tools_log = []
    rounds = 0
    final = None
    deadline = time.time() + OVERALL_TIMEOUT
    while rounds < MAX_ROUNDS:
        rounds += 1
        msg = _call_model(chat, msgs, rounds == 1, deadline)
        calls = msg.get('tool_calls') or []
        content = msg.get('content') or ''
        if calls:
            assistant = {'role': 'assistant', 'content': content, 'tool_calls': calls}
            chat['messages'].append(assistant)
            msgs.append(assistant)
            for call in calls:
                out = _execute(call)
                body = _clip(out['text'])
                tm = {'role': 'tool', 'tool_call_id': call['id'],
                      'name': call['function']['name'], 'content': body}
                chat['messages'].append(tm)
                msgs.append(tm)
                tools_log.append({'name': out['name'],
                                  'args': out['args'][:TOOL_ARGS_SUMMARY],
                                  'ok': out['ok'], 'ms': out['ms'],
                                  'summary': _one_line(body)[:TOOL_SUMMARY]})
            chat['last'] = {'text': text, 'reply': '', 'tools': tools_log,
                            'rounds': rounds,
                            'tools_supported': chat.get('tools_supported'),
                            'at': _now()}
            chat['updated'] = _now()
            _save(chat)
            continue
        if content:
            chat['messages'].append({'role': 'assistant', 'content': content})
            final = content
            break
        raise _ModelError('调用模型失败：模型返回了空回复（检查一下模型设置）')

    if final is None:
        final = '（工具调用到了上限，先说这些。可以让我接着做。）'
        chat['messages'].append({'role': 'assistant', 'content': final})
    chat['last'] = {'text': text, 'reply': final, 'tools': tools_log,
                    'rounds': rounds,
                    'tools_supported': chat.get('tools_supported'),
                    'at': _now()}
    chat['error'] = ''
    chat['updated'] = _now()
    _save(chat)


def _http_message(exc):
    body = ' '.join(str(exc.body or '').split())[:200]
    return '调用模型失败：HTTP %d%s' % (exc.code, (' ' + body) if body else '')


def _call_model(chat, msgs, allow_degrade, deadline):
    if chat.get('tools_supported') is False:
        # this endpoint already refused tools once; do not make every later turn
        # pay that 400 and retry again
        try:
            doc = _post_chat(msgs, False, deadline)
        except _HTTPFailure as exc:
            raise _ModelError(_http_message(exc))
        return _parse_message(doc)
    try:
        doc = _post_chat(msgs, True, deadline)
    except _HTTPFailure as exc:
        if allow_degrade and exc.code in (400, 404, 422):
            try:
                doc = _post_chat(msgs, False, deadline)
            except _HTTPFailure as exc2:
                raise _ModelError(_http_message(exc2))
            chat['tools_supported'] = False
            return _parse_message(doc)
        raise _ModelError(_http_message(exc))
    chat['tools_supported'] = True
    return _parse_message(doc)


def _post_chat(msgs, tools_on, deadline):
    LD = _logicd()

    state = LD.LLM_STATE
    url = LD.completion_url(str(state.get('url') or ''))
    body = {'messages': msgs,
            'temperature': 0.3,
            'max_tokens': int(state.get('max_tokens') or 4096)}
    if state.get('model'):
        body['model'] = state['model']
    if tools_on:
        body['tools'] = TOOLS
        body['tool_choice'] = 'auto'
    left = deadline - time.time()
    if left <= 1:
        raise _ModelError('调用模型失败：整体超时（%d 秒），先停一下' % int(OVERALL_TIMEOUT))
    timeout = max(5.0, min(float(state.get('timeout') or 60), left))
    headers = {'Content-Type': 'application/json'}
    key = state.get('key')
    if key:
        headers['Authorization'] = 'Bearer %s' % key
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                 headers=headers)
    try:
        with _opener(url).open(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        try:
            snippet = exc.read().decode('utf-8', 'replace')
        except Exception:
            snippet = ''
        raise _HTTPFailure(exc.code, snippet)
    except Exception as exc:
        raise _ModelError('调用模型失败：连不上模型端点（%s: %s）'
                          % (type(exc).__name__, exc))
    try:
        return json.loads(raw.decode('utf-8') or '{}')
    except Exception:
        raise _ModelError('调用模型失败：端点返回的不是 JSON')


def _parse_message(doc):
    try:
        message = doc['choices'][0]['message']
    except (KeyError, IndexError, TypeError):
        raise _ModelError('调用模型失败：返回里没有 choices[0].message')
    if not isinstance(message, dict):
        raise _ModelError('调用模型失败：choices[0].message 不是对象')
    content = message.get('content')
    if not isinstance(content, str):
        content = ''
    calls = []
    raw_calls = message.get('tool_calls')
    if isinstance(raw_calls, list):
        for i, call in enumerate(raw_calls):
            if not isinstance(call, dict):
                continue
            fn = call.get('function') if isinstance(call.get('function'), dict) else {}
            name = str(fn.get('name') or '')
            args = fn.get('arguments')
            if not isinstance(args, str):
                try:
                    args = json.dumps(args if args is not None else {}, ensure_ascii=False)
                except Exception:
                    args = '{}'
            calls.append({'id': str(call.get('id') or 'call_%d' % i),
                          'type': 'function',
                          'function': {'name': name, 'arguments': args}})
    return {'content': content, 'tool_calls': calls}


# ---------------------------------------------------------------- tool execution

def _execute(call):
    fn = call.get('function') or {}
    name = str(fn.get('name') or '')
    raw = fn.get('arguments')
    if not isinstance(raw, str):
        raw = '{}'
    started = time.time()
    ok = False
    text = ''
    try:
        args = json.loads(raw or '{}')
    except Exception:
        args = None
    if not isinstance(args, dict):
        ok, text = False, '工具参数不是合法的 JSON 对象：%s' % raw[:200]
    else:
        try:
            ok, text = _run_tool(name, args)
        except _MissingData as exc:
            ok, text = False, '数据文件缺失：%s' % exc.filename
        except Exception as exc:
            ok, text = False, '工具执行失败：%s: %s' % (type(exc).__name__, exc)
    ms = int((time.time() - started) * 1000)
    return {'name': name, 'args': raw, 'ok': bool(ok), 'ms': ms,
            'text': str(text or '')}


def _str_arg(args, key):
    value = args.get(key)
    if value is None:
        return ''
    if not isinstance(value, str):
        return str(value)
    return value


def _run_tool(name, args):
    if name == 'mlog_check':
        code = _str_arg(args, 'code')
        if not code:
            return False, 'code 不能为空'
        LD = _logicd()
        res = LD.analyse(code, None, ipt=2)
        lines = ['语句数：%d' % res.get('statements', 0),
                 'ERR：%d  WARN：%d' % (res.get('errors', 0), res.get('warns', 0))]
        for f in res.get('findings') or []:
            lines.append('line %s %s %s: %s' % (f.get('line'), f.get('sev'),
                                                f.get('rid') or '-', f.get('msg')))
        return (res.get('errors', 0) == 0), '\n'.join(lines)
    if name == 'mlog_value':
        program = _str_arg(args, 'program')
        if not program:
            return False, 'program 不能为空'
        import mlogir
        import mlogval
        try:
            prog = mlogir.parse(program)
        except Exception as exc:
            return False, '解析失败：%s' % exc
        return True, mlogval.format_report('program', prog, mlogval.analyze(prog))
    if name == 'mlog_pack':
        program = _str_arg(args, 'program')
        if not program:
            return False, 'program 不能为空'
        import mlogir
        import mlogpack
        try:
            prog = mlogir.parse(program)
        except Exception as exc:
            return False, '解析失败：%s' % exc
        return True, mlogpack.format_report(prog)
    if name == 'game_lookup':
        return _game_lookup(args)
    if name == 'mlog_template':
        return _mlog_template(args)
    if name == 'ws_list':
        return _ws_list(args)
    if name == 'ws_get':
        return _ws_get(args)
    if name == 'ws_search':
        return _ws_search(args)
    if name == 'player_info':
        return _player_info(args)
    if name == 'server_info':
        return _server_info(args)
    if name == 'bp_add':
        return _bp_add(args)
    if name == 'console_run':
        return _console_run(args)
    if name == 'console_help':
        return _console_help(args)
    return False, '没有这个工具：%s' % (name or '（空）')


def _mlog_template(args):
    tid = _str_arg(args, 'template').strip()
    params = args.get('params')
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return False, 'params 必须是对象'
    LD = _logicd()
    tmpl = LD.TEMPLATES.get(tid)
    if tmpl is None:
        return False, '没有 %s 这个模板；可用：%s' % (tid, ', '.join(sorted(LD.TEMPLATES)))
    try:
        lines, refs = tmpl.build(params)
    except ValueError as exc:
        return False, '参数不对：%s' % exc
    code = '\n'.join(lines)
    if not refs:
        refs = ['container1', 'message1']
    note = '\n\n（这里的 %s 等是占位链接名，按你的处理器实际链接改名）' % '/'.join(refs)
    return True, code + note


# ---------------------------------------------------------------- workspace tools (v0.22.0)

def _ws_ctx():
    """The snapshot the mod attached to this turn (empty when it sent none)."""
    ws = getattr(_CTX, 'ws', None)
    if not isinstance(ws, list):
        return []
    return [w for w in ws if isinstance(w, dict)]


def _ws_state_cn(state):
    return '已归档' if str(state or '') == 'archived' else '进行中'


def _ws_line(ws):
    procs = [r for r in (ws.get('procs') or []) if isinstance(r, dict)]
    notes = _one_line(ws.get('notes') or '')
    if len(notes) > 60:
        notes = notes[:60] + '…'
    bits = ['「%s」' % (ws.get('name') or '?'),
            'id=%s' % (ws.get('id') or '?'),
            _ws_state_cn(ws.get('state')),
            '%d 台' % len(procs)]
    if ws.get('map'):
        bits.append('地图 %s' % ws['map'])
    line = ' · '.join(bits)
    if notes:
        line += ' · 备注：' + notes
    return line


def _ws_list(args):
    ws = _ws_ctx()
    if not ws:
        return False, ('玩家这一轮没有提供工作区快照（可能是他一个工作区都没有，'
                       '或者这次是旧版模组）。不要编工作区，直接说没看到。')
    lines = ['共 %d 个工作区：' % len(ws)]
    for i, one in enumerate(ws):
        lines.append('%d. %s' % (i + 1, _ws_line(one)))
    return True, _clip('\n'.join(lines))


def _ws_find(ws, key):
    """id 精确 -> 名字精确 -> id/名字包含；都没命中返回 None。"""
    key = str(key or '').strip()
    if not key:
        return ws[0] if ws else None
    low = key.lower()
    for one in ws:
        if str(one.get('id') or '').lower() == low:
            return one
    for one in ws:
        if str(one.get('name') or '').strip().lower() == low:
            return one
    for one in ws:
        if low in str(one.get('name') or '').lower() or low in str(one.get('id') or '').lower():
            return one
    return None


def _ws_get(args):
    ws = _ws_ctx()
    if not ws:
        return False, '玩家这一轮没有提供工作区快照，看不到工作区里的代码'
    key = _str_arg(args, 'id').strip()
    one = _ws_find(ws, key)
    if one is None:
        names = '、'.join('「%s」' % (w.get('name') or w.get('id') or '?') for w in ws)
        return False, '没有这个工作区：%s；现在有：%s' % (key, names)
    procs = [r for r in (one.get('procs') or []) if isinstance(r, dict)]
    jobs = [j for j in (one.get('jobs') or []) if isinstance(j, dict)]
    out = ['工作区 %s' % _ws_line(one)]
    notes = str(one.get('notes') or '').strip()
    if notes:
        out.append('备注：' + notes)
    if jobs:
        out.append('作业：' + '、'.join('#%s %s' % (j.get('id') or '?', j.get('state') or '?')
                                       for j in jobs[-5:]))
    if not procs:
        out.append('这个工作区里没有处理器。')
        return True, _clip('\n'.join(out))
    out.append('处理器 %d 台：' % len(procs))
    for i, row in enumerate(procs):
        head = '%d) @%s,%s %s' % (i + 1, row.get('x', 0), row.get('y', 0),
                                  row.get('block') or '处理器')
        if row.get('desc'):
            head += ' —— ' + str(row['desc'])[:80]
        out.append(head)
        code = str(row.get('code') or '')
        if not code.strip():
            out.append('   （这台还没写过代码）')
            continue
        rows = code.split('\n')
        out.append('   代码（%d 行）：' % len(rows))
        for line in rows:
            out.append('   ' + line)
    return True, _clip('\n'.join(out))


def _ws_search(args):
    ws = _ws_ctx()
    if not ws:
        return False, '玩家这一轮没有提供工作区快照，搜不了工作区'
    q = _str_arg(args, 'q').strip()
    if not q:
        return False, 'q 不能为空'
    if len(q) > 200:
        return False, 'q 太长了（最多 200 字符）'
    low = q.lower()
    hits = []
    for one in ws:
        name = str(one.get('name') or '')
        if low in name.lower():
            hits.append('工作区「%s」的名字里命中' % (name or one.get('id') or '?'))
        notes = str(one.get('notes') or '')
        if low in notes.lower():
            hits.append('工作区「%s」的备注里命中：%s'
                        % (name or '?', _one_line(notes)[:120]))
        for row in (one.get('procs') or []):
            if not isinstance(row, dict):
                continue
            code = str(row.get('code') or '')
            if low not in code.lower():
                continue
            for n, line in enumerate(code.split('\n')):
                if low in line.lower():
                    hits.append('工作区「%s」@%s,%s 第 %d 行：%s'
                                % (name or '?', row.get('x', 0), row.get('y', 0),
                                   n + 1, line.strip()[:160]))
                    break
            if len(hits) >= 20:
                break
        if len(hits) >= 20:
            break
    if not hits:
        return False, '工作区里没有找到「%s」' % q
    return True, _clip('找到 %d 处：\n%s' % (len(hits), '\n'.join(hits)))


# ---------------------------------------------------------------- player / server + intents (v0.23.0)

def _snap_get(which):
    """The read-only snapshot the mod attached to this turn ({} when it sent none)."""
    snap = getattr(_CTX, which, None)
    return snap if isinstance(snap, dict) else {}


def _snap_lines(obj, prefix='  ', depth=0):
    """Render a snapshot as indented 键: 值 lines —— the model reads text, not JSON."""
    out = []
    if not isinstance(obj, dict):
        return out
    for key, value in obj.items():
        if len(out) >= SNAP_KEYS_MAX:
            out.append(prefix + '…（还有更多，已省略）')
            break
        label = prefix + str(key)
        if isinstance(value, dict):
            out.append(label + ':')
            if depth >= SNAP_DEPTH:
                out.append(label + '  …（太深，已省略）')
            else:
                out.extend(_snap_lines(value, prefix + '  ', depth + 1))
        elif isinstance(value, list):
            if not value:
                out.append(label + ': （空）')
                continue
            parts = []
            for item in value[:SNAP_LIST_MAX]:
                if isinstance(item, (dict, list)):
                    parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
                else:
                    parts.append(str(item))
            out.append(label + ': ' + '、'.join(parts))
        else:
            out.append('%s: %s' % (label, value))
    return out


def _snap_report(which, title):
    snap = _snap_get(which)
    if not snap:
        return False, ('玩家这一轮没有提供%s快照（可能是旧版模组，或者这不在一局游戏里）。'
                       '不要编里面的数据，直接说没看到。' % title)
    lines = ['%s（只读，模组这一轮发来的）：' % title]
    lines.extend(_snap_lines(snap))
    return True, _clip('\n'.join(lines))


def _player_info(args):
    return _snap_report('player', '玩家')


def _server_info(args):
    return _snap_report('server', '服务器')


def _intent_rows():
    """This turn's intent queue; start() gave the chat a fresh list."""
    rows = getattr(_CTX, 'intents', None)
    if not isinstance(rows, list):
        rows = []
        _CTX.intents = rows
    return rows


def _intent_refuse(what):
    return False, ('这一轮已经登记了 %d 条（每轮最多 %d 条），这条%s被丢掉了；'
                   '把剩下的写进回答里，让玩家下一轮再说。'
                   % (INTENTS_MAX, INTENTS_MAX, what))


def _bp_add(args):
    name = _str_arg(args, 'name').strip()
    if not name:
        return False, 'name 不能为空（蓝图得有名字，玩家才认得出）'
    code = _str_arg(args, 'code')
    if not code.strip():
        return False, 'code 不能为空（要的是蓝图 base64 串）'
    rows = _intent_rows()
    if len(rows) >= INTENTS_MAX:
        return _intent_refuse('蓝图')
    desc = _str_arg(args, 'desc').strip()
    lines = len(code.split('\n'))
    notes = []
    if len(name) > BP_NAME_MAX:
        name = name[:BP_NAME_MAX]
        notes.append('名字超过 %d 字符，已截断' % BP_NAME_MAX)
    if len(desc) > BP_DESC_MAX:
        desc = desc[:BP_DESC_MAX]
        notes.append('备注超过 %d 字符，已截断' % BP_DESC_MAX)
    if len(code) > BP_CODE_MAX:
        code = code[:BP_CODE_MAX]
        notes.append('代码超过 %d 字符，已截断' % BP_CODE_MAX)
    rows.append({'kind': 'bp', 'name': name, 'code': code, 'desc': desc})
    text = ('已登记：写入蓝图库「%s」（%d 行代码）。玩家确认后才会真正写进游戏。'
            % (name, lines))
    if notes:
        text += '\n（%s）' % '；'.join(notes)
    return True, text


def _norm_console(raw):
    """The mod's F8 console catalogue for this turn: {version, vars, api} (v0.23.1).

    Only names are kept.  The sidecar never executes anything: it needs just enough
    to tell the model what this game actually exposes, so a long or hostile payload
    cannot bloat the chat file either.
    """
    if not isinstance(raw, dict):
        return {}
    out = {}
    ver = raw.get('version')
    if isinstance(ver, str) and ver.strip():
        out['version'] = _cut(ver.strip(), 80)
    for key in ('vars', 'api'):
        items = raw.get(key)
        if isinstance(items, dict):
            items = list(items.keys())
        names = []
        for item in items if isinstance(items, list) else []:
            text = _cut(str(item).strip(), 80)
            if text and text not in names:
                names.append(text)
            if len(names) >= CONSOLE_API_MAX:
                break
        out[key] = names
    return out


def _console_run(args):
    code = _str_arg(args, 'command').strip()
    if not code:
        return False, 'command 不能为空（要的是 F8 控制台里执行的 JS 代码）'
    rows = _intent_rows()
    if len(rows) >= INTENTS_MAX:
        return _intent_refuse('控制台代码')
    notes = []
    if len(code) > CONSOLE_CODE_MAX:
        code = code[:CONSOLE_CODE_MAX]
        notes.append('代码超过 %d 字符，已截断' % CONSOLE_CODE_MAX)
    rows.append({'kind': 'console', 'cmd': code})
    shown = code if len(code) <= 300 else code[:300] + '…'
    text = '已登记：F8 控制台代码 %s。玩家确认后才会执行。' % shown
    if notes:
        text += '\n（%s）' % '；'.join(notes)
    return True, text


def _console_help(args):
    lines = [
        '游戏内的 F8 控制台是 JavaScript 控制台（Rhino），不是服务器聊天命令 —— 别写 /xxx。'
        '回车就执行，返回最后一个表达式的值；想拿结果直接写表达式。',
        '每次输入都会预置这些变量：%s（ConsoleFragment 注入，直接当全局变量用）。'
        % '、'.join(CONSOLE_INJECTED),
        '常用入口：Vars.state（游戏状态）、Vars.world（地图）、Vars.player（玩家单位）、'
        'Vars.schematics（蓝图）、Core.app、Groups.player、Time、Draw。',
    ]
    snap = _snap_get('console')
    api = snap.get('api') if isinstance(snap.get('api'), list) else []
    if api:
        ver = str(snap.get('version') or '').strip()
        head = '本局可用 api.* 函数（模组这一轮发来的清单'
        head += ('，%s）：' % ver) if ver else '）：'
        lines.append(head)
        lines.extend('  api.%s' % name for name in api[:CONSOLE_API_MAX])
        if len(api) > CONSOLE_API_MAX:
            lines.append('  …（还有 %d 个，已省略）' % (len(api) - CONSOLE_API_MAX))
    else:
        lines.append('本局没收到 api.* 清单（模组可能是旧版，或者这不在一局游戏里）：'
                     '不要凭印象调 api.xxx，只用手上这些 Vars / Core 入口。')
    injected = snap.get('vars') if isinstance(snap.get('vars'), list) else []
    if injected:
        lines.append('模组给的变量清单：%s' % '、'.join(injected[:CONSOLE_API_MAX]))
    lines.append('一段一次，不要写死循环、删文件这类破坏性代码；'
                 '登记后还要玩家确认才会执行，别说「已经执行了」。')
    return True, _clip('\n'.join(lines))


# ---------------------------------------------------------------- bundled data

def _lookup_names(kind):
    if kind == 'block':
        return sorted(_json('block_attrs.json').get('blocks') or {})
    if kind == 'item':
        items = _json('item_ids.json').get('items') or {}
        return [name for _, name in sorted(items.items(), key=lambda kv: int(kv[0]))]
    if kind == 'unit':
        return sorted(_json('unit_attrs.json').get('units') or {})
    if kind == 'status':
        return list(_json('status_names.json').get('status') or [])
    if kind == 'sensor':
        return sorted(_json('sensor_attrs.json').get('access') or {})
    if kind == 'instr':
        return sorted(_json('instr_semantics.json').get('instructions') or {})
    if kind == 'op':
        return sorted(_json('instr_semantics.json').get('ops') or {})
    if kind == 'template':
        LD = _logicd()
        return sorted(LD.TEMPLATES)
    if kind == 'cmd':
        return [e.get('title') or '' for e in _json('cmd_index.json')]
    return None


def _cmd_entries(name):
    entries = _json('cmd_index.json')
    low = name.lower()
    exact = [e for e in entries
             if str(e.get('num') or '').lower() == low
             or str(e.get('title') or '').lower() == low]
    if exact:
        return exact
    hits = [e for e in entries if low and low in str(e.get('title') or '').lower()]
    if hits:
        return hits
    titles = [str(e.get('title') or '') for e in entries]
    near = difflib.get_close_matches(name, titles, n=3, cutoff=0.35)
    return [e for e in entries if str(e.get('title') or '') in near]


def _details(kind, name):
    if kind == 'block':
        attrs = _json('block_attrs.json')
        sizes = _json('block_sizes.json')
        detail = {'name': name}
        blocks = attrs.get('blocks') or {}
        if name in blocks:
            detail.update(blocks[name])
        if name in sizes:
            detail['size'] = sizes[name]
        cls = (attrs.get('classes') or {}).get(name)
        if cls is not None:
            detail['class'] = cls
        return detail
    if kind == 'item':
        items = _json('item_ids.json').get('items') or {}
        for iid, nm in items.items():
            if nm == name:
                return {'name': name, 'id': iid}
        return {'name': name}
    if kind == 'unit':
        units = _json('unit_attrs.json').get('units') or {}
        detail = {'name': name}
        if name in units:
            detail.update(units[name])
        return detail
    if kind == 'status':
        names = _lookup_names('status')
        return {'name': name, 'index': names.index(name) if name in names else -1}
    if kind == 'sensor':
        return (_json('sensor_attrs.json').get('access') or {}).get(name) or {'name': name}
    if kind == 'instr':
        return ((_json('instr_semantics.json').get('instructions') or {}).get(name)
                or {'name': name})
    if kind == 'op':
        return (_json('instr_semantics.json').get('ops') or {}).get(name) or {'name': name}
    if kind == 'template':
        LD = _logicd()
        return LD.TEMPLATES[name].spec()
    if kind == 'cmd':
        return {'matches': _cmd_entries(name)}
    return {'name': name}


def _detail_text(obj):
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    if len(text) > DETAIL_MAX:
        return text[:DETAIL_MAX] + '…'
    return text


def _game_lookup(args):
    try:
        return _game_lookup_inner(args)
    except _MissingData as exc:
        return False, '数据文件缺失：%s' % exc.filename


def _game_lookup_inner(args):
    kind = _str_arg(args, 'kind').strip().lower()
    name = _str_arg(args, 'name').strip()
    names = _lookup_names(kind)
    if names is None:
        return False, 'kind 只能是 block / item / unit / status / sensor / instr / op / template / cmd'
    if not name:
        head = names[:40]
        return True, '%s 共 %d 个（先列前 %d 个）：\n%s' % (
            kind, len(names), len(head), ', '.join(head))
    low = name.lower()
    exact = [n for n in names if str(n).lower() == low]
    if exact:
        return True, _detail_text(_details(kind, exact[0]))
    if kind == 'cmd':
        hits = _cmd_entries(name)
        if hits:
            return True, _detail_text({'matches': hits[:3]})
    near = difflib.get_close_matches(name, names, n=3, cutoff=0.35)
    if not near:
        near = names[:3]
    return False, '没找到 %s（最近似的：%s）' % (name, ', '.join(str(n) for n in near))
