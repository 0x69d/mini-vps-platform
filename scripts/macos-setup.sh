#!/usr/bin/env bash
# mini-vps-platform の macOS ホスト事前セットアップ(Linux の ansible/playbook.yml 相当)。
#
# 何度実行してもよい(冪等)。既にあるものは確認だけしてスキップする。
#
#   1. Homebrew・Xcode Command Line Tools・HVF の有無を確認する
#   2. libvirt / qemu / pkgconf を導入し、libvirt をユーザーのサービスとして起動する
#   3. qemu:///session への疎通を確認する
#   4. データディレクトリと base image 用の `images` プールを用意する
#   5. アーキテクチャに合う Ubuntu 24.04 cloud image を ubuntu-24.04.img として取得する
#   6. ~/.ssh/minivps_ed25519 を生成する
#   7. UEFI firmware 記述子を確認し、brew upgrade で壊れない絶対パスに固定する
#
# 使い方: scripts/macos-setup.sh
# 詳細は docs/macos.md を参照。
set -euo pipefail

readonly URI="qemu:///session"
readonly POOL="images"
readonly IMAGE_NAME="ubuntu-24.04.img"
readonly UBUNTU_SERIES="noble"
readonly SSH_KEY="$HOME/.ssh/minivps_ed25519"

WARNINGS=()

log() { printf '==> %s\n' "$*"; }
warn() {
	printf 'WARNING: %s\n' "$*" >&2
	WARNINGS+=("$*")
}
die() {
	printf 'ERROR: %s\n' "$*" >&2
	exit 1
}

virsh_s() { LC_ALL=C virsh -c "$URI" "$@"; }

# --- 1. 前提の確認 -------------------------------------------------------------

[[ "$(uname -s)" == "Darwin" ]] || die "このスクリプトは macOS 専用です(Linux は ansible/playbook.yml)"
[[ "$EUID" -ne 0 ]] || die "sudo を付けずに実行してください(libvirt はユーザーごとの qemu:///session を使います)"

# Rosetta 2 上のシェルでは uname -m が x86_64 を返し、x86_64 の Homebrew・イメージを
# 取り違える。mini-vps 本体(platform.machine())も同じ理由で誤検出する。
if [[ "$(sysctl -in sysctl.proc_translated)" == "1" ]]; then
	die "Rosetta 2 上のシェルで実行されています。ネイティブ(arm64)のターミナルで実行してください"
fi

case "$(uname -m)" in
arm64)
	ARCH="aarch64"
	MACHINE="virt"
	CLOUD_ARCH="arm64"
	;;
x86_64)
	ARCH="x86_64"
	MACHINE="q35"
	CLOUD_ARCH="amd64"
	;;
*) die "未対応のアーキテクチャです: $(uname -m)" ;;
esac

ACCEL="${MINIVPS_ACCEL:-hvf}"
case "$ACCEL" in
hvf) VIRTTYPE="hvf" ;;
tcg) VIRTTYPE="qemu" ;;
*) die "MINIVPS_ACCEL は hvf か tcg を指定してください: $ACCEL" ;;
esac

log "ホスト: macOS $(sw_vers -productVersion) / $ARCH / accel=$ACCEL"

if [[ "$ACCEL" == "hvf" ]]; then
	if [[ "$(sysctl -in kern.hv_support)" != "1" ]]; then
		warn "Hypervisor.framework が使えません(kern.hv_support != 1)。VM 内の macOS などネストした環境では hvf を使えないため、MINIVPS_ACCEL=tcg を検討してください"
	fi
	# Homebrew の qemu は Apple Silicon の macOS 14 以前では --disable-hvf でビルドされる
	# (arm64 の HVF バックエンドが macOS 15 SDK を要するため)。
	macos_major="$(sw_vers -productVersion | cut -d. -f1)"
	if [[ "$ARCH" == "aarch64" && "$macos_major" -lt 15 ]]; then
		warn "Apple Silicon の macOS ${macos_major} では Homebrew の qemu が HVF 無しでビルドされています。macOS 15 以降へ更新するか、MINIVPS_ACCEL=tcg(低速)を使ってください"
	fi
fi

command -v brew >/dev/null 2>&1 || die "Homebrew が見つかりません。https://brew.sh の手順で導入してから再実行してください"
BREW_PREFIX="$(brew --prefix)"

