#!/usr/bin/env python3
"""
年龄验证 API 服务
=================
独立的年龄验证 API 服务，支持单个和批量验证

API 端点：
- POST /verify          单个验证
- POST /verify/batch    批量验证
- GET  /tasks/<task_id> 查询任务状态
- GET  /tasks           查询所有任务
- DELETE /tasks/<task_id> 取消任务

启动方式：
    python age_verification_api.py --port 5001
"""

import asyncio
import queue
import threading
import uuid
import re
import ipaddress
from datetime import datetime
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor
import os
from copy import deepcopy
from functools import wraps
from collections import deque
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from dotenv import load_dotenv

from services.age_verification_service import AgeVerificationService, MOBILE_DEVICE_CONFIGS


# ============================================================================
# Flask 应用配置
# ============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, 'frontend')
# 允许使用 .env 管理部署配置
load_dotenv(os.path.join(BASE_DIR, ".env"))
app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path='/static')
CORS(app)

import requests

# API 鉴权配置
API_KEY = os.environ.get("API_KEY", "")  # 默认 API Key，建议通过环境变量设置
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY") # Cloudflare Turnstile Secret Key
CLIENT_TOKEN_SECRET = os.environ.get("CLIENT_TOKEN_SECRET", os.urandom(32).hex())
CLIENT_TOKEN_TTL_SECONDS = max(60, int(os.environ.get("CLIENT_TOKEN_TTL_SECONDS", "900")))
CLIENT_TOKEN_HEADER = "X-Client-Token"
RATE_LIMIT_WINDOW_SECONDS = max(1, int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60")))
RATE_LIMIT_MAX_REQUESTS = max(1, int(os.environ.get("RATE_LIMIT_MAX_REQUESTS", "20")))
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
PUBLIC_PATH_PREFIXES = ("/verify", "/tasks", "/batches", "/stats")
USE_PROXY_HEADERS = os.environ.get("USE_PROXY_HEADERS", "false").strip().lower() in {"1", "true", "yes", "on"}
TRUSTED_PROXY_IPS = {
    ip.strip() for ip in os.environ.get("TRUSTED_PROXY_IPS", "").split(",") if ip.strip()
}
token_serializer = URLSafeTimedSerializer(CLIENT_TOKEN_SECRET, salt="age-verification-public-client")
rate_limit_lock = threading.Lock()
rate_limit_buckets: Dict[str, deque] = {}
rate_limit_last_seen: Dict[str, float] = {}
_rate_limit_gc_counter = 0


def _is_loopback_ip(ip: str) -> bool:
    if not ip:
        return False
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def _get_client_ip() -> str:
    """获取客户端真实 IP（默认不信任 X-Forwarded-For，除非显式开启并配置可信代理）。"""
    remote_ip = (request.remote_addr or "").strip()
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if USE_PROXY_HEADERS and forwarded_for and remote_ip in TRUSTED_PROXY_IPS:
        return forwarded_for.split(",")[0].strip()
    return remote_ip


def _is_local_request() -> bool:
    # 只基于 socket 对端地址判断本地，避免 Host/XFF 头伪造绕过。
    remote_ip = (request.remote_addr or "").strip()
    return _is_loopback_ip(remote_ip)


def _build_client_fingerprint(ip: str, user_agent: str) -> str:
    return f"{ip}|{(user_agent or '').strip()[:180]}"


def _issue_client_token() -> str:
    payload = {
        "fp": _build_client_fingerprint(_get_client_ip(), request.headers.get("User-Agent", "")),
        "path": "public-web",
    }
    return token_serializer.dumps(payload)


def _verify_client_token(token: str) -> bool:
    if not token:
        return False
    try:
        payload = token_serializer.loads(token, max_age=CLIENT_TOKEN_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return False
    expected_fp = _build_client_fingerprint(_get_client_ip(), request.headers.get("User-Agent", ""))
    return payload.get("fp") == expected_fp and payload.get("path") == "public-web"


def _check_rate_limit(bucket: str) -> bool:
    global _rate_limit_gc_counter
    now_ts = datetime.now().timestamp()
    with rate_limit_lock:
        _rate_limit_gc_counter += 1
        if _rate_limit_gc_counter % 200 == 0:
            stale_buckets = [
                b for b, ts in rate_limit_last_seen.items()
                if now_ts - ts > RATE_LIMIT_WINDOW_SECONDS * 2
            ]
            for b in stale_buckets:
                rate_limit_buckets.pop(b, None)
                rate_limit_last_seen.pop(b, None)

        queue_ref = rate_limit_buckets.get(bucket)
        if queue_ref is None:
            queue_ref = deque()
            rate_limit_buckets[bucket] = queue_ref
        while queue_ref and now_ts - queue_ref[0] > RATE_LIMIT_WINDOW_SECONDS:
            queue_ref.popleft()
        rate_limit_last_seen[bucket] = now_ts
        if len(queue_ref) >= RATE_LIMIT_MAX_REQUESTS:
            return False
        queue_ref.append(now_ts)
        return True

def verify_turnstile(token, ip=None):
    """验证 Turnstile Token"""
    if not TURNSTILE_SECRET_KEY:
        return True # 未配置则跳过验证

    if not token:
        return False

    try:
        url = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
        payload = {
            'secret': TURNSTILE_SECRET_KEY,
            'response': token,
            'remoteip': ip
        }
        res = requests.post(url, data=payload, timeout=5)
        outcome = res.json()
        return outcome.get('success', False)
    except Exception as e:
        print(f"Turnstile verification error: {e}")
        return False

def require_api_key(f):
    """API Key 鉴权装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # 本地访问默认放行（便于本机调试）
        if _is_local_request():
            return f(*args, **kwargs)

        # 允许使用 API Key 直接调用
        request_key = request.headers.get('X-API-Key') or request.args.get('api_key')
        if API_KEY and request_key and request_key == API_KEY:
            return f(*args, **kwargs)

        # 公网访问使用短期客户端令牌（用于前端用户）
        client_token = request.headers.get(CLIENT_TOKEN_HEADER) or request.args.get("client_token")
        if _verify_client_token(client_token):
            return f(*args, **kwargs)

        return jsonify({
            "success": False,
            "error": "Unauthorized: missing or invalid credentials"
        }), 401
    return decorated_function


def require_rate_limit(f):
    """对公开接口按 IP 限流，防止恶意刷接口。"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if _is_local_request():
            return f(*args, **kwargs)
        bucket = _get_client_ip()
        if not _check_rate_limit(bucket):
            return jsonify({
                "success": False,
                "error": "Too many requests, please retry later",
                "retry_window_seconds": RATE_LIMIT_WINDOW_SECONDS
            }), 429
        return f(*args, **kwargs)
    return decorated_function

# 统计数据持久化文件
STATS_FILE = os.path.join(BASE_DIR, 'stats.json')
global_stats = {"total_success": 0}
stats_lock = threading.Lock()

def load_stats():
    """加载统计数据"""
    try:
        if os.path.exists(STATS_FILE):
            import json
            with open(STATS_FILE, 'r') as f:
                data = json.load(f)
                global_stats.update(data)
    except Exception as e:
        print(f"加载统计数据失败: {e}")

def save_stats():
    """保存统计数据"""
    try:
        import json
        with open(STATS_FILE, 'w') as f:
            json.dump(global_stats, f)
    except Exception as e:
        print(f"保存统计数据失败: {e}")

# 启动时加载统计
load_stats()

# 任务存储
tasks: Dict[str, Dict[str, Any]] = {}
tasks_lock = threading.Lock()

# 线程池（用于并发执行验证任务）
executor = ThreadPoolExecutor(max_workers=5)

# 配置
DEFAULT_CONFIG = {
    "use_obs": False,
    "device": "pixel_7_pro",
    "headless": True,
    "timeout": 120,
    "wait_for_completion": True,
    "max_concurrent": 3  # 批量验证最大并发数
}
MAX_TASK_LOGS = 200
TASK_RETENTION_SECONDS = 6 * 60 * 60
MAX_TASKS = 2000


# ============================================================================
# 任务管理
# ============================================================================

class TaskStatus:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _is_valid_verification_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith("https://age-verification.privateid.com/")


def _is_terminal_status(status: str) -> bool:
    return status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


def _prune_tasks_locked():
    """清理过期任务并限制任务总数；调用方必须持有 tasks_lock。"""
    now = datetime.now()

    # 先清理终态且超过保留时长的任务
    stale_task_ids = []
    for task_id, task in tasks.items():
        if not _is_terminal_status(task.get("status")):
            continue
        completed_at = task.get("completed_at") or task.get("created_at")
        if not completed_at:
            continue
        try:
            ts = datetime.fromisoformat(completed_at)
        except ValueError:
            continue
        if (now - ts).total_seconds() > TASK_RETENTION_SECONDS:
            stale_task_ids.append(task_id)
    for task_id in stale_task_ids:
        tasks.pop(task_id, None)

    # 再做数量上限控制：优先删除最老的终态任务
    if len(tasks) <= MAX_TASKS:
        return

    sortable = []
    for task_id, task in tasks.items():
        created_at = task.get("created_at")
        try:
            ts = datetime.fromisoformat(created_at) if created_at else datetime.min
        except ValueError:
            ts = datetime.min
        sortable.append((task_id, task, ts))

    terminal_sorted = [it for it in sortable if _is_terminal_status(it[1].get("status"))]
    terminal_sorted.sort(key=lambda x: x[2])
    while len(tasks) > MAX_TASKS and terminal_sorted:
        task_id, _, _ = terminal_sorted.pop(0)
        tasks.pop(task_id, None)

    if len(tasks) <= MAX_TASKS:
        return

    # 仍超限时兜底删除最老任务
    sortable.sort(key=lambda x: x[2])
    while len(tasks) > MAX_TASKS and sortable:
        task_id, _, _ = sortable.pop(0)
        tasks.pop(task_id, None)


def prune_tasks():
    with tasks_lock:
        _prune_tasks_locked()


def create_task(
    verification_url: str,
    config: Dict[str, Any],
    batch_id: str = None
) -> str:
    """创建新任务"""
    task_id = str(uuid.uuid4())[:8]

    with tasks_lock:
        _prune_tasks_locked()
        tasks[task_id] = {
            "task_id": task_id,
            "batch_id": batch_id,
            "verification_url": verification_url,
            "config": config,
            "status": TaskStatus.PENDING,
            "result": None,
            "error": None,
            "logs": [],  # 新增日志列表
            "created_at": datetime.now().isoformat(),
            "started_at": None,
            "completed_at": None,
        }

    return task_id


def update_task(task_id: str, **kwargs):
    """更新任务状态"""
    with tasks_lock:
        if task_id in tasks:
            tasks[task_id].update(kwargs)


def append_log(task_id: str, message: str):
    """追加任务日志"""
    with tasks_lock:
        if task_id in tasks:
            timestamp = datetime.now().strftime("%H:%M:%S")
            log_entry = f"[{timestamp}] {message}"
            if "logs" not in tasks[task_id]:
                tasks[task_id]["logs"] = []
            tasks[task_id]["logs"].append(log_entry)
            if len(tasks[task_id]["logs"]) > MAX_TASK_LOGS:
                tasks[task_id]["logs"] = tasks[task_id]["logs"][-MAX_TASK_LOGS:]


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    """获取任务信息"""
    with tasks_lock:
        task = tasks.get(task_id)
        return deepcopy(task) if task else None


def get_batch_tasks(batch_id: str) -> List[Dict[str, Any]]:
    """获取批量任务的所有子任务"""
    with tasks_lock:
        return [deepcopy(t) for t in tasks.values() if t.get("batch_id") == batch_id]


# ============================================================================
# 验证执行器
# ============================================================================

def run_verification_sync(
    task_id: str,
    verification_url: str,
    config: Dict[str, Any]
):
    """同步执行单个验证任务"""
    update_task(task_id, status=TaskStatus.RUNNING, started_at=datetime.now().isoformat())

    try:
        # 创建事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # 定义日志回调
        def log_callback(msg):
            append_log(task_id, msg)

        service = None
        try:
            # 创建服务实例
            service = AgeVerificationService(
                use_obs=config.get("use_obs", DEFAULT_CONFIG["use_obs"]),
                camera_keyword=config.get("camera_keyword", "virtual"),
                device=config.get("device", DEFAULT_CONFIG["device"]),
                headless=config.get("headless", DEFAULT_CONFIG["headless"]),
                timeout=config.get("timeout", DEFAULT_CONFIG["timeout"]),
                debug_screenshots=config.get("debug_screenshots", False)
            )

            # 执行验证
            result = loop.run_until_complete(
                service.verify(
                    verification_url=verification_url,
                    video_file=config.get("video_file"),
                    wait_for_completion=config.get("wait_for_completion", True),
                    log_callback=log_callback
                )
            )

            # 关闭服务
            loop.run_until_complete(service.close())

            # 更新全局统计
            if result.get("success", False):
                with stats_lock:
                    global_stats["total_success"] += 1
                    save_stats()

            update_task(
                task_id,
                status=TaskStatus.COMPLETED,
                result=result,
                completed_at=datetime.now().isoformat()
            )

        except Exception as e:
            # 确保服务被关闭
            if service:
                try:
                    loop.run_until_complete(service.close())
                except:
                    pass
            raise e

        finally:
            # 正确清理 asyncio 资源（Windows 兼容）
            try:
                # 取消所有待处理任务
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()

                # 等待任务取消完成
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

                # 关闭异步生成器
                loop.run_until_complete(loop.shutdown_asyncgens())
            except:
                pass
            finally:
                loop.close()

    except Exception as e:
        update_task(
            task_id,
            status=TaskStatus.FAILED,
            error=str(e),
            completed_at=datetime.now().isoformat()
        )


def run_batch_verification(task_plan: List[Dict[str, str]], config: Dict[str, Any]):
    """运行批量验证"""
    max_concurrent = max(1, int(config.get("max_concurrent", 3)))
    task_queue: "queue.Queue[Dict[str, str]]" = queue.Queue()
    for task in task_plan:
        task_queue.put(task)

    threads = []

    def worker():
        while True:
            try:
                task = task_queue.get_nowait()
            except queue.Empty:
                return
            try:
                run_verification_sync(task["task_id"], task["url"], config)
            finally:
                task_queue.task_done()

    worker_count = min(max_concurrent, max(1, len(task_plan)))
    for _ in range(worker_count):
        t = threading.Thread(target=worker)
        t.start()
        threads.append(t)

    task_queue.join()
    for t in threads:
        t.join()

    return [task["task_id"] for task in task_plan]


# ============================================================================
# API 端点
# ============================================================================

@app.route('/')
def index():
    """前端测试页面"""
    return send_from_directory(FRONTEND_DIR, 'index.html')


@app.route('/auth/client-token', methods=['POST'])
@require_rate_limit
def issue_client_token():
    """签发公网访客短期令牌。"""
    if _is_local_request():
        return jsonify({
            "success": True,
            "token": "",
            "expires_in": 0,
            "local_bypass": True
        })

    data = request.get_json(silent=True) or {}
    if not verify_turnstile(data.get('turnstile_token'), _get_client_ip()):
        return jsonify({
            "success": False,
            "error": "CAPTCHA verification failed / 人机验证失败"
        }), 403

    token = _issue_client_token()
    return jsonify({
        "success": True,
        "token": token,
        "expires_in": CLIENT_TOKEN_TTL_SECONDS
    })


@app.route('/verify', methods=['POST'])
@require_rate_limit
@require_api_key
def verify_single():
    """
    单个验证 API

    请求体:
    {
        "url": "https://age-verification.privateid.com/...",
        "use_obs": true,
        "device": "pixel_7_pro",
        "headless": false,
        "timeout": 120,
        "wait_for_completion": true,
        "async": false  // 是否异步执行
    }

    响应:
    - 同步模式: 返回验证结果
    - 异步模式: 返回 task_id
    """
    data = request.get_json() or {}

    # Turnstile 验证
    if not verify_turnstile(data.get('turnstile_token'), _get_client_ip()):
        return jsonify({
            "success": False,
            "error": "CAPTCHA verification failed / 人机验证失败"
        }), 403

    # 验证必需参数
    verification_url = data.get("url")
    if not verification_url:
        return jsonify({
            "success": False,
            "error": "缺少必需参数: url"
        }), 400

    # 自动清理 URL (去除空格和换行)
    verification_url = verification_url.strip().replace(" ", "").replace("\n", "").replace("\r", "")

    # 验证 URL 格式 (域名必须是 age-verification.privateid.com)
    # 支持 /handoff-age?handoffId=... 和 /privacy-policy?sessionId=... 等多种格式
    if not _is_valid_verification_url(verification_url):
        return jsonify({
            "success": False,
            "error": "无效的验证链接格式",
            "hint": "链接必须以 https://age-verification.privateid.com/ 开头",
            "received": verification_url[:100] + "..." if len(verification_url) > 100 else verification_url
        }), 400

    # 构建配置
    video_file = data.get("video_file")
    print(f"DEBUG: 请求数据: {data}")
    print(f"DEBUG: 请求传入的 video_file: {video_file}")

    # 总是尝试加载默认视频，如果未提供视频文件
    # 即使 use_obs 为 True，由于服务层强制使用视频注入，我们也需要一个视频文件
    if not video_file:
        # 优先查找当前目录下的默认视频，兼容 Linux/Windows
        default_video_name = "546a131c1b386aa2856940da0f8737b2.mp4"
        possible_paths = [
            os.path.join(BASE_DIR, default_video_name),
            os.path.abspath(default_video_name),
            r"F:\SHIRO_Object\google_tools\age_verification_project\546a131c1b386aa2856940da0f8737b2.mp4" # 保留旧路径作为后备
        ]

        print(f"DEBUG: 正在查找默认视频文件: {default_video_name}")
        for path in possible_paths:
            exists = os.path.exists(path)
            # print(f"DEBUG: 检查路径: {path} -> {'存在' if exists else '不存在'}")
            if exists:
                video_file = path
                break

    if video_file:
        print(f"DEBUG: 最终使用的 video_file: {video_file}")
    else:
        print("DEBUG: 未找到任何可用视频文件")

    config = {
        "use_obs": data.get("use_obs", DEFAULT_CONFIG["use_obs"]),
        "device": data.get("device", DEFAULT_CONFIG["device"]),
        "headless": data.get("headless", DEFAULT_CONFIG["headless"]),
        "timeout": data.get("timeout", DEFAULT_CONFIG["timeout"]),
        "wait_for_completion": data.get("wait_for_completion", DEFAULT_CONFIG["wait_for_completion"]),
        "video_file": video_file,
        "camera_keyword": data.get("camera_keyword", "virtual"),
        "debug_screenshots": data.get("debug_screenshots", False),
    }

    # 验证设备配置
    if config["device"] not in MOBILE_DEVICE_CONFIGS:
        return jsonify({
            "success": False,
            "error": f"无效的设备配置: {config['device']}",
            "available_devices": list(MOBILE_DEVICE_CONFIGS.keys())
        }), 400

    is_async = data.get("async", False)

    if is_async:
        # 异步模式：创建任务并立即返回
        task_id = create_task(verification_url, config)
        executor.submit(run_verification_sync, task_id, verification_url, config)

        return jsonify({
            "success": True,
            "task_id": task_id,
            "message": "任务已创建，请使用 /tasks/<task_id> 查询状态",
            "status_url": f"/tasks/{task_id}"
        })
    else:
        # 同步模式：等待验证完成
        task_id = create_task(verification_url, config)
        run_verification_sync(task_id, verification_url, config)

        task = get_task(task_id)
        return jsonify({
            "success": task["status"] == TaskStatus.COMPLETED and (task.get("result") or {}).get("success", False),
            "task_id": task_id,
            "status": task["status"],
            "result": task["result"],
            "error": task["error"]
        })


@app.route('/verify/batch', methods=['POST'])
@require_rate_limit
@require_api_key
def verify_batch():
    """
    批量验证 API

    请求体:
    {
        "urls": [
            "https://age-verification.privateid.com/...",
            "https://age-verification.privateid.com/...",
            ...
        ],
        "use_obs": true,
        "device": "pixel_7_pro",
        "headless": false,
        "timeout": 120,
        "max_concurrent": 3  // 最大并发数
    }

    响应:
    {
        "success": true,
        "batch_id": "abc12345",
        "task_count": 5,
        "tasks": ["task1", "task2", ...],
        "status_url": "/batches/abc12345"
    }
    """
    data = request.get_json() or {}

    # Turnstile 验证
    if not verify_turnstile(data.get('turnstile_token'), _get_client_ip()):
        return jsonify({
            "success": False,
            "error": "CAPTCHA verification failed / 人机验证失败"
        }), 403

    # 验证必需参数
    urls = data.get("urls", [])
    if not urls or not isinstance(urls, list):
        return jsonify({
            "success": False,
            "error": "缺少必需参数: urls (需要是链接数组)"
        }), 400

    if len(urls) > 100:
        return jsonify({
            "success": False,
            "error": "批量验证最多支持 100 个链接"
        }), 400

    normalized_urls = []
    for raw_url in urls:
        if not isinstance(raw_url, str):
            return jsonify({
                "success": False,
                "error": "urls 数组中包含非字符串项"
            }), 400
        normalized_url = raw_url.strip().replace(" ", "").replace("\n", "").replace("\r", "")
        if not _is_valid_verification_url(normalized_url):
            return jsonify({
                "success": False,
                "error": f"无效的验证链接格式: {normalized_url[:100]}"
            }), 400
        normalized_urls.append(normalized_url)

    try:
        max_concurrent = int(data.get("max_concurrent", DEFAULT_CONFIG["max_concurrent"]))
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "error": "max_concurrent 必须是整数"
        }), 400
    max_concurrent = max(1, min(max_concurrent, 10))

    # 构建配置
    config = {
        "use_obs": data.get("use_obs", DEFAULT_CONFIG["use_obs"]),
        "device": data.get("device", DEFAULT_CONFIG["device"]),
        "headless": data.get("headless", DEFAULT_CONFIG["headless"]),
        "timeout": data.get("timeout", DEFAULT_CONFIG["timeout"]),
        "wait_for_completion": True,  # 批量模式始终等待完成
        "max_concurrent": max_concurrent,
        "video_file": data.get("video_file"),
        "camera_keyword": data.get("camera_keyword", "virtual"),
        "debug_screenshots": data.get("debug_screenshots", False),
    }

    # 处理默认视频文件逻辑 (批量模式也需要)
    if not config["video_file"]:
        # 优先查找当前目录下的默认视频
        default_video_name = "546a131c1b386aa2856940da0f8737b2.mp4"
        possible_paths = [
            os.path.join(BASE_DIR, default_video_name),
            os.path.abspath(default_video_name),
            r"F:\SHIRO_Object\google_tools\age_verification_project\546a131c1b386aa2856940da0f8737b2.mp4"
        ]
        for path in possible_paths:
            if os.path.exists(path):
                config["video_file"] = path
                break

    # 创建批次 ID
    batch_id = str(uuid.uuid4())[:8]

    # 先创建全部任务，确保返回 task 列表完整且无竞态
    task_plan = []
    for url in normalized_urls:
        task_id = create_task(url, config, batch_id=batch_id)
        task_plan.append({"task_id": task_id, "url": url})

    # 在后台线程中执行批量验证
    executor.submit(run_batch_verification, task_plan, config)
    task_ids = [task["task_id"] for task in task_plan]

    return jsonify({
        "success": True,
        "batch_id": batch_id,
        "task_count": len(task_ids),
        "tasks": task_ids,
        "status_url": f"/batches/{batch_id}",
        "message": f"批量任务已创建，共 {len(task_ids)} 个验证任务"
    })


