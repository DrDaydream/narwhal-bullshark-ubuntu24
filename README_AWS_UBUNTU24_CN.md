# Ubuntu 24.04：Bullshark 的 AWS 五区域测试

本仓库来自 MystenLabs/narwhal 的 Bullshark 分支。云端测试使用本仓库 `main`，保持原有 Rust 共识实现、Rust **1.60.0** 和 Cargo.lock，采用 GCC/G++ 12、Clang/libclang 14。不要套用 Orca-B 的 Rust 1.85.0 安装配置，也不要运行 `cargo update`。

一台 Ubuntu 24.04 amd64 控制机负责调用 AWS API、SSH 调度、生成配置、下载和解析日志；20 台 Ubuntu 24.04 amd64 实验机分布在五个 region，每区 4 台。每台实验机运行一个 primary、一个 worker 和一个负载客户端。控制机也需要编译，用于生成节点密钥。

默认 `fab remote` 的参数：

| 参数 | 默认值 |
|---|---|
| 节点数 / worker 数 | 20 / 每节点 1 个，与 primary 同机 |
| 故障节点 | 0 |
| 总输入速率 | 50,000 tx/s，分摊到客户端 |
| 交易大小 | 512 B |
| 运行时间 / 重复次数 | 80 秒 / 1 次 |
| Header / batch 大小 | 1,000 B / 500,000 B |
| 最大 header / batch 等待 | 200 ms / 200 ms |

安装、下载、编译时间另计。Header 保留这份 Bullshark 实现的默认值；要与 Orca-B 比较，需要记录并对齐工作负载、节点分布及各实现的组批参数。

## 1. 获取控制机代码和 Python 依赖

以下命令约定控制机目录为 `/home/ubuntu/experiments/narwhal-bullshark-ubuntu24`。如果已经克隆到其他位置，将每段 `cd` 路径替换成实际目录。不要进入内层 `benchmark/benchmark` 执行 Fabric。

首次克隆：

```bash
cd /home/ubuntu
mkdir -p /home/ubuntu/experiments
cd /home/ubuntu/experiments
git clone https://github.com/DrDaydream/narwhal-bullshark-ubuntu24.git
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24
git log -1 --oneline
```

已有本仓库时，在现有目录更新，不需要创建或切换本地分支。若工作区有改动，先备份或提交；`--ff-only` 会在无法快进时停止：

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24
git status --short
git fetch origin main
git merge --ff-only FETCH_HEAD
git log -1 --oneline
```

安装系统 Python 的用户包，不使用虚拟环境。若当前终端仍在旧虚拟环境中，先执行 `deactivate`：

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-dev git curl ca-certificates
/usr/bin/python3 -m pip install --user --break-system-packages -r benchmark/requirements.txt
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
fab --list
fab --help remote
```

完整依赖复用 `requirements-local.txt` 的 Fabric 3.2.2、Invoke 2.2.0、Paramiko 3.5.1、multiaddr 0.0.9、python-dateutil 2.9.0.post0，并添加 boto3 1.44.3、matplotlib 3.9.4。`--break-system-packages` 仅用于这次 `--user` 安装，不使用 `sudo pip`。

## 2. 写入 settings.json

现有上下文使用 `orca-amazon` RSA 密钥；相同公钥可以用于新的 Bullshark 实验机。`key.name` 是 EC2 密钥对名称，**不是 AWS_PROFILE**。加载器也支持 Ed25519。

配置文件与 `fabfile.py` 位于同一层。下面先备份已有配置，再明确写入完整的五区域设置：

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
if [ -f settings.json ]; then
    cp -a settings.json "/home/ubuntu/experiments/bullshark-settings-$(date +%Y%m%d-%H%M%S).json"
