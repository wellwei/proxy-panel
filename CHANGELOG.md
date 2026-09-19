# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.2.0] — 2026-09-19

支持挂在反向代理的子路径下，以及为此必需的防护措施。

### 新增

- **子路径挂载** — 页面里的 API 基址改为从 `location.pathname` 推导，
  可挂在 `https://example.com/panel/` 这类路径下（代理负责剥前缀）。
  直接跑在根路径的行为不变。
- **安全响应头** — `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、
  `Referrer-Policy: no-referrer`

### 安全

- **CSRF 闸门** — 状态变更接口（POST）现在要求带 `X-Panel-Request: 1` 头。
  背景：面板常被放在带认证的反代后面，而认证凭据是浏览器自动附带的，
  跨站页面也能触发 POST 且不需要预检；自定义头会强制预检，被 CORS 挡下。
  浏览器内正常点击不受影响；脚本调用需显式加该头。**这是一处破坏性变更**，
  升级后调用面板 API 的脚本要跟着改。

### 工程

- 测试：48 → 51 个（新增 CSRF 拒绝、安全响应头、API 基址为相对路径三项）
- 冒烟测试的 `_post` 辅助函数默认带上 CSRF 头，并新增 `csrf=False` 以验证拒绝路径
- 修正 README 里过时的测试计数（原文写 31，实际 48）

## [0.1.0] — 2026-09-18

首个版本。

### 新增

- **账号池总览** — 账号数 / CN·Global 分布 / 健康数 / 异常数
- **账号卡片** — 区域标签、积分进度条、成功次数、连续失败、冷却 / 熔断 / 降级状态
- **双位状态展示** — 区分「手动停用」与「自动禁用」，叠加态分别标注
- **模型限流台账** — 逐账号列出被限流的模型与恢复时间
- **模型目录** — 按 `cn:` / `global:` 分列，档位模型（快速/均衡/极致）高亮，
  点名字复制的字符串自带区域前缀
- **请求统计** — 按模型聚合请求数、成功率、平均延迟、tokens、缓存命中率、计费
- **下游接入信息** — Base URL + API Key 一键复制，附可运行 curl 示例
- **临时停用 / 恢复** — 走网关管理端点；未开启时给出开启指引
- **新增账号** — CN / Global 双入口，只展示登录链接、不自动打开窗口
- **实时积分 / CN 签到 / 领 Global 试用** — 调用网关自带 CLI

### 工程

- 零依赖：仅 Python 标准库（3.8+），无需 pip / 编译 / 构建
- 配置发现：命令行 > 环境变量 > 面板配置 > 自动探测网关 `config.json`
- 能力探测：检测不到网关 CLI 时禁用对应按钮并说明原因与替代方案
- 测试：36 个单元测试 + 一个可复用的 stub 网关（`tests/stub_gateway.py`）
- CI：Python 3.8 / 3.10 / 3.12 / 3.13 矩阵 + 真实启动冒烟测试
- Docker：alpine 基础镜像，非 root 运行，带 healthcheck

[0.2.0]: https://github.com/wellwei/wb2a-panel/releases/tag/v0.2.0
[0.1.0]: https://github.com/wellwei/wb2a-panel/releases/tag/v0.1.0
