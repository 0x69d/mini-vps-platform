"""リソースの定数と XML テンプレート群。"""

POOL_NAME = "vps-pool"
BASE_POOL = "images"
SEED_POOL_NAME = "vps-seeds"

# 管理対象 domain の <metadata> に spec を埋め込むための名前空間。
# URI は単なる一意識別子で、機能上は任意の文字列でよい(プレースホルダ)。
METADATA_NS = "https://example.org/minivps"
METADATA_KEY = "minivps"

# dir 型ストレージプール。パスはプラットフォームごとに異なるため
# HostProfile(pool_path / seed_dir)から埋める。
POOL_XML_TEMPLATE = """
<pool type='dir'>
  <name>{name}</name>
  <target>
    <path>{path}</path>
  </target>
</pool>
"""

OVERLAY_VOL_XML_TEMPLATE = """
<volume>
  <name>{name}.qcow2</name>
  <capacity unit='GiB'>{disk}</capacity>
  <target>
    <format type='qcow2'/>
  </target>
  <backingStore>
    <path>{base_path}</path>
    <format type='qcow2'/>
  </backingStore>
</volume>
"""

SEED_VOL_XML_TEMPLATE = """
<volume>
  <name>{name}</name>
  <capacity unit='bytes'>{capacity_bytes}</capacity>
  <target>
    <format type='raw'/>
  </target>
</volume>
"""

META_DATA_TEMPLATE = """\
instance-id: iid-{name}-001
local-hostname: {hostname}
"""

# <memballoon> の統計収集間隔(秒)。libvirt は既定でこの要素を暗黙に追加するが、
# <stats period> が無いと balloon.available/usable がゲスト内の実使用量として
# 更新されず、一度取った値のまま古くなる。Prometheus の scrape_interval 15s より
# 短くして、スクレイプごとに新しい値が乗るようにする。
BALLOON_STATS_PERIOD_SECONDS = 5

# qemu-guest-agent の virtio-serial チャネル名。libvirt と QEMU の既定値に合わせる。
GUEST_AGENT_CHANNEL = "org.qemu.guest_agent.0"

# domain XML で QEMU のコマンドライン引数を直接渡すための名前空間。user-mode
# ネットワーク(macOS)で SSH のポート転送を指定するのに使う。
QEMU_XML_NS = "http://libvirt.org/schemas/domain/qemu/1.0"

# nwfilter のルール priority。nwfilter は記述順ではなく priority 昇順で評価する
# (同じ priority 同士の順序は保証しない)。既定 drop を最後に置くため、他のルールは
# すべてこれより小さくする。
NWFILTER_DEFAULT_PRIORITY = 500
NWFILTER_DROP_PRIORITY = 1000
# egress ルールには記述順に NWFILTER_EGRESS_PRIORITY_START から 1 ずつ増える priority を
# 振る。戻り通信・DHCP・inbound の accept(500)より後、既定 drop(1000)より前に収まる
# ようにするため、egress ルールの件数には上限がある(spec.py で検証する)。
NWFILTER_EGRESS_PRIORITY_START = NWFILTER_DEFAULT_PRIORITY + 1
EGRESS_MAX_RULES = NWFILTER_DROP_PRIORITY - NWFILTER_EGRESS_PRIORITY_START

# 宣言ポート1件分の accept ルール。protocol("tcp"/"udp")に応じてタグ名を差し替える。
NWFILTER_PORT_RULE_TEMPLATE = """\
  <rule action='accept' direction='in' priority='500'>
    <{protocol} dstportstart='{port}'/>
  </rule>
"""

# inbound の既定 drop(filters がリストのとき)。
NWFILTER_INBOUND_DROP_RULE = """\
  <rule action='drop' direction='in' priority='1000'>
    <all/>
  </rule>
"""

# inbound の全許可(filters が None で egress だけを絞るとき)。libvirt は state 属性の
# 無い iptables 層の drop ルールを逆方向にも展開するため、下の out 方向の既定 drop は
# VM 宛ての通信にも効く。inbound を全許可のまま保つには、これを明示する必要がある。
NWFILTER_INBOUND_ACCEPT_ALL_RULE = """\
  <rule action='accept' direction='in' priority='500'>
    <all/>
  </rule>
"""

