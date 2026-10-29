# OpenClaw 薄壳插件

> 在 AstrBot 中一键部署并托管 OpenClaw（俗称“小龙虾”）智能体网关，覆盖安装、启动、任务派发、停止与残存清理的完整流程，生命周期与 AstrBot 严格绑定。

| 项目 | 说明 |
| --- | --- |
| 插件名 | `astrbot_plugin_openclaw` |
| 当前版本 | v1.1.1 |
| 运行平台 | Windows 10 / 11（x64） |
| 依赖 | AstrBot >= 4.24.0 |
| 通信方式 | 本地回环 HTTP（`127.0.0.1`） |
| 作者 | 鲸鱼娘 |

---

## 一、概述

OpenClaw 是一个独立的 Node.js 智能体网关，本身并非 AstrBot 插件。本插件以“薄壳”形式将其接入 AstrBot，使 AstrBot 的聊天入口与 LLM 工具能够直接向 OpenClaw 派发任务，同时保证：

- 安装过程无需手动操作 Node.js、npm 与环境变量；
- OpenClaw 的启停与 AstrBot 同步，不会遗留后台进程；
- 所有调用均受超时、限流与熔断护栏约束，避免进程互相调用造成的死循环。

---

## 二、功能特性

| 特性 | 说明 |
| --- | --- |
| 一键部署 | 单条指令完成脚本铺设、便携 Node 下载、openclaw 安装、配置写入与服务启动 |
| 会话派活 | 在私聊或群聊中以指令向 OpenClaw 下发任务，结果分段回传 |
| 生命周期绑定 | AstrBot 启动时自动拉起，关闭时自动停止；异常退出由看门狗兜底回收 |
| 进程看门狗 | 独立守护进程监控 AstrBot 父进程，父进程消失后 5 秒内完成清理 |
| 运行护栏 | 单次超时、任务长度、回复长度、去重、每小时配额、并发数、失败熔断、关键词拦截 |
| 环境隔离 | 便携版 Node 与依赖均安装在独立目录，不修改系统 PATH，不污染全局 npm |
| 可观测性 | 提供状态查询指令，网关、薄壳、看门狗与安装日志均有独立日志文件 |

---

## 三、运行环境

- **操作系统**：Windows 10 / 11（x64）。当前版本仅适配 Windows。
- **AstrBot**：4.24.0 及以上。
- **网络**：安装阶段需访问 Node 镜像与 npm 源；任务派发阶段需访问所配置的模型接口。
- **磁盘**：建议预留 500 MB 以上可用空间。

---

## 四、安装与启用

### 1. 放置插件

将 `astrbot_plugin_openclaw` 整个目录放入 AstrBot 的 `data/plugins/` 下。

### 2. 启用插件

在 WebUI 的插件管理中启用本插件，并重载插件或重启 AstrBot。

### 3. 填写配置

在插件配置页至少填写以下三项：

| 配置项 | 说明 |
| --- | --- |
| `provider_base_url` | OpenAI 兼容的模型接口地址，例如 `https://example.com/v1` |
| `provider_api_key` | 模型接口密钥 |
| `provider_model` | 模型 ID，例如 `glm-5.3-flash` |

建议同时填写 `owner_ids`（本人 QQ 号），以免他人调用并消耗密钥额度。

### 4. 执行安装

在私聊中向机器人发送：

```
/claw_install
```

插件将依次执行：铺设脚本 → 下载便携 Node（优先 npmmirror，失败回退官方源）→ 安装 openclaw → 写入 `openclaw.json` → 启动网关与薄壳。安装完成后机器人会主动回执一条消息。

安装日志位于 `<install_dir>\logs\install.log`。

---

## 五、配置项说明

### 基础

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 插件总开关 |
| `bridge_url` | string | `http://127.0.0.1:18790` | 薄壳服务地址，需与 `bridge_port` 保持一致 |
| `default_model` | string | 空 | 默认模型；留空则沿用 `provider_model` |
| `auto_start` | bool | `true` | 薄壳未运行时自动拉起 |
| `start_script` | string | 空 | 兜底启动脚本路径 |

