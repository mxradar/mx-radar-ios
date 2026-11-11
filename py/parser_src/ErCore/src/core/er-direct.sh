#!/bin/sh
# er-direct.sh —— 软路由侧「直采」一键装/卸/查（OpenWrt 系固件）
#
# 干什么：把本机（软路由）抓到的包推给 PC 上的雷达。手机连本机 Wi-Fi 时流量本来就
# 经过这里，所以**不需要镜像、不需要内核模块**，只要有 tcpdump 和 nc：
#
#     tcpdump -i <手机所在口> -U -w - [过滤] | nc <PC的IP> <监听端口>
#
# PC 侧在门户里选「软路由直采」并启动，它就在听这个端口。本脚本只管这条管道：
# 开机自启、管道断了自动重连、接口重建后重新拉起、以及一键清干净。
#
# 用法（在本机跑，需要 root）：
#     er-direct.sh detect
#     er-direct.sh install --pc 192.168.0.108 [--port 9099] [--iface br-lan] [--phone 192.168.8.50]
#     er-direct.sh status | restart | stop
#     er-direct.sh uninstall [--purge-tcpdump]
#
# --iface 要填**手机所在的口**（当 AP 一般是 br-lan；不想挑就填 any = 所有接口）。
# --phone 只用于少传无关流量；
# 不填就是整口 UDP（不会漏，只是流量大一点）。
# 纯 POSIX sh（busybox ash 可跑），不依赖 procd，也不需要装本项目的任何程序。

CONF=/etc/er-direct.conf
INIT=/etc/init.d/er-direct
HOTPLUG=/etc/hotplug.d/iface/99-er-direct
PIDFILE=/var/run/er-direct.pid
DEFAULT_PORT=9099

say() { echo "[er-direct] $*"; }
log() { logger -t er-direct "$*" 2>/dev/null || true; }
die() { echo "[er-direct] 错误: $*" >&2; exit 1; }

need_root() {
    [ "$(id -u)" = "0" ] || die "需要 root（要装包、写 /etc、并用 tcpdump）"
}

load_conf() { [ -f "$CONF" ] && . "$CONF"; }

pipeline_cmd() {
    printf 'tcpdump -i %s -U -w - %s | nc %s %s' "$IFACE" "$FILTER" "$PC" "$PORT"
}

# ---------------------------------------------------------------- detect
do_detect() {
    say "1) 本机接口与地址："
    if command -v ip >/dev/null 2>&1; then
        ip -4 -o addr show 2>/dev/null | while read -r _ i _ cidr _; do
            case "$i" in lo) continue ;; esac
            printf '  %-14s %s\n' "$i" "$cidr"
        done
    else
        ifconfig -a 2>/dev/null | sed 's/^/  /'
    fi
    echo

    # 无线口谁是 AP：iwinfo 直接给 ESSID + Mode(Master=AP)，手机连的那个 SSID 就是它。
    # 中继场景同一个 radio 会有两个 vif（xxx-ap0 给手机、xxx-sta0 连上游），要填给手机的。
    if command -v iwinfo >/dev/null 2>&1; then
        say "2) 无线口（Mode=Master 的那个在给手机当 AP；中继另有一个 Client/sta 口连上游）："
        iwinfo 2>/dev/null | sed 's/^/  /'
        echo
    elif command -v iw >/dev/null 2>&1; then
        say "2) 无线口（看 type 字段：AP 是给手机的，station 是连上游的）："
        iw dev 2>/dev/null | sed 's/^/  /'
        echo
    fi

    # 邻居表：手机 IP 出现在哪一行的最后一列(dev)，那个口就是它。
    if command -v ip >/dev/null 2>&1; then
        say "3) 邻居表（在手机上看到自己的 IP，或按 MAC 对号，看最后一列的 dev）："
        n=$(ip -4 neigh show 2>/dev/null | wc -l)
        if [ "${n:-0}" -gt 0 ]; then
            ip -4 neigh show 2>/dev/null | sed 's/^/  /'
        else
            echo "  （空的：手机刚连上/休眠时会没有记录。让手机刷一下网再跑一次，"
            echo "   或直接用保底办法——手机放个视频，逐个口试 tcpdump -i <口> -c 5 udp）"
        fi
        echo
    fi

    say "4) 结论怎么下："
    say "   · 当 AP 时手机的口几乎必然是 br-lan（无线 AP 口和 LAN 口都桥在它里面）——默认就填它；"
    say "   · 想确认：拿手机 IP 在第 3 步的表里找最后一列的 dev，那就是手机所在口；"
    say "   · 中继场景若无线口没桥进 br-lan，填第 2 步里那个给手机当 AP 的无线口名；"
    say "   · 懒得挑就用 --iface any：抓所有接口，不用知道手机在哪个口（流量略大）。"
    echo

    say "5) 工具检查："
    for c in tcpdump nc; do
        if command -v "$c" >/dev/null 2>&1; then
            printf '  %-8s 有\n' "$c"
        else
            printf '  %-8s 缺（install 会自动 opkg install）\n' "$c"
        fi
    done
    echo
    say "接着在 PC 侧：门户 →「软路由直采」→ 选「本机地址」和「监听端口」"
    say "然后回来跑： $0 install --pc <那个地址> --port <那个端口> --iface <上面定的口>"
}