# outbound の全許可(egress が None のとき。従来の挙動)。
NWFILTER_OUTBOUND_ACCEPT_ALL_RULE = """\
  <rule action='accept' direction='out' priority='500'>
    <all/>
  </rule>
"""

# egress を絞るときに egress ルールより前(小さい priority)に常に入るルール。
# - ESTABLISHED,RELATED の out accept: inbound で受けた接続への戻りを通す。
#   egress ルールより小さい priority に置き、戻りパケットが egress の drop に
#   落ちないようにする。
# - DHCP の out accept: allow-dhcp は 0.0.0.0 → 255.255.255.255 の初回取得しか
#   許さず、リース更新(クライアント IP からサーバへのユニキャスト)は既定 drop に
#   落ちるため、ポートだけで明示的に許可する。
NWFILTER_EGRESS_HEAD_RULES = """\
  <rule action='accept' direction='out' priority='500'>
    <all state='ESTABLISHED,RELATED'/>
  </rule>
  <rule action='accept' direction='out' priority='500'>
    <udp srcportstart='68' dstportstart='67'/>
  </rule>
"""

# egress を絞るときに egress ルールより後(既定 drop)に常に入るルール。
# - IPv6 の送信 drop(ebtables 層): egress ルールは IPv4 しか表せないため、IPv6 を
#   通すと link-local 経由でホストや同じブリッジ上の VM へ抜けられる。
# - out 方向の既定 drop。
NWFILTER_EGRESS_TAIL_RULES = """\
  <rule action='drop' direction='out' priority='1000'>
    <mac protocolid='ipv6'/>
  </rule>
  <rule action='drop' direction='out' priority='1000'>
    <all/>
  </rule>
"""

# egress ルール1件。state='NEW' を明示するのは、新しい接続の最初のパケットだけを
# このルールで判定するため(確立後は上の ESTABLISHED,RELATED で通る)。state を
# 明示すると libvirt は逆方向(VM 宛て)のルールを作らないため、egress ルールが
# inbound に影響しない。attrs は dstipaddr/dstipmask/dstportstart。
NWFILTER_EGRESS_RULE_TEMPLATE = """\
  <rule action='{action}' direction='out' priority='{priority}'>
    <{protocol} state='NEW'{attrs}/>
  </rule>
"""

# ESTABLISHED,RELATED の accept が無いと、VM 自身が発信した通信(DNS/apt 等)への
# 応答まで default drop に落ちる。nwfilter は記述順ではなく priority 昇順で評価される
# ため、default drop には他より大きい priority を明示する必要がある。rules には
# inbound(filters)と egress の両方から組み立てたルールが入る。
NWFILTER_XML_TEMPLATE = """
<filter name='{name}' chain='root'>
  <filterref filter='allow-arp'/>
  <filterref filter='allow-dhcp'/>
  <rule action='accept' direction='in' priority='500'>
    <all state='ESTABLISHED,RELATED'/>
  </rule>
{rules}\
</filter>
"""

# spec["static_routes"] をゲスト起動時に永続適用するための systemd oneshot ユニット。
# runcmd(cloud-init 初回起動時のみ実行)だけでは再起動後にルートが消えるため、
# systemctl enable でブートのたびに再適用する形にしている。
STATIC_ROUTES_UNIT_NAME = "minivps-static-routes.service"
STATIC_ROUTES_UNIT_PATH = f"/etc/systemd/system/{STATIC_ROUTES_UNIT_NAME}"

STATIC_ROUTES_UNIT_TEMPLATE = """\
[Unit]
Description=mini-vps-platform static routes (managed, do not edit)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
{exec_lines}

[Install]
WantedBy=multi-user.target
"""

# ExecStart 1行分。先頭の "-" は、この経路の via が到達不能で失敗しても他の
# ExecStart 行の適用を止めないためのもの。失敗はユニット全体のステータスには
# 現れなくなるため、確認には journalctl -u が必要。
STATIC_ROUTES_EXEC_LINE_TEMPLATE = "ExecStart=-ip route replace {destination} via {via}"
