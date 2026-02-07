#!/usr/bin/env python3
"""
年龄验证服务模块
================
使用虚拟摄像头完成 PrivateID 年龄验证

功能：
1. 启动带虚拟摄像头的 Android 模拟浏览器
2. 访问 PrivateID 验证链接
3. 自动点击验证按钮
4. 检测验证完成状态
5. 返回验证结果
"""

import asyncio
import os
import sys
import hashlib
import threading
from pathlib import Path
from typing import Optional, Dict, Any

# 添加项目根目录到路径
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext
    PLAYWRIGHT_AVAILABLE = True
    PLAYWRIGHT_IMPORT_ERROR = None
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    PLAYWRIGHT_IMPORT_ERROR = "playwright is not installed"
    async_playwright = None
    Page = Any
    Browser = Any
    BrowserContext = Any


# 预定义的移动端设备配置
MOBILE_DEVICE_CONFIGS = {
    "pixel_7_pro": {
        "name": "Pixel 7 Pro (Android 14, Chrome 120)",
        "ua": "Mozilla/5.0 (Linux; Android 14; Pixel 7 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.210 Mobile Safari/537.36",
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "platform": "Linux armv8l"
    },
    "galaxy_s23": {
        "name": "Samsung Galaxy S23 Ultra (Android 13, Chrome 119)",
        "ua": "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.6045.163 Mobile Safari/537.36",
        "viewport": {"width": 384, "height": 854},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "platform": "Linux armv8l"
    },
    "iphone_15": {
        "name": "iPhone 15 Pro Max (iOS 17, Safari)",
        "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
        "viewport": {"width": 430, "height": 932},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "platform": "iPhone"
    }
}

BASE_DIR = Path(__file__).resolve().parent.parent
VIDEO_CACHE_DIR = BASE_DIR / ".video_cache"
VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_video_cache_lock = threading.Lock()
_video_cache_key_locks: Dict[str, threading.Lock] = {}