# ---------------------------------------------------------------- 起停
start_pipe() {
    load_conf
    [ -n "$IFACE" ] && [ -n "$PC" ] || die "配置不全，请重新 install"
    stop_pipe >/dev/null 2>&1
    # 常驻：断了 3 秒后重连（软路由重启 / PC 重启 / 换网都不用管）
    (
        while true; do
            log "pipe up: $(pipeline_cmd)"
            tcpdump -i "$IFACE" -U -w - $FILTER 2>/dev/null | nc "$PC" "$PORT"
            log "pipe down, retry in 3s"
            sleep 3
        done
    ) >/dev/null 2>&1 &
    echo $! > "$PIDFILE"
    say "已启动：$(pipeline_cmd)"
}

stop_pipe() {
    load_conf
    if [ -f "$PIDFILE" ]; then
        kill "$(cat "$PIDFILE")" 2>/dev/null
        rm -f "$PIDFILE"
    fi
    # 兜底：管道里的子进程不在 pidfile 里，按命令行特征收掉
    if [ -n "$IFACE" ] && [ -n "$PC" ]; then
        ps 2>/dev/null | grep -E "tcpdump -i $IFACE" | grep -v grep | awk '{print $1}' \
            | while read -r pid; do kill "$pid" 2>/dev/null; done
        ps 2>/dev/null | grep -E "nc $PC $PORT" | grep -v grep | awk '{print $1}' \
            | while read -r pid; do kill "$pid" 2>/dev/null; done
    fi
    say "已停止"
}

# ---------------------------------------------------------------- 自启 + 热插拔
install_init() {
    cat > "$INIT" <<'EOF'
#!/bin/sh /etc/rc.common
# 由 er-direct.sh 生成：软路由直采管道（开机自启）
START=99
STOP=10
start()   { [ -x /usr/bin/er-direct.sh ] && /usr/bin/er-direct.sh _bg-up; }
stop()    { [ -x /usr/bin/er-direct.sh ] && /usr/bin/er-direct.sh stop; }
restart() { stop; start; }
EOF
    chmod 755 "$INIT"
    "$INIT" enable 2>/dev/null || ln -sf "$INIT" /etc/rc.d/S99er-direct 2>/dev/null
}

install_hotplug() {
    # 中继场景接口会因上游掉线而 down/up，管道跟着没；up 之后补回
    cat > "$HOTPLUG" <<'EOF'
#!/bin/sh
# 由 er-direct.sh 生成：接口 up 后确保直采管道在跑
[ "$ACTION" = "ifup" ] || exit 0
[ -f /etc/er-direct.conf ] || exit 0
. /etc/er-direct.conf
case " $DEVICE $INTERFACE " in
    *" $IFACE "*) ;;
    *) exit 0 ;;
esac
[ -x /usr/bin/er-direct.sh ] && /usr/bin/er-direct.sh restart >/dev/null 2>&1
EOF
    chmod 755 "$HOTPLUG"
}

