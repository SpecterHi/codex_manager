# Codex Manager

`codex_manager` 是一个面向 Codex 会话的本地优先监督台。

它解决的不是“agent 能不能干活”，而是：

> “我已经有很多真实的 Codex 会话在跑了，怎么才能稳定地观察、判断、接管、续跑，而不是被一堆客户端和超大的会话文件拖垮？”

[English README](./README.en.md)

## 截图

### 完整页

![完整页](./docs/screenshots/full-manager.png)

### 轻量页

<table>
  <tr>
    <td><img src="./docs/screenshots/remote-mobile-01.png" alt="轻量页 - 总览" width="320" /></td>
    <td><img src="./docs/screenshots/remote-mobile-02.png" alt="轻量页 - 展开状态" width="320" /></td>
  </tr>
</table>

## 它最擅长什么

- **多会话监督**
  - 同时扫很多个 Codex 会话，而不是只盯着一个当前对话
- **有界的实时观察**
  - 看最近事件窗口、过程说明、工具调用、工具输出摘要、完成标记
  - 默认不去加载整份超大的 `.jsonl`
- **适合手机介入**
  - `/remote` 轻量页可以在离开电脑时看进度、继续推进、停止网页续跑
- **保守的“持续推进”**
  - 只在明确出现 `task_complete` 后再继续
  - 不靠“空闲了几分钟”这种猜测
  - 有单实例巡检锁，避免双重巡检
- **小规模多机控制**
  - 一个控制面切换几台 Codex 主机
  - 通过 `ssh + sudo -n` 自举远端辅助服务，不用手工长期维护第二份副本
- **更安全的远端接管**
  - 接管前先检查远端服务、接口能力和版本是否兼容

## 它不是什么

`codex_manager` 并不是想替代所有相关工具。

