# Google Age Verifier

一个用于 Google PrivateID 年龄验证流程的自动化服务，提供前端页面和后端 API。

## 功能

- 单个验证与批量验证（前端批量上限 6）
- 公网访问使用短期令牌鉴权（`X-Client-Token`）
- 本地访问（`localhost/127.0.0.1`）可免鉴权调试

## 目录结构

- `age_verification_api.py`：Flask API 服务入口
- `services/age_verification_service.py`：自动化验证核心逻辑
- `frontend/index.html`：前端页面
- `.env`：运行配置

## 快速开始

1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

2. 准备 `.env`

至少确认以下配置：

- `CLIENT_TOKEN_SECRET`：固定随机密钥
- `CLIENT_TOKEN_TTL_SECONDS`：令牌有效期（默认 900）
- `RATE_LIMIT_WINDOW_SECONDS`：限流窗口（默认 60）
- `RATE_LIMIT_MAX_REQUESTS`：窗口内最大请求数（默认 20）
- `TURNSTILE_SECRET_KEY`：建议配置（公益站）

3. 启动服务

```bash
python age_verification_api.py --host 0.0.0.0 --port 5001
```

访问：

- `http://<host>:5001/`

## 使用方法

### 单个验证

1. 打开首页
2. 粘贴一个验证链接
3. 点击 `Start Verification`
4. 页面会显示当前状态：`Ready / Queued / Running / Success / Failed`

### 批量验证

1. 切换到 `Batch`
2. 每行粘贴一个链接（最多 6 个）
3. 点击 `Start Batch Verification`
4. 页面显示整体进度与当前状态

## 鉴权说明

- 前端会自动调用 `POST /auth/client-token` 获取短期令牌
- 之后请求自动携带 `X-Client-Token`
- 默认不需要前端输入 API Key

## 视频文件配置

- 默认视频文件名：`546a131c1b386aa2856940da0f8737b2.mp4`
- 默认读取位置（按优先级）：
  1. 项目根目录同名文件
  2. 当前工作目录同名文件
  3. 代码中的旧兼容路径（如存在）
- 也可在请求体中显式传 `video_file` 指定绝对/相对路径

示例：

```json
{
  "url": "https://age-verification.privateid.com/...",
  "video_file": "F:/path/to/your.mp4",
  "async": true
}
```

- 服务会在首次使用时将视频转换并缓存到 `.video_cache/`，后续直接复用
- 请勿将隐私视频提交到仓库（已通过 `.gitignore` 忽略常见视频格式）

## 部署注意事项

- 生产环境建议无头运行（默认 `headless=true`）
- 建议使用反向代理（Apache/Nginx）+ HTTPS
- 请保护好 `.env`，不要公开 `CLIENT_TOKEN_SECRET`
