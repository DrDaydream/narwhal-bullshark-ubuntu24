# Ubuntu 24.04：Bullshark 分支的 fab local

完整源码及本地运行适配，包含批次摘要统计修复。**不使用虚拟环境、Conda、pipx 或 Docker**。直接使用 Ubuntu 的 `/usr/bin/python3`，Python 包安装到当前用户的 `~/.local/`。运行无需 `env-ubuntu24.sh`。AWS 多区域测试请阅读 [远程部署说明](README_AWS_UBUNTU24_CN.md)。

## 快速运行

在 Ubuntu 24.04 EC2 上，以普通用户安装系统依赖：

```bash
sudo apt-get update
sudo apt-get install -y software-properties-common
sudo add-apt-repository -y universe
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    build-essential gcc-12 g++-12 clang-14 libclang-14-dev \
    cmake pkg-config libssl-dev zlib1g-dev protobuf-compiler \
    tmux git curl ca-certificates python3 python3-pip python3-dev
```

下载本仓库（公开仓库，HTTPS 克隆无需 GitHub 登录或 Token）：

```bash
cd "$HOME"
git clone https://github.com/DrDaydream/narwhal-bullshark-ubuntu24.git narwhal-bullshark
cd "$HOME/narwhal-bullshark"
```

安装 Rust 1.60.0；如果已有 rustup，可以跳过下载安装器的两条命令：

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/narwhal-rustup.sh
sh /tmp/narwhal-rustup.sh -y --profile minimal --default-toolchain none
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
rustup toolchain install 1.60.0 --profile minimal
```

项目的 `.cargo/config` 已指定 GCC 12 和 `/usr/lib/llvm-14/lib`。RocksDB 由 Cargo 按 Cargo.lock 中的版本编译，不依赖另装的系统 RocksDB。若当前终端曾设置外部 RocksDB 路径，先清除，再安装 Python 包并运行：

```bash
cd "$HOME/narwhal-bullshark"
unset ROCKSDB_LIB_DIR ROCKSDB_INCLUDE_DIR ROCKSDB_STATIC
/usr/bin/python3 -m pip install --user --break-system-packages -r benchmark/requirements-local.txt
cd benchmark
fab local
```

无需激活环境。不要使用 `sudo pip` 或 `sudo fab local`。

后续重新登录后直接执行：

```bash
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
cd "$HOME/narwhal-bullshark/benchmark"
fab local
```

首次运行会下载 Rust 依赖、编译 release 和 RocksDB，所需时间远长于测试本身的 20 秒。默认 2 个编译任务，编译输出保留。可在项目根目录提前编译：

```bash
cd node
cargo build --locked --release --features benchmark
```

仓库也保留可选的 `setup_ubuntu24.sh` 和 `run_local.sh`，上述直接命令不依赖它们。

## 来源与范围

- 仓库：<https://github.com/MystenLabs/narwhal/tree/Bullshark>
- 检出提交：`3257759165a2f9ec09157509294146adcc0c2393`，提交日期 2022-06-17。
- 保留原始 Rust 1.60.0、Cargo.lock、共识/网络/存储 Rust 源码。上游分支内部仍有 tusk 命名；此适配不另行替换算法。
- 目标：Ubuntu 24.04、系统 Python 3.12、GCC/G++ 12、Clang/libclang 14。
- 本地最小依赖支持 `fab local`、`fab logs`。AWS 和绘图任务使用已更新的 `benchmark/requirements.txt`，不在本地安装脚本的依赖范围。
- 保留上游 GitHub Actions 文件；新仓库的 Actions 初始关闭，未启用上游每日构建和自动评论任务。

## 参数和输出

默认保留上游设置：4 节点、每节点 1 worker、0 故障节点、总输入速率 50,000 tx/s、交易 512 B、持续 20 秒。可以在 benchmark 目录直接指定：

```bash
fab local --nodes=4 --workers=1 --rate=5000 --tx-size=512 --duration=30
fab --help local
```

节点协议端口从 3000 开始；默认 4 节点共使用 3000–3019，其他服务可能使用自动分配的端口。同一源码目录一次只运行一组测试。

终端输出 SUMMARY，包含 Consensus / End-to-end TPS 和 latency。日志在 `benchmark/logs/`，可重新汇总：

```bash
fab logs
```

每次 local 运行会删除旧的 `benchmark/logs/`、`.db-*` 和隐藏 JSON 配置，这是上游测试行为；先备份需要保留的数据。

## 无虚拟环境安装方式

Python 依赖安装命令（在项目根目录）：

```bash
/usr/bin/python3 -m pip install --user --break-system-packages -r benchmark/requirements-local.txt
```

Ubuntu 24.04 的系统 Python 有 externally-managed 标记，因此仅在这次 pip 调用中显式使用 `--break-system-packages`，并用 `--user` 安装到当前用户目录。它不会写入 `/usr/lib/python3/dist-packages`，但用户包可能影响当前用户的其他 Python 程序。不会删除系统标记文件，也不会全局设置 pip 的 break-system-packages。

本地运行只需要 `benchmark/requirements-local.txt`。完整的 `benchmark/requirements.txt` 现在复用这些版本并添加 boto3、matplotlib；这两个包延迟到 AWS 或绘图任务执行时才导入。

## 修改内容

- `setup_ubuntu24.sh`：可选安装脚本，安装系统包、Rust 1.60.0、用户 Python 包，并检查任务加载。
- `run_local.sh`：可选便捷入口；日常可直接 `fab local`。
- `.cargo/config`：选择 GCC 12 和 libclang 14，默认 2 个编译任务，不更改系统默认 C/C++ 编译器。
- `benchmark/requirements-local.txt`：Python 3.12 本地依赖。
- `benchmark/fabfile.py`：延迟加载可选依赖、暴露测试参数、检查输入、失败返回非零退出码。
- `benchmark/benchmark/local.py`：每个源码目录独立的 tmux socket、记录 stdout/stderr、Ctrl+C 清理；不关闭用户普通 tmux 会话。
- `benchmark/benchmark/commands.py`：构建使用 `--locked`。
- `benchmark/benchmark/logs.py`：兼容 worker 日志中 `Ok(摘要)` 的格式，使批次字节数、采样交易与 primary 的提交摘要正确匹配。可直接 `fab logs` 重算现有日志。
- `benchmark/tests/`：本地运行和摘要匹配的回归测试。

## 排查

- 找不到 fab：重新执行 `export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"`。
- 找不到 libclang：检查 `dpkg -s libclang-14-dev`；项目配置使用 `/usr/lib/llvm-14/lib`。
- 编译进程被 kill：检查可用内存，在 benchmark 目录尝试 `CARGO_BUILD_JOBS=1 fab local --rate=5000 --duration=30`。
- 网络下载失败：需要能访问 Ubuntu 软件源、PyPI、static.rust-lang.org、crates.io、GitHub，含原始 Git 依赖。不要删除 Cargo.lock 或运行 cargo update。
- Rust 依赖错误：保留 1.60.0，升级到最新 Rust 不属于此次适配。
- 没有提交或日志解析失败：检查 `benchmark/logs/` 中 primary、worker、client 日志，确认端口空闲，再降低速率、延长测试时间。

紧急停止本次本地测试，在 benchmark 目录执行：

```bash
python3 -c 'from benchmark.local import LocalBench; LocalBench._kill_nodes(object.__new__(LocalBench))'
```

`fab kill` 是上游 AWS 远程任务，不是本地停止命令。

## 验证边界

交付前执行 Python 3.12 的任务加载检查、10 项回归测试和 Bash 语法检查。当前 Windows 工作区没有已配置的 Ubuntu/WSL 发行版，**未完成 Ubuntu 24.04 上的 Rust release 编译及真实四节点 fab local 实测**。不能把脚本检查当作实机跑通。

在 Ubuntu 上完成整体验证：

```bash
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
cd "$HOME/narwhal-bullshark/node"
cargo build --locked --release --features benchmark
cd ..
cd benchmark
python3 -m unittest discover -s tests -v
fab local --rate=5000 --duration=30
```