### 模型

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `provider_base_url` | string | 空 | 模型接口地址，安装时写入 `openclaw.json` |
| `provider_api_key` | string | 空 | 模型接口密钥 |
| `provider_model` | string | 空 | 模型 ID |

### 权限与安全

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `owner_only` | bool | `true` | 仅允许主人派发任务 |
| `owner_ids` | list | 空 | 主人 QQ 号列表 |
| `allow_group` | bool | `false` | 是否允许群聊中派发任务 |
| `llm_tool_enable` | bool | `true` | 是否向 LLM 开放 `openclaw_task` / `openclaw_setup` 工具 |

### 护栏参数

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `task_timeout` | int | `120` | 单次任务超时（秒），最小 10 |
| `max_prompt_chars` | int | `4000` | 任务文本长度上限（字），最小 100 |
| `reply_chunk` | int | `1200` | 回复分段长度（字），便于长文本分条发送 |

### 安装与启动

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `install_dir` | string | 空 | 安装目录。留空时默认为 `<AstrBot data>\openclaw`；若 AstrBot 位于系统盘，则自动改选剩余空间最大的非系统盘（`<盘符>:\AstrBot\openclaw`） |
| `prewarm` | bool | `true` | AstrBot 启动时即拉起 OpenClaw |
| `stop_on_exit` | bool | `true` | AstrBot 关闭时停止 OpenClaw 并清理残存 |
| `auto_install` | bool | `false` | 启动时若检测到未安装，自动执行安装 |
| `node_version` | string | `v24.22.2` | 便携 Node 版本 |
| `node_mirror` | string | `https://npmmirror.com/mirrors/node` | Node 下载镜像 |
| `npm_registry` | string | `https://registry.npmmirror.com` | npm 源 |

### 端口

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `gateway_port` | int | `18789` | OpenClaw 网关端口 |
| `bridge_port` | int | `18790` | 薄壳服务端口，修改后需同步调整 `bridge_url` |

---

## 六、指令与工具

### 聊天指令

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `/claw <任务>` | `/小龙虾 <任务>` | 向 OpenClaw 派发任务 |
| `/claw_install` | `/安装小龙虾` | 安装整套环境并启动服务 |
| `/claw_start` | `/开小龙虾` | 启动网关与薄壳（含看门狗） |
| `/claw_stop` | `/关小龙虾` | 停止全部服务并清理残存 |
| `/claw_status` | `/小龙虾状态` | 查看安装完整性、薄壳在线状态与看门狗状态 |

### LLM 工具

| 工具名 | 参数 | 说明 |
| --- | --- | --- |
| `openclaw_task` | `prompt` | 由模型自动调用，向 OpenClaw 派发任务 |
| `openclaw_setup` | `action` | 由模型自动调用，执行 `install` / `start` / `stop` / `status` |

---

## 七、运行架构

调用链路如下：

```
用户消息 / LLM 工具
        ↓
插件 main.py（权限校验、参数与长度校验）
        ↓  HTTP
薄壳 bridge（127.0.0.1:18790，护栏与去重）
        ↓
OpenClaw 网关（127.0.0.1:18789）
        ↓
模型接口（provider_base_url）
```

### 组件职责

| 组件 | 文件 | 职责 |
| --- | --- | --- |
| 插件主程序 | `main.py` | 指令注册、权限校验、调用薄壳、回复分段 |
| 安装器 | `installer.py` | 下载 Node、安装 openclaw、生成配置、部署脚本 |
| 薄壳服务 | `bridge/bridge.py` | 任务转发、护栏执行、并发与熔断控制 |
| 服务管家 | `svc.py` | 统一执行 `start` / `stop` / `status` |
| 看门狗 | `lifecycle.py` | 监控 AstrBot 父进程，退出后回收全部子进程 |
| 全链路自检 | `selftest.py` | 校验安装目录、端口与进程状态 |

### 生命周期