fi
cat > settings.json <<'JSON'
{
    "key": {
        "name": "orca-amazon",
        "path": "/home/ubuntu/.ssh/orca-amazon.pem"
    },
    "port": 3000,
    "repo": {
        "name": "narwhal-bullshark-ubuntu24",
        "url": "https://github.com/DrDaydream/narwhal-bullshark-ubuntu24.git",
        "branch": "main"
    },
    "instances": {
        "type": "m5.8xlarge",
        "regions": [
            "us-east-1",
            "eu-north-1",
            "ap-southeast-2",
            "us-west-1",
            "ap-northeast-1"
        ]
    }
}
JSON
python3 -m json.tool settings.json
chmod 600 /home/ubuntu/.ssh/orca-amazon.pem
```

同样的配置也保存在 `benchmark/settings.example.json`，以后可复制使用。这里不包含 AWS API 凭据或 SSH 私钥内容。

| Region | 位置 |
|---|---|
| us-east-1 | 美国弗吉尼亚北部 |
| eu-north-1 | 瑞典斯德哥尔摩 |
| ap-southeast-2 | 澳大利亚悉尼 |
| us-west-1 | 美国加利福尼亚北部 |
| ap-northeast-1 | 日本东京 |

这是五个区域，覆盖北美洲、欧洲、大洋洲和亚洲四个大洲。

## 3. 验证 AWS 身份和五区域密钥

沿用已配置的 EC2 控制机 IAM role。若之前设置了不存在的 AWS profile，会出现 `ProfileNotFound`；使用实例角色时先清除这两个变量。若你实际使用命名 profile，则应设置真实存在的 profile，跳过 `unset`。

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
unset AWS_PROFILE AWS_DEFAULT_PROFILE
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
python3 - <<'PY'
import json
from pathlib import Path
import boto3
from botocore.exceptions import ClientError
from paramiko import PKey

settings = json.loads(Path('settings.json').read_text())
session = boto3.Session()
credentials = session.get_credentials()
if credentials is None:
    raise SystemExit('控制机没有可用 AWS 凭据，请检查 EC2 IAM role。')
regions = settings['instances']['regions']
identity = session.client('sts', region_name=regions[0]).get_caller_identity()
print('凭据来源:', credentials.method)
print('身份:', identity['Arn'])
key = PKey.from_path(Path(settings['key']['path']).expanduser())
expected = [key.get_name(), key.get_base64()]
public = (' '.join(expected) + '\n').encode('ascii')
for region in regions:
    ec2 = session.client('ec2', region_name=region)
    try:
        keys = ec2.describe_key_pairs(KeyNames=[settings['key']['name']], IncludePublicKey=True)
    except ClientError as error:
        if error.response['Error']['Code'] != 'InvalidKeyPair.NotFound':
            raise
        ec2.import_key_pair(KeyName=settings['key']['name'], PublicKeyMaterial=public)
        print(region, '已导入公钥')
    else:
        if keys['KeyPairs'][0].get('PublicKey', '').split()[:2] != expected:
            raise SystemExit(f'{region}: 同名密钥不匹配，停止，请检查后再创建实例。')
        print(region, '密钥匹配')
PY
fab info
```

角色需要以下 EC2 权限，可继续使用已有角色中允许这些操作的策略：`DescribeKeyPairs`、`ImportKeyPair`、`DescribeImages`、`DescribeInstances`、`CreateSecurityGroup`、`AuthorizeSecurityGroupIngress`、`DeleteSecurityGroup`、`RunInstances`、`CreateTags`、`StartInstances`、`StopInstances`、`TerminateInstances`。如策略按区域或资源标签限制，需允许上述五个 region 和 `bullshark-node` 标签。

脚本使用每个 region 的默认 VPC/默认子网，要求有公网 IPv4、Internet Gateway 路由和可用的 m5 配额；自定义私有 VPC 需要另外配置网络。它为实验安全组开放 TCP 22 和 TCP 3000–5000，20 节点 × 1 worker 的协议地址使用 3000–3099。跨区域使用公网地址；SSH 必须能从控制机连通。默认未开放 ICMP，`ping` 超时不能单独证明节点不通。

本版使用 **`Name=bullshark-node`** 和安全组 **`bullshark`**，以区分原 Orca-B 的 `dag-node` / `dag`。已有 Orca-B 机器不会出现在这里的 `fab info` 中，仍需通过 Orca-B 目录管理；两套集群同时存在会分别计费。

## 4. 创建、安装、运行

首次创建：`--nodes 4` 表示**每个 region 新增 4 台**，合计 20 台。每执行一次都会新增实例；如果已经有 Bullshark 机器，先 `fab info`，停止状态的机器用 `fab start` 恢复。镜像选用各区域最新的 Canonical Ubuntu 24.04 amd64，根卷为 200 GiB gp3。

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
fab create --nodes 4
fab info
```

先运行 `fab install`。它在控制机和实验机准备系统依赖、Rust 1.60.0、tmux、chrony，在实验机克隆/更新配置的仓库，然后编译全部运行中的 Bullshark 实验机和控制机。安装和编译的 stdout/stderr 实时显示，多台机器的输出可能交错。已有依赖和 Cargo 构建产物会复用；要求 `ubuntu` 用户能够无交互执行 sudo。

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
fab install
```