@app.route('/tasks/<task_id>', methods=['GET'])
@require_rate_limit
@require_api_key
def get_task_status(task_id: str):
    """查询单个任务状态"""
    prune_tasks()
    task = get_task(task_id)

    if not task:
        return jsonify({
            "success": False,
            "error": f"任务不存在: {task_id}"
        }), 404

    return jsonify({
        "success": True,
        "task": task
    })


@app.route('/tasks/<task_id>', methods=['DELETE'])
@require_rate_limit
@require_api_key
def cancel_task(task_id: str):
    """取消任务（仅限待处理任务）"""
    prune_tasks()
    task = get_task(task_id)

    if not task:
        return jsonify({
            "success": False,
            "error": f"任务不存在: {task_id}"
        }), 404

    if task["status"] != TaskStatus.PENDING:
        return jsonify({
            "success": False,
            "error": f"只能取消待处理任务，当前状态: {task['status']}"
        }), 400

    update_task(task_id, status=TaskStatus.CANCELLED, completed_at=datetime.now().isoformat())

    return jsonify({
        "success": True,
        "message": f"任务 {task_id} 已取消"
    })


@app.route('/tasks', methods=['GET'])
@require_rate_limit
@require_api_key
def list_tasks():
    """查询所有任务"""
    prune_tasks()
    status_filter = request.args.get("status")
    batch_filter = request.args.get("batch_id")
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "error": "limit 必须是整数"
        }), 400
    limit = max(1, min(limit, 1000))

    with tasks_lock:
        result = [deepcopy(t) for t in tasks.values()]

    # 过滤状态
    if status_filter:
        result = [t for t in result if t["status"] == status_filter]

    # 过滤批次
    if batch_filter:
        result = [t for t in result if t.get("batch_id") == batch_filter]

    # 按创建时间倒序
    result.sort(key=lambda x: x["created_at"], reverse=True)

    # 限制数量
    result = result[:limit]

    return jsonify({
        "success": True,
        "count": len(result),
        "tasks": result
    })