| AstrBot 状态 | 插件行为 |
| --- | --- |
| 启动 | 按 `prewarm` 配置拉起网关与薄壳，并启动看门狗 |
| 正常运行 | 薄壳不在线时按 `auto_start` 自动重启 |
| 正常关闭 | 按 `stop_on_exit` 配置停止服务并清理残存 |
| 异常崩溃 | 看门狗检测到父进程消失后 5 秒内完成清理，不遗留孤儿进程与锁文件 |

---

## 八、目录结构

安装完成后目录结构如下（`<install_dir>` 默认与 `plugins` 同级）：

```
<install_dir>\
├─ node-v24.22.2-win-x64\   便携 Node 运行时（不修改系统 PATH）
├─ npm-global\              openclaw 及其依赖
├─ home\.openclaw\          openclaw.json 等网关配置
├─ bridge\                  薄壳程序 bridge.py 与 config.json
├─ svc.py                   服务管家（start / stop / status）
├─ lifecycle.py             看门狗
├─ logs\                    gateway.log、bridge.log、install.log
├─ run\                     运行期 pid 与 lock（停止时自动清理）
├─ work\                    任务临时工作目录
├─ start_openclaw.bat       手动启动
└─ stop_openclaw.bat        手动停止并清理
```

---

## 九、运行护栏

薄壳层内置以下限制，防止资源滥用与进程互调：

| 护栏项 | 默认值 | 说明 |
| --- | --- | --- |
| 任务长度上限 | 6000 字 | 超出直接拒绝 |
| 回复长度上限 | 4000 字 | 超出截断 |
| 单次执行超时 | 120 秒（上限 300 秒） | 到时强制终止 |
| 重复任务拦截 | 90 秒 | 相同任务在窗口内只执行一次 |
| 每小时配额 | 20 单 | 超限返回 429 |
| 并发上限 | 1 | 同时仅允许一个任务在跑 |
| 失败熔断 | 连续 3 次失败后冷却 600 秒 | 冷却期内拒绝新任务 |
| 关键词拦截 | 内置列表 | 拦截自调用、循环调用等高风险任务 |

---

## 十、日志与排障

| 现象 | 排查方向 |
| --- | --- |
| 安装失败 | 查看 `<install_dir>\logs\install.log`，多为网络或镜像问题 |
| 薄壳未在线 | 执行 `/claw_status`，再查看 `logs\bridge.log` |
| npm 安装卡住 | 更换 `npm_registry`，或手动执行 `npm install -g openclaw --prefix npm-global` |
| 端口被占用 | 修改 `gateway_port` / `bridge_port`，并同步 `bridge_url` |
| 任务被拦截 | 检查是否触发关键词、小时配额或失败熔断，熔断到期后自动恢复 |

彻底重装流程：执行 `/claw_stop` → 删除 `npm-global` 与 `home\.openclaw` → 再次执行 `/claw_install`。

---

## 十一、已知限制

1. 仅支持 Windows x64，未适配 Linux 与 macOS。
2. 首次安装依赖网络下载，耗时受镜像速度影响。
3. 常驻网关会占用一定内存（实测约 1.3 GB），启动阶段短时占用 CPU。
4. 任务为同步等待模式，长任务会占用会话直至超时。
5. OpenClaw 版本需不低于 2026.1.29，以规避 CVE-2026-25253 远程执行风险。
6. 服务仅监听本地回环地址，请勿将端口映射至公网。

---

## 十二、卸载

1. 执行 `/claw_stop`，停止服务并清理残存。
2. 在 WebUI 中停用并移除本插件。
3. 如需彻底清除，删除 `<install_dir>` 整个目录。

`svc.py stop` 的清理范围：停止看门狗、薄壳与网关进程树（仅限本插件自身进程，不影响其他 Python / Node 进程）；删除 `run\*.pid`、`run\svc.lock`、`.openclaw` 下的锁文件与临时目录；复核 18789 / 18790 端口与进程是否真正释放，结果体现在 `procs_left` 与 `ports_left` 字段中。

---

## 十三、许可

本插件为本地部署工具，未发布至公开仓库。如需分发，请自行确认 OpenClaw 与各依赖的许可条款。
