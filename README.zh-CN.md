# Magpie 服务器部署助手

> [English](README.md) | 简体中文

参照 `cliproxyapi-helper`，为 [Magpie](https://github.com/yetone/magpie) 提供 Ubuntu 服务器一键安装、后台运行、IP HTTPS 证书及卸载脚本。管理页默认只通过 SSH 隧道访问。

## 环境要求

- Ubuntu，使用 systemd；具有 root 权限；CPU 为 `amd64` 或 `arm64`。
- 公网 IPv4 直接绑定在服务器网卡上；不支持仅通过 NAT 映射的主机和域名证书。
- 本机端口 `80`、`443` 空闲。公网 TCP `80` 必须持续可达，用于申请和续期证书；TCP `443` 提供模型接口。[`--local-only`](#作为-cliproxyapi-的上游) 模式不需要公网地址和证书端口。
- 可以访问 GitHub、Ubuntu 软件源、Snap Store 和 Let's Encrypt；`--local-only` 只需要 GitHub 和 Ubuntu 软件源。

管理页独立使用 `3430` 端口，默认只监听 `127.0.0.1`。脚本不会修改主机防火墙、云防火墙或安全组。

## 快速安装

把文档示例地址 `203.0.113.10` 替换为服务器真实公网 IPv4，在保存脚本的本机运行：

```bash
ssh root@203.0.113.10 'bash -s -- --ip 203.0.113.10' < ./install-magpie.sh
```

安装器默认下载固定版本 `v0.1.1268` 的无桌面二进制，通过官方 GitHub 发布信息中的 SHA256 校验后安装；创建专用系统用户 `magpie`，用 Certbot 申请 IP HTTPS 证书，启动并启用开机自启的 `magpie.service`。

安装结束前检查 HTTPS、模型接口和管理页拒绝未认证请求、管理密钥可以登录、新装的客户端密钥可以访问模型列表，并执行证书续期预演。`--local-only` 模式改为检查：带转发头的本机请求能访问模型接口，管理页拒绝未认证请求并接受管理密钥。任何检查失败均以非零状态退出；不会自动撤销已经完成的安装步骤。

### 通过 SSH 管理

在自己的电脑运行并保持连接：

```bash
ssh -N -L 3430:127.0.0.1:3430 root@203.0.113.10
```

浏览器打开 `http://127.0.0.1:3430`。在交互式 SSH 会话中，用 `sudoedit` 私下查看 `/etc/magpie/service.env`，把 `MAGPIE_WEB_KEY` 的值填入页面的密钥输入框。管理页可以修改设置并查看供应商密钥；不要把密钥或整个文件复制到日志、issue、聊天记录或版本控制。

随后在页面中添加供应商、登录订阅。若订阅登录后跳转到了自己电脑的 localhost 页面，将浏览器最终地址复制到 Magpie 的 **Callback URL** 输入框完成登录。

也可以在服务器上用 Magpie CLI 添加。需以服务用户身份、并带上服务的 HOME 与 XDG 目录运行，才能写入服务读取的同一份配置。例如 GitHub Copilot 订阅会在终端打印设备验证码：

```bash
sudo runuser -u magpie -- env $(sudo grep -E '^(HOME|XDG_[A-Z_]+)=' /etc/magpie/service.env) \
  /usr/local/bin/magpie accounts add copilot
sudo systemctl restart magpie
```

API key 类供应商用同样方式运行 `magpie provider add <preset> <key>`。CLI 添加的供应商和账号要等服务重启后才会加载。

### 作为 CLIProxyAPI 的上游

同一台服务器的 TCP `443` 已由 `cliproxyapi-helper` 部署的 CLIProxyAPI 占用时，用 `--local-only` 安装 Magpie。客户端仍然只连接 CLIProxyAPI，Magpie 不对外开放：`client → CLIProxyAPI (https://<ip>/v1) → Magpie (http://127.0.0.1:3425/v1)`。

```bash
ssh root@203.0.113.10 'bash -s -- --local-only' < ./install-magpie.sh
```

该模式不需要 `--ip`，不占用 `80`/`443`，不申请证书，也不安装 Certbot。模型接口只以 HTTP 监听 `127.0.0.1:3425`；管理页仍在 `127.0.0.1:3430`，按上文通过 SSH 隧道访问。安装器会设置 `MAGPIE_TRUST_PROXY=1`，因此即使 CLIProxyAPI 经本机转发的请求带有转发头，也按本机请求处理；客户端鉴权由 CLIProxyAPI 负责。和 Magpie 默认安装一样，本机任何进程都能调用这个回环接口。`--local-only` 不能与 `--ip` 或 `--enable-remote-management` 同用；在公网模式的安装上改用该模式时，管理页回到仅本机访问，原有证书及其续期保留，需要时自行用 Certbot 删除。

安装器不修改 CLIProxyAPI。在 Magpie 中添加供应商后，可使用下文的[模型同步脚本](#同步模型到-cliproxyapi)，或于服务器上运行 `curl -s http://127.0.0.1:3425/v1/models` 查看模型名，再在 CLIProxyAPI 管理页或 `config.yaml` 中添加。`config-version: 8` 的配置需放在 `api-keys` 下：

```yaml
api-keys:
  openai-compatibility:
    - name: "magpie"
      base-url: "http://127.0.0.1:3425/v1"
      keys:
        - api-key: "<内容取自 /etc/magpie/client.key>"
      models:
        - name: "copilot/gpt-5.5"           # Magpie 中的模型名
          alias: "copilot/gpt-5.5"          # 客户端请求时使用的名称
```

没有 `config-version: 8` 的旧配置把 `openai-compatibility` 放在顶层。想给模型加命名前缀时，直接在每个 `alias` 中写完整名称，不要设置 `prefix`：CLIProxyAPI 仍会同时公布不带前缀的别名，可能与其他上游的模型重名。修改后应检查 CLIProxyAPI 的 `/v1/models`；原子替换文件可能未被自动重载检测到。同步脚本应用新增项时会显式重启 CLIProxyAPI。

Magpie 在本机回环上接受任意令牌，但使用初始网关密钥可以把用量归到具名身份。客户端继续使用 CLIProxyAPI 原来的地址和 API key。

### 同步模型到 CLIProxyAPI

`sync-magpie-models.py` 从已配置的 Magpie 供应商读取 `base-url`，查询实时模型清单，批量补入缺失映射。支持版本 8 和旧版配置结构。已有别名、上游已不再列出的旧映射、注释和无关配置保留。脚本不修正旧模型名称、不创建供应商：先按上文配置名为 `magpie` 的条目，空列表可写 `models: []`，供应商级别不要设置 `prefix`。共享的 YAML 模型引用需先展开为独立列表。

先在**目标服务器**安装同步脚本唯一的额外依赖：

```bash
ssh root@203.0.113.10 'apt-get update && apt-get install -y python3-yaml'
```

预演选定的模型组，不写文件、不重启服务：

```bash
ssh root@203.0.113.10 'python3 - --prefix qoder/ --prefix opencode-go/' < ./sync-magpie-models.py
```

确认输出后，应用同样的新增项：

```bash
ssh root@203.0.113.10 'python3 - --prefix qoder/ --prefix opencode-go/ --apply' < ./sync-magpie-models.py
```

省略 `--prefix` 会包含所有上游模型。重复执行只补缺失的上游名称、保留已有别名；没有新增项时不创建备份、不重启。`--apply` 需要 root：先在配置旁保存仅所有者可读的 `config.yaml.bak-*`，保留原配置所有者与权限，写入后重启 CLIProxyAPI，再检查旧模型名称和新增项是否均已公布。重启会短暂中断请求。重启或验证失败时恢复旧配置并再次重启；检测到其他程序修改配置时，保留该修改并报告备份路径。运行期间应确保没有其他工具或人员同时编辑此配置。检查发生在每次实际替换前和验证后，不能与任意外部写入者做原子协作。验证模型列表不发送推理请求。密钥只在服务器进程内使用，不打印；HTTPS 校验证书，带认证的请求不跟随跳转。

| 选项 | 默认值 | 用途 |
|---|---|---|
| `--config` | `/var/lib/cli-proxy-api/config.yaml` | 已有的私有 CLIProxyAPI YAML 配置 |
| `--provider` | `magpie` | 要扩展的 `openai-compatibility` 条目 |
| `--prefix` | 所有模型 | 模型 ID 的字面前缀，可重复传入选择多组 |
| `--api-base-url` | 从配置推导 | CLIProxyAPI 客户端地址，如 `https://203.0.113.10/v1`；TLS 监听地址与证书不匹配时需要显式指定 |
| `--service` | `cli-proxy-api.service` | `--apply` 重启的 systemd 服务 |
| `--apply` | 关闭，仅预演 | 备份、写入、重启并验证 |
| `--json` | 关闭 | 输出 JSON 结果；错误仍输出到 stderr |

版本 8 的客户端密钥读取自 `access.api-keys`，旧版读取顶层 `api-keys` 列表，无需把密钥写在命令里。退出码：`0` 表示预演成功、无需变更或更新已验证；`1` 表示操作失败；`2` 表示参数错误。需要人工恢复时私下查看输出的备份路径并还原，不要将备份内容复制到日志或版本控制。

### 连接本地编码工具

把客户端模型接口地址设置为 `https://203.0.113.10/v1`，并使用已启用的网关密钥。首次安装生成的客户端密钥保存在仅 root 可读的 `/etc/magpie/client.key`，同样应私下读取。更多密钥及其访问限制、撤销操作在 **Gateway → Gateway keys** 中管理。

Magpie 支持 OpenAI Chat、OpenAI Responses 和 Anthropic Messages；按客户端要求填写相应接口。服务器不会自动发现或修改其他电脑上的编码工具，实际模型调用还需要先配置供应商。

## 安装选项

| 选项 | 默认值 | 说明 |
|---|---|---|
| `--ip <public-ipv4>` | 未用 `--local-only` 时必填 | 证书身份和模型接口绑定地址 |
| `--local-only` | 关闭 | 模型接口只以 HTTP 监听 `127.0.0.1`，不申请证书，作为本机代理的上游 |
| `--gateway-port <port>` | `443`（`--local-only` 时为 `3425`） | 模型接口端口 |
| `--management-port <port>` | `3430` | 独立管理页面端口 |
| `--enable-remote-management` | 新装时关闭 | 管理页绑定公网 IP，以 HTTPS 访问 |
| `--disable-remote-management` | — | 管理页改为仅本机访问，通过 SSH 连接 |
| `--magpie-version <tag\|latest>` | `v0.1.1268` | 指定发布版本，或获取最新稳定版本 |
| `--acme-email <email>` | 未设置 | Let's Encrypt 账户邮箱 |
| `-h`, `--help` | — | 显示帮助，不修改主机 |

取值优先级：命令行 > 环境变量 `MAGPIE_VERSION`、`ACME_EMAIL` > 内置默认值。两个服务端口不能相同，也不能使用证书续期保留的 `80`。

如需管理页直接接受远程连接，必须显式开启：

```bash
ssh root@203.0.113.10 \
  'bash -s -- --ip 203.0.113.10 --enable-remote-management' < ./install-magpie.sh
```

开启后地址为 `https://203.0.113.10:3430`，持有管理密钥的人可以修改配置并查看供应商密钥。只有计划直接访问管理页时，才在自己的网络策略中放行对应端口；通过 SSH 管理不需要这个选项。

## 重装、升级和恢复

重复执行安装命令即可重装；加 `--magpie-version <tag>` 指定升级版本。没有管理开关时保留原来的管理方式和管理密钥；供应商配置、账号、用量及网关密钥保留，不接管其他方式安装的 Magpie。

撤销初始网关密钥后，重装不会重新启用它；此时 `/etc/magpie/client.key` 是旧副本，应使用页面创建的新密钥。

程序和运行环境由 root 管理，应使用本安装器升级，而非浏览器里的更新按钮。升级前私下备份 `/etc/magpie` 和 `/var/lib/magpie`；旧版 Magpie 不一定兼容新版数据。安装中断时先查看日志，解决报错后重新运行安装器；不提供自动降级或数据回滚。

```bash
ssh root@203.0.113.10 'systemctl status magpie.service --no-pager'
ssh root@203.0.113.10 'journalctl -u magpie.service -n 100 --no-pager'
```

Magpie 的启动输出包含管理登录链接，因此服务丢弃标准输出，标准错误仍写入系统日志。证书续期后自动复制证书，Magpie 会重新读取，无需重启服务。

## 文件位置

| 路径 | 用途 |
|---|---|
| `/usr/local/bin/magpie` | root 管理的命令行程序 |
| `/etc/systemd/system/magpie.service` | 以 `magpie` 用户运行的系统服务 |
| `/etc/magpie/service.env` | 仅 root 可读的管理密钥及运行环境 |
| `/etc/magpie/client.key` | 初始客户端密钥的仅 root 可读副本，如有生成 |
| `/etc/magpie/tls/` | 服务可读取的证书副本（`--local-only` 不使用） |
| `/var/lib/magpie/` | 私有 HOME、账号、配置、缓存、数据和状态 |
| `/etc/letsencrypt/live/magpie-<ip>/` | Certbot 管理的证书（`--local-only` 不创建） |
| `/etc/letsencrypt/renewal-hooks/deploy/50-magpie` | 证书续期后复制证书的脚本（`--local-only` 不创建） |

## 卸载

先预演完整删除，确认目标：

```bash
ssh root@203.0.113.10 'bash -s -- --purge --dry-run' < ./uninstall-magpie.sh
```

卸载程序、后台服务和续期复制脚本，保留账号和数据：

```bash
ssh root@203.0.113.10 'bash -s -- --yes' < ./uninstall-magpie.sh
```

**不可恢复的删除**：额外传入 `--purge` 会永久删除 `/etc/magpie`、`/var/lib/magpie` 及专用用户和组，包含供应商账号、密钥和用量。先备份需要的数据。交互运行且不带 `--yes` 时，需要输入 `uninstall`；非交互执行必须传 `--yes`。

所有卸载模式都保留 Certbot、续期定时器和 `/etc/letsencrypt` 下的全部证书，包括 `magpie-<ip>`。确认没有其他服务使用后，再单独通过 Certbot 删除证书；helper 不删除这个共享证书目录。

## 本地验证

先创建包含同步依赖的本地测试环境：

```bash
python3 -m venv /tmp/magpie-helper-venv
/tmp/magpie-helper-venv/bin/python -m pip install -r requirements.txt
```

```bash
bash -n install-magpie.sh uninstall-magpie.sh
shellcheck install-magpie.sh uninstall-magpie.sh
/tmp/magpie-helper-venv/bin/python -m unittest discover -s tests -v
```

测试使用临时目录并替换主机操作，不安装软件、不修改真实服务、不申请公网证书。Ubuntu 主机完整安装和 Let's Encrypt 签发需要真实目标环境，应另行验证。

依据：[Magpie 部署参考](https://github.com/yetone/magpie/blob/main/docs/reference.md#docker)、[官方发布包](https://github.com/yetone/magpie-releases/releases)、[Let's Encrypt 的 IP 证书与 Certbot 说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot/)。

## 许可证

[MIT](LICENSE)