# libvirt-python は sdist からビルドされるため C コンパイラが要る。
if ! xcode-select -p >/dev/null 2>&1; then
	warn "Xcode Command Line Tools がありません。libvirt-python のビルドに必要です: xcode-select --install"
fi

# --- 2. パッケージとサービス ---------------------------------------------------

# pkg-config は Homebrew では pkgconf の別名。
for formula in libvirt qemu pkgconf; do
	if brew list --formula --versions "$formula" >/dev/null 2>&1; then
		log "$formula は導入済み"
	else
		log "$formula を導入する"
		brew install "$formula"
	fi
done

# ログイン時に libvirtd(ユーザー権限 = session デーモン)を起動し、autostart の VM を
# 戻す。launchd 経由なので PATH に $(brew --prefix)/bin が入り、qemu-img なども見つかる。
service_status="$(brew services list | awk '$1 == "libvirt" { print $2 }')"
if [[ "$service_status" == "started" ]]; then
	log "libvirt サービスは起動済み"
else
	log "libvirt サービスを起動する"
	brew services start libvirt
fi

# --- 3. 疎通確認 ----------------------------------------------------------------

log "$URI への疎通を確認する"
connected=false
for _ in 1 2 3 4 5 6 7 8 9 10; do
	if virsh_s version >/dev/null 2>&1; then
		connected=true
		break
	fi
	sleep 1
done
$connected || die "$URI に接続できません。'brew services info libvirt' でサービスの状態を確認し、'brew services restart libvirt' を試してください"
virsh_s version

# --- 4. データディレクトリと images プール -------------------------------------

DATA_DIR="${MINIVPS_DATA_DIR:-$HOME/Library/Application Support/mini-vps}"
IMAGES_DIR="${MINIVPS_IMAGES_DIR:-$DATA_DIR/images}"
for dir in \
	"${MINIVPS_POOL_PATH:-$DATA_DIR/vps-pool}" \
	"${MINIVPS_SEED_DIR:-$DATA_DIR/seeds}" \
	"$IMAGES_DIR" \
	"${MINIVPS_LOCK_DIR:-$DATA_DIR/locks}"; do
	mkdir -p "$dir"
done
log "データディレクトリ: $DATA_DIR"

if ! virsh_s pool-info "$POOL" >/dev/null 2>&1; then
	log "$POOL プールを定義する: $IMAGES_DIR"
	virsh_s pool-define-as "$POOL" dir --target "$IMAGES_DIR" >/dev/null
fi

pool_target="$(virsh_s pool-dumpxml "$POOL" | sed -n 's:.*<path>\(.*\)</path>.*:\1:p' | head -n 1)"
if [[ "$pool_target" != "$IMAGES_DIR" ]]; then
	warn "$POOL プールは既に別のディレクトリで定義されています($pool_target)。base image はそちらに置きます"
	IMAGES_DIR="$pool_target"
	mkdir -p "$IMAGES_DIR"
fi

pool_info="$(virsh_s pool-info "$POOL")"
if ! grep -Eq 'State:[[:space:]]+running' <<<"$pool_info"; then
	virsh_s pool-start "$POOL" >/dev/null
	log "$POOL プールを起動した"
fi
if ! grep -Eq 'Autostart:[[:space:]]+yes' <<<"$pool_info"; then
	virsh_s pool-autostart "$POOL" >/dev/null
	log "$POOL プールの autostart を有効にした"
fi

# --- 5. base image --------------------------------------------------------------

image_file="${UBUNTU_SERIES}-server-cloudimg-${CLOUD_ARCH}.img"
image_base_url="https://cloud-images.ubuntu.com/${UBUNTU_SERIES}/current"
image_path="$IMAGES_DIR/$IMAGE_NAME"
if [[ -e "$image_path" ]]; then
	log "$IMAGE_NAME は取得済み"
else
	log "$image_file を $IMAGE_NAME として取得する"
	partial="$image_path.part"
	trap 'rm -f "$partial"' EXIT
	curl -fL --retry 3 -o "$partial" "$image_base_url/$image_file"
	expected="$(curl -fsSL "$image_base_url/SHA256SUMS" |
		awk -v f="$image_file" '$2 == f || $2 == "*" f { print $1 }')"
	actual="$(shasum -a 256 "$partial" | awk '{ print $1 }')"
	if [[ -z "$expected" || "$expected" != "$actual" ]]; then
		die "$image_file の SHA256 が一致しません(expected=${expected:-不明} actual=$actual)"
	fi
	# 使用中の backing file を誤って上書きしないよう読み取り専用にする。
	chmod 0444 "$partial"
	mv "$partial" "$image_path"
	trap - EXIT