@app.route('/batches/<batch_id>', methods=['GET'])
@require_rate_limit
@require_api_key
def get_batch_status(batch_id: str):
    """查询批量任务状态"""
    prune_tasks()
    batch_tasks = get_batch_tasks(batch_id)

    if not batch_tasks:
        return jsonify({
            "success": False,
            "error": f"批次不存在: {batch_id}"
        }), 404

    # 统计各状态数量
    status_counts = {
        TaskStatus.PENDING: 0,
        TaskStatus.RUNNING: 0,
        TaskStatus.COMPLETED: 0,
        TaskStatus.FAILED: 0,
        TaskStatus.CANCELLED: 0,
    }

    success_count = 0

    for task in batch_tasks:
        status_counts[task["status"]] = status_counts.get(task["status"], 0) + 1
        if task["status"] == TaskStatus.COMPLETED and task["result"] and task["result"].get("success"):
            success_count += 1

    total = len(batch_tasks)
    completed = status_counts[TaskStatus.COMPLETED] + status_counts[TaskStatus.FAILED]

    return jsonify({
        "success": True,
        "batch_id": batch_id,
        "total": total,
        "completed": completed,
        "progress": f"{completed}/{total}",
        "progress_percent": round(completed / total * 100, 1) if total > 0 else 0,
        "success_count": success_count,
        "status_counts": status_counts,
        "is_finished": completed == total,
        "tasks": batch_tasks
    })