等待 `fab install` 成功结束，显示 `ready for fab remote`，再运行默认 20 节点实验：

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
unset AWS_PROFILE AWS_DEFAULT_PROFILE
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
fab remote
```

两条命令分工明确：**`fab install` 负责安装、同步代码和编译；`fab remote` 负责检查已编译版本、生成/上传配置、启动测试、下载日志和汇总结果。** `fab remote` 不运行 apt、rustup、Git fetch 或 Cargo build。

`fab install` 在所有实验机和控制机执行 `cargo build --locked --release --features benchmark`，固定到控制机当前 HEAD；该提交必须已经推送且属于 `settings.json` 的 `repo.branch`。编译成功后在各自的 `target/release/.bullshark-build-commit` 写入提交号。`fab remote` 检查版本记录和可执行文件，缺失或不匹配就报错并提示先运行 `fab install`。重复运行相同版本的测试无需重新安装/编译；更新控制机提交、新增机器或删除构建产物后需要再次 `fab install`。首次从旧版迁移也要执行一次 `fab install` 来生成版本记录，已有 `target/` 缓存可以复用。

脚本拒绝未提交的 Rust、proto、Cargo 配置和锁文件改动，但允许本地调整 `fabfile.py` 和 `settings.json`。请先提交并推送 Rust 源码改动，再更新控制机、执行 `fab install`。可以在 `results/remote-commit.txt` 查看每次测试使用的提交。

降低输入速率进行首次连通性测试，或调整节点数量时，无需编辑 fabfile：

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
fab remote --nodes 20 --rate 5000 --duration 30 --faults 0 --runs 1
```

如果目前每个区域只有两台、总共 10 台机器，`fab install` 会编译这 10 台，运行时明确传入节点数：

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
fab remote --nodes 10
```

默认 `--workers 1`、`--tx-size 512`；`--faults` 表示不启动委员会尾部的部分节点，只模拟离线故障。非零故障时客户端输入也受到在线 worker 数量影响，不能把相同 `--rate` 参数直接理解为相同实际注入吞吐。

如果使用 `fab start --max 4` 恢复已停止实例，等待 `fab info` 列出运行中的机器及其新公网 IP，然后再次 `fab remote`；它会重新生成委员会地址。`fab start`/`fab stop` 发出请求后立即返回，状态变化需要时间。

## 5. 查看和保存结果

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
fab logs
cat results/remote-commit.txt
cat results/bench-0-20-1-True-50000-512.txt
```

`logs/` 保存 primary、worker、client 的原始日志；`results/` 保存每种参数组合的结果。同名汇总文件会追加运行结果；原始日志在下一次测试时会清理。保存实验配置和日志：

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
mkdir -p /home/ubuntu/experiments/bullshark-archives
tar -czf "/home/ubuntu/experiments/bullshark-archives/run-$(date +%Y%m%d-%H%M%S).tar.gz" \
    logs results settings.json fabfile.py
```

首次 `fab install` 的编译通常明显长于 80 秒，终端现在实时显示安装和编译输出。Cargo 会复用已有构建产物，但 RocksDB 首次构建和 release 链接仍可能耗时。Rust 下载需要访问 GitHub、crates.io 和 static.rust-lang.org。安装/编译失败使 `fab install` 返回非零退出码；缺少主机、缺少匹配的构建产物、启动或日志解析失败使 `fab remote` 返回非零退出码。`chronyc waitsync` 超时则检查节点时间同步后重试，跨机器的端到端延迟依赖时钟对齐。

## 6. 停止、恢复和销毁

中途停止本次远程测试进程（EC2 仍运行）。它只停止 Bullshark 专用 tmux 服务：

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
fab kill
```

暂时停止实验机并保留磁盘：

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
fab stop
```

下次每个区域启动最多四台，待启动完成后运行：

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
fab start --max 4
fab info
```

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
fab remote
```

实验结束、不再保留这批实例时，先保存结果，再单独执行：

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
fab destroy
fab info
```

销毁范围为配置五区域中标记为 `bullshark-node` 的实例及 `bullshark` 安全组；根卷随实例终止删除。停止状态仍保留 EBS 卷并产生存储费用。控制机本身和已下载到控制机的结果另行保留。

## 验证范围与参考

自动化验证覆盖 AMI 查询和选择、AWS 分页发现及实例隔离、地址到 SSH 主机的转换、默认参数、错误退出、安装编译与运行分离、可见输出、失败编译清除版本记录、缺失/过期构建检查、不均衡区域分配，以及真实本地 Git 仓库的重复更新和固定提交检出。检查使用 Python 3.12，并保留原有本地和日志解析回归测试。测试不连接 AWS。

当前修改环境是 Windows，没有可用 Ubuntu/WSL 发行版或此控制机的 SSH 会话；**尚未完成 Ubuntu 24.04 Rust release 编译及真实 EC2 集群的端到端实测**。上述检查不能替代首次云端低负载运行。

在 Ubuntu 控制机复查：

```bash
cd /home/ubuntu/experiments/narwhal-bullshark-ubuntu24/benchmark
python3 -m unittest discover -s tests -v
fab install
fab remote --nodes 20 --rate 5000 --duration 30
```

- [Canonical：按所有者、镜像名称及 CreationDate 选择 Ubuntu AMI](https://documentation.ubuntu.com/aws/aws-how-to/instances/find-ubuntu-images/)
- [AWS：EC2 密钥对](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-key-pairs.html)
- [Boto3：身份凭据与 EC2 IAM role](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html)
- [AWS：实例 IP 地址和停止/启动时的公网地址变化](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-instance-addressing.html)
