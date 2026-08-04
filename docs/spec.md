# spec リファレンス

VM の spec で指定できるフィールドの詳細。最小構成と全フィールドの一覧は
[README.md の「spec」](../README.md#spec) を参照。検証の実装は `mini_vps/spec.py`。

## 複合型

`FilterRule`: `{port: int(1-65535), protocol: "tcp" | "udp"}` の inbound 許可ルール1件。

`StaticRoute`: `{destination: str(CIDR), via: str(IPv4アドレス)}` のスタティックルート1件。

`NetworkAttachment`: `{name: str, address: str(CIDR、ホストアドレス), gateway: str(IPv4アドレス) | null,
nameservers: list[str(IPv4アドレス)], search: list[str(ドメイン名)]}` の静的IP割当1件。
`gateway` は任意で、省略時はそのNICにデフォルトルートを追加しない。`nameservers`/`search`
はそのNICに設定するDNSサーバIPと検索ドメインのリストで、省略時は netplan に出力しない。
`gateway` と異なり `nameservers` にはサブネット内検証を掛けない(ルータVM越しの別セグメントに
立つDNSサーバのIPが正当な値のため)。

## disk と base image の仮想サイズ

`disk` は `base_image` の仮想サイズ以上にすること。overlay volume の capacity が
backing store の仮想サイズを下回っても libvirt は volume 作成を拒否しないため、この
条件は spec 側で守る必要がある。下回るとゲストから見えるディスクが base image より
小さくなり、base image のパーティションが収まらない。ゴールデンイメージを使う場合は
そのイメージの仮想サイズが基準になる。

## ネットワークセグメント

VM を互いに隔離するための独立 NAT ネットワーク。Ansible playbook が以下を事前定義する。セグメントのサブネットは
「192.168.(200+セグメント番号).0/24」の規則。

| name | bridge | サブネット | DHCP レンジ | 用途 |
|---|---|---|---|---|
| `seg1` | virbr-seg1 | 192.168.201.0/24 | .2〜.254 | 汎用 |
| `seg2` | virbr-seg2 | 192.168.202.0/24 | .2〜.254 | 汎用 |
| `seg3` | virbr-seg3 | 192.168.203.0/24 | .2〜.254 | 汎用 |
| `seg4` | virbr-seg4 | 192.168.204.0/24 | .200〜.254 | 静的IPを連番で並べる用途。`.2`〜`.199` を空けてある |
| `default` | virbr0 | 192.168.122.0/24 | .2〜.254 | `networks` 未指定時の受け皿。libvirt 標準ネットワーク |

`default` だけ最後に置いてあるのは、これがセグメントではないため。上の採番規則にも
`virbr-segN` の命名にも従わない。

`default` とセグメントの違いは管理元と役割のみ。`default` はディストリ同梱 XML から
定義される libvirt 標準ネットワークで、spec で `networks` 未指定時の受け皿。
`seg1`〜`seg4` は本プロジェクトが vars で管理する、分離を明示的に意図した配置先。
遮断の機構は共通で、`default` も各セグメントから見れば相互遮断されたネットワークの
1つとして振る舞う。

`seg4` だけ DHCP レンジが狭いのは、静的IPとDHCPリースの衝突を構成の側で避けるため。

VM の所属セグメントは spec の `networks` で指定する。ここに書けるのは上表の
事前定義済みの名前だけで、未定義の名前を指定すると作成時に libvirt エラーになる。

```yaml
name: web-1
memory: 1024
vcpus: 2
base_image: ubuntu-26.04.img
disk: 10
networks: [seg1]
```

複数のネットワーク名を指定すると、VM に NIC が複数付き、それぞれのセグメントに同時所属できる
(`networks: [seg1, seg2]`)。

ポリシー: 同一セグメント内の VM は自由に通信できる。セグメント間は相互遮断され、
各セグメントからインターネット方向の通信は NAT 経由で許可される。

この遮断に追加のファイアウォール設定は不要である。libvirt は NAT ネットワークの起動時に
ネットワーク単位の FORWARD ルール(iptables backend では `LIBVIRT_FWI`/`LIBVIRT_FWO`
チェーン)を自動投入する。別ブリッジ宛の新規パケットは宛先ネットワーク側の REJECT に当たる
ため、独立 NAT ネットワークに分けた時点でセグメント間通信は遮断される。

> **注意**: ホスト側で FORWARD チェーンの `LIBVIRT_*` より前に広範な ACCEPT ルールを
> 手動追加すると、この遮断は崩れる。

セグメントを追加する場合は `ansible/vars/network_segments.yml` に1エントリ追記してplaybook を再実行する。

## 静的IP割当

`networks` の要素にネットワーク名の文字列ではなく `NetworkAttachment` オブジェクトを
指定すると、そのNICに固定IPを割り当てる。DHCPの文字列要素と混在できる。

```yaml
name: router-1
memory: 1024
vcpus: 2
base_image: ubuntu-26.04.img
disk: 10
networks:
  - default
  - name: seg1
    address: 192.168.201.10/24
    nameservers:
      - 192.168.203.30
    search:
      - minivps.internal
  - name: seg2
    address: 192.168.202.10/24
    gateway: 192.168.202.1
```

仕組み: `create()` は VM名とNICインデックスから決定的にMACアドレスを生成し
(`52:54:00` プレフィックス)、domain XML の各 `<interface>` に埋め込む。静的IPを持つ
NICが1つでもあれば、cloud-init の `network-config`を生成し
`cloud-localds -N` で seed ISO に組み込む。`network-config` を渡すとそれが唯一の
設定源になるため、DHCPの文字列要素も含めて全NICをMACマッチで列挙する。静的IPを1つも持たないVMでは
`network-config` 自体を生成せず、cloud-localds の呼び出しも変わらない。

`gateway` を指定すると、そのNICに `routes: [{to: default, via: gateway}]` として
デフォルトルートを追加する。省略するとそのNICにはルートを追加しない。`gateway` は
`address` のサブネット内にあることを検証し、外れていれば作成時にエラーになる
(同一NIC・同一セグメント内であるべき値のため、`static_routes` の `via` とは異なり
運用者の決め打ちには委ねない)。

`nameservers`/`search` を指定すると、netplan v2 の
`nameservers: {addresses: [...], search: [...]}` として当該NICに出力され、ゲストの
systemd-resolved がリゾルバとして使う(`resolvectl status` で確認できる)。両方空なら
`nameservers` キー自体を出力しない。DHCPの文字列要素にはこの設定は付かない
(DHCPのNICはDHCPオプションでリゾルバを受ける)。内部DNS(dns-1)を参照するVMは
静的IP + `nameservers` 指定が前提の運用規約とする。

VM 作成/削除時に A/PTR レコードを内部DNSへ自動登録する opt-in 機能もある
([dns-registration.md](dns-registration.md) 参照)。

`status`/`get` のIP表示: 静的アドレスを持つNICが1つでもあれば、VMの起動状態に
関わらずそれを(宣言値として)優先表示する。cloud-initが実際に適用したかは確認しない。
静的アドレスが無ければ従来通り起動中のみDHCPリースを表示する。いずれの場合も複数NIC
中の最初の1件のみ。

既知の制約: `seg1`〜`seg3` のDHCPレンジは `.2`〜`.254` とセグメント全域のため、静的
アドレスの割当範囲と重複しうる。dnsmasqのICMP到達確認である程度は緩和されるが、
起動順序次第では衝突する可能性がある。回避したい場合は `seg4` と同様に
`ansible/vars/network_segments.yml` で `dhcp_start`/`dhcp_end` を狭め、空けた範囲を
静的IP専用にする。プラットフォーム側で自動的に調整することはしない。

## スタティックルート

ゲストに追加のスタティックルートを注入する機能。`static_routes` に宛先ネットワーク
(`destination`)と次ホップ(`via`)の組を指定すると、VM 初回起動時に systemd の
oneshot ユニット(`minivps-static-routes.service`)として登録される。

```yaml
name: web-1
memory: 1024
vcpus: 2
base_image: ubuntu-26.04.img
disk: 10
networks: [seg1]
static_routes:
  - destination: 192.168.202.0/24
    via: 192.168.201.1
```

`via` はセグメント内の到達可能な IP を運用者が決め打ちで指定する値であり、`static_routes`
自体はそれを検証しない。次ホップ側(例: ルータVM)のIPを安定させたい場合は
[静的IP割当](#静的ip割当)を参照。

永続化の仕組み: cloud-init の `runcmd` は初回起動時にしか実行されないため、単純な
`ip route add` では VM 再起動後にルートが消える。そのため `ip route replace` を
`ExecStart` に持つ systemd oneshot ユニットを書き込み、`systemctl enable --now` で
有効化する。`enable` により次回以降の起動でも自動的に再適用され、これが再起動をまたぐ
永続化の実体になる。

トラブルシューティング: `ExecStart` の各行は先頭に `-` を付けており、1つの経路が
`via` 未到達で失敗しても他の経路の適用を妨げない。この `-` はエラーを握りつぶすため、
失敗はユニット全体のステータスには現れない。適用結果を確認するには、ゲスト内で以下を
実行する。

```bash
journalctl -u minivps-static-routes.service
ip route show
```