class AgeVerificationService:
    """年龄验证服务类 - 使用虚拟摄像头完成 PrivateID 验证"""

    # 验证完成的标志文本
    COMPLETION_TEXTS = [
        "Complete, your selfie was deleted",
        "Verification complete",
        "验证完成",
        "成功",
    ]

    # 验证失败的标志文本
    FAILURE_TEXTS = [
        "Verification failed",
        "Try again",
        "Unable to verify",
        "验证失败",
        "重试",
    ]

    def __init__(
        self,
        use_obs: bool = False,
        camera_keyword: str = "virtual",
        device: str = "pixel_7_pro",
        headless: bool = True,
        timeout: int = 60,
        debug_screenshots: bool = False
    ):
        """
        初始化年龄验证服务

        Args:
            use_obs: 是否使用 OBS 虚拟摄像头（推荐）
            camera_keyword: 摄像头选择关键字
            device: 设备配置名称 (pixel_7_pro, galaxy_s23, iphone_15)
            headless: 是否无头模式
            timeout: 验证超时时间（秒）
        """
        self.use_obs = use_obs
        self.camera_keyword = camera_keyword
        self.device_config = MOBILE_DEVICE_CONFIGS.get(device, MOBILE_DEVICE_CONFIGS["pixel_7_pro"])
        self.headless = headless
        self.timeout = timeout
        self.debug_screenshots = debug_screenshots

        self.playwright = None  # Playwright 实例，需要正确关闭
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._temp_files = []  # 临时文件列表，用于清理

    def _prepare_video_file(self, video_file: str, apply_position_shift: bool = True) -> str:
        """准备视频文件 (转码为 Y4M 并应用位置调整)

        Args:
            video_file: 原始视频文件路径
            apply_position_shift: 是否应用摄像机位置调整（向上移动）
        """
        if not video_file or not os.path.exists(video_file):
            return video_file

        ext = os.path.splitext(video_file)[1].lower()

        # 如果不需要位置调整，且已经是兼容格式，直接返回
        if not apply_position_shift and ext in ['.y4m', '.mjpeg']:
            return video_file

        # 需要转码或应用位置调整，改为持久化缓存避免重复 ffmpeg
        import subprocess
        import tempfile

        source_path = Path(video_file).resolve()
        stat = source_path.stat()
        cache_fingerprint = f"{source_path}|{stat.st_size}|{stat.st_mtime_ns}|shift={int(apply_position_shift)}"
        cache_key = hashlib.sha256(cache_fingerprint.encode("utf-8")).hexdigest()[:20]
        cache_path = VIDEO_CACHE_DIR / f"{cache_key}.y4m"

        # 使用细粒度锁避免并发重复转码
        with _video_cache_lock:
            key_lock = _video_cache_key_locks.setdefault(cache_key, threading.Lock())

        with key_lock:
            if cache_path.exists() and cache_path.stat().st_size > 0:
                print(f"[AgeVerify] 使用缓存视频: {cache_path}")
                return str(cache_path)

            print(f"[AgeVerify] 正在处理视频文件 ({ext})...")
            print(f"[AgeVerify] 位置调整: {'启用 (向上移动300像素)' if apply_position_shift else '禁用'}")
            try:
                fd, temp_path = tempfile.mkstemp(suffix='.y4m', dir=str(VIDEO_CACHE_DIR))
                os.close(fd)

                # 位置调整滤镜说明：
                # crop=iw:ih-300:0:0 - 先裁掉底部300像素
                # pad=iw:ih+300:0:300:black - 在顶部添加300像素黑边，恢复原始高度
                # 效果：视频内容向下移动300像素（相当于相机向上移动300像素）
                if apply_position_shift:
                    vf_filter = 'crop=iw:ih-300:0:0,pad=iw:ih+300:0:300:black,format=yuv420p'
                else:
                    vf_filter = 'format=yuv420p'

                cmd = [
                    'ffmpeg', '-y',
                    '-i', str(source_path),
                    '-vf', vf_filter,
                    '-pix_fmt', 'yuv420p',
                    temp_path
                ]

                print(f"[AgeVerify] FFmpeg 滤镜: {vf_filter}")
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                # 原子替换，避免并发读到半文件
                os.replace(temp_path, str(cache_path))
                print(f"[AgeVerify] 转码完成并写入缓存: {cache_path}")
                return str(cache_path)
            except Exception as e:
                print(f"[AgeVerify] 视频转码失败: {e}")
                print(f"[AgeVerify] 尝试直接使用原文件 (可能无法工作)")
                return video_file
            finally:
                try:
                    if 'temp_path' in locals() and os.path.exists(temp_path):
                        os.remove(temp_path)
                except Exception:
                    pass

    def _get_chromium_args(self, video_file: str = None) -> list:
        """获取 Chromium 启动参数"""
        args = [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=WebRtcHideLocalIpsWithMdns",
            "--disable-blink-features=AutomationControlled",
            "--unsafely-treat-insecure-origin-as-secure=http://localhost",
        ]

        # 强制使用视频文件注入模式，忽略 self.use_obs
        # if self.use_obs:
        #     # OBS 模式：只自动授权，不使用虚拟设备
        #     args.append("--use-fake-ui-for-media-stream")
        # else:

        # Chromium 虚拟设备模式 (视频注入)
        args.append("--use-fake-ui-for-media-stream")
        args.append("--use-fake-device-for-media-stream")

        if video_file:
            video_path = os.path.abspath(video_file)
            if os.path.exists(video_path):
                args.append(f"--use-file-for-fake-video-capture={video_path}")
            else:
                print(f"[AgeVerify] 警告: 视频文件不存在: {video_path}")

        return args

    def _get_camera_disguise_script(self) -> str:
        """获取摄像头伪装脚本"""
        return """
            (function() {
                // 设备名称伪装映射表
                const labelRenameMap = {
                    'OBS Virtual Camera': 'Pixel 7 Pro Front Camera',
                    'OBS-Camera': 'Pixel 7 Pro Front Camera',
                    'VTubeStudioCam': 'Samsung S23 Camera',
                    'ManyCam Virtual Webcam': 'Android HD Camera',
                    'XSplit VCam': 'Google Pixel Camera',
                    'Snap Camera': 'Selfie Camera',
                };

                // 目标摄像头关键字
                const targetCameraKeywords = ['obs', 'virtual'];

                // 需要隐藏的摄像头关键字
                const hiddenCameraKeywords = ['vtube', 'studio', 'webcam', 'hd webcam', 'full hd'];

                // 通用规则：需要伪装的关键字
                const suspiciousKeywords = ['virtual', 'obs', 'fake', 'dummy', 'test', 'vcam', 'snap'];

                function disguiseLabel(label) {
                    if (labelRenameMap[label]) {
                        return labelRenameMap[label];
                    }
                    const lowerLabel = label.toLowerCase();
                    for (const keyword of suspiciousKeywords) {
                        if (lowerLabel.includes(keyword)) {
                            return 'Front Camera (0)';
                        }
                    }
                    return label;
                }

                function isTargetCamera(label) {
                    const lowerLabel = label.toLowerCase();
                    for (const keyword of targetCameraKeywords) {
                        if (lowerLabel.includes(keyword)) {
                            return true;
                        }
                    }
                    return false;
                }

                function shouldHideCamera(label) {
                    const lowerLabel = label.toLowerCase();
                    for (const keyword of hiddenCameraKeywords) {
                        if (lowerLabel.includes(keyword)) {
                            return true;
                        }
                    }
                    return false;
                }

                const originalGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                const originalEnumerateDevices = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);

                let selectedCameraId = null;

                navigator.mediaDevices.enumerateDevices = async function() {
                    const devices = await originalEnumerateDevices();
                    const filteredDevices = [];
                    let targetCamera = null;

                    for (const device of devices) {
                        if (device.kind === 'videoinput') {
                            if (isTargetCamera(device.label) && !shouldHideCamera(device.label)) {
                                targetCamera = device;
                                selectedCameraId = device.deviceId;
                                break;
                            }
                        }
                    }

                    for (const device of devices) {
                        if (device.kind === 'videoinput') {
                            if (targetCamera && device.deviceId === targetCamera.deviceId) {
                                filteredDevices.push({
                                    deviceId: device.deviceId,
                                    groupId: device.groupId,
                                    kind: device.kind,
                                    label: disguiseLabel(device.label),
                                    toJSON: () => ({
                                        deviceId: device.deviceId,
                                        groupId: device.groupId,
                                        kind: device.kind,
                                        label: disguiseLabel(device.label)
                                    })
                                });
                            }
                        } else {
                            filteredDevices.push(device);
                        }
                    }

                    console.log('[AgeVerify] 目标摄像头:', targetCamera?.label, '->', disguiseLabel(targetCamera?.label || ''));
                    return filteredDevices;
                };

                navigator.mediaDevices.getUserMedia = async function(constraints) {
                    if (constraints && constraints.video) {
                        if (!selectedCameraId) {
                            await navigator.mediaDevices.enumerateDevices();
                        }
                        if (selectedCameraId) {
                            if (typeof constraints.video === 'boolean') {
                                constraints.video = { deviceId: { exact: selectedCameraId } };
                            } else if (typeof constraints.video === 'object') {
                                constraints.video.deviceId = { exact: selectedCameraId };
                            }
                        }
                    }
                    return originalGetUserMedia(constraints);
                };

                console.log('[AgeVerify] 摄像头伪装脚本已加载');
            })();
        """

    async def _setup_browser(self, video_file: str = None):
        """设置浏览器和上下文"""
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "playwright 未安装。请先执行: pip install playwright && playwright install chromium"
            )

        self.playwright = await async_playwright().start()

        chromium_args = self._get_chromium_args(video_file)

        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=chromium_args
        )

        self.context = await self.browser.new_context(
            user_agent=self.device_config["ua"],
            viewport=self.device_config["viewport"],
            device_scale_factor=self.device_config["device_scale_factor"],
            is_mobile=self.device_config["is_mobile"],
            has_touch=self.device_config["has_touch"],
            permissions=["camera", "microphone"],
            ignore_https_errors=True,
        )

        # 注入摄像头伪装脚本 (仅在 OBS 模式下需要，已禁用)
        # if self.use_obs:
        #     await self.context.add_init_script(self._get_camera_disguise_script())

        self.page = await self.context.new_page()

        # CDP 深度伪装
        try:
            cdp = await self.context.new_cdp_session(self.page)

            await cdp.send("Emulation.setUserAgentOverride", {
                "userAgent": self.device_config["ua"],
                "platform": self.device_config["platform"],
                "userAgentMetadata": {
                    "brands": [
                        {"brand": "Chromium", "version": "120"},
                        {"brand": "Google Chrome", "version": "120"},
                    ],
                    "fullVersion": "120.0.6099.210",
                    "platform": "Android",
                    "platformVersion": "14.0",
                    "architecture": "arm",
                    "model": "Pixel 7 Pro",
                    "mobile": True,
                }
            })

            await cdp.send("Emulation.setTouchEmulationEnabled", {
                "enabled": True,
                "maxTouchPoints": 5
            })
        except Exception as e:
            print(f"[AgeVerify] CDP 配置警告: {e}")

    async def _auto_click_buttons(self, log_callback=None) -> list:
        """自动点击验证页面按钮"""

        def log(msg):
            if log_callback:
                try: log_callback(msg)
                except: pass
            else:
                print(f"[AgeVerify] {msg}")

        button_configs = [
            {
                "selectors": [
                    'button:has-text("Agree and Continue")',
                    'button:has-text("Agree & Continue")',
                    'button:has-text("同意并继续")',
                    '[data-testid="agree-button"]',
                    'button.agree-button',
                    'button[type="submit"]:has-text("Agree")',
                ],
                "name": "Agree and Continue",
                "wait_after": 2.5,
            },
            {
                "selectors": [
                    'button:has-text("Start")',
                    'button:has-text("开始")',
                    'button:has-text("Begin")',
                    '[data-testid="start-button"]',
                    'button.start-button',
                    '#start',
                ],
                "name": "Start",
                "wait_after": 1.5,
            },
            {
                "selectors": [
                    'button:has-text("Continue")',
                    'button:has-text("继续")',
                    'button:has-text("Next")',
                    'button:has-text("下一步")',
                ],
                "name": "Continue",
                "wait_after": 1.0,
            },
            {
                "selectors": [
                    'button:has-text("Allow")',
                    'button:has-text("允许")',
                    'button:has-text("Grant Access")',
                ],
                "name": "Allow",
                "wait_after": 1.0,
            },
        ]

        clicked_buttons = []
        max_attempts = 25  # 增加最大尝试次数
        attempt = 0
        consecutive_clicks = {} # 记录连续点击次数

        # 首次等待页面稳定（等待 JavaScript 框架加载）
        log("等待页面加载完成...")
        await asyncio.sleep(1)

        # 尝试等待第一个按钮出现
        try:
            first_btn = self.page.locator('button:has-text("Agree")').first
            await first_btn.wait_for(state="visible", timeout=10000)
            log("检测到 Agree 按钮")
        except Exception as e:
            log(f"未能等待到 Agree 按钮: {e}")
            # 截图以便调试
            if self.debug_screenshots:
                try:
                    await self.page.screenshot(path="debug_no_agree_button.png")
                    log("已保存截图: debug_no_agree_button.png")
                except:
                    pass

        while attempt < max_attempts:
            attempt += 1
            clicked_this_round = False

            # 实时检测 URL 变化，如果已跳转到 Google 域名则立即退出
            try:
                current_url = self.page.url
                if "accounts.google.com" in current_url or "myaccount.google.com" in current_url:
                    log(f"检测到验证完成，已跳转至: {current_url}")
                    return clicked_buttons
            except Exception as e:
                pass  # 页面可能正在加载，忽略错误

            for config in button_configs:
                # 检查是否连续点击过多
                btn_name = config["name"]
                if consecutive_clicks.get(btn_name, 0) >= 5:
                    if attempt % 5 == 0: # 每 5 次尝试才重试一次被屏蔽的按钮
                        log(f"尝试重试被限制的按钮: {btn_name}")
                    else:
                        continue

                for selector in config["selectors"]:
                    try:
                        btn = self.page.locator(selector).first
                        # 增加可见性检测超时到 1 秒
                        if await btn.is_visible(timeout=1000):
                            is_disabled = await btn.is_disabled()
                            if not is_disabled:
                                await btn.click()
                                log(f"点击按钮: {config['name']} (选择器: {selector})")

                                # 更新统计
                                clicked_buttons.append(config['name'])
                                clicked_this_round = True

                                # 更新连续点击计数
                                for k in consecutive_clicks:
                                    consecutive_clicks[k] = 0 # 重置其他按钮
                                consecutive_clicks[btn_name] = consecutive_clicks.get(btn_name, 0) + 1

                                # 截图记录点击后的状态
                                if self.debug_screenshots:
                                    try:
                                        screenshot_path = f"debug_click_{len(clicked_buttons)}_{btn_name.replace(' ', '_')}.png"
                                        await self.page.screenshot(path=screenshot_path)
                                        # log(f"已保存截图: {screenshot_path}") # 避免刷屏
                                    except:
                                        pass

                                await asyncio.sleep(config["wait_after"])
                                break
                    except Exception as e:
                        # 只在调试时记录详细错误
                        pass
                if clicked_this_round:
                    break

            if not clicked_this_round:
                await asyncio.sleep(0.8)  # 增加等待时间

                # 截图查看当前状态
                if self.debug_screenshots and attempt % 5 == 0:
                    try:
                        await self.page.screenshot(path=f"debug_waiting_{attempt}.png")
                        log(f"等待中... 已保存截图: debug_waiting_{attempt}.png")

                        # 打印页面文本帮助诊断
                        text = await self.page.evaluate("document.body.innerText")
                        # log(f"当前页面文本片段: {text[:200].replace(chr(10), ' ')}...")
                    except:
                        pass

                # 放宽早期退出条件
                if attempt >= 15 and not clicked_buttons:
                    log(f"{attempt} 次尝试后未找到任何按钮，退出")
                    break
                if attempt >= 20 and len(clicked_buttons) > 0:
                    log(f"已点击 {len(clicked_buttons)} 个按钮，无更多按钮")
                    break
            else:
                # 点击成功后重置尝试计数，继续寻找下一个按钮
                attempt = max(0, attempt - 2)

        return clicked_buttons

    async def _check_completion(self, log_callback=None) -> Dict[str, Any]:
        """检查验证是否完成"""
        def log(msg):
            if log_callback:
                try: log_callback(msg)
                except: pass
            else:
                print(f"[AgeVerify] {msg}")

        try:
            # 1. 检查 URL 变化 (最优先检查，无需等待页面完全加载)
            current_url = self.page.url
            if "myaccount.google.com" in current_url or "accounts.google.com" in current_url:
                log(f"检测到页面跳转回 Google: {current_url}")

                # 只要跳转回 Google 域名，且不在 privateid.com，通常意味着流程结束
                # 检查 URL 参数中是否包含结果标识，或者这是否是登录/后续页面

                if "result" in current_url or "success" in current_url:
                    return {
                        "completed": True,
                        "success": True,
                        "message": "验证流程已完成 (检测到结果页跳转)"
                    }
                elif "signin" in current_url:
                    # 如果跳转到登录页，检查 referrer 或参数中是否有 result
                    # PrivateID 完成后通常会带上 result 路径
                    if "result" in current_url or "%2Fresult" in current_url:
                         return {
                            "completed": True,
                            "success": True,
                            "message": "验证流程已完成 (检测到登录页跳转，含结果参数)"
                        }
                    else:
                        # 可能是无效链接直接跳回登录，但也可能是成功后 session 失效跳转
                        # 对于自动化流程，跳转回 Google 登录页通常视为验证环节已结束
                        log(f"跳转到登录页，视为验证环节结束")
                        return {
                            "completed": True,
                            "success": True, # 乐观假定成功，因为失败通常会停留在 PrivateID 页面或显示错误
                            "message": "验证流程已结束 (跳转回 Google 登录页)"
                        }
                elif "age-verification" in current_url:
                     return {
                        "completed": True,
                        "success": True,
                        "message": "验证流程已完成 (返回年龄验证页面)"
                    }
                else:
                    # 其他 Google 页面，也视为结束
                     return {
                        "completed": True,
                        "success": True,
                        "message": f"验证流程已结束 (跳转至 {current_url})"
                    }

            # 2. 检查 404/错误页面 (优先检查标题)
            try:
                page_title = await self.page.title()
                if "404" in page_title or "Not Found" in page_title:
                     return {
                        "completed": True,
                        "success": False,
                        "message": "链接无效 (404 Not Found)"
                    }
            except:
                pass

            # 3. 检查页面文本 (使用 locator 而不是 evaluate 整个 body，性能更好)
            try:
                # 检查特定元素是否存在，而不是获取全文
                # Google 404 页面通常有特定的结构
                if await self.page.locator("text=404").count() > 0 and await self.page.locator("text=Not Found").count() > 0:
                     return {
                        "completed": True,
                        "success": False,
                        "message": "链接无效 (404 Not Found)"
                    }

                # 获取页面可见文本用于其他检查
                page_text = await self.page.evaluate("document.body.innerText")
            except:
                page_text = ""

            # 检查成功标志
            for text in self.COMPLETION_TEXTS:
                if text.lower() in page_text.lower():
                    return {
                        "completed": True,
                        "success": True,
                        "message": "年龄验证成功完成"
                    }

            # 检查失败标志
            for text in self.FAILURE_TEXTS:
                if text.lower() in page_text.lower():
                    return {
                        "completed": True,
                        "success": False,
                        "message": "年龄验证失败"
                    }

            # 检查是否还在 PrivateID 页面
            if "privateid.com" not in current_url and "google.com" not in current_url:
                 # 如果跑到了其他地方
                 pass

            return {
                "completed": False,
                "success": False,
                "message": "验证进行中"
            }
        except Exception as e:
            # 忽略一些因页面跳转导致的元素访问错误
            if "Target closed" in str(e):
                return {
                    "completed": False,
                    "success": False,
                    "message": "页面已关闭"
                }
            return {
                "completed": False,
                "success": False,
                "message": f"检查状态出错: {str(e)}"
            }

    async def _wait_for_completion(self, log_callback=None) -> Dict[str, Any]:
        """等待验证完成"""
        start_time = asyncio.get_event_loop().time()

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > self.timeout:
                return {
                    "completed": False,
                    "success": False,
                    "message": f"验证超时 ({self.timeout}秒)"
                }

            result = await self._check_completion(log_callback)
            if result["completed"]:
                return result

            await asyncio.sleep(1)

    async def verify(
        self,
        verification_url: str,
        video_file: str = None,
        wait_for_completion: bool = True,
        log_callback=None
    ) -> Dict[str, Any]:
        """
        执行年龄验证
        ...
        """
        def log(msg):
            print(f"[AgeVerify] {msg}")
            if log_callback:
                try:
                    log_callback(msg)
                except:
                    pass

        log("开始年龄验证")
        log(f"URL: {verification_url}")
        log(f"设备: {self.device_config['name']}")
        log(f"摄像头模式: 视频注入 (强制)")

        try:
            # 准备视频文件 (如果需要转码)
            actual_video_file = self._prepare_video_file(video_file)
            if actual_video_file != video_file:
                log(f"视频已转码为临时文件: {actual_video_file}")

            # 检查视频文件有效性
            if not actual_video_file or not os.path.exists(actual_video_file):
                log("警告: 未找到视频文件，将使用 Chrome 内置默认测试视频 (绿色画面)")
            else:
                log(f"使用视频文件: {actual_video_file}")

            # 设置浏览器
            await self._setup_browser(actual_video_file)

            # 导航到验证页面
            log("正在访问验证页面...")
            await self.page.goto(verification_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            # 自动点击按钮
            clicked_buttons = await self._auto_click_buttons(log_callback=log)
            log(f"点击了 {len(clicked_buttons)} 个按钮: {', '.join(clicked_buttons)}")

            if wait_for_completion:
                # 等待验证完成
                log(f"等待验证完成 (最长 {self.timeout} 秒)...")
                result = await self._wait_for_completion(log_callback=log)

                if result['success']:
                    log(f"验证完成: {result['message']}")
                else:
                    log(f"验证结束: {result['message']}")

                return {
                    "success": result["success"],
                    "message": result["message"],
                    "clicked_buttons": clicked_buttons,
                    "verification_url": verification_url
                }
            else:
                return {
                    "success": True,
                    "message": "验证流程已启动，请手动完成",
                    "clicked_buttons": clicked_buttons,
                    "verification_url": verification_url
                }

        except Exception as e:
            log(f"验证出错: {e}")
            return {
                "success": False,
                "message": f"验证出错: {str(e)}",
                "clicked_buttons": [],
                "verification_url": verification_url
            }

    async def close(self):
        """关闭浏览器和 Playwright"""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        print("[AgeVerify] 浏览器已关闭")

        # 清理临时文件
        if self._temp_files:
            print(f"[AgeVerify] 清理 {len(self._temp_files)} 个临时文件...")
            for path in self._temp_files:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception as e:
                    print(f"[AgeVerify] 清理临时文件失败 {path}: {e}")
            self._temp_files = []


def run_age_verification(
    verification_url: str,
    use_obs: bool = False,
    video_file: str = None,
    device: str = "pixel_7_pro",
    headless: bool = True,
    timeout: int = 60,
    wait_for_completion: bool = True
) -> Dict[str, Any]:
    """
    同步接口 - 执行年龄验证

    Args:
        verification_url: PrivateID 验证链接
        use_obs: 是否使用 OBS 虚拟摄像头
        video_file: Y4M 视频文件路径（非 OBS 模式）
        device: 设备配置名称
        headless: 是否无头模式
        timeout: 验证超时时间（秒）
        wait_for_completion: 是否等待验证完成

    Returns:
        dict: 验证结果
    """
    async def _run():
        service = AgeVerificationService(
            use_obs=use_obs,
            device=device,
            headless=headless,
            timeout=timeout
        )
        try:
            result = await service.verify(
                verification_url=verification_url,
                video_file=video_file,
                wait_for_completion=wait_for_completion
            )
            return result
        finally:
            await service.close()

    return asyncio.run(_run())


# 命令行测试
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="年龄验证服务测试")
    parser.add_argument("--url", "-u", required=True, help="PrivateID 验证链接")
    parser.add_argument("--obs", action="store_true", help="使用 OBS 虚拟摄像头")
    parser.add_argument("--video", "-v", help="Y4M 视频文件路径")
    parser.add_argument("--device", "-d", default="pixel_7_pro",
                       choices=["pixel_7_pro", "galaxy_s23", "iphone_15"],
                       help="设备配置")
    parser.add_argument("--show-browser", action="store_true", help="显示浏览器窗口（有头模式）")
    parser.add_argument("--timeout", "-t", type=int, default=60, help="超时时间（秒）")
    parser.add_argument("--no-wait", action="store_true", help="不等待验证完成")

    args = parser.parse_args()

    result = run_age_verification(
        verification_url=args.url,
        use_obs=args.obs,
        video_file=args.video,
        device=args.device,
        headless=not args.show_browser,
        timeout=args.timeout,
        wait_for_completion=not args.no_wait
    )

    print("\n" + "=" * 60)
    print("验证结果:")
    print(f"  成功: {result['success']}")
    print(f"  消息: {result['message']}")
    print(f"  点击按钮: {result.get('clicked_buttons', [])}")
    print("=" * 60)