@app.route('/devices', methods=['GET'])
def list_devices():
    """列出可用的设备配置"""
    return jsonify({
        "success": True,
        "devices": {
            name: {
                "name": config["name"],
                "platform": config["platform"],
                "viewport": config["viewport"]
            }
            for name, config in MOBILE_DEVICE_CONFIGS.items()
        }
    })


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查"""
    prune_tasks()
    with tasks_lock:
        running_tasks = len([t for t in tasks.values() if t["status"] == TaskStatus.RUNNING])
        total_tasks = len(tasks)
    return jsonify({
        "status": "ok",
        "service": "age-verification-api",
        "timestamp": datetime.now().isoformat(),
        "active_tasks": running_tasks,
        "total_tasks": total_tasks
    })


@app.route('/stats', methods=['GET'])
@require_rate_limit
@require_api_key
def get_stats():
    """获取全局统计数据"""
    with stats_lock:
        stats_snapshot = deepcopy(global_stats)
    return jsonify({
        "success": True,
        "stats": stats_snapshot
    })


@app.route('/api-docs', methods=['GET'])
def api_docs():
    """API 文档"""
    return jsonify({
        "service": "年龄验证 API 服务",
        "version": "1.0.0",
        "endpoints": {
            "POST /verify": "单个验证 (支持同步/异步)",
            "POST /verify/batch": "批量验证 (异步)",
            "GET /tasks": "查询所有任务",
            "GET /tasks/<task_id>": "查询单个任务状态",
            "DELETE /tasks/<task_id>": "取消任务",
            "GET /batches/<batch_id>": "查询批量任务状态",
            "GET /devices": "列出可用设备配置",
            "GET /health": "健康检查"
        },
        "example": {
            "single_sync": {
                "method": "POST",
                "url": "/verify",
                "body": {
                    "url": "https://age-verification.privateid.com/...",
                    "use_obs": True,
                    "device": "pixel_7_pro"
                }
            },
            "single_async": {
                "method": "POST",
                "url": "/verify",
                "body": {
                    "url": "https://age-verification.privateid.com/...",
                    "async": True
                }
            },
            "batch": {
                "method": "POST",
                "url": "/verify/batch",
                "body": {
                    "urls": ["url1", "url2", "url3"],
                    "max_concurrent": 3
                }
            }
        }
    })


# 前端页面现在位于 frontend/ 目录下，请直接打开 frontend/index.html 使用
# 或者使用 HTTP 服务器托管 frontend/ 目录
# 本服务仅作为 API 后端


# ============================================================================
# 主程序
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="年龄验证 API 服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", "-p", type=int, default=5001, help="监听端口")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    parser.add_argument("--workers", "-w", type=int, default=5, help="最大工作线程数")
    parser.add_argument("--show-browser", action="store_true", help="显示浏览器窗口（有头模式）")

    args = parser.parse_args()

    # 根据启动参数更新默认配置
    # 如果指定了 --show-browser，则 headless=False，否则默认为 True
    DEFAULT_CONFIG["headless"] = not args.show_browser

    # 更新线程池大小（覆盖默认值）
    executor.shutdown(wait=False)
    executor = ThreadPoolExecutor(max_workers=args.workers)

    print("=" * 60)
    print("年龄验证 API 服务")
    print("=" * 60)
    print(f"监听地址: http://{args.host}:{args.port}")
    print(f"API Key : {API_KEY if API_KEY else '(未设置，无需认证)'}")
    print(f"工作线程: {args.workers}")
    print(f"浏览器  : {'有头模式（显示窗口）' if args.show_browser else '无头模式'}")
    print("")
    print("API 端点:")
    print("  POST /verify          - 单个验证")
    print("  POST /verify/batch    - 批量验证")
    print("  GET  /tasks           - 查询所有任务")
    print("  GET  /tasks/<task_id> - 查询任务状态")
    print("  GET  /batches/<batch_id> - 查询批量任务状态")
    print("  GET  /devices         - 列出设备配置")
    print("  GET  /health          - 健康检查")
    print("=" * 60)

    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