fi
virsh_s pool-refresh "$POOL" >/dev/null

# --- 6. SSH 鍵 -----------------------------------------------------------------

# 公開鍵はゲストの authorized_keys にそのまま入るため、コメントは空にする。
if [[ -f "$SSH_KEY.pub" ]]; then
	log "SSH 鍵 $SSH_KEY は生成済み"
else
	[[ -d "$HOME/.ssh" ]] || mkdir -m 700 "$HOME/.ssh"
	ssh-keygen -q -t ed25519 -f "$SSH_KEY" -N "" -C ""
	log "SSH 鍵 $SSH_KEY を生成した"
fi

# --- 7. UEFI firmware 記述子 ----------------------------------------------------

# libvirt(Homebrew 版は qemu_datadir に qemu の share/qemu を指す)は
# <qemu share>/firmware/*.json から UEFI firmware を自動選択し、選んだ loader の
# パスを domain 定義に書き込む。qemu 同梱の記述子は Cellar のバージョン付きパス
# (例: /opt/homebrew/Cellar/qemu/11.1.1/share/qemu/edk2-aarch64-code.fd)を指すため、
# brew upgrade qemu で古い Cellar が消えると既存 VM が起動できなくなる。
# そこで同名の記述子を、優先度が最も高いユーザー設定ディレクトリに、バージョンに
# 依存しない opt パスへ書き換えて置く。
qemu_opt="$(brew --prefix qemu)"
qemu_cellar="$(brew --cellar qemu)"
fw_src_dir="$qemu_opt/share/qemu/firmware"
fw_user_dir="${XDG_CONFIG_HOME:-$HOME/.config}/qemu/firmware"

shopt -s nullglob
descriptors=("$fw_src_dir"/*.json)
shopt -u nullglob
if ((${#descriptors[@]} == 0)); then
	warn "UEFI firmware 記述子が見つかりません($fw_src_dir/*.json)。'brew reinstall qemu' を試してください"
else
	mkdir -p "$fw_user_dir"
	for src in "${descriptors[@]}"; do
		dest="$fw_user_dir/$(basename "$src")"
		pinned="$(sed -E "s#${qemu_cellar}/[^/\"]+/#${qemu_opt}/#g" "$src")"
		if [[ ! -f "$dest" ]] || [[ "$(cat "$dest")" != "$pinned" ]]; then
			printf '%s\n' "$pinned" >"$dest"
			log "firmware 記述子を固定した: $dest"
		fi
	done
fi

if ! domcaps="$(virsh_s domcapabilities --virttype "$VIRTTYPE" --arch "$ARCH" --machine "$MACHINE" 2>&1)"; then
	warn "domain type '$VIRTTYPE'($ARCH/$MACHINE)を libvirt が使えません: $domcaps"
elif ! grep -q '<value>efi</value>' <<<"$domcaps"; then
	warn "libvirt から UEFI firmware(efi)が見えません。$fw_src_dir と $fw_user_dir を確認してください"
else
	log "libvirt から $VIRTTYPE と UEFI firmware が使えることを確認した"
fi

# --- libvirt-python のビルド環境 ------------------------------------------------

# Homebrew の pkgconf は $(brew --prefix)/lib/pkgconfig を既定で探すため通常は不要。
if ! pkg-config --exists libvirt 2>/dev/null; then
	warn "pkg-config が libvirt を見つけられません。uv sync の前に次を実行してください: export PKG_CONFIG_PATH=\"$BREW_PREFIX/lib/pkgconfig\""
fi

# --- 完了 -----------------------------------------------------------------------

echo
log "セットアップが完了しました"
if ((${#WARNINGS[@]} > 0)); then
	echo
	echo "警告(${#WARNINGS[@]} 件):"
	for w in "${WARNINGS[@]}"; do
		echo "  - $w"
	done
fi
cat <<EOF

次に実行するコマンド(リポジトリのルートで):

  uv sync
  uv run mini-vps create examples/macos-quickstart.yaml
  uv run mini-vps ssh quickstart

詳しくは docs/macos.md を参照してください。
EOF