# ---------------------------------------------------------------- 子命令
do_install() {
    need_root
    PORT=$DEFAULT_PORT
    IFACE=""; PC=""; PHONE=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --pc)    PC=$2;    shift 2 ;;
            --port)  PORT=$2;  shift 2 ;;
            --iface) IFACE=$2; shift 2 ;;
            --phone) PHONE=$2; shift 2 ;;
            *) die "未知参数: $1" ;;
        esac
    done
    [ -n "$PC" ] || die "必须给 --pc <PC 的地址>（门户里显示的那个“本机地址”）"
    ANY_IFACE=""
    if [ -z "$IFACE" ]; then
        IFACE=$(ip -4 -o addr show 2>/dev/null | awk '$2=="br-lan"{print $2; exit}')
        [ -n "$IFACE" ] || die "自动找不到 br-lan，请用 --iface 指定（先跑 detect），或 --iface any"
        say "未指定 --iface，按 br-lan 处理"
    fi
    if [ "$IFACE" = "any" ]; then
        # 「所有接口」：不用知道手机连在哪个口上，代价是流量大一点（无关接口的 UDP 也会推给 PC）。
        # 好处是接口 up/down（中继场景常见）不用管，所以下面不装热插拔钩子。
        ANY_IFACE=1
        say "按「所有接口」抓（-i any）：不用挑口；流量稍大，且不需要热插拔重连钩子"
    fi
    if [ -n "$PHONE" ]; then
        FILTER="udp and host $PHONE"
    else
        FILTER="udp"
        say "未指定 --phone：整口 UDP 都推给 PC（流量稍大，但不会漏）"
    fi

    if ! command -v tcpdump >/dev/null 2>&1; then
        say "装 tcpdump ..."
        opkg update >/dev/null 2>&1
        opkg install tcpdump >/dev/null 2>&1 || die "opkg install tcpdump 失败"
    fi
    if ! command -v nc >/dev/null 2>&1; then
        say "装 netcat ..."
        opkg install netcat >/dev/null 2>&1 || opkg install netcat-bsd >/dev/null 2>&1 \
            || die "opkg install netcat 失败"
    fi

    cat > "$CONF" <<EOF
# er-direct.sh 的配置（由 install 生成）
IFACE="$IFACE"
PC="$PC"
PORT="$PORT"
FILTER="$FILTER"
EOF
    chmod 644 "$CONF"
    install_init
    if [ -n "$ANY_IFACE" ]; then
        rm -f "$HOTPLUG"          # -i any 不怕单个接口掉，钩子没必要
    else
        install_hotplug
    fi
    # 拷到 /usr/bin，方便直接敲 er-direct.sh / 给 init 调用
    if [ "$(readlink -f "$0" 2>/dev/null || echo "$0")" != "/usr/bin/er-direct.sh" ]; then
        cp "$0" /usr/bin/er-direct.sh 2>/dev/null
        chmod 755 /usr/bin/er-direct.sh 2>/dev/null
    fi
    start_pipe
    echo
    say "装好了。核对："
    say "  1) 本机执行： /usr/bin/er-direct.sh status"
    say "  2) PC 侧抓包窗口应打印 [直采] 已连接 ...，captures/ 下会出现 run_*"
    say "卸载： /usr/bin/er-direct.sh uninstall"
}

do_uninstall() {
    need_root
    PURGE=""
    [ "$1" = "--purge-tcpdump" ] && PURGE=1
    stop_pipe
    if [ -x "$INIT" ]; then
        "$INIT" disable 2>/dev/null
        rm -f "$INIT"
    fi
    rm -f /etc/rc.d/S99er-direct "$HOTPLUG" "$PIDFILE"
    if [ -f "$CONF" ] && grep -q 'er-direct.sh 的配置' "$CONF" 2>/dev/null; then
        rm -f "$CONF"
    fi
    rm -f /usr/bin/er-direct.sh
    say "已卸载（管道、自启、热插拔钩子、配置都清了）"
    if [ -n "$PURGE" ]; then
        say "顺带卸 tcpdump（nc 常被别的功能用到，不动）..."
        opkg remove tcpdump >/dev/null 2>&1
        say "tcpdump 已卸"
    fi
}

do_status() {
    load_conf
    echo "[er-direct] 配置: iface=${IFACE:-?} pc=${PC:-?} port=${PORT:-?} filter=${FILTER:-?}"
    running=""
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        running="是(pid=$(cat "$PIDFILE"))"
    elif ps 2>/dev/null | grep -E "nc $PC $PORT" | grep -v grep >/dev/null 2>&1; then
        running="是(未走 pidfile)"
    fi
    echo "[er-direct] 管道: ${running:-未运行}"
    if command -v nc >/dev/null 2>&1 && [ -n "$PC" ]; then
        if nc -z -w 2 "$PC" "$PORT" >/dev/null 2>&1; then
            echo "[er-direct] PC 监听: 可达（$PC:$PORT）"
        else
            echo "[er-direct] PC 监听: 不可达 —— PC 侧雷达起了吗？地址/端口对吗？防火墙放行了吗？"
        fi
    fi
}

case "$1" in
    detect)    do_detect ;;
    install)   shift; do_install "$@" ;;
    uninstall) shift; do_uninstall "$@" ;;
    status)    do_status ;;
    stop)      stop_pipe ;;
    restart)   stop_pipe; start_pipe ;;
    _bg-up)    start_pipe ;;       # init 脚本用的内部入口
    *)         sed -n '2,26p' "$0"; exit 1 ;;
esac