- 如果你主要需要的是**一个主力编码线程的官方体验**，包括 diff review、worktree 驱动的开发流程，那官方 Codex app 更合适。
- 如果你主要想要的是**手机远程接管一个 agent**，像 [Happy](https://github.com/slopus/happy) 这种产品更贴。
- 如果你的主入口是**飞书 / Slack / Telegram / Discord / 企业微信** 等聊天平台，那 [cc-connect](https://github.com/chenhg5/cc-connect) 更合适。

`codex_manager` 针对的是另一类问题：

> “我已经有一批真实会话在本机、WSL 或远端机器上运行，我需要一个低内存、低意外、可观察、可插话的值班台。”

## 和同类项目相比，强在哪里

| 工具 | 更擅长什么 | `codex_manager` 的差异 |
| --- | --- | --- |
| [OpenAI Codex app](https://openai.com/index/introducing-the-codex-app/) | 单线程主工作流、官方多智能体体验、diff/worktree 代码工作流 | `codex_manager` 更强在“很多既有会话的监督与值班” |
| [Happy](https://github.com/slopus/happy) | 手机/Web 远控、推送提醒、跨设备切换、加密远控体验 | `codex_manager` 不要求你用额外包装器启动智能体，更适合观察已经存在的会话资产 |
| [cc-connect](https://github.com/chenhg5/cc-connect) | 把本地智能体接入聊天平台，做聊天式运维控制 | `codex_manager` 是浏览器控制面，重点是可观察性和介入，不是聊天入口 |

这里真正的差异点是：

- **避免默认全量吃内存**
  - 核心界面围绕“最近尾部窗口”和“有界实时观察”，而不是整份对话一次性载入
- **保守的监督逻辑**
  - `持续推进` 只在明确完成后接一句，不做模糊猜测
- **多浏览器共享目标机器**
  - 远端机器配置存在服务端，不用每个浏览器重新加一遍
- **远端自举和版本检查**
  - 不是只会“连”，还会判断能不能安全接管

## 当前产品形态

目前主要有两个入口：

- `codex_sessions.py`
  - CLI，负责列会话、看详情、改标题、归档、删除、续跑等
- `codex_sessions_web.py`
  - Web UI + JSON API
  - 完整页：`/`
  - 轻量页：`/remote`

### 完整页 `/`

完整页是一个双栏监督台：

- **左边**：会话总览列表
  - 快速扫会话
  - 看状态、最近进展、注意力提示、轻量元数据
- **右边**：当前会话的实时控制台
  - 过程说明
  - 工具调用
  - 工具输出摘要
  - token / 完成标记
  - 真要看历史时再显式加载

重点不是“像聊天软件一样翻完整记录”，而是“判断现在发生了什么”。

### 轻量页 `/remote`

轻量页是手机/平板值班页：

- 已关注会话
- 快速筛选
- 最近交付/结果预览
- 一键继续
- 一键停止网页触发的续跑
- 可选 `持续推进`

`持续推进` 的原则是：

- 每 3 分钟检查一次
- 只在显式 `task_complete` 后接一句
- 记住已经续跑过的 completed turn
- 用巡检锁避免两个实例同时巡检

## 远端机器模型

远端不是“纯 SSH 生读文件”模式，而是：

- 控制面机器负责出 UI
- 每台目标机器各自跑一个只绑定本机回环地址的 `codex_manager`
- 控制面通过 SSH 代理 API 动作到目标机器

这样做是刻意的。因为一旦你真的需要这些能力：

- 会话列表
- 最近事件窗口
- 历史兜底
- 继续 / 停止
- 保守的自动续推
- 兼容性检查

直接靠控制面用 SSH 临时解析远端文件，反而更复杂、更脆弱。

### 远端自举

如果远端满足：

- SSH 可访问
- `sudo -n true`
- `python3`
- `curl`
- `tar`

就可以直接从当前本地 checkout 自举：

```bash
python codex_sessions_bootstrap.py \
  --host remote-host \
  --user your-ssh-user \
  --label "Remote box" \
  --bind-port 8765
```

它会自动：

- 打包当前仓库
- 通过 SSH 上传
- 安装到 `~/.local/share/codex_manager`
- 写/更新 `systemd` 服务
- 启动远端本机回环服务
- 验证 `http://127.0.0.1:<port>/api/remote_sessions`
- 可选写回本机目标配置

Web UI 还可以在部署前先检查：

- 远端是否已经装了 `codex_manager`
- 服务是否已运行
- API 能力是否齐全（`sessions` / `remote_sessions` / `events`）
- 远端版本和本地版本是否一致

## 认证与暴露模型

Web UI 支持本地密码认证。

常见部署方式是：

- `127.0.0.1 / localhost` 直连可绕过登录
- 非本机回环访问必须登录
- 变更类 API 带 CSRF 保护

示例本地地址：

- `http://127.0.0.1:8765/`
- `http://127.0.0.1:8765/remote`

在这台机器上的常见实际部署方式：

- 本机回环免密
- 局域网 / 隧道访问需要密码
- Linux 侧限制为仅局域网访问
- Windows 侧配对应的 WSL/Hyper-V 放行规则

## 仓库结构

- `codex_sessions.py`
  - CLI 入口
- `codex_sessions_web.py`
  - Web 界面、API、远端代理、监督逻辑
- `codex_sessions_bootstrap.py`
  - `ssh + sudo` 远端自举 / 升级辅助脚本
- `codex_manager_release.py`
  - 发布元数据和版本比较辅助
- `test_codex_sessions_web.py`
  - Web/API 行为测试

## 快速开始

在仓库根目录执行：

```bash
uv run python codex_sessions.py --help
uv run python codex_sessions_web.py --help
```

常见 CLI 用法：

```bash
uv run python codex_sessions.py list --limit 20
uv run python codex_sessions.py show <session-id>
uv run python codex_sessions.py set-alias <session-id> <alias>
uv run python codex_sessions.py set-title <session-id> "Clearer title"
uv run python codex_sessions.py set-source <session-id> vscode
uv run python codex_sessions.py set-workdir <session-id> ~/project
uv run python codex_sessions.py resume <session-id>
uv run python codex_sessions.py resume <session-id> --non-interactive --prompt "Please continue pushing toward a verifiable result."
uv run python codex_sessions.py paths
```

启动 Web UI：

```bash
uv run python codex_sessions_web.py --host 127.0.0.1 --port 8765
```

打开：

- `http://127.0.0.1:8765/`
- `http://127.0.0.1:8765/remote`

## 主要路由与 API

主页面：

- `GET /`
- `GET /remote`
- `GET /login`

读取类 API：

- `GET /api/sessions`
- `GET /api/history`
- `GET /api/events`
- `GET /api/remote_sessions`
- `GET /api/remote_guard`
- `GET /api/progress`
- `GET /api/targets`

变更类 API：

- `POST /api/continue`
- `POST /api/stop`
- `POST /api/set_title`
- `POST /api/clear_title`
- `POST /api/set_source`
- `POST /api/set_workdir`
- `POST /api/archive`
- `POST /api/delete`
- `POST /api/targets`
- `POST /api/targets/delete`

兼容别名仍然保留：

- `POST /api/rename` -> `POST /api/set_title`
- `POST /api/unname` -> `POST /api/clear_title`
- `POST /api/set_cwd` -> `POST /api/set_workdir`

## 开发

基础检查：

```bash
python -m py_compile \
  codex_sessions.py \
  codex_sessions_web.py \
  codex_sessions_bootstrap.py \
  codex_manager_release.py \
  test_codex_sessions_web.py

python -m unittest test_codex_sessions_web.py
```

## 版本控制说明

有些部署是普通 Git 仓库；有些是 `jj + git`；还有些只是运行时副本。

先看清楚当前目录是什么：

```bash
git status
jj status
```

## License

Apache-2.0，见 [LICENSE](./LICENSE)。

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=SpecterHi/codex_manager&type=Date)](https://www.star-history.com/#SpecterHi/codex_manager&Date)
